(() => {
  "use strict";

  const config = window.batchGenerateConfig;
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const workflowSelect = document.getElementById("batch-workflow");
  const audioModeSelect = document.getElementById("batch-audio-mode");
  const batchNameInput = document.getElementById("batch-name");
  const personModeSelect = document.getElementById("batch-person-mode");
  const instanceTypeSelect = document.getElementById("batch-instance-type");
  const resolutionInput = document.getElementById("batch-resolution");
  const longAudioReviewRequired = document.getElementById(
    "batch-long-audio-review-required",
  );
  const videoReviewRequired = document.getElementById(
    "batch-video-review-required",
  );
  const quickConfirm = document.getElementById("quick-confirm");
  const advancedConfirm = document.getElementById("advanced-confirm");
  const voicePicker = document.getElementById("speech-voice-picker");
  const voiceValueInput = document.getElementById("speech-voice");
  const voiceSourceButtons = Array.from(
    document.querySelectorAll("[data-voice-source]"),
  );
  const customVoiceSelect = document.getElementById("speech-custom-voice");
  const systemVoiceCategory = document.getElementById("speech-system-category");
  const systemVoiceSelects = Array.from(
    document.querySelectorAll(".speech-system-voice"),
  );

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
    advancedLeftAudio: [],
    advancedRightAudio: [],
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
  // Automatic names follow the current first task until the user explicitly
  // edits the field. Reordering stays readable without replacing a chosen name.
  let batchNameWasEdited = false;

  const groupElements = {
    primary: document.getElementById("quick-primary-list"),
    audio: document.getElementById("quick-audio-list"),
    leftAudio: document.getElementById("quick-left-audio-list"),
    rightAudio: document.getElementById("quick-right-audio-list"),
  };

  const advancedColumns = {
    digital_human: [
      ["row_id", "任务编号"],
      ["prompt", "提示词"],
    ],
    ltx_lip_sync: [
      ["row_id", "任务编号"],
      ["speech_script", "口播脚本"],
    ],
  };

  function isSpeechMode() {
    return audioModeSelect.value === "minimax";
  }

  function identifierWithoutExtension(value) {
    return String(value || "").trim().replace(/\.[^./\\]+$/, "");
  }

  function formatAutoBatchName(identifier, count) {
    const suffix = count > 1 ? ` 等${count}条` : "";
    const availableLength = Math.max(1, 100 - Array.from(suffix).length);
    return `${Array.from(identifier).slice(0, availableLength).join("")}${suffix}`;
  }

  function autoBatchNameParts() {
    const advanced = activeEntry === "manifest";
    const primaryAssets = advanced
      ? assetGroups.advancedPrimary
      : assetGroups.primary;
    const audioAssets = advanced
      ? assetGroups.advancedAudio
      : assetGroups.audio;
    const rowCount = advanced
      ? Math.max(advancedRows.length, primaryAssets.length, audioAssets.length)
      : quickRowCount();

    if (!isSpeechMode() && audioAssets.length) {
      return {
        identifier: identifierWithoutExtension(audioAssets[0].originalName),
        count: rowCount,
      };
    }
    if (advanced && advancedRows.length) {
      const rowIdentifier = String(advancedRows[0].row_id || "").trim();
      if (rowIdentifier) {
        return {identifier: rowIdentifier, count: rowCount};
      }
    }
    if (primaryAssets.length) {
      return {
        identifier: identifierWithoutExtension(primaryAssets[0].originalName),
        count: rowCount,
      };
    }
    return {identifier: "", count: 0};
  }

  function syncAutoBatchName() {
    if (batchNameWasEdited) return;
    const {identifier, count} = autoBatchNameParts();
    batchNameInput.value = identifier
      ? formatAutoBatchName(identifier, count)
      : "";
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
      prompt_prefix: document.getElementById("speech-ltx-prompt-prefix").value,
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
      if (
        !isSpeechMode()
        && workflowSelect.value === "digital_human"
        && personModeSelect.value === "双人"
      ) {
        groups.push("advancedLeftAudio", "advancedRightAudio");
      }
    }
    return Array.from(new Set(
      groups.flatMap((group) => assetGroups[group].map((asset) => asset.assetId)),
    ));
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
    group.startsWith("advanced") ? renderAdvancedAssets() : renderQuick();
  }

  function reuseAsset(group, index, fillGroup = false) {
    const assets = assetGroups[group];
    const source = assets[index];
    if (!source) return;
    const targetCount = fillGroup
      ? (
        group.startsWith("advanced")
          ? advancedRows.length
          : quickRowCount()
      )
      : assets.length + 1;
    if (targetCount > config.maxBatchItems) {
      const errorId = group.startsWith("advanced") ? "advanced-errors" : "quick-errors";
      showErrors(errorId, [{message: `单批最多 ${config.maxBatchItems} 条任务`}]);
      return;
    }
    if (fillGroup) {
      while (assets.length < targetCount) {
        assets.push({...source, reused: true});
      }
    } else {
      assets.splice(index + 1, 0, {...source, reused: true});
    }
    resetConfirmation();
    group.startsWith("advanced") ? renderAdvancedAssets() : renderQuick();
  }

  function renderOrderedGroup(group) {
    const list = groupElements[group];
    const assets = assetGroups[group];
    const targetCount = quickRowCount();
    list.innerHTML = assets.length
      ? assets.map((asset, index) => `
        <li class="ordered-upload-item" draggable="true" data-group="${group}" data-index="${index}">
          <span class="order-number">${index + 1}</span>
          <span class="ordered-file-name" title="${escapeHtml(asset.originalName)}">
            ${escapeHtml(asset.originalName)}
            ${asset.reused ? '<small class="asset-reused-label">复用</small>' : ""}
          </span>
          <span class="order-actions">
            <button type="button" class="order-button" data-action="up" aria-label="上移">↑</button>
            <button type="button" class="order-button" data-action="down" aria-label="下移">↓</button>
            <button type="button" class="order-button reuse" data-action="reuse" title="不重新上传，复制一条素材引用">复用</button>
            ${
              targetCount > assets.length
                ? '<button type="button" class="order-button reuse" data-action="fill" title="用本素材补齐到当前任务数量">补齐</button>'
                : ""
            }
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

  function renderQuickPairing() {
    const digital = workflowSelect.value === "digital_human";
    const dual = digital && personModeSelect.value === "双人";
    const needsScript = isSpeechMode() || !digital;
    const headers = ["序号", digital ? "参考图片" : "源视频"];
    if (!isSpeechMode()) headers.push(digital ? "总参考音频" : "音频");
    if (dual) headers.push("左人物音频", "右人物音频");
    if (needsScript) headers.push("口播脚本");
    if (digital) headers.push("提示词");
    document.getElementById("quick-pairing-head").innerHTML =
      headers.map((label) => `<th>${label}</th>`).join("");

    const count = quickRowCount();
    while (quickPrompts.length < count) {
      quickPrompts.push(config.digitalDefaultPrompt);
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
        if (needsScript) {
          cells.push(`<td><textarea class="batch-script-input" data-index="${index}" rows="7"
            maxlength="4990" placeholder="填写音频中的完整口播内容，只需填写一次">${escapeHtml(quickScripts[index] || "")}</textarea></td>`);
        }
        if (digital) {
          cells.push(`<td><textarea class="batch-prompt-input" data-index="${index}" rows="5"
            maxlength="5000" placeholder="填写本条数字人任务的提示词">${escapeHtml(quickPrompts[index] || "")}</textarea></td>`);
        }
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
      if (
        (isSpeechMode() || workflowSelect.value === "ltx_lip_sync")
        && !String(quickScripts[index] || "").trim()
      ) {
        errors.push({rowNumber: index + 1, message: "口播脚本不能为空"});
      }
      if (
        workflowSelect.value === "digital_human"
        && !String(quickPrompts[index] || "").trim()
      ) {
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
            image_asset_id: primary.assetId,
            image_file: primary.originalName,
            audio_asset_id: isSpeechMode() ? "" : assetGroups.audio[index].assetId,
            audio_file: isSpeechMode() ? "" : assetGroups.audio[index].originalName,
            speech_script: isSpeechMode() ? quickScripts[index].trim() : "",
            prompt: quickPrompts[index].trim(),
            ...(dual
              ? {
                  left_audio_asset_id: assetGroups.leftAudio[index].assetId,
                  left_audio_file: assetGroups.leftAudio[index].originalName,
                  right_audio_asset_id: assetGroups.rightAudio[index].assetId,
                  right_audio_file: assetGroups.rightAudio[index].originalName,
                }
              : {}),
          }
        : {
            source_video_asset_id: primary.assetId,
            source_video_file: primary.originalName,
            audio_asset_id: isSpeechMode() ? "" : assetGroups.audio[index].assetId,
            audio_file: isSpeechMode() ? "" : assetGroups.audio[index].originalName,
            speech_script: quickScripts[index].trim(),
          }),
    }));
  }

  function buildAdvancedRows() {
    const digital = workflowSelect.value === "digital_human";
    const dual = digital && personModeSelect.value === "双人";
    return advancedRows.map((row, index) => ({
      ...row,
      ...(digital
        ? {
            image_asset_id: assetGroups.advancedPrimary[index]?.assetId || "",
            image_file:
              row.image_file
              || assetGroups.advancedPrimary[index]?.originalName
              || "",
            audio_asset_id: isSpeechMode()
              ? ""
              : assetGroups.advancedAudio[index]?.assetId || "",
            audio_file: isSpeechMode()
              ? ""
              : (
                row.audio_file
                || assetGroups.advancedAudio[index]?.originalName
                || ""
              ),
            ...(dual && !isSpeechMode()
              ? {
                  left_audio_asset_id:
                    assetGroups.advancedLeftAudio[index]?.assetId || "",
                  left_audio_file:
                    row.left_audio_file
                    || assetGroups.advancedLeftAudio[index]?.originalName
                    || "",
                  right_audio_asset_id:
                    assetGroups.advancedRightAudio[index]?.assetId || "",
                  right_audio_file:
                    row.right_audio_file
                    || assetGroups.advancedRightAudio[index]?.originalName
                    || "",
                }
              : {}),
          }
        : {
            source_video_asset_id:
              assetGroups.advancedPrimary[index]?.assetId || "",
            source_video_file:
              row.source_video_file
              || assetGroups.advancedPrimary[index]?.originalName
              || "",
            audio_asset_id: isSpeechMode()
              ? ""
              : assetGroups.advancedAudio[index]?.assetId || "",
            audio_file: isSpeechMode()
              ? ""
              : (
                row.audio_file
                || assetGroups.advancedAudio[index]?.originalName
                || ""
              ),
          }),
    }));
  }

  function advancedValidationErrors() {
    const errors = [];
    const count = advancedRows.length;
    if (!count) {
      errors.push({message: "请先导入任务清单"});
      return errors;
    }
    const expectedGroups = [
      {
        group: "advancedPrimary",
        label: workflowSelect.value === "digital_human" ? "参考图片" : "源视频",
      },
      ...(!isSpeechMode()
        ? [{
            group: "advancedAudio",
            label: workflowSelect.value === "digital_human" ? "总参考音频" : "音频",
          }]
        : []),
      ...(
        !isSpeechMode()
        && workflowSelect.value === "digital_human"
        && personModeSelect.value === "双人"
          ? [
              {group: "advancedLeftAudio", label: "左人物音频"},
              {group: "advancedRightAudio", label: "右人物音频"},
            ]
          : []
      ),
    ];
    expectedGroups.forEach(({group, label}) => {
      if (assetGroups[group].length !== count) {
        errors.push({
          message: `${label}需要上传 ${count} 个，当前为 ${assetGroups[group].length} 个`,
        });
      }
    });
    advancedRows.forEach((row, index) => {
      if (!String(row.row_id || "").trim()) {
        errors.push({rowNumber: index + 1, message: "任务编号不能为空"});
      }
      if (
        workflowSelect.value === "digital_human"
        && !isSpeechMode()
        && !String(row.prompt || "").trim()
      ) {
        errors.push({rowNumber: index + 1, message: "提示词不能为空"});
      }
      if (
        (isSpeechMode() || workflowSelect.value === "ltx_lip_sync")
        && !String(row.speech_script || "").trim()
      ) {
        errors.push({rowNumber: index + 1, message: "口播脚本不能为空"});
      }
    });
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
    if (!advancedConfirm.checked) {
      errors.push({message: "请先确认每类素材序号与表格行序号一致"});
    }
    return errors;
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
        : (
          isSpeechMode() || workflowSelect.value === "ltx_lip_sync"
            ? "请先上传画面素材并填写口播脚本。"
            : "请先上传画面素材和对应音频。"
        );
    syncAutoBatchName();
  }

  function renderAdvancedRows() {
    const columns = isSpeechMode()
      ? [
        ["row_id", "任务编号"],
        ["speech_script", "口播脚本"],
      ]
      : advancedColumns[workflowSelect.value];
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
    syncAutoBatchName();
  }

  function finalVideoPrompt(row) {
    if (workflowSelect.value === "digital_human") {
      return String(row.prompt || "").trim();
    }
    const prefix = String(
      row.prompt_prefix
      || document.getElementById("speech-ltx-prompt-prefix").value
    ).trim().replace(/[：:]$/, "");
    const script = String(row.speech_script || "").trim();
    return `${prefix}：“${script}”`;
  }

  function openCopyPreview(entry) {
    const rows = entry === "quick" ? buildQuickRows() : buildAdvancedRows();
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
        ${(isSpeechMode() || workflowSelect.value === "ltx_lip_sync")
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
    const digital = workflowSelect.value === "digital_human";
    const dual = digital && personModeSelect.value === "双人" && !isSpeechMode();
    const visibleGroups = [
      {
        group: "advancedPrimary",
        title: digital ? "参考图片" : "源视频",
      },
      ...(!isSpeechMode()
        ? [{
            group: "advancedAudio",
            title: digital ? "总参考音频" : "音频",
          }]
        : []),
      ...(dual
        ? [
            {group: "advancedLeftAudio", title: "左人物音频"},
            {group: "advancedRightAudio", title: "右人物音频"},
          ]
        : []),
    ];
    document.getElementById("advanced-asset-list").innerHTML = visibleGroups
      .map(({group, title}) => {
        const items = assetGroups[group].map((asset, index) =>
          `<span class="asset-chip"><strong>${index + 1}.</strong> ${escapeHtml(asset.originalName)}
            ${asset.reused ? '<small class="asset-reused-label">复用</small>' : ""}
            <button type="button" class="advanced-order-asset" data-action="up" data-group="${group}" data-index="${index}" aria-label="上移">↑</button>
            <button type="button" class="advanced-order-asset" data-action="down" data-group="${group}" data-index="${index}" aria-label="下移">↓</button>
            <button type="button" class="advanced-order-asset reuse" data-action="reuse" data-group="${group}" data-index="${index}" title="不重新上传，复制一条素材引用">复用</button>
            ${
              advancedRows.length > assetGroups[group].length
                ? `<button type="button" class="advanced-order-asset reuse" data-action="fill" data-group="${group}" data-index="${index}" title="用本素材补齐到任务行数">填满</button>`
                : ""
            }
            <button type="button" class="advanced-order-asset" data-action="remove" data-group="${group}" data-index="${index}" aria-label="移除">×</button>
          </span>`
        ).join("");
        return `<section class="advanced-asset-group">
          <strong>${title}</strong>
          <div class="compact-list">${items || '<span class="muted">尚未上传</span>'}</div>
        </section>`;
      }).join("");
    const assets = visibleGroups.flatMap(
      ({group}) => assetGroups[group],
    );
    document.getElementById("advanced-upload-progress").textContent = assets.length
      ? `已暂存 ${assets.length} 个文件；请核对每一组内的序号。`
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
    document.getElementById("advanced-audio-upload-title").textContent =
      digital ? "全部总参考音频" : "全部音频";
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
      : (
        isSpeechMode()
          ? "同一份口播脚本会同时用于生成语音和视频正向提示词，只需填写一次。"
          : "口播脚本会自动写入视频正向提示词；音频直接使用上传文件。"
      );
    updateAudioModeUi();
    updateDualAudioUi();
    if (resetData) resetWorkflowData();
  }

  function activeSystemVoiceSelect() {
    return systemVoiceSelects.find(
      (select) => select.dataset.categoryIndex === systemVoiceCategory?.value,
    ) || null;
  }

  function selectedVoiceControl() {
    return voicePicker?.dataset.activeSource === "custom"
      ? customVoiceSelect
      : activeSystemVoiceSelect();
  }

  function updateSpeechCostText() {
    const costText = document.getElementById("speech-cost-confirm-text");
    const method = voiceValueInput.dataset.method;
    const activated = voiceValueInput.dataset.activated === "true";
    if (!voiceValueInput.value) {
      costText.textContent = "请先选择音色；文本生成会按实际用量计费。";
    } else if (method === "system") {
      costText.textContent = "官方系统音色不收取克隆音色费；我已了解本批文本生成仍会按用量计费。";
    } else if (activated) {
      costText.textContent = "所选音色已完成首次正式使用；我已了解本批文本生成仍会计费。";
    } else {
      costText.textContent = "所选音色首次正式使用可能产生 ¥9.90 音色费用；我已了解文本生成仍会另外计费。";
    }
    resetConfirmation();
  }

  function syncVoiceSelection() {
    const control = selectedVoiceControl();
    const option = control?.selectedOptions[0];
    voiceValueInput.value = control?.value || "";
    voiceValueInput.dataset.method = option?.dataset.method || "";
    voiceValueInput.dataset.activated = option?.dataset.activated || "false";
    updateSpeechCostText();
  }

  function updateSystemVoiceCategory() {
    systemVoiceSelects.forEach((select) => {
      select.classList.toggle(
        "hidden",
        select.dataset.categoryIndex !== systemVoiceCategory?.value,
      );
    });
    if (voicePicker?.dataset.activeSource === "system") syncVoiceSelection();
  }

  function setVoiceSource(source) {
    const targetButton = voiceSourceButtons.find(
      (button) => button.dataset.voiceSource === source,
    );
    if (!targetButton || targetButton.disabled) return;
    voicePicker.dataset.activeSource = source;
    voiceSourceButtons.forEach((button) => {
      const active = button.dataset.voiceSource === source;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.getElementById("speech-custom-voice-panel").classList.toggle(
      "hidden",
      source !== "custom",
    );
    document.getElementById("speech-system-voice-panel").classList.toggle(
      "hidden",
      source !== "system",
    );
    syncVoiceSelection();
  }

  function initializeVoicePicker() {
    updateSystemVoiceCategory();
    setVoiceSource(voicePicker.dataset.defaultSource);
  }

  function updateAudioModeUi() {
    const speech = isSpeechMode();
    document.getElementById("long-audio-review-row").classList.toggle(
      "hidden",
      speech,
    );
    document.getElementById("minimax-speech-settings").classList.toggle("hidden", !speech);
    document.getElementById("quick-direct-audio-group").classList.toggle("hidden", speech);
    document.getElementById("advanced-direct-audio-group").classList.toggle("hidden", speech);
    document.getElementById("manifest-step-title").textContent = speech
      ? "1. 导入脚本清单"
      : "1. 导入高级任务清单";
    document.getElementById("manifest-entry-button").textContent = speech
      ? "Excel / CSV 脚本导入"
      : "Excel / CSV 高级导入";
    document.getElementById("manifest-step-help").textContent = speech
      ? "Excel 和 CSV 使用同一个两列模板：任务编号、口播脚本。"
      : (
        workflowSelect.value === "digital_human"
          ? "表格只有“任务编号、提示词”两列；图片和音频按页面序号对应。"
          : "表格只有“任务编号、口播脚本”两列；视频和音频按页面序号对应。"
      );
    document.getElementById("advanced-upload-help").textContent =
      "每类素材序号与表格行序号一致；同一素材可“复用”或“填满”到多行。";
    document.getElementById("quick-upload-help").textContent = speech
      ? "按最终任务顺序准备图片或视频；同一画面可点击“复用”，无需重复上传。"
      : (
        workflowSelect.value === "digital_human"
          ? "先上传图片和音频；一张图片对应多条音频时，点击图片旁的“复用”或“补齐”。"
          : "先上传视频和音频；一段视频对应多条音频时，点击视频旁的“复用”或“补齐”。"
      );
    document.getElementById("quick-order-guidance").textContent = speech
      ? "系统按照页面序号创建任务；同一画面可复用，上传后可调整顺序。"
      : "系统按照页面序号逐一配对；同一素材可复用，文件名相同也不会混淆。";
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
    document.getElementById("advanced-left-audio-group").classList.toggle("hidden", !show);
    document.getElementById("advanced-right-audio-group").classList.toggle("hidden", !show);
    resetConfirmation();
    renderQuick();
    renderAdvancedAssets();
  }

  function setEntry(entry) {
    activeEntry = entry;
    document.getElementById("quick-entry-panel").classList.toggle("hidden", entry !== "quick");
    document.getElementById("manifest-entry-panel").classList.toggle("hidden", entry !== "manifest");
    document.getElementById("quick-entry-button").classList.toggle("active", entry === "quick");
    document.getElementById("manifest-entry-button").classList.toggle("active", entry === "manifest");
    syncAutoBatchName();
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
      longAudioReviewRequired:
        !isSpeechMode() && longAudioReviewRequired.checked,
      videoReviewRequired: videoReviewRequired.checked,
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
  bindUpload(
    "advanced-left-audio-files",
    "audio",
    "advancedLeftAudio",
    "advanced-upload-progress",
    "advanced-errors",
  );
  bindUpload(
    "advanced-right-audio-files",
    "audio",
    "advancedRightAudio",
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
      if (button.dataset.action === "reuse" || button.dataset.action === "fill") {
        reuseAsset(
          item.dataset.group,
          fromIndex,
          button.dataset.action === "fill",
        );
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
    } else if (button.dataset.action === "reuse" || button.dataset.action === "fill") {
      reuseAsset(group, index, button.dataset.action === "fill");
      return;
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
    syncAutoBatchName();
    resetConfirmation();
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
    const errors = advancedValidationErrors();
    if (errors.length) {
      showErrors("advanced-errors", errors);
      return;
    }
    submitBatch(
      buildAdvancedRows(),
      "advanced",
      "advanced-create-button",
      "advanced-errors",
    );
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
  longAudioReviewRequired.addEventListener("change", resetConfirmation);
  videoReviewRequired.addEventListener("change", resetConfirmation);
  personModeSelect.addEventListener("change", updateDualAudioUi);
  resolutionInput.addEventListener("input", resetConfirmation);
  instanceTypeSelect.addEventListener("change", resetConfirmation);
  batchNameInput.addEventListener("input", () => {
    batchNameWasEdited = true;
    resetConfirmation();
  });
  document.getElementById("quick-entry-button").addEventListener("click", () => setEntry("quick"));
  document.getElementById("manifest-entry-button").addEventListener("click", () => setEntry("manifest"));
  voiceSourceButtons.forEach((button) => {
    button.addEventListener("click", () => setVoiceSource(button.dataset.voiceSource));
  });
  customVoiceSelect?.addEventListener("change", syncVoiceSelection);
  systemVoiceCategory?.addEventListener("change", updateSystemVoiceCategory);
  systemVoiceSelects.forEach((select) => {
    select.addEventListener("change", syncVoiceSelection);
  });
  document.getElementById("speech-ltx-prompt-prefix").addEventListener("input", resetConfirmation);
  document.querySelectorAll("#minimax-speech-settings input, #minimax-speech-settings select")
    .forEach((input) => input.addEventListener("change", resetConfirmation));

  workflowSelect.value = config.initialWorkflow;
  instanceTypeSelect.value = config.ltxDefaultInstance === "plus" ? "plus" : "default";
  initializeVoicePicker();
  updateWorkflowUi(false);
  renderQuick();
  renderAdvancedAssets();
})();
