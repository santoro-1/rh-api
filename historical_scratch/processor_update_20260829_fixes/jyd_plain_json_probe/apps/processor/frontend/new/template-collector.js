(() => {
  "use strict";

  const STORAGE_KEY = "jyd.collectorUrl";
  const localState = { drafts: [], busy: false };
  const byId = (id) => document.getElementById(id);

  function defaultCollectorUrl() {
    return window.location.port === "8001"
      ? "http://127.0.0.1:8766"
      : "http://127.0.0.1:8765";
  }

  function collectorBaseUrl() {
    return String(byId("collector-import-url").value || defaultCollectorUrl())
      .trim()
      .replace(/\/+$/, "");
  }

  function message(value, warning = false) {
    const target = byId("collector-import-progress");
    target.textContent = value;
    target.className = `min-h-[38px] mt-3 rounded-xl bg-slate-950/55 px-3 py-2 text-[10px] leading-5 ${warning ? "text-rose-300" : "text-gray-400"}`;
  }

  async function collectorFetch(path, options = {}, timeoutMs = 15000) {
    const controller = timeoutMs > 0 ? new AbortController() : null;
    const timeout = controller
      ? setTimeout(() => controller.abort(), timeoutMs)
      : null;
    let response;
    try {
      response = await fetch(`${collectorBaseUrl()}${path}`, {
        ...options,
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (error) {
      const reason = error?.name === "AbortError" ? "连接超时" : error.message;
      throw new Error(`无法连接本机采集器（${reason}）。请先启动采集器，并确认地址是 ${collectorBaseUrl()}`);
    } finally {
      if (timeout) clearTimeout(timeout);
    }
    const text = await response.text();
    let data = text;
    try { data = text ? JSON.parse(text) : null; } catch (_) { /* 保留原始响应。 */ }
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail : data;
      throw new Error(typeof detail === "string" ? detail : `本机采集器请求失败（${response.status}）`);
    }
    return data;
  }

  function durationLabel(value) {
    const seconds = Math.max(0, Number(value || 0) / 1_000_000);
    return seconds ? `${seconds.toFixed(seconds >= 10 ? 0 : 1)} 秒` : "时长未知";
  }

  function renderDrafts() {
    const select = byId("collector-draft-select");
    select.replaceChildren();
    if (!localState.drafts.length) {
      select.append(new Option("当前草稿目录没有找到剪映草稿", ""));
      byId("collector-import-button").disabled = true;
      return;
    }
    for (const draft of localState.drafts) {
      const encryption = draft.encryption_status === "plain" ? "明文" : "自动解密";
      select.append(new Option(
        `${draft.name || "未命名草稿"} · ${durationLabel(draft.duration_us)} · ${encryption}`,
        draft.path,
      ));
    }
    byId("collector-import-button").disabled = false;
    fillNameFromDraft();
  }

  function selectedDraft() {
    const path = byId("collector-draft-select").value;
    return localState.drafts.find((item) => item.path === path) || null;
  }

  function fillNameFromDraft() {
    const draft = selectedDraft();
    const input = byId("collector-template-name");
    if (draft && !input.value.trim()) input.value = draft.name || "剪映模板";
  }

  async function refreshDrafts() {
    if (localState.busy) return;
    const button = byId("collector-refresh-button");
    button.disabled = true;
    message("正在连接本机采集器并扫描剪映草稿...");
    try {
      localStorage.setItem(STORAGE_KEY, collectorBaseUrl());
      localState.drafts = await collectorFetch("/api/drafts");
      renderDrafts();
      message(`已找到 ${localState.drafts.length} 个草稿。选择后即可自动收集模板资源。`);
    } catch (error) {
      localState.drafts = [];
      renderDrafts();
      message(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function blockedPlanMessage(plan) {
    const blocked = (plan.dependencies || []).filter((item) =>
      ["blocked_missing", "blocked_external"].includes(item.decision),
    );
    const sample = blocked.slice(0, 3).map((item) => {
      const path = item.original_path || item.path || item.kind || "未知资源";
      return String(path).split(/[\\/]/).pop();
    });
    return `仍有 ${blocked.length} 个必须保留的模板资源无法采集${sample.length ? `：${sample.join("、")}` : ""}`;
  }

  async function importTemplate() {
    if (localState.busy) return;
    const draft = selectedDraft();
    const name = byId("collector-template-name").value.trim();
    if (!draft) return message("请先选择一个剪映草稿。", true);
    if (!name) return message("请填写模板名称。", true);

    localState.busy = true;
    const importButton = byId("collector-import-button");
    importButton.disabled = true;
    byId("collector-refresh-button").disabled = true;
    try {
      message("第 1/4 步：正在解密并分析草稿资源...");
      const report = await collectorFetch("/api/drafts/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_dir: draft.path, hash_mode: "small_files" }),
      }, 120000);

      message("第 2/4 步：正在生成模板中心专用上传清单...");
      const plan = await collectorFetch("/api/upload-plans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          report_id: report.report_id,
          mode: "template_center",
          policies: {
            audio: "replace",
            video_effects: "keep",
            text_style: "keep",
            text_effects: "keep",
            text_templates: "keep",
          },
        }),
      }, 120000);
      if (plan.mode !== "template_center") {
        throw new Error("本机采集器版本过旧，不支持账号模板中心。请更新并重新启动采集器。 ");
      }
      if (!plan.summary?.ready_for_upload) throw new Error(blockedPlanMessage(plan));

      message("第 3/4 步：正在申请当前账号的一次性上传凭证...");
      const ticket = await api("/api/new/jianying-template-import-tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });

      const size = Number(plan.summary?.upload_size_bytes || 0);
      message(`第 4/4 步：正在打包并上传模板资源${size ? `（约 ${(size / 1024 / 1024).toFixed(1)} MB）` : ""}...`);
      const uploaded = await collectorFetch(
        `/api/upload-plans/${encodeURIComponent(plan.plan_id)}/upload`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            server_url: window.location.origin,
            template_name: name,
            template_import_ticket: ticket.ticket,
          }),
        },
        0,
      );
      const template = uploaded.server_result?.template || {};
      await loadTemplates();
      message(`模板“${template.name || name}”已保存到当前账号。`);
      showToast("模板导入完成", "本机资源已经随模板保存，可以重复使用。", "success");
      setTimeout(() => closeCollectorImport(), 600);
    } catch (error) {
      message(error.message, true);
    } finally {
      localState.busy = false;
      importButton.disabled = !localState.drafts.length;
      byId("collector-refresh-button").disabled = false;
    }
  }

  window.openCollectorImport = () => {
    byId("collector-import-modal").classList.remove("modal-hidden");
    byId("collector-import-url").value = localStorage.getItem(STORAGE_KEY) || defaultCollectorUrl();
    refreshDrafts();
  };
  window.closeCollectorImport = () => {
    if (!localState.busy) byId("collector-import-modal").classList.add("modal-hidden");
  };

  byId("collector-refresh-button").addEventListener("click", refreshDrafts);
  byId("collector-draft-select").addEventListener("change", fillNameFromDraft);
  byId("collector-import-button").addEventListener("click", importTemplate);
})();
