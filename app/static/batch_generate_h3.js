(() => {
  "use strict";

  const workflow = document.getElementById("batch-workflow");
  const panel = document.getElementById("h3-entry-panel");
  if (!workflow || !panel) return;

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const state = {images: [], videos: [], audios: [], scripts: []};
  let accountIds = [];
  let requestKey = crypto.randomUUID();
  let preparedBatchId = "";

  const byId = (id) => document.getElementById(id);
  const isH3 = () => workflow.value === "minimax_h3_ref2va";
  const escapeHtml = (value) => String(value || "").replace(
    /[&<>"']/g,
    (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character],
  );

  function errorMessage(payload, fallback) {
    if (Array.isArray(payload?.errors) && payload.errors.length) {
      return payload.errors.map((entry) => entry.message).join("；");
    }
    return payload?.detail || fallback;
  }

  function showError(message) {
    const target = byId("h3-errors");
    target.textContent = message;
    target.classList.remove("hidden");
  }

  function clearError() {
    byId("h3-errors").classList.add("hidden");
  }

  function resetQuote() {
    if (preparedBatchId) requestKey = crypto.randomUUID();
    preparedBatchId = "";
    byId("h3-fee-panel").classList.add("hidden");
    byId("h3-cost-confirm").checked = false;
  }

  function renderAssetList(kind) {
    const target = byId(`h3-${kind.slice(0, -1)}-list`);
    target.innerHTML = state[kind].map((asset, index) => `
      <li>
        <span>${index + 1}. ${escapeHtml(asset.originalName)}</span>
        <button type="button" class="button secondary" data-h3-remove="${kind}" data-index="${index}">移除</button>
        ${kind === "audios" && asset.previewUrl ? `<audio controls preload="metadata" src="${asset.previewUrl}"></audio>` : ""}
      </li>
    `).join("");
  }

  function renderRows() {
    const count = Math.max(state.videos.length, state.audios.length);
    state.scripts.length = count;
    const body = byId("h3-pairing-body");
    body.innerHTML = Array.from({length: count}, (_, index) => `
      <tr>
        <td>${index + 1}</td>
        <td>${escapeHtml(state.videos[index]?.originalName || "缺少视频")}</td>
        <td>${escapeHtml(state.audios[index]?.originalName || "缺少音频")}</td>
        <td><textarea data-h3-script="${index}" rows="4" maxlength="100000" placeholder="粘贴这份音频对应的完整原稿">${escapeHtml(state.scripts[index] || "")}</textarea></td>
      </tr>
    `).join("");
    byId("h3-ready-summary").textContent = count
      ? `当前 ${count} 条；视频、音频和原稿必须逐行对应。`
      : "请先上传等量的视频和音频，并填写对应原稿。";
  }

  function render() {
    renderAssetList("images");
    renderAssetList("videos");
    renderAssetList("audios");
    renderRows();
    byId("h3-upload-progress").textContent =
      `人物图 ${state.images.length}/4，参考视频 ${state.videos.length}，成品音频 ${state.audios.length}`;
    resetQuote();
  }

  async function uploadFiles(files, kind, bucket) {
    if (!files.length) return;
    if (bucket === "images" && state.images.length + files.length > 4) {
      showError("人物参考图最多上传 4 张");
      return;
    }
    clearError();
    for (const file of files) {
      byId("h3-upload-progress").textContent = `正在上传：${file.name}`;
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      const response = await fetch("/api/batch-assets", {
        method: "POST",
        headers: {"X-CSRF-Token": csrf},
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, `${file.name} 上传失败`));
      state[bucket].push({
        assetId: payload.assetId,
        originalName: payload.originalName,
        previewUrl: kind === "audio" ? URL.createObjectURL(file) : "",
      });
    }
    render();
    const name = byId("batch-name");
    if (!name.value.trim() && (state.videos[0] || state.audios[0])) {
      name.value = (state.videos[0] || state.audios[0]).originalName.replace(/\.[^./\\]+$/, "");
    }
  }

  async function loadAccounts() {
    const response = await fetch("/api/h3-page/accounts");
    const payload = await response.json();
    if (!response.ok) throw new Error(errorMessage(payload, "未读取到可用 H3 执行账号"));
    accountIds = payload.default_selected_account_ids || [];
    if (!accountIds.length) throw new Error("当前没有可用 H3 执行账号");
  }

  function validateInputs() {
    if (!state.videos.length) return "请至少上传一段参考视频";
    if (state.videos.length !== state.audios.length) return "参考视频和成品音频数量必须一致";
    if (state.scripts.some((value) => !String(value || "").trim())) return "每条音频都必须填写对应原稿";
    if (!byId("h3-input-confirm").checked) return "请先试听并确认音频、视频和原稿顺序";
    const megapixels = Number(byId("h3-megapixels").value);
    if (!Number.isFinite(megapixels) || megapixels < 0.2 || megapixels > 2) return "H3 清晰度必须在 0.2～2 MP 之间";
    return "";
  }

  const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  async function createAudioAlignment(audio, scriptText) {
    const response = await fetch("/api/h3-page/audio-alignments", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({
        audio_asset_id: audio.assetId,
        script_text: scriptText,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(errorMessage(payload, "H3 音频对齐任务创建失败"));
    return payload;
  }

  async function waitForAudioAlignment(initial, position, total) {
    let payload = initial;
    while (["PENDING", "RUNNING"].includes(payload.status)) {
      byId("h3-prepare-button").textContent =
        `正在等待 ASR 对齐 ${position}/${total}（${payload.status === "RUNNING" ? "处理中" : "排队中"}）…`;
      await wait(Math.max(Number(payload.retry_after_seconds || 2), 1) * 1000);
      const response = await fetch(`/api/h3-page/audio-alignments/${payload.job_id}`);
      payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "H3 音频对齐状态读取失败"));
    }
    if (payload.status !== "SUCCESS" || !payload.alignment) {
      throw new Error(payload.error_message || `第 ${position} 条音频 ASR 对齐失败`);
    }
    return payload.alignment;
  }

  async function alignUploadedAudios() {
    const created = [];
    for (let index = 0; index < state.audios.length; index += 1) {
      byId("h3-prepare-button").textContent =
        `正在创建 ASR 对齐任务 ${index + 1}/${state.audios.length}…`;
      created.push(await createAudioAlignment(
        state.audios[index],
        state.scripts[index].trim(),
      ));
    }
    return Promise.all(created.map((job, index) =>
      waitForAudioAlignment(job, index + 1, created.length),
    ));
  }

  async function prepare() {
    clearError();
    const validation = validateInputs();
    if (validation) return showError(validation);
    const button = byId("h3-prepare-button");
    button.disabled = true;
    button.textContent = "正在对齐音频并计算…";
    try {
      if (!accountIds.length) await loadAccounts();
      const alignments = await alignUploadedAudios();
      button.textContent = "正在生成安全切段与费用快照…";
      const rows = state.videos.map((video, index) => ({
        row_id: `H3-${String(index + 1).padStart(3, "0")}`,
        script_text: state.scripts[index].trim(),
        video_asset_id: video.assetId,
        audio_asset_id: state.audios[index].assetId,
        audio_alignment: alignments[index],
      }));
      const response = await fetch("/api/h3-page/batches/prepare", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({
          name: byId("batch-name").value.trim() || "H3 多参考批次",
          request_key: requestKey,
          reference_image_asset_ids: state.images.map((asset) => asset.assetId),
          selected_account_ids: accountIds,
          defaults: {
            continuity_mode: byId("h3-continuity-mode").value,
            generation_tail_seconds: 0.1,
            user_direction: byId("h3-user-direction").value.trim(),
            resolution: {
              aspect_ratio: byId("h3-aspect-ratio").value,
              megapixels: Number(byId("h3-megapixels").value),
              multiple: 32,
            },
          },
          rows,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "H3 分段费用计算失败"));
      preparedBatchId = payload.batch_id;
      const fee = payload.fee_snapshot || {};
      byId("h3-fee-summary").textContent =
        `共 ${fee.segment_count || 0} 个分段；预计 ${fee.estimated_paid_calls || 0} 次付费调用；` +
        `可直接复用 ${fee.reusable_result_count || 0} 个结果。`;
      byId("h3-fee-panel").classList.remove("hidden");
      byId("h3-fee-panel").scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) {
      showError(error.message || "H3 分段费用计算失败");
    } finally {
      button.disabled = false;
      button.textContent = "计算分段与费用";
    }
  }

  async function confirmCost() {
    if (!preparedBatchId) return showError("请先计算 H3 分段与费用");
    if (!byId("h3-cost-confirm").checked) return showError("请先勾选 H3 费用确认");
    const button = byId("h3-confirm-button");
    button.disabled = true;
    button.textContent = "正在提交 H3…";
    try {
      const response = await fetch(`/api/h3-page/batches/${preparedBatchId}/confirm`, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({cost_confirmed: true}),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "H3 任务提交失败"));
      window.location.href = `/batches/${payload.batch_id}`;
    } catch (error) {
      showError(error.message || "H3 任务提交失败");
      button.disabled = false;
      button.textContent = "确认费用并开始 H3";
    }
  }

  byId("h3-image-files").addEventListener("change", (event) => {
    uploadFiles(Array.from(event.target.files || []), "image", "images").catch((error) => showError(error.message));
    event.target.value = "";
  });
  byId("h3-video-files").addEventListener("change", (event) => {
    uploadFiles(Array.from(event.target.files || []), "video", "videos").catch((error) => showError(error.message));
    event.target.value = "";
  });
  byId("h3-audio-files").addEventListener("change", (event) => {
    uploadFiles(Array.from(event.target.files || []), "audio", "audios").catch((error) => showError(error.message));
    event.target.value = "";
  });
  panel.addEventListener("click", (event) => {
    const button = event.target.closest("[data-h3-remove]");
    if (!button) return;
    const bucket = button.dataset.h3Remove;
    const index = Number(button.dataset.index);
    const [removed] = state[bucket].splice(index, 1);
    if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
    if (bucket === "videos" || bucket === "audios") state.scripts.splice(index, 1);
    requestKey = crypto.randomUUID();
    render();
  });
  byId("h3-pairing-body").addEventListener("input", (event) => {
    if (!event.target.matches("[data-h3-script]")) return;
    state.scripts[Number(event.target.dataset.h3Script)] = event.target.value;
    resetQuote();
  });
  ["h3-continuity-mode", "h3-aspect-ratio", "h3-megapixels", "h3-user-direction"]
    .forEach((id) => byId(id).addEventListener("change", resetQuote));
  byId("h3-prepare-button").addEventListener("click", prepare);
  byId("h3-confirm-button").addEventListener("click", confirmCost);
  workflow.addEventListener("change", () => {
    if (isH3()) loadAccounts().catch((error) => showError(error.message));
  });

  render();
  if (isH3()) loadAccounts().catch((error) => showError(error.message));
})();
