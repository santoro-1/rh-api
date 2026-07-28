(() => {
  "use strict";

  const config = window.batchGenerateConfig;
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const workflowSelect = document.getElementById("batch-workflow");
  const audioModeSelect = document.getElementById("batch-audio-mode");
  const personModeSelect = document.getElementById("batch-person-mode");
  const instanceTypeSelect = document.getElementById("batch-instance-type");
  const resolutionInput = document.getElementById("batch-resolution");
  const quickConfirm = document.getElementById("quick-confirm");
  const advancedConfirm = document.getElementById("advanced-confirm");

  // Browser-only staged asset state. Each array preserves the visible upload
  // order; the backend receives opaque asset IDs and performs authoritative
  // ownership, type, filename and batch-limit validation again.
  const assetGroups = {
    primary: [],
    audio: [],
    leftAudio: [],
    rightAudio: [],
    advancedPrimary: [],
    advancedAudio: [],
  };
  // Arrays use the same zero-based index as the visible row so reordering a
  // primary file also identifies which prompt/script belongs to that row.
  let quickPrompts = [];
  let quickScripts = [];
  // advancedRows keeps editable Excel/CSV values; it is never trusted as a
  // validated task plan until the server accepts the create request.
  let advancedRows = [];
  // requestKey makes a repeated click/network retry idempotent for one draft.
  let requestKey = crypto.randomUUID();
  let activeEntry = "quick";
  let draggedAsset = null;

  const groupElements = {
    primary: document.getElementById("quick-primary-list"),
    audio: document.getElementById("quick-audio-list"),
    leftAudio: document.getElementById("quick-left-audio-list"),
    rightAudio: document.getElementById("quick-right-audio-list"),
  };

  const advancedColumns = {
    digital_human: [
      ["row_id", "任务编号"],
      ["image_file", "图片文件"],
      ["audio_file", "总参考音频"],
      ["speech_script", "口播脚本"],
      ["prompt", "提示词"],
      ["left_audio_file", "左人物音频"],
      ["right_audio_file", "右人物音频"],
    ],
    ltx_lip_sync: [
      ["row_id", "任务编号"],
      ["source_video_file", "源视频文件"],
      ["audio_file", "音频文件"],
      ["speech_script", "口播脚本"],
      ["positive_prompt", "视频正向提示词"],
    ],
  };

  function isSpeechMode() {
    return audioModeSelect.value === "minimax";
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function showErrors(targetId, errors) {
    const target = document.getElementById(targetId);
    target.innerHTML = `<strong>请修正以下问题：</strong><ul>${
      errors.map((error) => `<li>${
        error.rowNumber
          ? `第 ${error.rowNumber} 行${error.rowId ? `（${escapeHtml(error.rowId)}）` : ""}：`
          : ""
      }${escapeHtml(error.message || "未知错误")}</li>`).join("")
    }</ul>`;
    target.classList.remove("hidden");
  }

  function hideErrors(targetId) {
    document.getElementById(targetId).classList.add("hidden");
  }

  function batchParameters() {
    if (workflowSelect.value === "digital_human") {
      return {
        resolution: resolutionInput.value,
        person_mode: personModeSelect.value,
        ...(isSpeechMode()
          ? {default_prompt: config.digitalDefaultPrompt}
          : {}),
      };
    }
    return {
      instance_type: instanceTypeSelect.value,
      ...(isSpeechMode()
        ? {prompt_prefix: document.getElementById("speech-ltx-prompt-prefix").value}
        : {}),
    };
  }

  function speechOptions() {
    if (!isSpeechMode()) return {};
    return {
      voiceAssetId: document.getElementById("speech-voice").value,
      model: document.getElementById("speech-model").value,
      speed: Number(document.getElementById("speech-speed").value),
      volume: Number(document.getElementById("speech-volume").value),
      pitch: Number(document.getElementById("speech-pitch").value),
      languageBoost: document.getElementById("speech-language-boost").value,
      outputFormat: document.getElementById("speech-output-format").value,
      pronunciationTones:
        document.getElementById("speech-pronunciation-tones").value.trim(),
      reviewRequired:
        document.getElementById("speech-review-required").checked,
      costConfirmed: document.getElementById("speech-cost-confirm").checked,
    };
  }

  function allAssetIds(entry) {
    let groups;
    if (entry === "quick") {
      groups = ["primary"];
      if (!isSpeechMode()) groups.push("audio");
      if (!isSpeechMode() && workflowSelect.value === "digital_human" && personModeSelect.value === "双人") {
        groups.push("leftAudio", "rightAudio");
      }
    } else {
      groups = ["advancedPrimary"];
      if (!isSpeechMode()) groups.push("advancedAudio");
    }
    return groups.flatMap((group) => assetGroups[group].map((asset) => asset.assetId));
  }

  function resetConfirmation() {
    quickConfirm.checked = false;
    advancedConfirm.checked = false;
  }

  function moveAsset(group, fromIndex, toIndex) {
    const assets = assetGroups[group];
    if (toIndex < 0 || toIndex >= assets.length || fromIndex === toIndex) return;
    const [asset] = assets.splice(fromIndex, 1);
    assets.splice(toIndex, 0, asset);
    resetConfirmation();
    renderQuick();
  }

  function renderOrderedGroup(group) {
    const list = groupElements[group];
    const assets = assetGroups[group];
    list.innerHTML = assets.length
      ? assets.map((asset, index) => `
        <li class="ordered-upload-item" draggable="true" data-group="${group}" data-index="${index}">
          <span class="order-number">${index + 1}</span>
          <span class="ordered-file-name" title="${escapeHtml(asset.originalName)}">${escapeHtml(asset.originalName)}</span>
          <span class="order-actions">
            <button type="button" class="order-button" data-action="up" aria-label="上移">↑</button>
            <button type="button" class="order-button" data-action="down" aria-label="下移">↓</button>
            <button type="button" class="order-button remove" data-action="remove" aria-label="移除">×</button>
          </span>
        </li>`).join("")
      : '<li class="empty-upload-item">尚未上传</li>';
  }

  function quickRowCount() {
    if (isSpeechMode()) return assetGroups.primary.length;
    return Math.max(
      assetGroups.primary.length,
      assetGroups.audio.length,
      personModeSelect.value === "双人" ? assetGroups.leftAudio.length : 0,
      personModeSelect.value === "双人" ? assetGroups.rightAudio.length : 0,
    );
  }

  function quickPromptPlaceholder() {
    return workflowSelect.value === "ltx_lip_sync"
      ? (
        isSpeechMode()
          ? "例如：一名女性用中文说（系统自动追加当前分段台词）"
          : "例如：一名女性用中文说：“今天给大家介绍这款产品。”"
      )
      : "填写本条数字人任务的提示词";
  }

  function renderQuickPairing() {
    const digital = workflowSelect.value === "digital_human";
    const dual = digital && personModeSelect.value === "双人";
    const headers = ["序号", digital ? "参考图片" : "源视频"];
    if (!isSpeechMode()) headers.push(digital ? "总参考音频" : "音频");
    if (dual) headers.push("左人物音频", "右人物音频");
    if (isSpeechMode()) headers.push("口播脚本（生成语音）");
    headers.push(
      digital
        ? "提示词"
        : (
          isSpeechMode()
            ? "人物与语言（自动追加分段台词）"
            : "视频正向提示词（人物 + 语言 + 完整台词）"
        )
    );
    document.getElementById("quick-pairing-head").innerHTML =
      headers.map((label) => `<th>${label}</th>`).join("");

    const count = quickRowCount();
    while (quickPrompts.length < count) {
      quickPrompts.push(
        digital
          ? config.digitalDefaultPrompt
          : (
            isSpeechMode()
              ? document.getElementById("speech-ltx-prompt-prefix").value
              : ""
          )
      );
    }
    while (quickScripts.length < count) quickScripts.push("");
    if (!count) {
      document.getElementById("quick-pairing-body").innerHTML =
        `<tr><td colspan="${headers.length}" class="empty-cell">上传素材后显示最终配对结果</td></tr>`;
      return;
    }

    document.getElementById("quick-pairing-body").innerHTML =
      Array.from({length: count}, (_, index) => {
        const cells = [
          `<td><strong>${index + 1}</strong></td>`,
          `<td>${assetNameOrMissing(assetGroups.primary[index])}</td>`,
        ];
        if (!isSpeechMode()) {
          cells.push(`<td>${assetNameOrMissing(assetGroups.audio[index])}</td>`);
        }
        if (dual) {
          cells.push(
            `<td>${assetNameOrMissing(assetGroups.leftAudio[index])}</td>`,
            `<td>${assetNameOrMissing(assetGroups.rightAudio[index])}</td>`,
          );
        }
        if (isSpeechMode()) {
          cells.push(`<td><textarea class="batch-script-input" data-index="${index}" rows="7"
            maxlength="9999" placeholder="填写本条任务要朗读的完整口播脚本">${escapeHtml(quickScripts[index] || "")}</textarea></td>`);
        }
        cells.push(`<td><textarea class="batch-prompt-input" data-index="${index}" rows="5"
          maxlength="5000" placeholder="${escapeHtml(quickPromptPlaceholder())}">${escapeHtml(quickPrompts[index] || "")}</textarea></td>`);
        return `<tr>${cells.join("")}</tr>`;
      }).join("");
  }

  function assetNameOrMissing(asset) {
    return asset
      ? `<span class="paired-file">${escapeHtml(asset.originalName)}</span>`
      : '<span class="pair-missing">缺少文件</span>';
  }

  function quickValidationErrors() {
    const count = assetGroups.primary.length;
    const errors = [];
    if (!count) errors.push({message: "请先上传图片或源视频"});
    if (count > config.maxBatchItems) {
      errors.push({message: `单批最多 ${config.maxBatchItems} 条任务`});
    }
    if (!isSpeechMode() && assetGroups.audio.length !== count) {
      errors.push({message: "画面素材与音频数量不一致，请按相同顺序上传"});
    }
    if (!isSpeechMode() && workflowSelect.value === "digital_human" && personModeSelect.value === "双人") {
      if (assetGroups.leftAudio.length !== count) {
        errors.push({message: "双人批次的左人物音频数量与图片数量不一致"});
      }
      if (assetGroups.rightAudio.length !== count) {
        errors.push({message: "双人批次的右人物音频数量与图片数量不一致"});
      }
    }
    for (let index = 0; index < count; index += 1) {
      if (isSpeechMode() && !String(quickScripts[index] || "").trim()) {
        errors.push({rowNumber: index + 1, message: "口播脚本不能为空"});
      }
      if (!String(quickPrompts[index] || "").trim()) {
        errors.push({rowNumber: index + 1, message: "提示词不能为空"});
      }
    }
    if (isSpeechMode()) {
      if (!config.minimaxConfigured) {
        errors.push({message: "当前账号尚未配置 MiniMax API Key"});
      }
      if (!document.getElementById("speech-voice").value) {
        errors.push({message: "请先选择声音管理中已经保存的音色"});
      }
      if (!document.getElementById("speech-cost-confirm").checked) {
        errors.push({message: "请先确认语音文本生成及可能的音色费用"});
      }
    }
    if (!quickConfirm.checked) {
      errors.push({message: "请先勾选确认，表示已经核对素材顺序和配对关系"});
    }
    return errors;
  }

  function buildQuickRows() {
    const digital = workflowSelect.value === "digital_human";
    const dual = digital && personModeSelect.value === "双人";
    return assetGroups.primary.map((primary, index) => ({
      row_id: `TASK-${String(index + 1).padStart(3, "0")}`,
      ...(digital
        ? {
            image_file: primary.originalName,
            audio_file: isSpeechMode() ? "" : assetGroups.audio[index].originalName,
            speech_script: isSpeechMode() ? quickScripts[index].trim() : "",
            prompt: quickPrompts[index].trim(),
            ...(dual
              ? {
                  left_audio_file: assetGroups.leftAudio[index].originalName,
                  right_audio_file: assetGroups.rightAudio[index].originalName,
                }
              : {}),
          }
        : {
            source_video_file: primary.originalName,
            audio_file: isSpeechMode() ? "" : assetGroups.audio[index].originalName,
            speech_script: isSpeechMode() ? quickScripts[index].trim() : "",
            ...(isSpeechMode()
              ? {prompt_prefix: quickPrompts[index].trim()}
              : {positive_prompt: quickPrompts[index].trim()}),
          }),
    }));
  }

  function renderQuick() {
    ["primary", "audio", "leftAudio", "rightAudio"].forEach(renderOrderedGroup);
    renderQuickPairing();
    const count = assetGroups.primary.length;
    document.getElementById("quick-upload-progress").textContent =
      count ? `当前有 ${count} 个画面素材，请在下方核对所有配对。` : "尚未上传素材。";
    document.getElementById("quick-ready-summary").textContent =
      count
        ? `准备创建 ${count} 条独立任务。`
        : `请先上传画面素材${isSpeechMode() ? "并填写脚本" : "和对应音频"}。`;
  }

  function renderAdvancedRows() {
    const columns = isSpeechMode()
      ? (
        workflowSelect.value === "digital_human"
          ? [
            ["row_id", "脚本编号"],
            ["speech_script", "脚本内容"],
            ["prompt", "数字人提示词"],
          ]
          : [
            ["row_id", "脚本编号"],
            ["speech_script", "脚本内容"],
            ["prompt_prefix", "人物与语言"],
          ]
      )
      : advancedColumns[workflowSelect.value].filter(
        ([key]) => key !== "speech_script"
      );
    document.getElementById("advanced-preview-head").innerHTML =
      columns.map(([, label]) => `<th>${label}</th>`).join("");
    const body = document.getElementById("advanced-preview-body");
    if (!advancedRows.length) {
      body.innerHTML = `<tr><td colspan="${columns.length}" class="empty-cell">导入 Excel 或 CSV 后显示清单</td></tr>`;
    } else {
      body.innerHTML = advancedRows.map((row, rowIndex) =>
        `<tr>${columns.map(([key]) => {
          const value = escapeHtml(row[key] || "");
          const longText = [
            "speech_script",
            "prompt",
            "prompt_prefix",
            "positive_prompt",
          ].includes(key);
          return longText
            ? `<td><textarea class="batch-cell-input batch-long-text-input"
                data-row-index="${rowIndex}" data-key="${key}" rows="4">${value}</textarea></td>`
            : `<td><input class="batch-cell-input" data-row-index="${rowIndex}"
                data-key="${key}" value="${value}"></td>`;
        }).join("")}</tr>`
      ).join("");
    }
    document.getElementById("advanced-ready-summary").textContent = advancedRows.length
      ? `已读取 ${advancedRows.length} 行，已暂存 ${allAssetIds("advanced").length} 个文件。`
      : "请先导入清单并上传素材。";
  }

  function finalVideoPrompt(row) {
    if (workflowSelect.value === "digital_human") {
      return String(row.prompt || "").trim();
    }
    if (!isSpeechMode()) {
      return String(row.positive_prompt || "").trim();
    }
    const prefix = String(row.prompt_prefix || "").trim().replace(/[：:]$/, "");
    const script = String(row.speech_script || "").trim();
    return `${prefix}：“${script}”`;
  }

  function openCopyPreview(entry) {
    const rows = entry === "quick" ? buildQuickRows() : advancedRows;
    const content = document.getElementById("copy-preview-content");
    const pronunciation = isSpeechMode()
      ? document.getElementById("speech-pronunciation-tones").value.trim()
      : "";
    const sharedRules = pronunciation
      ? `<section class="copy-preview-shared">
          <h3>本批次读音标注</h3>
          <pre>${escapeHtml(pronunciation)}</pre>
        </section>`
      : "";
    const cards = rows.map((row, index) => {
      const script = String(row.speech_script || "").trim();
      const prompt = finalVideoPrompt(row);
      return `<article class="copy-preview-card">
        <h3>${index + 1}. ${escapeHtml(row.row_id || `TASK-${index + 1}`)}</h3>
        ${isSpeechMode()
          ? `<h4>完整口播脚本</h4><p>${escapeHtml(script) || "（空）"}</p>`
          : ""}
        <h4>${workflowSelect.value === "ltx_lip_sync" ? "最终视频正向提示词" : "数字人提示词"}</h4>
        <p>${escapeHtml(prompt) || "（空）"}</p>
      </article>`;
    }).join("");
    content.innerHTML = sharedRules + (
      cards || '<p class="muted">当前还没有可预览的任务行。</p>'
    );
    document.getElementById("copy-preview-dialog").showModal();
  }

  function renderAdvancedAssets() {
    const assets = [...assetGroups.advancedPrimary, ...assetGroups.advancedAudio];
    document.getElementById("advanced-asset-list").innerHTML = [
      ...assetGroups.advancedPrimary.map((asset, index) => ({asset, group: "advancedPrimary", index})),
      ...assetGroups.advancedAudio.map((asset, index) => ({asset, group: "advancedAudio", index})),
    ].map(({asset, group, index}) =>
      `<span class="asset-chip"><strong>${index + 1}.</strong> ${escapeHtml(asset.originalName)} · ${asset.kind}
        <button type="button" class="advanced-order-asset" data-action="up" data-group="${group}" data-index="${index}" aria-label="上移">↑</button>
        <button type="button" class="advanced-order-asset" data-action="down" data-group="${group}" data-index="${index}" aria-label="下移">↓</button>
        <button type="button" class="advanced-order-asset" data-action="remove" data-group="${group}" data-index="${index}" aria-label="移除">×</button>
      </span>`
    ).join("");
    document.getElementById("advanced-upload-progress").textContent = assets.length
      ? `已暂存 ${assets.length} 个文件。`
      : "尚未上传素材。";
    renderAdvancedRows();
  }

  async function uploadFiles(fileList, kind, group, progressId) {
    const files = Array.from(fileList);
    for (let index = 0; index < files.length; index += 1) {
      document.getElementById(progressId).textContent =
        `正在上传 ${index + 1}/${files.length}：${files[index].name}`;
      const form = new FormData();
      form.append("file", files[index]);
      form.append("kind", kind);
      const response = await fetch("/api/batch-assets", {
        method: "POST",
        headers: {"X-CSRF-Token": csrfToken},
        body: form,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || `${files[index].name} 上传失败`);
      }
      assetGroups[group].push(data);
      resetConfirmation();
      activeEntry === "quick" ? renderQuick() : renderAdvancedAssets();
    }
  }

  function bindUpload(inputId, kind, group, progressId, errorId) {
    document.getElementById(inputId).addEventListener("change", async (event) => {
      try {
        const resolvedKind = typeof kind === "function" ? kind() : kind;
        await uploadFiles(event.target.files, resolvedKind, group, progressId);
      } catch (error) {
        showErrors(errorId, [{message: error.message}]);
      } finally {
        // Clearing the picker lets a user select the same file again after removal.
        event.target.value = "";
      }
    });
  }

  function resetWorkflowData() {
    Object.values(assetGroups).forEach((assets) => assets.splice(0));
    quickPrompts = [];
    quickScripts = [];
    advancedRows = [];
    requestKey = crypto.randomUUID();
    document.querySelectorAll('input[type="file"]').forEach((input) => {
      input.value = "";
    });
    document.getElementById("manifest-info").textContent = "尚未导入清单。";
    resetConfirmation();
    renderQuick();
    renderAdvancedAssets();
  }

  function updateWorkflowUi(resetData = true) {
    const digital = workflowSelect.value === "digital_human";
    document.getElementById("digital-batch-settings").classList.toggle("hidden", !digital);
    document.getElementById("ltx-batch-settings").classList.toggle("hidden", digital);
    document.getElementById("primary-upload-title").textContent = digital ? "参考图片" : "源视频";
    document.getElementById("main-audio-upload-title").textContent = digital ? "总参考音频" : "音频";
    document.getElementById("advanced-primary-upload-title").textContent = digital ? "全部参考图片" : "全部源视频";
    const primaryAccept = digital
      ? ".jpg,.jpeg,.png,.webp,image/*"
      : ".mp4,.mov,.webm,video/*";
    document.getElementById("quick-primary-files").accept = primaryAccept;
    document.getElementById("advanced-primary-files").accept = primaryAccept;
    document.getElementById("xlsx-template-link").href =
      `/api/batch-templates/${isSpeechMode() ? "script" : workflowSelect.value}.xlsx`;
    document.getElementById("csv-template-link").href =
      `/api/batch-templates/${isSpeechMode() ? "script" : workflowSelect.value}.csv`;
    document.getElementById("quick-prompt-help").textContent = digital
      ? `${isSpeechMode() ? "每行分别填写口播脚本和提示词；" : "每条任务只需要单独修改提示词；"}分辨率、模式和时间无需重复填写。`
      : `${isSpeechMode() ? "口播脚本用于生成语音；" : ""}正向提示词只填写什么人、使用什么语言、音频中的完整说话内容，不需要动作或画面描述。`;
    updateAudioModeUi();
    updateDualAudioUi();
    if (resetData) resetWorkflowData();
  }

  function updateSpeechCostText() {
    const selected = document.getElementById("speech-voice").selectedOptions[0];
    const activated = selected?.dataset.activated === "true";
    document.getElementById("speech-cost-confirm-text").textContent = activated
      ? "所选音色已完成首次正式使用；我已了解本批文本生成仍会计费。"
      : "所选音色首次正式使用可能产生 ¥9.90 音色费用；我已了解文本生成仍会另外计费。";
    resetConfirmation();
  }

  function updateAudioModeUi() {
    const speech = isSpeechMode();
    document.getElementById("minimax-speech-settings").classList.toggle("hidden", !speech);
    document.getElementById("quick-direct-audio-group").classList.toggle("hidden", speech);
    document.getElementById("advanced-direct-audio-group").classList.toggle("hidden", speech);
    document.getElementById("speech-ltx-prompt-prefix-row").classList.toggle(
      "hidden",
      !(speech && workflowSelect.value === "ltx_lip_sync"),
    );
    document.getElementById("advanced-confirm-row").classList.toggle("hidden", !speech);
    document.getElementById("manifest-step-title").textContent = speech
      ? "1. 导入脚本清单"
      : "1. 导入高级任务清单";
    document.getElementById("manifest-entry-button").textContent = speech
      ? "Excel / CSV 脚本导入"
      : "Excel / CSV 高级导入";
    document.getElementById("manifest-step-help").textContent = speech
      ? "Excel 和 CSV 使用同一个两列模板：脚本编号、脚本内容。"
      : "适合已经由运营表格整理好文件对应关系的批次。";
    document.getElementById("advanced-upload-help").textContent = speech
      ? "按表格第 1、2、3……行的顺序上传图片或视频；上传后可用箭头调整顺序。"
      : "高级导入按清单中的完整文件名匹配，不依赖上传顺序。";
    document.getElementById("quick-upload-help").textContent = speech
      ? "按最终任务顺序上传图片或视频，再在下方逐行填写对应口播脚本。"
      : "先上传图片或视频，再按完全相同的顺序上传对应音频；每条音频不能超过 45 秒。";
    document.getElementById("quick-order-guidance").textContent = speech
      ? "系统按照页面显示的第 1、2、3……项创建任务。建议画面文件名带 01、02、03 序号；上传后可拖动或使用箭头调整顺序。"
      : "系统按照页面显示的第 1、2、3……项逐一配对。建议文件名加上 01、02、03 序号；上传后可拖动或使用箭头调整顺序。";
    if (speech && workflowSelect.value === "digital_human") {
      personModeSelect.value = "单人";
    }
    personModeSelect.disabled = speech && workflowSelect.value === "digital_human";
    updateSpeechCostText();
    renderQuick();
    renderAdvancedRows();
  }

  function updateDualAudioUi() {
    const show = !isSpeechMode() && workflowSelect.value === "digital_human" && personModeSelect.value === "双人";
    document.getElementById("left-audio-group").classList.toggle("hidden", !show);
    document.getElementById("right-audio-group").classList.toggle("hidden", !show);
    resetConfirmation();
    renderQuick();
  }

  function setEntry(entry) {
    activeEntry = entry;
    document.getElementById("quick-entry-panel").classList.toggle("hidden", entry !== "quick");
    document.getElementById("manifest-entry-panel").classList.toggle("hidden", entry !== "manifest");
    document.getElementById("quick-entry-button").classList.toggle("active", entry === "quick");
    document.getElementById("manifest-entry-button").classList.toggle("active", entry === "manifest");
  }

  async function submitBatch(rows, entry, buttonId, errorId) {
    const button = document.getElementById(buttonId);
    hideErrors(errorId);
    button.disabled = true;
    button.textContent = "正在校验…";
    const payload = {
      name: document.getElementById("batch-name").value,
      workflowType: workflowSelect.value,
      audioMode: audioModeSelect.value,
      requestKey,
      batchParameters: batchParameters(),
      speechOptions: speechOptions(),
      rows,
      assetIds: allAssetIds(entry),
    };
    try {
      const validation = await fetch("/api/batches/validate", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
        body: JSON.stringify(payload),
      });
      const validationData = await validation.json();
      if (!validation.ok) {
        showErrors(errorId, validationData.errors || [
          {message: validationData.detail || "批次校验失败"},
        ]);
        return;
      }
      button.textContent = "正在创建任务…";
      const response = await fetch("/api/batches", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        showErrors(errorId, data.errors || [{message: data.detail || "批次创建失败"}]);
        return;
      }
      location.href = `/batches/${data.batchId}`;
    } catch (error) {
      showErrors(errorId, [{message: error.message || "批次创建失败"}]);
    } finally {
      button.disabled = false;
      button.textContent = "校验并创建批次";
    }
  }

  bindUpload(
    "quick-primary-files",
    () => workflowSelect.value === "digital_human" ? "image" : "video",
    "primary",
    "quick-upload-progress",
    "quick-errors",
  );
  bindUpload("quick-audio-files", "audio", "audio", "quick-upload-progress", "quick-errors");
  bindUpload(
    "quick-left-audio-files",
    "audio",
    "leftAudio",
    "quick-upload-progress",
    "quick-errors",
  );
  bindUpload(
    "quick-right-audio-files",
    "audio",
    "rightAudio",
    "quick-upload-progress",
    "quick-errors",
  );
  bindUpload(
    "advanced-primary-files",
    () => workflowSelect.value === "digital_human" ? "image" : "video",
    "advancedPrimary",
    "advanced-upload-progress",
    "advanced-errors",
  );
  bindUpload(
    "advanced-audio-files",
    "audio",
    "advancedAudio",
    "advanced-upload-progress",
    "advanced-errors",
  );

  document.querySelectorAll(".ordered-upload-list").forEach((list) => {
    list.addEventListener("click", (event) => {
      const button = event.target.closest(".order-button");
      if (!button) return;
      const item = button.closest(".ordered-upload-item");
      const fromIndex = Number(item.dataset.index);
      if (button.dataset.action === "remove") {
        assetGroups[item.dataset.group].splice(fromIndex, 1);
        resetConfirmation();
        renderQuick();
        return;
      }
      moveAsset(item.dataset.group, fromIndex, fromIndex + (button.dataset.action === "up" ? -1 : 1));
    });
    list.addEventListener("dragstart", (event) => {
      const item = event.target.closest(".ordered-upload-item");
      if (!item) return;
      draggedAsset = {group: item.dataset.group, index: Number(item.dataset.index)};
      event.dataTransfer.effectAllowed = "move";
    });
    list.addEventListener("dragover", (event) => {
      if (draggedAsset) event.preventDefault();
    });
    list.addEventListener("drop", (event) => {
      event.preventDefault();
      const target = event.target.closest(".ordered-upload-item");
      if (!target || !draggedAsset || target.dataset.group !== draggedAsset.group) return;
      moveAsset(draggedAsset.group, draggedAsset.index, Number(target.dataset.index));
      draggedAsset = null;
    });
    list.addEventListener("dragend", () => {
      draggedAsset = null;
    });
  });

  document.getElementById("quick-pairing-body").addEventListener("input", (event) => {
    const scriptInput = event.target.closest(".batch-script-input");
    if (scriptInput) {
      quickScripts[Number(scriptInput.dataset.index)] = scriptInput.value;
      resetConfirmation();
      return;
    }
    const input = event.target.closest(".batch-prompt-input");
    if (!input) return;
    quickPrompts[Number(input.dataset.index)] = input.value;
    resetConfirmation();
  });
  document.getElementById("advanced-asset-list").addEventListener("click", (event) => {
    const button = event.target.closest(".advanced-order-asset");
    if (!button) return;
    const group = button.dataset.group;
    const index = Number(button.dataset.index);
    if (button.dataset.action === "remove") {
      assetGroups[group].splice(index, 1);
    } else {
      const target = index + (button.dataset.action === "up" ? -1 : 1);
      if (target >= 0 && target < assetGroups[group].length) {
        const [asset] = assetGroups[group].splice(index, 1);
        assetGroups[group].splice(target, 0, asset);
      }
    }
    resetConfirmation();
    renderAdvancedAssets();
  });
  document.getElementById("advanced-preview-body").addEventListener("input", (event) => {
    const input = event.target.closest(".batch-cell-input");
    if (!input) return;
    advancedRows[Number(input.dataset.rowIndex)][input.dataset.key] = input.value;
  });

  document.getElementById("manifest-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    hideErrors("advanced-errors");
    document.getElementById("manifest-info").textContent = "正在读取任务清单…";
    const form = new FormData();
    form.append("manifest", file);
    form.append("workflowType", workflowSelect.value);
    form.append("audioMode", audioModeSelect.value);
    try {
      const response = await fetch("/api/batch-manifests/parse", {
        method: "POST",
        headers: {"X-CSRF-Token": csrfToken},
        body: form,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "清单读取失败");
      advancedRows = data.rows;
      if (isSpeechMode()) {
        advancedRows = advancedRows.map((row) => ({
          ...row,
          ...(workflowSelect.value === "digital_human"
            ? {prompt: row.prompt || config.digitalDefaultPrompt}
            : {
              prompt_prefix:
                row.prompt_prefix
                || document.getElementById("speech-ltx-prompt-prefix").value,
            }),
        }));
      }
      document.getElementById("manifest-info").textContent =
        `${file.name} · ${advancedRows.length} 行`;
      renderAdvancedRows();
    } catch (error) {
      advancedRows = [];
      document.getElementById("manifest-info").textContent = "清单读取失败";
      showErrors("advanced-errors", [{message: error.message}]);
      renderAdvancedRows();
    }
  });

  document.getElementById("quick-create-button").addEventListener("click", () => {
    const errors = quickValidationErrors();
    if (errors.length) {
      showErrors("quick-errors", errors);
      return;
    }
    submitBatch(buildQuickRows(), "quick", "quick-create-button", "quick-errors");
  });
  document.getElementById("quick-copy-preview-button").addEventListener(
    "click",
    () => openCopyPreview("quick"),
  );
  document.getElementById("advanced-create-button").addEventListener("click", () => {
    const errors = [];
    if (!advancedRows.length) errors.push({message: "请先导入任务清单"});
    if (!allAssetIds("advanced").length) errors.push({message: "请先上传清单引用的素材"});
    if (isSpeechMode() && !advancedConfirm.checked) {
      errors.push({message: "请先确认素材顺序与脚本表格行顺序一致"});
    }
    if (errors.length) {
      showErrors("advanced-errors", errors);
      return;
    }
    submitBatch(advancedRows, "advanced", "advanced-create-button", "advanced-errors");
  });
  document.getElementById("advanced-copy-preview-button").addEventListener(
    "click",
    () => openCopyPreview("advanced"),
  );
  document.getElementById("copy-preview-close-button").addEventListener(
    "click",
    () => document.getElementById("copy-preview-dialog").close(),
  );
  document.getElementById("copy-preview-dialog").addEventListener(
    "click",
    (event) => {
      if (event.target === event.currentTarget) event.currentTarget.close();
    },
  );

  workflowSelect.addEventListener("change", () => updateWorkflowUi(true));
  audioModeSelect.addEventListener("change", () => {
    requestKey = crypto.randomUUID();
    resetWorkflowData();
    updateWorkflowUi(false);
  });
  personModeSelect.addEventListener("change", updateDualAudioUi);
  resolutionInput.addEventListener("input", resetConfirmation);
  instanceTypeSelect.addEventListener("change", resetConfirmation);
  document.getElementById("quick-entry-button").addEventListener("click", () => setEntry("quick"));
  document.getElementById("manifest-entry-button").addEventListener("click", () => setEntry("manifest"));
  document.getElementById("speech-voice").addEventListener("change", updateSpeechCostText);
  document.getElementById("speech-ltx-prompt-prefix").addEventListener("input", resetConfirmation);
  document.querySelectorAll("#minimax-speech-settings input, #minimax-speech-settings select")
    .forEach((input) => input.addEventListener("change", resetConfirmation));

  workflowSelect.value = config.initialWorkflow;
  instanceTypeSelect.value = config.ltxDefaultInstance === "plus" ? "plus" : "default";
  updateWorkflowUi(false);
  renderQuick();
  renderAdvancedAssets();
})();
