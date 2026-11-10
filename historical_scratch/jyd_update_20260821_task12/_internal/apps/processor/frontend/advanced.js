const state = {
  templates: [],
  drafts: [],
  textStyles: [],
  textEffects: [],
  textTemplates: [],
  effects: [],
  audioLibrary: { categories: [], assets: [] },
  selectedAudioIdentities: new Set(),
  selectedEffectPaths: new Set(),
  selectedTextEffectPaths: new Set(),
  selectedTextTemplatePaths: new Set(),
  textTemplateConfigs: new Map(),
  activeTextTemplatePath: "",
  pollTimer: null,
  activeJobId: null,
  activeBatchId: null,
  captionCues: [],
  captionPreviewTimer: null,
  captionPreviewRequest: 0,
  videoObjectUrl: "",
  audioObjectUrl: "",
  loadedCaptionFont: "",
};

const $ = (id) => document.getElementById(id);

function setLog(value) {
  $("logBox").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    const message = data && data.detail ? JSON.stringify(data.detail, null, 2) : text;
    throw new Error(message || `HTTP ${response.status}`);
  }
  return data;
}

async function checkHealth() {
  try {
    const data = await apiFetch("/api/health");
    $("apiStatus").textContent = "后端已连接";
    $("apiStatus").className = "status ok";
    return data;
  } catch (error) {
    $("apiStatus").textContent = "后端连接失败";
    $("apiStatus").className = "status bad";
    setLog(error.message);
    return null;
  }
}

async function loadTemplates() {
  try {
    state.templates = await apiFetch("/api/templates");
    fillTemplateSelectForSourceMode();
  } catch (error) {
    state.templates = [];
    const select = $("templateSelect");
    select.innerHTML = "";
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "模板加载失败";
    select.appendChild(option);
    updateTemplateUsageSummary();
    setLog(error.message);
  }
}

function isImportedMother(template) {
  return template?.import_info?.source === "local_collector";
}

function templatesForSourceMode() {
  const mode = $("sourceMode").value;
  if (mode === "mother") return state.templates.filter(isImportedMother);
  if (mode === "template-replace") return state.templates.filter((item) => !isImportedMother(item));
  return [];
}

function fillTemplateSelectForSourceMode() {
  const select = $("templateSelect");
  const previous = select.value;
  const items = templatesForSourceMode();
  select.innerHTML = "";
  if (!items.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = $("sourceMode").value === "mother" ? "还没有从本地采集器上传母版" : "旧模板库为空";
    select.appendChild(option);
  } else {
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.template_id;
      option.textContent = item.name || item.template_id;
      select.appendChild(option);
    }
    if (items.some((item) => item.template_id === previous)) select.value = previous;
  }
  updateTemplateUsageSummary();
}

function draftSummaryText(draft) {
  const summary = draft.summary || {};
  const textCount = summary.text_count || 0;
  const audioCount = summary.audio_count || 0;
  const effectCount = summary.effect_count || 0;
  const nestedCount = summary.nested_draft_count || 0;
  const decrypt = draft.needs_decrypt ? "需解密" : "明文";
  return `${draft.name} | ${decrypt} | 文字 ${textCount} | 音频 ${audioCount} | 特效 ${effectCount} | 嵌套 ${nestedCount}`;
}

function fillDraftSelect(items) {
  const select = $("draftSelect");
  select.innerHTML = "";
  if (!items.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "没有找到剪映草稿";
    select.appendChild(option);
    return;
  }
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.path;
    option.textContent = draftSummaryText(item);
    select.appendChild(option);
  }
}

async function scanDrafts() {
  const root = $("draftScanRoot").value.trim();
  const url = root ? `/api/drafts?root=${encodeURIComponent(root)}` : "/api/drafts";
  setLog("正在扫描服务器剪映草稿...");
  const data = await apiFetch(url);
  state.drafts = data.drafts || [];
  fillDraftSelect(state.drafts);
  setLog({
    status: "drafts_loaded",
    root: data.root,
    count: state.drafts.length,
    drafts: state.drafts,
  });
}

async function importSelectedDraft() {
  const sourceDraftDir = $("draftSelect").value;
  if (!sourceDraftDir) throw new Error("请先扫描并选择一个剪映草稿");

  $("importDraftBtn").disabled = true;
  setLog("正在导入模板，必要时会自动解密...");
  try {
    const payload = {
      source_draft_dir: sourceDraftDir,
      template_id: $("templateImportId").value.trim(),
      name: $("templateImportName").value.trim(),
      replace: $("templateImportReplace").checked,
    };
    const result = await apiFetch("/api/templates/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("sourceMode").value = "template-replace";
    await loadTemplates();
    $("templateSelect").value = result.template_id;
    updateSourceMode();
    setLog({
      status: "template_imported",
      template: result,
    });
  } finally {
    $("importDraftBtn").disabled = false;
  }
}

function fillLibrarySelect(select, items, emptyLabel) {
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = emptyLabel;
  select.appendChild(empty);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.path;
    option.textContent = item.effect_name ? `${item.name} - ${item.effect_name}` : item.name;
    if (item.error) option.textContent = `${item.name}（读取失败）`;
    select.appendChild(option);
  }
}

async function loadLibraries() {
  try {
    state.textStyles = await apiFetch("/api/assets/text-styles");
    fillLibrarySelect($("textStyleSelect"), state.textStyles, "不套用样式");
    if (state.textStyles.length) {
      $("textStyleSelect").value = state.textStyles[0].path;
      applySelectedTextStyle();
    }
  } catch (error) {
    fillLibrarySelect($("textStyleSelect"), [], "文字样式加载失败");
  }

  try {
    state.textEffects = await apiFetch("/api/assets/text-effects");
    const validTextEffectPaths = new Set(
      state.textEffects.filter((item) => !item.error).map((item) => item.path),
    );
    state.selectedTextEffectPaths = new Set(
      [...state.selectedTextEffectPaths].filter((path) => validTextEffectPaths.has(path)),
    );
    fillLibrarySelect($("textEffectSelect"), state.textEffects, "不使用花字");
    renderBatchTextEffectList();
  } catch (error) {
    state.textEffects = [];
    state.selectedTextEffectPaths.clear();
    fillLibrarySelect($("textEffectSelect"), [], "花字库加载失败");
    renderBatchTextEffectList();
  }

  try {
    state.textTemplates = await apiFetch("/api/assets/text-templates");
    const validTextTemplatePaths = new Set(
      state.textTemplates.filter((item) => !item.error).map((item) => item.path),
    );
    state.selectedTextTemplatePaths = new Set(
      [...state.selectedTextTemplatePaths].filter((path) => validTextTemplatePaths.has(path)),
    );
    fillLibrarySelect($("textTemplateSelect"), state.textTemplates, "不添加复合文字模板");
    renderTextTemplateSlots();
    renderBatchTextTemplateList();
  } catch (error) {
    state.textTemplates = [];
    state.selectedTextTemplatePaths.clear();
    fillLibrarySelect($("textTemplateSelect"), [], "复合文字模板库加载失败");
    renderTextTemplateSlots();
    renderBatchTextTemplateList();
  }

  try {
    state.effects = await apiFetch("/api/assets/effects");
    const validEffectPaths = new Set(state.effects.filter((item) => !item.error).map((item) => item.path));
    state.selectedEffectPaths = new Set(
      [...state.selectedEffectPaths].filter((path) => validEffectPaths.has(path)),
    );
    fillLibrarySelect($("effectSelect"), state.effects, "不添加特效");
    renderBatchEffectList();
  } catch (error) {
    state.effects = [];
    fillLibrarySelect($("effectSelect"), [], "特效加载失败");
    renderBatchEffectList();
  }
}

function formatAudioDuration(durationUs) {
  const seconds = Math.max(0, Number(durationUs || 0) / 1000000);
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.floor(seconds % 60);
  return `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function audioAssetsForCategory(categoryId) {
  return state.audioLibrary.assets.filter(
    (asset) => asset.available && (asset.category_ids || []).includes(categoryId),
  );
}

function fillAudioCategorySelect() {
  const select = $("audioCategorySelect");
  const previous = select.value;
  select.innerHTML = "";
  for (const category of state.audioLibrary.categories) {
    const option = document.createElement("option");
    option.value = category.id;
    option.textContent = `${category.name}（${category.available_count}/${category.asset_count}）`;
    select.appendChild(option);
  }
  if (previous && state.audioLibrary.categories.some((item) => item.id === previous)) {
    select.value = previous;
  }
}

function fillBatchAudioCategorySelect() {
  const select = $("batchAudioCategorySelect");
  const previous = select.value;
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = `全部音乐（${state.audioLibrary.assets.filter((item) => item.available).length}）`;
  select.appendChild(all);
  for (const category of state.audioLibrary.categories) {
    const option = document.createElement("option");
    option.value = category.id;
    option.textContent = `${category.name}（${category.available_count}）`;
    select.appendChild(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

function visibleBatchAudioAssets() {
  const categoryId = $("batchAudioCategorySelect").value;
  return state.audioLibrary.assets.filter(
    (asset) => asset.available && (!categoryId || (asset.category_ids || []).includes(categoryId)),
  );
}

function dimensionMode(selectId) {
  return $(selectId).value;
}

function normalizeFixedSelection(selection, selectId) {
  if (dimensionMode(selectId) !== "fixed" || selection.size <= 1) return;
  const first = selection.values().next().value;
  selection.clear();
  if (first) selection.add(first);
}

function selectForDimension(selection, values, selectId) {
  const available = [...values];
  if (dimensionMode(selectId) === "fixed") {
    selection.clear();
    if (available.length) selection.add(available[0]);
    return;
  }
  for (const value of available) selection.add(value);
}

function updateDimensionPickerState(selectId) {
  const picker = $(selectId).closest(".asset-picker");
  if (picker) picker.classList.toggle("dimension-disabled", dimensionMode(selectId) === "disabled");
}

function batchCombinationState() {
  const dimensions = [
    {
      key: "bgm",
      label: "BGM",
      mode: dimensionMode("batchAudioDimensionMode"),
      count: state.selectedAudioIdentities.size,
    },
    {
      key: "video_effect",
      label: "视频特效",
      mode: dimensionMode("batchEffectDimensionMode"),
      count: state.selectedEffectPaths.size,
    },
    {
      key: "text_effect",
      label: "新增文字花字",
      mode: dimensionMode("batchTextEffectDimensionMode"),
      count: state.selectedTextEffectPaths.size,
    },
    {
      key: "text_template",
      label: "复合文字模板",
      mode: dimensionMode("batchTextTemplateDimensionMode"),
      count: state.selectedTextTemplatePaths.size,
    },
  ];
  const errors = [];
  let rawTotal = 1;
  const productCounts = [];
  for (const dimension of dimensions) {
    if (dimension.mode === "disabled") continue;
    if (dimension.mode === "fixed" && dimension.count !== 1) {
      errors.push(`${dimension.label}设为固定时必须选择 1 项`);
    }
    if (dimension.mode === "product" && dimension.count < 1) {
      errors.push(`${dimension.label}参与组合时至少选择 1 项`);
    }
    if (dimension.mode === "product") {
      rawTotal *= Math.max(1, dimension.count);
      productCounts.push(dimension.count);
    }
  }
  const textEffectDimension = dimensions.find((item) => item.key === "text_effect");
  if (
    textEffectDimension.mode !== "disabled" &&
    ($("textMode").value !== "add" || !$("textValue").value.trim())
  ) {
    errors.push("花字参与批量任务前，需要在上方选择“新增文字”并填写文字内容");
  }
  const total = rawTotal;
  const removed = 0;
  const coreChangeCount = dimensions.filter(
    (dimension) => ["bgm", "video_effect"].includes(dimension.key) && dimension.mode !== "disabled",
  ).length;
  if (coreChangeCount < 2) {
    errors.push("每个结果必须相对原视频至少改变两个核心元素；当前后台可计入的是 BGM 和视频特效");
  }
  const maxJobs = Math.max(1, Math.min(1000, Number($("batchMaxJobs").value || 500)));
  if (total > maxJobs) errors.push(`完整组合会生成 ${total} 个任务，超过当前上限 ${maxJobs}`);
  return { dimensions, rawTotal, total, removed, coreChangeCount, maxJobs, errors, valid: errors.length === 0 };
}

function updateCombinationSummary() {
  $("batchAudioCount").textContent = `已选 ${state.selectedAudioIdentities.size} 首`;
  $("batchEffectCount").textContent = `已选 ${state.selectedEffectPaths.size} 个`;
  $("batchTextEffectCount").textContent = `已选 ${state.selectedTextEffectPaths.size} 个`;
  $("batchTextTemplateCount").textContent = `已选 ${state.selectedTextTemplatePaths.size} 个`;
  [
    "batchAudioDimensionMode",
    "batchEffectDimensionMode",
    "batchTextEffectDimensionMode",
    "batchTextTemplateDimensionMode",
  ].forEach(updateDimensionPickerState);

  const combination = batchCombinationState();
  const active = combination.dimensions.filter((item) => item.mode !== "disabled");
  const description = active.length
    ? active
        .map((item) => `${item.label}${item.mode === "fixed" ? "固定" : "组合"} ${item.count} 项`)
        .join("，")
    : "所有可变素材均不使用";
  const summary = $("batchCombinationSummary");
  if (combination.valid) {
    summary.textContent = `${description}；每个结果相对原视频改变 ${combination.coreChangeCount} 个核心元素，花字和复合文字模板不计入；将生成 ${combination.total} 个独立视频任务。`;
    summary.classList.remove("invalid");
  } else {
    summary.textContent = `${description}；${combination.errors.join("；")}。`;
    summary.classList.add("invalid");
  }
  if ($("combinationMode").value === "batch" && !state.activeBatchId) {
    $("renderBtn").disabled = !combination.valid;
    $("renderBtn").textContent = combination.valid
      ? `创建 ${combination.total} 个任务`
      : "请检查组合设置";
  }
}

function renderBatchAudioList() {
  const container = $("batchAudioList");
  container.innerHTML = "";
  const assets = visibleBatchAudioAssets();
  if (!assets.length) {
    const empty = document.createElement("div");
    empty.className = "asset-list-empty";
    empty.textContent = "当前分类没有可用音乐。";
    container.appendChild(empty);
    updateCombinationSummary();
    return;
  }

  for (const asset of assets) {
    const row = document.createElement("label");
    row.className = "asset-check-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedAudioIdentities.has(asset.identity);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        if (dimensionMode("batchAudioDimensionMode") === "fixed") {
          state.selectedAudioIdentities.clear();
        }
        state.selectedAudioIdentities.add(asset.identity);
      } else state.selectedAudioIdentities.delete(asset.identity);
      if (dimensionMode("batchAudioDimensionMode") === "fixed") renderBatchAudioList();
      else updateCombinationSummary();
    });

    const name = document.createElement("span");
    name.className = "asset-check-name";
    const strong = document.createElement("strong");
    strong.textContent = asset.name || asset.identity;
    const detail = document.createElement("small");
    detail.textContent = `${formatAudioDuration(asset.duration_us)} · ${asset.music_id || asset.identity}`;
    name.append(strong, detail);

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "asset-preview-button";
    preview.textContent = "试听";
    preview.addEventListener("click", (event) => {
      event.preventDefault();
      const player = $("batchAudioPreview");
      player.src = `/api/audio-library/file?identity=${encodeURIComponent(asset.identity)}`;
      player.load();
      player.play().catch(() => {});
    });
    row.append(checkbox, name, preview);
    container.appendChild(row);
  }
  updateCombinationSummary();
}

function renderBatchEffectList() {
  const container = $("batchEffectList");
  container.innerHTML = "";
  if (!state.effects.length) {
    const empty = document.createElement("div");
    empty.className = "asset-list-empty";
    empty.textContent = "特效库为空。";
    container.appendChild(empty);
    updateCombinationSummary();
    return;
  }
  for (const effect of state.effects) {
    const row = document.createElement("label");
    row.className = "asset-check-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedEffectPaths.has(effect.path);
    checkbox.disabled = Boolean(effect.error);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        if (dimensionMode("batchEffectDimensionMode") === "fixed") {
          state.selectedEffectPaths.clear();
        }
        state.selectedEffectPaths.add(effect.path);
      } else state.selectedEffectPaths.delete(effect.path);
      if (dimensionMode("batchEffectDimensionMode") === "fixed") renderBatchEffectList();
      else updateCombinationSummary();
    });
    const name = document.createElement("span");
    name.className = "asset-check-name";
    const strong = document.createElement("strong");
    strong.textContent = effect.effect_name || effect.name;
    const detail = document.createElement("small");
    detail.textContent = effect.error ? "读取失败" : effect.name;
    name.append(strong, detail);
    row.append(checkbox, name);
    container.appendChild(row);
  }
  updateCombinationSummary();
}

function renderBatchTextEffectList() {
  const container = $("batchTextEffectList");
  container.innerHTML = "";
  const effects = state.textEffects.filter((item) => !item.error);
  if (!effects.length) {
    const empty = document.createElement("div");
    empty.className = "asset-list-empty";
    empty.textContent = "花字库为空。";
    container.appendChild(empty);
    updateCombinationSummary();
    return;
  }
  for (const effect of effects) {
    const row = document.createElement("label");
    row.className = "asset-check-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedTextEffectPaths.has(effect.path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        if (dimensionMode("batchTextEffectDimensionMode") === "fixed") {
          state.selectedTextEffectPaths.clear();
        }
        state.selectedTextEffectPaths.add(effect.path);
      } else state.selectedTextEffectPaths.delete(effect.path);
      if (dimensionMode("batchTextEffectDimensionMode") === "fixed") {
        renderBatchTextEffectList();
      } else updateCombinationSummary();
    });
    const name = document.createElement("span");
    name.className = "asset-check-name";
    const strong = document.createElement("strong");
    strong.textContent = effect.name || "未命名花字";
    const detail = document.createElement("small");
    detail.textContent = effect.identity || effect.path;
    name.append(strong, detail);
    row.append(checkbox, name);
    container.appendChild(row);
  }
  updateCombinationSummary();
}

function renderBatchTextTemplateList() {
  const container = $("batchTextTemplateList");
  container.innerHTML = "";
  const templates = state.textTemplates.filter((item) => !item.error);
  if (!templates.length) {
    const empty = document.createElement("div");
    empty.className = "asset-list-empty";
    empty.textContent = "复合文字模板库为空。";
    container.appendChild(empty);
    updateCombinationSummary();
    return;
  }
  for (const template of templates) {
    const row = document.createElement("label");
    row.className = "asset-check-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedTextTemplatePaths.has(template.path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        if (dimensionMode("batchTextTemplateDimensionMode") === "fixed") {
          state.selectedTextTemplatePaths.clear();
        }
        state.selectedTextTemplatePaths.add(template.path);
      } else state.selectedTextTemplatePaths.delete(template.path);
      if (dimensionMode("batchTextTemplateDimensionMode") === "fixed") {
        renderBatchTextTemplateList();
      } else updateCombinationSummary();
    });
    const name = document.createElement("span");
    name.className = "asset-check-name";
    const strong = document.createElement("strong");
    strong.textContent = template.name || "未命名复合模板";
    const detail = document.createElement("small");
    detail.textContent = `${(template.text_slots || []).length} 个文字槽`;
    name.append(strong, detail);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "asset-preview-button";
    edit.textContent = "编辑文字";
    edit.addEventListener("click", (event) => {
      event.preventDefault();
      saveActiveTextTemplateConfig();
      $("textTemplateSelect").value = template.path;
      renderTextTemplateSlots();
      document.querySelector(".text-template-editor")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
    row.append(checkbox, name, edit);
    container.appendChild(row);
  }
  updateCombinationSummary();
}

function updateCombinationMode() {
  const batch = $("combinationMode").value === "batch";
  $("batchCombinationEditor").classList.toggle("hidden", !batch);
  $("singleCombinationEditor").classList.toggle("hidden", batch);
  $("batchResultList").classList.add("hidden");
  if (batch) updateCombinationSummary();
  else {
    $("renderBtn").disabled = false;
    $("renderBtn").textContent = "开始生成";
  }
}

function selectedAudioAsset() {
  const identity = $("audioLibrarySelect").value;
  return state.audioLibrary.assets.find((asset) => asset.identity === identity) || null;
}

function updateAudioPreview() {
  const mode = $("audioMode").value;
  const preview = $("audioPreview");
  if (mode === "upload") {
    const file = $("audioFile").files[0];
    if (state.audioObjectUrl) URL.revokeObjectURL(state.audioObjectUrl);
    state.audioObjectUrl = file ? URL.createObjectURL(file) : "";
    if (state.audioObjectUrl) preview.src = state.audioObjectUrl;
    else preview.removeAttribute("src");
    preview.load();
    return;
  }
  if (!mode.startsWith("library")) {
    preview.removeAttribute("src");
    preview.load();
    return;
  }
  const asset = selectedAudioAsset();
  if (!asset) {
    preview.removeAttribute("src");
    preview.load();
    return;
  }
  preview.src = `/api/audio-library/file?identity=${encodeURIComponent(asset.identity)}`;
  preview.load();
}

function updateAudioAssetSelect() {
  const mode = $("audioMode").value;
  const categoryId = $("audioCategorySelect").value;
  const select = $("audioLibrarySelect");
  const assets = audioAssetsForCategory(categoryId);
  const previous = select.value;
  select.innerHTML = "";

  for (const asset of assets) {
    const option = document.createElement("option");
    option.value = asset.identity;
    option.textContent = `${asset.name || asset.identity} · ${formatAudioDuration(asset.duration_us)}`;
    select.appendChild(option);
  }
  if (previous && assets.some((item) => item.identity === previous)) select.value = previous;

  if (mode === "library-next" && assets.length) {
    const category = state.audioLibrary.categories.find((item) => item.id === categoryId);
    const nextIndex = Number(category?.next_index || 0) % assets.length;
    select.value = assets[nextIndex].identity;
  }
  select.disabled = mode !== "library-specific";

  const asset = selectedAudioAsset();
  if (!assets.length) {
    $("audioSelectionHint").textContent = "当前分类没有可用音乐，请先在下方完成分类。";
  } else if (mode === "library-next") {
    $("audioSelectionHint").textContent = `本次预计选择：${asset?.name || ""}。提交任务时会原子地推进到下一首。`;
  } else if (mode === "library-specific") {
    $("audioSelectionHint").textContent = `指定音乐：${asset?.name || "请选择"}。`;
  }
  updateAudioPreview();
}

function updateAudioMode() {
  const mode = $("audioMode").value;
  const libraryMode = mode.startsWith("library");
  $("audioCategoryField").classList.toggle("hidden", !libraryMode);
  $("audioAssetField").classList.toggle("hidden", !libraryMode);
  $("audioUploadField").classList.toggle("hidden", mode !== "upload");
  if (mode === "none") {
    $("audioSelectionHint").textContent = "本次不添加 BGM。";
  } else if (mode === "upload") {
    $("audioSelectionHint").textContent = "本次使用临时上传的音乐，不影响音乐库顺序。";
  }
  updateAudioAssetSelect();
}

function renderAudioCatalogManager() {
  const container = $("audioCatalogManager");
  container.innerHTML = "";
  if (!state.audioLibrary.assets.length) {
    container.textContent = "音乐库为空，请先运行 export_audio_library.py。";
    return;
  }

  for (const asset of state.audioLibrary.assets) {
    const row = document.createElement("div");
    row.className = "audio-catalog-row";

    const name = document.createElement("div");
    name.className = "audio-catalog-name";
    const strong = document.createElement("strong");
    strong.textContent = asset.name || asset.identity;
    const detail = document.createElement("small");
    detail.textContent = `${formatAudioDuration(asset.duration_us)} · ${asset.music_id || asset.identity}`;
    name.append(strong, detail);

    const category = document.createElement("select");
    for (const item of state.audioLibrary.categories) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name;
      category.appendChild(option);
    }
    category.value = (asset.category_ids || ["unclassified"])[0];
    category.addEventListener("change", async () => {
      category.disabled = true;
      try {
        const categoryIds = category.value === "unclassified" ? [] : [category.value];
        await apiFetch("/api/audio-library/assign", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ identity: asset.identity, category_ids: categoryIds }),
        });
        await loadAudioLibrary();
      } catch (error) {
        setLog(error.message);
      } finally {
        category.disabled = false;
      }
    });

    const status = document.createElement("span");
    status.className = "audio-catalog-status";
    status.textContent = asset.available ? "可用" : "文件缺失";
    row.append(name, category, status);
    container.appendChild(row);
  }
}

async function loadAudioLibrary() {
  try {
    state.audioLibrary = await apiFetch("/api/audio-library");
    const validIdentities = new Set(
      state.audioLibrary.assets.filter((item) => item.available).map((item) => item.identity),
    );
    state.selectedAudioIdentities = new Set(
      [...state.selectedAudioIdentities].filter((identity) => validIdentities.has(identity)),
    );
    fillAudioCategorySelect();
    fillBatchAudioCategorySelect();
    renderBatchAudioList();
    renderAudioCatalogManager();
    updateAudioMode();
  } catch (error) {
    state.audioLibrary = { categories: [], assets: [] };
    $("audioSelectionHint").textContent = `音乐库加载失败：${error.message}`;
  }
}

async function createAudioCategory() {
  const name = $("audioCategoryName").value.trim();
  if (!name) throw new Error("请输入音乐分类名称");
  await apiFetch("/api/audio-library/categories", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  $("audioCategoryName").value = "";
  await loadAudioLibrary();
}

async function uploadFile(kind, file) {
  const filename = encodeURIComponent(file.name || `upload_${kind}`);
  return apiFetch(`/api/media/${kind}?filename=${filename}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/octet-stream",
    },
    body: file,
  });
}

function currentDraftRoot() {
  return $("draftRoot").value.trim();
}

function selectedTemplate() {
  const templateId = $("templateSelect").value;
  return state.templates.find((item) => item.template_id === templateId) || null;
}

function templateVideoTargetKind() {
  const summary = selectedTemplate()?.summary || {};
  return Number(summary.nested_draft_count || 0) > 0 ? "nested-video" : "video-segment";
}

function updateTemplateUsageSummary() {
  const template = selectedTemplate();
  const mode = $("sourceMode").value;
  if (!template) {
    $("templateUsageSummary").textContent = mode === "mother"
      ? "请先在本地草稿采集器中分析草稿并上传到网站。"
      : "模板库为空，请先在下方开发工具中导入剪映草稿。";
    $("sourceSummary").textContent = $("templateUsageSummary").textContent;
    return;
  }
  const summary = template.summary || {};
  if (mode === "mother") {
    const policies = template.import_info?.policies || {};
    const policyLabels = {
      keep: "保留",
      replace: "生成时替换",
      remove: "移除",
    };
    const duration = formatTime(Number(template.duration_us || 0) / 1000000);
    $("templateUsageSummary").textContent = `${template.name}：${duration}，${template.track_count || 0} 条轨道，原视频保持不变。`;
    $("sourceSummary").textContent = `母版策略：BGM/音效 ${policyLabels[policies.audio] || "保留"}；视频特效 ${policyLabels[policies.video_effects] || "保留"}；字幕样式 ${policyLabels[policies.text_style] || "保留"}；花字 ${policyLabels[policies.text_effects] || "保留"}；复合文字 ${policyLabels[policies.text_templates] || "保留"}。`;
    const textMode = $("textMode");
    if (policies.text_style === "replace" && textMode.value === "preserve") {
      textMode.value = "restyle";
      updateTextMode();
    }
    return;
  }
  const nestedCount = Number(summary.nested_draft_count || 0);
  const targetLabel = nestedCount > 0 ? "第一个嵌套视频槽" : "第一个普通视频片段";
  const structureLabel = nestedCount > 0 ? `已识别为嵌套模板（嵌套草稿 ${nestedCount}）` : "已识别为普通草稿模板";
  $("templateUsageSummary").textContent = `${template.name || template.template_id}：${structureLabel}，上传的 MP4 将替换${targetLabel}。`;
  $("sourceSummary").textContent = "模板保留原时间线、特效和文字，只替换第一个视频槽。";
}

function updateSourceMode() {
  const mode = $("sourceMode").value;
  const usesTemplate = mode !== "video";
  const usesVideoUpload = mode !== "mother";
  $("templateUsage").classList.toggle("hidden", !usesTemplate);
  $("videoSourceField").classList.toggle("hidden", !usesVideoUpload);
  $("templateSelectLabel").textContent = mode === "mother" ? "选择已导入母版" : "选择旧模板";
  fillTemplateSelectForSourceMode();
  const replaceOption = $("textMode").querySelector('option[value="replace"]');
  const preserveOption = $("textMode").querySelector('option[value="preserve"]');
  const restyleOption = $("textMode").querySelector('option[value="restyle"]');
  replaceOption.disabled = mode === "video";
  preserveOption.disabled = mode !== "mother";
  restyleOption.disabled = mode !== "mother";
  if (mode === "mother" && !["preserve", "restyle"].includes($("textMode").value)) {
    $("textMode").value = selectedTemplate()?.import_info?.policies?.text_style === "replace"
      ? "restyle"
      : "preserve";
  } else if (mode !== "mother" && ["preserve", "restyle"].includes($("textMode").value)) {
    $("textMode").value = "captions";
  }
  if (mode === "video") {
    $("sourceSummary").textContent = "上传一个 MP4 后，系统会先创建基础剪映草稿。";
  }
  loadVideoPreview();
  updateTextMode();
}

function selectedTextStyle() {
  const path = $("textStyleSelect").value;
  return state.textStyles.find((item) => item.path === path) || null;
}

async function loadCaptionPreviewFont(style) {
  if (!style?.preview?.font_path || !window.FontFace) {
    $("videoPreviewFrame").style.setProperty("--caption-font-family", '"Microsoft YaHei", sans-serif');
    return;
  }
  if (state.loadedCaptionFont === style.name) return;
  try {
    const family = `CaptionPreview_${style.name.replace(/[^A-Za-z0-9_\u4e00-\u9fff]/g, "_")}`;
    const url = `/api/assets/text-styles/${encodeURIComponent(style.name)}/font`;
    const face = new FontFace(family, `url("${url}")`);
    await face.load();
    document.fonts.add(face);
    state.loadedCaptionFont = style.name;
    $("videoPreviewFrame").style.setProperty("--caption-font-family", `"${family}", "Microsoft YaHei", sans-serif`);
  } catch {
    state.loadedCaptionFont = "";
    $("videoPreviewFrame").style.setProperty("--caption-font-family", '"Microsoft YaHei", sans-serif');
  }
}

function applySelectedTextStyle() {
  const style = selectedTextStyle();
  if (!style || !style.preview) {
    updateCaptionVisual();
    return;
  }
  const preview = style.preview;
  $("captionSize").value = preview.size ?? 8;
  $("captionColor").value = preview.color || "#ffffff";
  $("captionX").value = preview.transform_x ?? 0;
  $("captionY").value = preview.transform_y ?? -0.8;
  $("captionWidth").value = Math.round((preview.line_max_width ?? 0.82) * 100);
  loadCaptionPreviewFont(style).then(updateCaptionVisual);
  updateCaptionVisual();
}

function formatTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const remaining = value - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remaining.toFixed(1).padStart(4, "0")}`;
}

function captionPreviewDurationUs() {
  const requested = secondsToUs($("captionDuration").value, 0);
  if (requested > 0) return requested;
  const video = $("captionVideo");
  if (!Number.isFinite(video.duration) || video.duration <= 0) return 0;
  const startSeconds = Math.max(0, Number($("captionStart").value) || 0);
  return Math.max(0, Math.round((video.duration - startSeconds) * 1000000));
}

function updateCaptionVisual() {
  const frame = $("videoPreviewFrame");
  const style = selectedTextStyle()?.preview || {};
  const x = Number($("captionX").value || 0);
  const y = Number($("captionY").value);
  const width = Number($("captionWidth").value || 82);
  const size = Number($("captionSize").value || 8);
  const previewWidth = frame.clientWidth || 360;

  frame.style.setProperty("--caption-x", `${(x + 1) * 50}%`);
  frame.style.setProperty("--caption-y", `${(1 - y) * 50}%`);
  frame.style.setProperty("--caption-width", `${width}%`);
  frame.style.setProperty("--caption-size", `${Math.max(12, (size * previewWidth) / 150)}px`);
  frame.style.setProperty("--caption-color", $("captionColor").value || "#ffffff");
  frame.style.setProperty("--caption-weight", style.bold ? "700" : "400");
  frame.style.setProperty("--caption-font-style", style.italic ? "italic" : "normal");
  frame.style.setProperty("--caption-decoration", style.underline ? "underline" : "none");

  $("captionXValue").textContent = x.toFixed(2);
  $("captionYValue").textContent = y.toFixed(2);
  $("captionWidthValue").textContent = `${Math.round(width)}%`;
  updateCaptionAtCurrentTime();
}

function renderCaptionCueList() {
  const list = $("captionCueList");
  list.innerHTML = "";
  state.captionCues.forEach((cue, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "caption-cue";
    item.dataset.cueIndex = String(index);
    const time = document.createElement("span");
    time.className = "caption-cue-time";
    time.textContent = `${formatTime(cue.start_us / 1000000)} - ${formatTime(cue.end_us / 1000000)}`;
    const content = document.createElement("span");
    content.textContent = cue.text;
    item.append(time, content);
    item.addEventListener("click", () => {
      const video = $("captionVideo");
      if (Number.isFinite(video.duration)) video.currentTime = cue.start_us / 1000000;
      updateCaptionAtCurrentTime();
    });
    list.appendChild(item);
  });
}

function updateCaptionAtCurrentTime() {
  const video = $("captionVideo");
  const currentUs = Math.round((video.currentTime || 0) * 1000000);
  const activeIndex = state.captionCues.findIndex(
    (cue) => currentUs >= cue.start_us && currentUs < cue.end_us,
  );
  const overlay = $("captionOverlay");
  if (activeIndex >= 0) {
    overlay.textContent = state.captionCues[activeIndex].text;
    overlay.classList.remove("hidden");
  } else {
    overlay.textContent = "";
    overlay.classList.add("hidden");
  }
  document.querySelectorAll(".caption-cue").forEach((item, index) => {
    item.classList.toggle("active", index === activeIndex);
  });
  const duration = Number.isFinite(video.duration) ? video.duration : 0;
  $("previewTime").textContent = `${formatTime(video.currentTime)} / ${formatTime(duration)}`;
}

async function refreshCaptionPreview() {
  const text = $("captionText").value.trim();
  const durationUs = captionPreviewDurationUs();
  const video = $("captionVideo");
  const startSeconds = Math.max(0, Number($("captionStart").value) || 0);
  const requestedSeconds = Math.max(0, Number($("captionDuration").value) || 0);
  if (!text) {
    state.captionCues = [];
    renderCaptionCueList();
    updateCaptionAtCurrentTime();
    $("captionSummary").textContent = "输入长文案后生成切分预览。";
    return;
  }
  if (durationUs <= 0) {
    state.captionCues = [];
    renderCaptionCueList();
    $("captionSummary").textContent = "请选择视频，或者填写字幕覆盖时长。";
    return;
  }
  if (Number.isFinite(video.duration) && video.duration > 0) {
    const endSeconds = requestedSeconds > 0 ? startSeconds + requestedSeconds : video.duration;
    if (startSeconds >= video.duration || endSeconds > video.duration + 0.01) {
      state.captionCues = [];
      renderCaptionCueList();
      updateCaptionAtCurrentTime();
      $("captionSummary").textContent = `字幕范围超出视频时长 ${formatTime(video.duration)}。`;
      return;
    }
  }

  const requestId = ++state.captionPreviewRequest;
  try {
    const data = await apiFetch("/api/captions/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        start_us: secondsToUs($("captionStart").value, 0),
        duration_us: durationUs,
        max_chars: Number($("captionMaxChars").value || 16),
      }),
    });
    if (requestId !== state.captionPreviewRequest) return;
    state.captionCues = data.cues || [];
    renderCaptionCueList();
    updateCaptionAtCurrentTime();
    $("captionSummary").textContent = `已切分为 ${data.cue_count} 条字幕，覆盖 ${formatTime(durationUs / 1000000)}。时间暂按字数比例分配，可在预览中检查换行、字号和位置。`;
  } catch (error) {
    if (requestId !== state.captionPreviewRequest) return;
    state.captionCues = [];
    renderCaptionCueList();
    updateCaptionAtCurrentTime();
    $("captionSummary").textContent = error.message;
  }
}

function scheduleCaptionPreview() {
  clearTimeout(state.captionPreviewTimer);
  state.captionPreviewTimer = setTimeout(refreshCaptionPreview, 250);
  updateCaptionVisual();
}

function loadVideoPreview() {
  const video = $("captionVideo");
  if (state.videoObjectUrl) URL.revokeObjectURL(state.videoObjectUrl);
  state.videoObjectUrl = "";
  state.captionCues = [];
  if ($("sourceMode").value === "mother") {
    const template = selectedTemplate();
    if (!template) {
      video.removeAttribute("src");
      video.load();
      $("previewEmpty").textContent = "选择已导入母版后在这里预览";
      $("previewEmpty").classList.remove("hidden");
      scheduleCaptionPreview();
      return;
    }
    video.src = `/api/templates/${encodeURIComponent(template.template_id)}/preview-video`;
    $("previewEmpty").textContent = "当前母版没有可预览的视频";
    $("previewEmpty").classList.add("hidden");
    video.load();
    return;
  }
  const file = $("videoFile").files[0];
  if (!file) {
    video.removeAttribute("src");
    video.load();
    $("previewEmpty").textContent = "选择 MP4 后在这里预览";
    $("previewEmpty").classList.remove("hidden");
    scheduleCaptionPreview();
    return;
  }
  state.videoObjectUrl = URL.createObjectURL(file);
  video.src = state.videoObjectUrl;
  $("previewEmpty").classList.add("hidden");
  video.load();
}

function buildCaptionConfig() {
  if ($("textMode").value !== "captions") return null;
  const text = $("captionText").value.trim();
  if (!text) return null;
  return {
    text,
    start_us: secondsToUs($("captionStart").value, 0),
    duration_us: secondsToUs($("captionDuration").value, 0),
    max_chars: Number($("captionMaxChars").value || 16),
    style_json_path: $("textStyleSelect").value,
    size: Number($("captionSize").value || 8),
    color: $("captionColor").value,
    transform_x: Number($("captionX").value || 0),
    transform_y: Number($("captionY").value),
    line_max_width: Number($("captionWidth").value || 82) / 100,
  };
}

function buildTextConfig() {
  if (["captions", "preserve", "restyle"].includes($("textMode").value)) return [];
  const text = $("textValue").value.trim();
  if (!text) return [];
  const mode = $("textMode").value;
  const stylePath = $("textStyleSelect").value;
  const item = {
    type: mode,
    scope: "top",
    text,
    style_json_path: stylePath,
    apply_clip: Boolean(stylePath),
  };
  if (mode === "replace") {
    item.track_index = Number($("textTrackIndex").value || 0);
    item.segment_index = Number($("textSegmentIndex").value || 0);
  } else {
    item.start_us = secondsToUs($("textStart").value, 0);
    item.duration_us = secondsToUs($("textDuration").value, 0);
    item.text_effect_json_path = $("textEffectSelect").value;
  }
  return [item];
}

function selectedTextTemplate() {
  const path = $("textTemplateSelect").value;
  return state.textTemplates.find((item) => item.path === path) || null;
}

function defaultTextTemplateConfig(template) {
  return {
    template_json_path: template.path,
    texts: (template.text_slots || []).map((slot) => slot.text || ""),
    start_us: 0,
    duration_us: 0,
  };
}

function saveActiveTextTemplateConfig() {
  const path = state.activeTextTemplatePath;
  if (!path) return;
  const template = state.textTemplates.find((item) => item.path === path);
  if (!template) return;
  state.textTemplateConfigs.set(path, {
    template_json_path: path,
    texts: [...document.querySelectorAll(".text-template-slot-input")].map((input) => input.value),
    start_us: secondsToUs($("textTemplateStart").value, 0),
    duration_us: secondsToUs($("textTemplateDuration").value, 0),
  });
}

function textTemplateConfig(template) {
  if (state.activeTextTemplatePath === template.path) saveActiveTextTemplateConfig();
  return structuredClone(
    state.textTemplateConfigs.get(template.path) || defaultTextTemplateConfig(template),
  );
}

function renderTextTemplateSlots() {
  saveActiveTextTemplateConfig();
  const container = $("textTemplateSlots");
  const summary = $("textTemplateSummary");
  container.innerHTML = "";
  const template = selectedTextTemplate();
  if (!template) {
    state.activeTextTemplatePath = "";
    $("textTemplateStart").value = "0";
    $("textTemplateDuration").value = "0";
    summary.textContent = "不添加复合文字模板。";
    return;
  }
  const config = state.textTemplateConfigs.get(template.path) || defaultTextTemplateConfig(template);
  state.activeTextTemplatePath = template.path;
  $("textTemplateStart").value = String(Number(config.start_us || 0) / 1000000);
  $("textTemplateDuration").value = String(Number(config.duration_us || 0) / 1000000);
  const slots = Array.isArray(template.text_slots) ? template.text_slots : [];
  slots.forEach((slot, index) => {
    const label = document.createElement("label");
    label.className = "field";
    const title = document.createElement("span");
    title.textContent = `文字槽 ${index + 1}`;
    const input = document.createElement("textarea");
    input.className = "text-template-slot-input";
    input.rows = String(slot.text || "").includes("\n") ? 4 : 2;
    input.value = config.texts?.[index] ?? slot.text ?? "";
    input.dataset.slotIndex = String(index);
    label.append(title, input);
    container.appendChild(label);
  });
  summary.textContent = `${template.name}：${slots.length} 个可替换文字槽。开始和持续时间为 0 时，从视频开头显示到结尾。`;
}

function buildTextTemplateConfig() {
  const template = selectedTextTemplate();
  if (!template || template.error) return [];
  return [textTemplateConfig(template)];
}

function buildEffectConfig() {
  const effectPath = $("effectSelect").value;
  if (!effectPath) return [];
  return [
    {
      effect_json_path: effectPath,
      target_video_track_index: 0,
      target_video_segment_index: 0,
      start_us: -1,
      duration_us: 0,
    },
  ];
}

function buildAudioCommonConfig() {
  const sourceStartUs = secondsToUs($("audioSourceStart").value, 0);
  const targetDurationUs = secondsToUs($("audioDuration").value, 0);
  const config = {
    type: "add",
    target_start_us: secondsToUs($("audioStart").value, 0),
    target_duration_us: targetDurationUs,
    fit_to_video: targetDurationUs <= 0,
    volume: Number($("audioVolume").value || 0) / 100,
  };
  if (sourceStartUs > 0 || targetDurationUs > 0) config.source_start_us = sourceStartUs;
  if (targetDurationUs > 0) config.source_duration_us = targetDurationUs;
  return config;
}

function buildBatchBaseTexts() {
  const texts = buildTextConfig();
  if ($("textMode").value === "add" && texts.length) {
    delete texts[0].text_effect_json_path;
  }
  return texts;
}

function buildBatchDimensions() {
  const audioDefaults = buildAudioCommonConfig();
  const effectDefaults = {
    target_video_track_index: 0,
    target_video_segment_index: 0,
    start_us: -1,
    duration_us: 0,
  };
  const baseTexts = buildBatchBaseTexts();
  const audioCandidates = [...state.selectedAudioIdentities].map((identity) => {
    const asset = state.audioLibrary.assets.find((item) => item.identity === identity);
    return {
      id: identity,
      label: asset?.name || identity,
      append: {
        audios: [
          {
            ...audioDefaults,
            type: "add",
            library_identity: identity,
            selection_mode: "specific",
          },
        ],
      },
    };
  });
  const effectCandidates = [...state.selectedEffectPaths].map((path) => {
    const effect = state.effects.find((item) => item.path === path);
    return {
      id: path,
      label: effect?.effect_name || effect?.name || path,
      append: {
        effects: [{ ...effectDefaults, effect_json_path: path }],
      },
    };
  });
  const textEffectCandidates = [...state.selectedTextEffectPaths].map((path) => {
    const effect = state.textEffects.find((item) => item.path === path);
    const texts = structuredClone(baseTexts);
    if (texts[0]) texts[0].text_effect_json_path = path;
    return {
      id: effect?.identity || path,
      label: effect?.name || path,
      patch: { texts },
    };
  });
  const textTemplateCandidates = [...state.selectedTextTemplatePaths].map((path) => {
    const template = state.textTemplates.find((item) => item.path === path);
    return {
      id: template?.identity || path,
      label: template?.name || path,
      append: {
        text_templates: [textTemplateConfig(template)],
      },
    };
  });
  return [
    {
      key: "bgm",
      label: "BGM",
      mode: dimensionMode("batchAudioDimensionMode"),
      candidates: audioCandidates,
    },
    {
      key: "video_effect",
      label: "视频特效",
      mode: dimensionMode("batchEffectDimensionMode"),
      candidates: effectCandidates,
    },
    {
      key: "text_effect",
      label: "新增文字花字",
      mode: dimensionMode("batchTextEffectDimensionMode"),
      candidates: textEffectCandidates,
    },
    {
      key: "text_template",
      label: "复合文字模板",
      mode: dimensionMode("batchTextTemplateDimensionMode"),
      candidates: textTemplateCandidates,
    },
  ];
}

function secondsToUs(value, fallback = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.round(Math.max(0, number) * 1000000);
}

function clearJobPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function isFinishedStatus(status) {
  return status === "completed" || status === "failed";
}

function showJobStatus(jobStatus) {
  const statusText = {
    pending: "排队中",
    running: "剪映导出中",
    completed: "已完成",
    failed: "失败",
  }[jobStatus.status] || jobStatus.status;

  setLog({
    ...jobStatus,
    status_text: statusText,
  });
}

async function pollJob(jobId) {
  try {
    const jobStatus = await apiFetch(`/api/jobs/${jobId}`);
    showJobStatus(jobStatus);

    if (!isFinishedStatus(jobStatus.status)) return;

    clearJobPolling();
    $("renderBtn").disabled = false;

    if (jobStatus.status === "completed" && jobStatus.result && jobStatus.result.exported) {
      $("downloadLink").href = `/api/jobs/${jobId}/download`;
      $("downloadLink").classList.remove("hidden");
    }
  } catch (error) {
    clearJobPolling();
    $("renderBtn").disabled = false;
    setLog(error.message);
  }
}

function startJobPolling(jobId) {
  state.activeJobId = jobId;
  state.activeBatchId = null;
  clearJobPolling();
  pollJob(jobId);
  state.pollTimer = setInterval(() => pollJob(jobId), 2000);
}

function batchStatusText(status) {
  return {
    pending: "排队中",
    running: "正在处理",
    completed: "全部完成",
    failed: "已结束，部分任务失败",
  }[status] || status;
}

function renderBatchResults(batchStatus) {
  const container = $("batchResultList");
  container.innerHTML = "";
  for (const item of batchStatus.jobs || []) {
    const row = document.createElement("div");
    row.className = "batch-result-row";
    const index = document.createElement("span");
    index.textContent = `#${item.index}`;
    const variant = document.createElement("span");
    if (item.variant?.summary) {
      variant.textContent = item.variant.summary;
    } else {
      const audio = state.audioLibrary.assets.find(
        (asset) => asset.identity === item.variant?.music_identity,
      );
      variant.textContent = `${audio?.name || item.variant?.music_identity || "音乐"} + ${item.variant?.effect_name || "特效"}`;
    }
    const status = document.createElement("span");
    status.textContent = {
      pending: "排队中",
      running: "处理中",
      completed: "已完成",
      failed: "失败",
    }[item.status] || item.status;
    row.append(index, variant, status);
    if (item.status === "completed" && item.result?.exported) {
      const link = document.createElement("a");
      link.href = `/api/jobs/${item.job_id}/download`;
      link.textContent = "下载 MP4";
      row.appendChild(link);
    } else if (item.error) {
      const error = document.createElement("span");
      error.title = item.error;
      error.textContent = "查看错误";
      row.appendChild(error);
    }
    container.appendChild(row);
  }
  container.classList.toggle("hidden", !(batchStatus.jobs || []).length);
}

function showBatchStatus(batchStatus) {
  setLog({
    batch_id: batchStatus.batch_id,
    status: batchStatus.status,
    status_text: batchStatusText(batchStatus.status),
    total: batchStatus.total,
    finished: batchStatus.finished || 0,
    counts: batchStatus.counts || {},
  });
  renderBatchResults(batchStatus);
}

async function pollBatch(batchId) {
  try {
    const batchStatus = await apiFetch(`/api/batches/${batchId}`);
    showBatchStatus(batchStatus);
    if (!isFinishedStatus(batchStatus.status)) return;
    clearJobPolling();
    state.activeBatchId = null;
    updateCombinationSummary();
  } catch (error) {
    clearJobPolling();
    state.activeBatchId = null;
    updateCombinationSummary();
    setLog(error.message);
  }
}

function startBatchPolling(batchId) {
  state.activeBatchId = batchId;
  state.activeJobId = null;
  clearJobPolling();
  pollBatch(batchId);
  state.pollTimer = setInterval(() => pollBatch(batchId), 2000);
}

async function submitRender() {
  $("renderBtn").disabled = true;
  $("downloadLink").classList.add("hidden");
  $("batchResultList").classList.add("hidden");
  setLog("准备任务...");

  try {
    const sourceMode = $("sourceMode").value;
    const source = {};
    const videos = [];
    let media = null;
    if (sourceMode !== "mother") {
      const file = $("videoFile").files[0];
      if (!file) throw new Error("请选择一个 MP4 视频。");
      setLog("正在上传视频...");
      media = await uploadFile("video", file);
    }
    if (sourceMode === "mother") {
      const templateId = $("templateSelect").value;
      if (!templateId) throw new Error("请选择一个已导入母版。");
      source.type = "template";
      source.template_id = templateId;
      source.preserve_original_video = true;
    } else if (sourceMode === "template-replace") {
      const templateId = $("templateSelect").value;
      if (!templateId) throw new Error("请选择一个模板。");
      source.type = "template";
      source.template_id = templateId;
      const targetKind = templateVideoTargetKind();
      const replacement = {
        type: targetKind,
        media_id: media.media_id,
        segment_index: 0,
      };
      if (targetKind === "nested-video") {
        replacement.nested_draft_index = 0;
        replacement.video_track_index = 0;
      } else {
        replacement.track_index = 0;
      }
      videos.push(replacement);
    } else {
      source.type = "video";
      source.media_id = media.media_id;
    }

    const job = {
      schema: "jyd.render_job.v1",
      source,
      output: {
        draft_root: currentDraftRoot(),
        skip_export: $("skipExport").checked,
      },
      texts: buildTextConfig(),
      text_templates: buildTextTemplateConfig(),
      videos,
      export: {
        resolution: $("resolution").value,
        framerate: $("framerate").value,
        timeout: 1200,
      },
    };
    const captions = buildCaptionConfig();
    if (captions) job.captions = captions;
    if ($("textMode").value === "restyle") {
      const stylePath = $("textStyleSelect").value;
      if (!stylePath) throw new Error("请选择一个新的字幕样式。");
      job.existing_text_style = {
        style_json_path: stylePath,
        apply_clip: true,
      };
    }

    if ($("combinationMode").value === "batch") {
      const combination = batchCombinationState();
      if (!combination.valid) throw new Error(combination.errors.join("；"));
      job.texts = buildBatchBaseTexts();
      job.text_templates = [];
      job.audios = [];
      job.effects = [];
      const batchRequest = {
        job,
        dimensions: buildBatchDimensions(),
        max_jobs: combination.maxJobs,
      };
      setLog({ status: "submitting_batch", total: combination.total });
      const result = await apiFetch("/api/render/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batchRequest),
      });
      setLog({
        batch_id: result.batch_id,
        status: result.status,
        total: result.total,
        status_text: "批次已入队",
      });
      startBatchPolling(result.batch_id);
      return;
    }

    const audios = [];
    const audioMode = $("audioMode").value;
    if (audioMode !== "none") {
      const audioConfig = buildAudioCommonConfig();
      if (audioMode === "upload") {
        const audio = $("audioFile").files[0];
        if (!audio) throw new Error("请选择一个临时上传的 BGM 文件。");
        setLog("正在上传 BGM...");
        const audioMedia = await uploadFile("audio", audio);
        audioConfig.media_id = audioMedia.media_id;
      } else if (audioMode === "library-next") {
        const categoryId = $("audioCategorySelect").value;
        if (!categoryId) throw new Error("请选择音乐大类。");
        audioConfig.library_category_id = categoryId;
        audioConfig.selection_mode = "next";
      } else if (audioMode === "library-specific") {
        const identity = $("audioLibrarySelect").value;
        if (!identity) throw new Error("请选择一首音乐。");
        audioConfig.library_identity = identity;
        audioConfig.selection_mode = "specific";
      }
      audios.push(audioConfig);
    }
    job.audios = audios;
    job.effects = buildEffectConfig();

    setLog({ status: "submitting", job });
    const result = await apiFetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    });

    showJobStatus(result);
    startJobPolling(result.job_id);
  } catch (error) {
    state.activeBatchId = null;
    if ($("combinationMode").value === "batch") updateCombinationSummary();
    else $("renderBtn").disabled = false;
    setLog(error.message);
  }
}

function updateTextMode() {
  const mode = $("textMode").value;
  const replace = mode === "replace";
  $("captionEditor").classList.toggle("hidden", mode !== "captions");
  $("captionCueList").classList.toggle("hidden", mode !== "captions");
  $("singleTextEditor").classList.toggle("hidden", ["captions", "preserve", "restyle"].includes(mode));
  document.querySelectorAll(".text-replace-only").forEach((node) => {
    node.classList.toggle("hidden", !replace);
  });
  $("textEffectSelect").disabled = mode !== "add";
  $("textStyleSelect").disabled = mode === "preserve";
  if (mode === "captions") scheduleCaptionPreview();
  updateCombinationSummary();
}

function bindDimensionMode(selectId, selection, render) {
  $(selectId).addEventListener("change", () => {
    normalizeFixedSelection(selection, selectId);
    render();
  });
}

function bindEvents() {
  $("renderBtn").addEventListener("click", submitRender);
  $("combinationMode").addEventListener("change", updateCombinationMode);
  $("batchAudioCategorySelect").addEventListener("change", renderBatchAudioList);
  $("selectVisibleAudioBtn").addEventListener("click", () => {
    selectForDimension(
      state.selectedAudioIdentities,
      visibleBatchAudioAssets().map((asset) => asset.identity),
      "batchAudioDimensionMode",
    );
    renderBatchAudioList();
  });
  $("clearAudioSelectionBtn").addEventListener("click", () => {
    state.selectedAudioIdentities.clear();
    renderBatchAudioList();
  });
  $("selectAllEffectsBtn").addEventListener("click", () => {
    selectForDimension(
      state.selectedEffectPaths,
      state.effects.filter((effect) => !effect.error).map((effect) => effect.path),
      "batchEffectDimensionMode",
    );
    renderBatchEffectList();
  });
  $("clearEffectSelectionBtn").addEventListener("click", () => {
    state.selectedEffectPaths.clear();
    renderBatchEffectList();
  });
  $("selectAllTextEffectsBtn").addEventListener("click", () => {
    selectForDimension(
      state.selectedTextEffectPaths,
      state.textEffects.filter((effect) => !effect.error).map((effect) => effect.path),
      "batchTextEffectDimensionMode",
    );
    renderBatchTextEffectList();
  });
  $("clearTextEffectSelectionBtn").addEventListener("click", () => {
    state.selectedTextEffectPaths.clear();
    renderBatchTextEffectList();
  });
  $("selectAllTextTemplatesBtn").addEventListener("click", () => {
    selectForDimension(
      state.selectedTextTemplatePaths,
      state.textTemplates.filter((template) => !template.error).map((template) => template.path),
      "batchTextTemplateDimensionMode",
    );
    renderBatchTextTemplateList();
  });
  $("clearTextTemplateSelectionBtn").addEventListener("click", () => {
    state.selectedTextTemplatePaths.clear();
    renderBatchTextTemplateList();
  });
  bindDimensionMode(
    "batchAudioDimensionMode",
    state.selectedAudioIdentities,
    renderBatchAudioList,
  );
  bindDimensionMode(
    "batchEffectDimensionMode",
    state.selectedEffectPaths,
    renderBatchEffectList,
  );
  bindDimensionMode(
    "batchTextEffectDimensionMode",
    state.selectedTextEffectPaths,
    renderBatchTextEffectList,
  );
  bindDimensionMode(
    "batchTextTemplateDimensionMode",
    state.selectedTextTemplatePaths,
    renderBatchTextTemplateList,
  );
  $("batchMaxJobs").addEventListener("input", updateCombinationSummary);
  $("refreshTemplatesBtn").addEventListener("click", loadTemplates);
  $("scanDraftsBtn").addEventListener("click", () => scanDrafts().catch((error) => setLog(error.message)));
  $("importDraftBtn").addEventListener("click", () => importSelectedDraft().catch((error) => setLog(error.message)));
  $("sourceMode").addEventListener("change", updateSourceMode);
  $("templateSelect").addEventListener("change", () => {
    updateTemplateUsageSummary();
    loadVideoPreview();
  });
  $("textMode").addEventListener("change", updateTextMode);
  $("textTemplateSelect").addEventListener("change", renderTextTemplateSlots);
  $("textTemplateStart").addEventListener("input", saveActiveTextTemplateConfig);
  $("textTemplateDuration").addEventListener("input", saveActiveTextTemplateConfig);
  $("textTemplateSlots").addEventListener("input", saveActiveTextTemplateConfig);
  $("audioMode").addEventListener("change", updateAudioMode);
  $("audioCategorySelect").addEventListener("change", updateAudioAssetSelect);
  $("audioLibrarySelect").addEventListener("change", updateAudioPreview);
  $("audioFile").addEventListener("change", updateAudioPreview);
  $("audioVolume").addEventListener("input", () => {
    $("audioVolumeValue").textContent = `${$("audioVolume").value}%`;
  });
  $("createAudioCategoryBtn").addEventListener("click", () => {
    createAudioCategory().catch((error) => setLog(error.message));
  });
  $("refreshAudioLibraryBtn").addEventListener("click", () => {
    loadAudioLibrary().catch((error) => setLog(error.message));
  });
  $("textStyleSelect").addEventListener("change", () => {
    applySelectedTextStyle();
    scheduleCaptionPreview();
  });
  $("textValue").addEventListener("input", updateCombinationSummary);
  $("videoFile").addEventListener("change", loadVideoPreview);
  $("captionVideo").addEventListener("loadedmetadata", scheduleCaptionPreview);
  $("captionVideo").addEventListener("error", () => {
    if ($("sourceMode").value === "mother") $("previewEmpty").classList.remove("hidden");
  });
  $("captionVideo").addEventListener("timeupdate", updateCaptionAtCurrentTime);
  ["captionText", "captionStart", "captionDuration", "captionMaxChars"].forEach((id) => {
    $(id).addEventListener("input", scheduleCaptionPreview);
  });
  ["captionSize", "captionColor", "captionWidth", "captionX", "captionY"].forEach((id) => {
    $(id).addEventListener("input", updateCaptionVisual);
  });
  window.addEventListener("resize", updateCaptionVisual);
}

async function init() {
  bindEvents();
  await checkHealth();
  await loadTemplates();
  await loadLibraries();
  await loadAudioLibrary();
  updateSourceMode();
  updateCombinationMode();
  updateCaptionVisual();
}

init();
