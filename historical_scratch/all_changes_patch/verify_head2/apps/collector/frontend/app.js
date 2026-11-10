const state = {
  drafts: [],
  query: "",
  analyzingPath: "",
  currentDraftPath: "",
  currentReport: null,
  currentUploadPlan: null,
  page: 1,
  pageSize: 20,
};

const elements = {
  rootForm: document.querySelector("#rootForm"),
  draftRoot: document.querySelector("#draftRoot"),
  renderServerUrl: document.querySelector("#renderServerUrl"),
  accessToken: document.querySelector("#accessToken"),
  rootMessage: document.querySelector("#rootMessage"),
  refreshBtn: document.querySelector("#refreshBtn"),
  draftSearch: document.querySelector("#draftSearch"),
  draftCount: document.querySelector("#draftCount"),
  draftRows: document.querySelector("#draftRows"),
  draftEmpty: document.querySelector("#draftEmpty"),
  draftPageSize: document.querySelector("#draftPageSize"),
  previousPageBtn: document.querySelector("#previousPageBtn"),
  nextPageBtn: document.querySelector("#nextPageBtn"),
  pageStatus: document.querySelector("#pageStatus"),
  analysisSection: document.querySelector("#analysisSection"),
  analysisTitle: document.querySelector("#analysisTitle"),
  analysisMeta: document.querySelector("#analysisMeta"),
  packagingStatus: document.querySelector("#packagingStatus"),
  analysisWarnings: document.querySelector("#analysisWarnings"),
  analysisMetrics: document.querySelector("#analysisMetrics"),
  slotTotal: document.querySelector("#slotTotal"),
  slotGroups: document.querySelector("#slotGroups"),
  dependencyTotal: document.querySelector("#dependencyTotal"),
  dependencySummary: document.querySelector("#dependencySummary"),
  dependencyList: document.querySelector("#dependencyList"),
  loadingOverlay: document.querySelector("#loadingOverlay"),
  extractFontsBtn: document.querySelector("#extractFontsBtn"),
  analysisActionMessage: document.querySelector("#analysisActionMessage"),
  policyAudio: document.querySelector("#policyAudio"),
  policyVideoEffects: document.querySelector("#policyVideoEffects"),
  policyTextStyle: document.querySelector("#policyTextStyle"),
  policyTextEffects: document.querySelector("#policyTextEffects"),
  policyTextTemplates: document.querySelector("#policyTextTemplates"),
  buildUploadPlanBtn: document.querySelector("#buildUploadPlanBtn"),
  uploadPlanBtn: document.querySelector("#uploadPlanBtn"),
  uploadPlanSummary: document.querySelector("#uploadPlanSummary"),
  uploadResult: document.querySelector("#uploadResult"),
};

document.addEventListener("DOMContentLoaded", initialize);

async function initialize() {
  bindEvents();
  try {
    const config = await apiFetch("/api/config");
    elements.draftRoot.value = config.draft_root || "";
    elements.renderServerUrl.value = config.render_server_url || "http://127.0.0.1:8010";
    elements.accessToken.placeholder = config.access_token_configured ? "已保存；不修改可留空" : "请输入网站内部访问密码";
    await loadDrafts();
  } catch (error) {
    showMessage(error.message, true);
    elements.draftCount.textContent = "无法读取草稿";
  }
}

function bindEvents() {
  elements.rootForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const draftRoot = elements.draftRoot.value.trim();
    const renderServerUrl = elements.renderServerUrl.value.trim();
    const accessToken = elements.accessToken.value.trim();
    if (!draftRoot) {
      showMessage("请填写剪映草稿目录。", true);
      return;
    }
    if (!renderServerUrl) {
      showMessage("请填写网站后端地址。", true);
      return;
    }
    await withButtonDisabled(event.submitter, async () => {
      try {
        await apiFetch("/api/config/draft-root", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ draft_root: draftRoot }),
        });
        await apiFetch("/api/config/server-url", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ render_server_url: renderServerUrl }),
        });
        if (accessToken) {
          await apiFetch("/api/config/access-token", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ access_token: accessToken }),
          });
          elements.accessToken.value = "";
          elements.accessToken.placeholder = "已保存；不修改可留空";
        }
        showMessage("草稿目录和网站后端地址已保存。", false);
        await loadDrafts();
      } catch (error) {
        showMessage(error.message, true);
      }
    });
  });

  elements.refreshBtn.addEventListener("click", () => loadDrafts());
  elements.draftSearch.addEventListener("input", (event) => {
    state.query = event.target.value.trim().toLowerCase();
    state.page = 1;
    renderDrafts();
  });
  elements.draftPageSize.addEventListener("change", (event) => {
    state.pageSize = Number(event.target.value) || 20;
    state.page = 1;
    renderDrafts();
  });
  elements.previousPageBtn.addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    renderDrafts();
  });
  elements.nextPageBtn.addEventListener("click", () => {
    state.page += 1;
    renderDrafts();
  });
  elements.draftRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-draft-path]");
    if (!button) return;
    await analyzeDraft(button.dataset.draftPath);
  });
  elements.extractFontsBtn.addEventListener("click", extractCurrentDraftFonts);
  elements.buildUploadPlanBtn.addEventListener("click", buildCurrentUploadPlan);
  elements.uploadPlanBtn.addEventListener("click", uploadCurrentPlan);
}

async function loadDrafts() {
  elements.refreshBtn.disabled = true;
  elements.draftCount.textContent = "正在扫描...";
  try {
    state.drafts = await apiFetch("/api/drafts");
    renderDrafts();
  } catch (error) {
    state.drafts = [];
    renderDrafts();
    showMessage(error.message, true);
  } finally {
    elements.refreshBtn.disabled = false;
  }
}

function renderDrafts() {
  const drafts = state.drafts.filter((draft) =>
    String(draft.name || "").toLowerCase().includes(state.query),
  );
  const pageCount = Math.max(1, Math.ceil(drafts.length / state.pageSize));
  state.page = Math.min(Math.max(1, state.page), pageCount);
  const startIndex = (state.page - 1) * state.pageSize;
  const pageDrafts = drafts.slice(startIndex, startIndex + state.pageSize);
  elements.draftRows.replaceChildren();
  elements.draftEmpty.classList.toggle("hidden", drafts.length > 0);
  const visibleStart = drafts.length ? startIndex + 1 : 0;
  const visibleEnd = Math.min(startIndex + pageDrafts.length, drafts.length);
  elements.draftCount.textContent = state.query
    ? `找到 ${drafts.length} 个，共 ${state.drafts.length} 个草稿；当前显示 ${visibleStart}-${visibleEnd}`
    : `共 ${state.drafts.length} 个草稿；当前显示 ${visibleStart}-${visibleEnd}`;
  elements.pageStatus.textContent = `第 ${state.page} / ${pageCount} 页`;
  elements.previousPageBtn.disabled = state.page <= 1;
  elements.nextPageBtn.disabled = state.page >= pageCount;

  for (const draft of pageDrafts) {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    const name = document.createElement("div");
    name.className = "draft-name";
    name.textContent = draft.name;
    name.title = draft.name;
    const path = document.createElement("div");
    path.className = "draft-path";
    path.textContent = draft.path;
    path.title = draft.path;
    nameCell.append(name, path);

    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `tag ${draft.encryption_status}`;
    status.textContent = draft.encryption_status === "plain" ? "明文草稿" : "自动解密";
    statusCell.append(status);

    const durationCell = document.createElement("td");
    durationCell.textContent = draft.duration_us ? formatDuration(draft.duration_us) : "分析后显示";

    const modifiedCell = document.createElement("td");
    modifiedCell.textContent = formatDate(draft.modified_at);

    const actionCell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "analyze-button";
    button.dataset.draftPath = draft.path;
    button.textContent = "分析";
    actionCell.append(button);

    row.append(nameCell, statusCell, durationCell, modifiedCell, actionCell);
    elements.draftRows.append(row);
  }
}

async function analyzeDraft(draftPath) {
  if (!draftPath || state.analyzingPath) return;
  state.analyzingPath = draftPath;
  elements.loadingOverlay.classList.remove("hidden");
  try {
    const report = await apiFetch("/api/drafts/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_dir: draftPath, hash_mode: "small_files" }),
    });
    state.currentDraftPath = draftPath;
    state.currentReport = report;
    state.currentUploadPlan = null;
    renderReport(report);
    elements.analysisSection.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    state.analyzingPath = "";
    elements.loadingOverlay.classList.add("hidden");
  }
}

function renderReport(report) {
  const draft = report.draft || {};
  const summary = report.summary || {};
  const counts = summary.slot_counts || {};
  const canvas = draft.canvas || {};
  const slots = report.editable_slots || {};
  const dependencies = report.dependencies || [];

  elements.analysisTitle.textContent = draft.name || "分析结果";
  elements.analysisMeta.textContent = `${formatDuration(draft.duration_us)} · ${canvas.width || 0} × ${canvas.height || 0} · ${draft.track_count || 0} 条轨道${draft.was_decrypted ? " · 已自动解密副本" : ""}`;
  elements.packagingStatus.textContent = summary.ready_for_packaging ? "依赖检查通过" : "存在必要素材缺失";
  elements.packagingStatus.className = `status-badge ${summary.ready_for_packaging ? "ready" : "blocked"}`;

  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  elements.analysisWarnings.classList.toggle("hidden", warnings.length === 0);
  elements.analysisWarnings.replaceChildren();
  for (const warning of warnings) {
    const item = document.createElement("div");
    item.textContent = warning;
    elements.analysisWarnings.append(item);
  }

  const metrics = [
    ["BGM / 音效", counts.audio || 0],
    ["视频特效", counts.video_effects || 0],
    ["普通文字 / 花字", counts.texts || 0],
    ["复合文字模板", counts.text_templates || 0],
    ["策略前待上传素材", summary.upload_required_count || 0],
  ];
  elements.analysisMetrics.replaceChildren(...metrics.map(([label, value]) => metricNode(label, value)));

  const slotDefinitions = [
    ["audio", "BGM / 音效"],
    ["video_effects", "视频特效"],
    ["texts", "普通文字 / 花字"],
    ["text_templates", "复合文字模板"],
  ];
  const slotTotal = Object.values(counts).reduce((total, value) => total + Number(value || 0), 0);
  elements.slotTotal.textContent = `共 ${slotTotal} 个位置`;
  elements.slotGroups.replaceChildren();
  for (const [key, label] of slotDefinitions) {
    elements.slotGroups.append(slotGroupNode(label, slots[key] || [], key));
  }

  elements.dependencyTotal.textContent = `共 ${dependencies.length} 项`;
  renderDependencySummary(summary.dependency_status_counts || {});
  renderDependencies(dependencies);
  const extractableFontCount = dependencies.filter(
    (item) => item.kind === "font" && item.exists && item.status !== "central_library",
  ).length;
  elements.extractFontsBtn.disabled = extractableFontCount === 0;
  elements.extractFontsBtn.textContent = extractableFontCount
    ? `提取本草稿字体（${extractableFontCount}）`
    : "字体已在素材库或没有字体";
  elements.analysisActionMessage.classList.add("hidden");
  resetMigrationPolicies();
  elements.uploadPlanSummary.classList.add("hidden");
  elements.uploadPlanBtn.disabled = true;
  elements.uploadResult.classList.add("hidden");
  elements.analysisSection.classList.remove("hidden");
}

function resetMigrationPolicies() {
  elements.policyAudio.value = "keep";
  elements.policyVideoEffects.value = "keep";
  elements.policyTextStyle.value = "keep";
  elements.policyTextEffects.value = "keep";
  elements.policyTextTemplates.value = "keep";
}

async function buildCurrentUploadPlan() {
  if (!state.currentReport?.report_id) return;
  await withButtonDisabled(elements.buildUploadPlanBtn, async () => {
    try {
      const plan = await apiFetch("/api/upload-plans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          report_id: state.currentReport.report_id,
          policies: {
            audio: elements.policyAudio.value,
            video_effects: elements.policyVideoEffects.value,
            text_style: elements.policyTextStyle.value,
            text_effects: elements.policyTextEffects.value,
            text_templates: elements.policyTextTemplates.value,
          },
        }),
      });
      state.currentUploadPlan = plan;
      renderUploadPlan(plan);
    } catch (error) {
      elements.uploadPlanSummary.replaceChildren(planMetricNode("生成失败", error.message));
      elements.uploadPlanSummary.classList.remove("hidden");
    }
  });
}

function renderUploadPlan(plan) {
  const summary = plan.summary || {};
  const metrics = [
    ["草稿结构", "必须迁移"],
    ["需要上传", `${summary.upload_count || 0} 项`],
    ["预计素材大小", formatBytes(summary.upload_size_bytes || 0)],
    ["素材库复用", `${summary.reuse_library_count || 0} 项`],
    ["因替换而跳过", `${summary.skipped_count || 0} 项`],
    ["阻塞问题", `${summary.blocked_count || 0} 项`],
  ];
  elements.uploadPlanSummary.replaceChildren(...metrics.map(([label, value]) => planMetricNode(label, value)));
  elements.uploadPlanSummary.classList.remove("hidden");
  elements.uploadPlanBtn.disabled = !summary.ready_for_upload;
  elements.uploadResult.classList.add("hidden");
}

async function uploadCurrentPlan() {
  const planId = state.currentUploadPlan?.plan_id;
  if (!planId) return;
  const originalText = elements.uploadPlanBtn.textContent;
  elements.uploadPlanBtn.disabled = true;
  elements.uploadPlanBtn.textContent = "正在打包并上传...";
  elements.uploadResult.classList.add("hidden");
  try {
    const result = await apiFetch(`/api/upload-plans/${encodeURIComponent(planId)}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: elements.renderServerUrl.value.trim(),
        template_name: state.currentReport?.draft?.name || "",
      }),
    });
    const serverResult = result.server_result || {};
    const template = serverResult.template || {};
    const message = document.createElement("span");
    message.textContent = `上传完成，已登记母版：${template.name || "未命名"}（${template.template_id || "-"}）`;
    const link = document.createElement("a");
    link.href = new URL("/app", result.server_url).href;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "打开生成网站";
    elements.uploadResult.replaceChildren(message, link);
    elements.uploadResult.className = "message";
    elements.uploadResult.classList.remove("hidden");
  } catch (error) {
    elements.uploadResult.textContent = error.message;
    elements.uploadResult.className = "message error";
    elements.uploadResult.classList.remove("hidden");
  } finally {
    elements.uploadPlanBtn.textContent = originalText;
    elements.uploadPlanBtn.disabled = !(state.currentUploadPlan?.summary?.ready_for_upload);
  }
}

function planMetricNode(label, value) {
  const node = document.createElement("div");
  node.className = "plan-metric";
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value;
  content.title = value;
  node.append(name, content);
  return node;
}

async function extractCurrentDraftFonts() {
  if (!state.currentDraftPath) return;
  await withButtonDisabled(elements.extractFontsBtn, async () => {
    try {
      const result = await apiFetch("/api/drafts/extract-fonts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_dir: state.currentDraftPath }),
      });
      elements.analysisActionMessage.textContent = `字体提取完成：新复制 ${result.copied_count || 0} 个，已存在 ${result.existing_count || 0} 个，本机缺失 ${result.missing_count || 0} 个。再次分析后会显示为素材库已有。`;
      elements.analysisActionMessage.className = "message";
      elements.analysisActionMessage.classList.remove("hidden");
    } catch (error) {
      elements.analysisActionMessage.textContent = error.message;
      elements.analysisActionMessage.className = "message error";
      elements.analysisActionMessage.classList.remove("hidden");
    }
  });
}

function metricNode(label, value) {
  const node = document.createElement("div");
  node.className = "metric";
  const name = document.createElement("span");
  name.textContent = label;
  const count = document.createElement("strong");
  count.textContent = value;
  node.append(name, count);
  return node;
}

function slotGroupNode(label, items, kind) {
  const group = document.createElement("div");
  group.className = "slot-group";
  const header = document.createElement("div");
  header.className = "slot-group-header";
  const name = document.createElement("span");
  name.textContent = label;
  const count = document.createElement("span");
  count.textContent = `${items.length} 个`;
  header.append(name, count);
  group.append(header);

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "slot-item";
    empty.textContent = "未发现";
    group.append(empty);
    return group;
  }

  for (const item of items.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "slot-item";
    const content = document.createElement("span");
    content.textContent = slotLabel(item, kind);
    content.title = content.textContent;
    const time = document.createElement("span");
    time.className = "slot-time";
    time.textContent = formatRange(item.target_timerange);
    row.append(content, time);
    group.append(row);
  }
  if (items.length > 12) {
    const more = document.createElement("div");
    more.className = "slot-item";
    more.textContent = `还有 ${items.length - 12} 个位置`;
    group.append(more);
  }
  return group;
}

function slotLabel(item, kind) {
  if (kind === "texts") return item.text || "空文字";
  if (kind === "text_templates") {
    const texts = Array.isArray(item.texts) ? item.texts.filter(Boolean).join(" / ") : "";
    return texts || item.name || "复合文字模板";
  }
  return item.name || item.suggested_role || "未命名素材";
}

function renderDependencySummary(counts) {
  const definitions = [
    ["central_library", "素材库已有"],
    ["upload_required", "需要上传"],
    ["missing", "本机缺失"],
    ["external", "外部依赖"],
  ];
  elements.dependencySummary.replaceChildren();
  for (const [key, label] of definitions) {
    const node = document.createElement("span");
    node.textContent = `${label} ${counts[key] || 0}`;
    elements.dependencySummary.append(node);
  }
}

function renderDependencies(dependencies) {
  const statusLabels = {
    central_library: "素材库已有",
    upload_required: "需要上传",
    missing: "本机缺失",
    external: "外部依赖",
  };
  elements.dependencyList.replaceChildren();
  if (!dependencies.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "这个草稿没有发现本地素材依赖。";
    elements.dependencyList.append(empty);
    return;
  }
  for (const dependency of dependencies.slice(0, 40)) {
    const row = document.createElement("div");
    row.className = "dependency-item";
    const kind = document.createElement("span");
    kind.textContent = dependencyKindLabel(dependency.kind);
    const path = document.createElement("span");
    path.className = "dependency-path";
    path.textContent = dependency.path || dependency.original_path || "-";
    path.title = path.textContent;
    const status = document.createElement("span");
    status.className = `dependency-status ${dependency.status || "external"}`;
    status.textContent = statusLabels[dependency.status] || dependency.status || "未知";
    row.append(kind, path, status);
    elements.dependencyList.append(row);
  }
}

function dependencyKindLabel(kind) {
  return {
    video: "视频",
    audio: "音频",
    video_effect: "视频特效",
    text_effect: "花字",
    text_template_resource: "文字模板",
    font: "字体",
    image: "图片",
    sticker: "贴纸",
    resource: "资源",
  }[kind] || kind || "资源";
}

function formatRange(range) {
  const start = Number(range?.start || 0) / 1_000_000;
  const duration = Number(range?.duration || 0) / 1_000_000;
  return `${formatSeconds(start)} - ${formatSeconds(start + duration)}`;
}

function formatDuration(microseconds) {
  const seconds = Number(microseconds || 0) / 1_000_000;
  return formatSeconds(seconds);
}

function formatSeconds(value) {
  const total = Math.max(0, Number(value || 0));
  const minutes = Math.floor(total / 60);
  const seconds = Math.floor(total % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return payload;
}

async function withButtonDisabled(button, action) {
  if (!button) return action();
  button.disabled = true;
  try {
    return await action();
  } finally {
    button.disabled = false;
  }
}

function showMessage(text, isError) {
  elements.rootMessage.textContent = text;
  elements.rootMessage.className = `message${isError ? " error" : ""}`;
  elements.rootMessage.classList.remove("hidden");
}
