const SHARED_WORKSPACE_STORAGE_KEY = "jyd_shared_workspace_url";
const SHARED_WORKSPACES_STORAGE_KEY = "jyd_shared_workspace_urls";
const LOCAL_OUTPUT_FOLDER_STORAGE_KEY = "jyd_local_output_folder";

function savedSharedWorkspaceUrls() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(SHARED_WORKSPACES_STORAGE_KEY) || "[]");
    if (Array.isArray(parsed) && parsed.length) return parsed.map(String).filter(Boolean);
    const legacy = window.localStorage.getItem(SHARED_WORKSPACE_STORAGE_KEY) || "";
    return legacy ? [legacy] : [];
  } catch { return []; }
}

const state = {
  templates: [],
  audio: { categories: [], assets: [] },
  effects: [],
  fonts: [],
  textStyles: [],
  textEffects: [],
  stickers: [],
  cornerStickers: [],
  videoUrl: "",
  batchId: "",
  pollTimer: null,
  lastBatch: null,
  selectedResultJobIds: new Set(),
  terminalResultsShown: false,
  excelRows: [],
  excelFileName: "",
  excelSelectedDraftPaths: new Set(),
  excelTemporaryTemplateIds: new Set(),
  batchSelectedRowIds: new Set(),
  batchRowSequence: 0,
  batchCoverRowId: "",
  coverEditorMode: "",
  coverEditorConfig: null,
  batchVisualRowId: "",
  batchVisualEditorConfig: null,
  singleCover: null,
  localFileAccess: false,
  localVideo: null,
  localOutputFolder: "",
  digitalHumanTasks: [],
  digitalHumanPollTimer: null,
  digitalHumanCaptionCues: [],
  digitalHumanSourceItemId: "",
  personalLibraryRoot: "",
  personalAssets: {
    items: [],
    page: 1,
    pageSize: 20,
    loaded: false,
    loading: false,
  },
  sharedWorkspaceUrls: savedSharedWorkspaceUrls(),
  sharedWorkspaceStatuses: [],
  collector: {
    drafts: [],
    report: null,
    plan: null,
    connected: false,
    loading: false,
    query: "",
  },
};

const $ = (id) => document.getElementById(id);
const LAST_BATCH_STORAGE_KEY = "jyd.lastBatchId";
const COLLECTOR_URL_STORAGE_KEY = "jyd.collectorUrl";

function defaultCollectorUrl() {
  return window.location.port === "8001" ? "http://127.0.0.1:8766" : "http://127.0.0.1:8765";
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* Keep the response text. */ }
  if (!response.ok) {
    if (response.status === 401 && !url.startsWith("/api/auth/")) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.assign(`/login?next=${next}`);
    }
    const detail = data && typeof data === "object" ? data.detail : data;
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
    error.status = response.status;
    throw error;
  }
  return data;
}

function collectorBaseUrl() {
  const input = $("collectorUrl")?.value.trim();
  return (input || localStorage.getItem(COLLECTOR_URL_STORAGE_KEY) || defaultCollectorUrl()).replace(/\/+$/, "");
}

async function collectorFetch(path, options = {}) {
  const baseUrl = collectorBaseUrl();
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, options);
  } catch (error) {
    throw new Error(`无法连接本机草稿工具 ${baseUrl}，请先启动采集器（${error.message}）`);
  }
  const text = await response.text();
  let data = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* Keep response text. */ }
  if (!response.ok) {
    const detail = data && typeof data === "object" ? data.detail : data;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
  }
  return data;
}

function setCollectorStatus(message, mode = "") {
  const header = $("collectorStatus");
  const inline = $("collectorInlineStatus");
  header.textContent = message;
  inline.textContent = message.replace(/^本机草稿工具\s*/, "");
  header.className = `status ${mode}`.trim();
  inline.className = `inline-status ${mode}`.trim();
}

function setCollectorMessage(message = "", isError = false) {
  const element = $("collectorMessage");
  element.textContent = message;
  element.className = `message${isError ? " error" : ""}`;
  element.classList.toggle("hidden", !message);
}

function setPersonalAssetMessage(message = "", isError = false) {
  const element = $("personalAssetMessage");
  element.textContent = message;
  element.className = `message${isError ? " error" : ""}`;
  element.classList.toggle("hidden", !message);
}

function collectorMetric(label, value) {
  const node = document.createElement("div");
  node.className = "collector-metric";
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = String(value);
  content.title = String(value);
  node.append(name, content);
  return node;
}

function collectorDraftLabel(draft) {
  const stateLabel = draft.encryption_status === "plain" ? "明文" : "自动解密";
  return `${draft.name || "未命名草稿"} · ${stateLabel} · ${formatDuration(draft.duration_us || 0)}`;
}

function renderCollectorDrafts() {
  const allDrafts = state.collector.drafts;
  const drafts = allDrafts.filter((draft) =>
    String(draft.name || "").toLowerCase().includes(state.collector.query),
  );
  for (const id of ["collectorDraftSelect", "personalAssetDraftSelect"]) {
    const select = $(id);
    const previous = select.value;
    select.replaceChildren();
    if (!drafts.length) {
      select.append(new Option("当前目录没有找到剪映草稿", ""));
    } else {
      for (const draft of drafts) select.append(new Option(collectorDraftLabel(draft), draft.path));
      if (drafts.some((draft) => draft.path === previous)) select.value = previous;
    }
  }
  $("collectorDraftCount").textContent = state.collector.query
    ? `${drafts.length} / ${allDrafts.length} 个`
    : `${allDrafts.length} 个`;
  $("analyzeCollectorDraftBtn").disabled = !drafts.length;
}

async function connectCollector({ quiet = false } = {}) {
  if (state.collector.loading) return;
  state.collector.loading = true;
  setCollectorStatus("本机草稿工具连接中");
  try {
    const config = await collectorFetch("/api/config");
    const configuredServerUrl = String(config.render_server_url || "").replace(/\/+$/, "");
    if (configuredServerUrl
      && !/^https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?$/i.test(configuredServerUrl)) {
      try {
        const configuredOrigin = normalizedServerUrl(configuredServerUrl);
        if (!state.sharedWorkspaceUrls.includes(configuredOrigin)) {
          state.sharedWorkspaceUrls.push(configuredOrigin);
          saveSharedWorkspaceUrls();
        }
      } catch { /* Ignore obsolete collector addresses. */ }
    }
    state.collector.connected = true;
    if (state.personalLibraryRoot) {
      await collectorFetch("/api/config/personal-library-root", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ personal_library_root: state.personalLibraryRoot }),
      });
    }
    localStorage.setItem(COLLECTOR_URL_STORAGE_KEY, collectorBaseUrl());
    $("collectorDraftRoot").value = config.draft_root || "";
    $("collectorAccessToken").placeholder = config.access_token_configured
      ? "已保存；不修改可留空"
      : "请输入网站内部访问密码";
    state.collector.drafts = await collectorFetch("/api/drafts");
    renderCollectorDrafts();
    setCollectorStatus("本机草稿工具已连接", "ok");
    if (!quiet) {
      const detected = config.draft_root_mode === "auto" ? "系统已自动识别草稿目录，" : "";
      setCollectorMessage(`${detected}已读取 ${state.collector.drafts.length} 个本机草稿。`);
    }
  } catch (error) {
    state.collector.connected = false;
    state.collector.drafts = [];
    renderCollectorDrafts();
    setCollectorStatus("本机草稿工具未启动", "bad");
    if (!quiet) setCollectorMessage(error.message, true);
  } finally {
    state.collector.loading = false;
  }
}

async function saveCollectorConfig() {
  const button = $("saveCollectorConfigBtn");
  const draftRoot = $("collectorDraftRoot").value.trim();
  const accessToken = $("collectorAccessToken").value.trim();
  if (!draftRoot) {
    setCollectorMessage("请填写 JianyingPro Drafts 草稿目录。", true);
    return;
  }
  button.disabled = true;
  setCollectorMessage("正在保存本机草稿工具配置...");
  try {
    await collectorFetch("/api/config/draft-root", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_root: draftRoot }),
    });
    await collectorFetch("/api/config/server-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ render_server_url: window.location.origin }),
    });
    if (accessToken) {
      await collectorFetch("/api/config/access-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token: accessToken }),
      });
      $("collectorAccessToken").value = "";
      $("collectorAccessToken").placeholder = "已保存；不修改可留空";
    }
    localStorage.setItem(COLLECTOR_URL_STORAGE_KEY, collectorBaseUrl());
    state.collector.drafts = await collectorFetch("/api/drafts");
    renderCollectorDrafts();
    setCollectorStatus("本机草稿工具已连接", "ok");
    setCollectorMessage(`配置已保存，找到 ${state.collector.drafts.length} 个草稿。`);
  } catch (error) {
    setCollectorMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function refreshCollectorDrafts() {
  const button = $("refreshCollectorDraftsBtn");
  button.disabled = true;
  try {
    state.collector.drafts = await collectorFetch("/api/drafts");
    renderCollectorDrafts();
    state.collector.connected = true;
    localStorage.setItem(COLLECTOR_URL_STORAGE_KEY, collectorBaseUrl());
    setCollectorStatus("本机草稿工具已连接", "ok");
    setCollectorMessage(`扫描完成，找到 ${state.collector.drafts.length} 个草稿。`);
  } catch (error) {
    setCollectorStatus("本机草稿工具连接失败", "bad");
    setCollectorMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function collectPersonalAssets() {
  const draftDir = $("personalAssetDraftSelect").value;
  if (!draftDir) {
    setPersonalAssetMessage("请先选择一个包含素材的剪映草稿。", true);
    return;
  }
  const kinds = Array.from(document.querySelectorAll('input[name="personalAssetKind"]:checked'))
    .map((input) => input.value);
  if (!kinds.length) {
    setPersonalAssetMessage("请至少选择一种要采集的素材。", true);
    return;
  }
  const button = $("uploadPersonalAssetsBtn");
  button.disabled = true;
  setPersonalAssetMessage("正在解密草稿、提取素材并上传到当前处理机...");
  try {
    const result = await collectorFetch("/api/drafts/collect-personal-assets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft_dir: draftDir,
        kinds,
        upload: true,
        server_url: window.location.origin,
      }),
    });
    const counts = Object.entries(result.results || {}).map(([kind, item]) => {
      const count = Number(item.exported_count ?? item.copied_count ?? 0);
      const labels = {
        audio: "音乐", effects: "特效", fonts: "字体", stickers: "全屏贴纸",
        corner_stickers: "四角贴纸",
        text_effects: "花字", text_templates: "文字模板",
      };
      return `${labels[kind] || kind} ${count}`;
    });
    await refreshGenerationAssetLibraries();
    await loadPersonalAssets();
    setPersonalAssetMessage(`采集并上传完成：${counts.join("，")}。已自动刷新素材列表。`);
  } catch (error) {
    setPersonalAssetMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

const PERSONAL_ASSET_KIND_LABELS = {
  audio: "背景音乐",
  effect: "视频特效",
  font: "字体",
  sticker: "全屏贴纸",
  corner_sticker: "四角贴纸",
  text_effect: "花字",
  text_style: "字幕样式",
  text_template: "文字模板",
  template: "母版",
};

async function refreshGenerationAssetLibraries() {
  const [audio, effects, fonts, textStyles, textEffects, stickers, cornerStickers] = await Promise.all([
    apiFetch("/api/audio-library"),
    apiFetch("/api/assets/effects"),
    apiFetch("/api/assets/fonts"),
    apiFetch("/api/assets/text-styles"),
    apiFetch("/api/assets/text-effects"),
    apiFetch("/api/assets/stickers"),
    apiFetch("/api/assets/corner-stickers"),
  ]);
  state.audio = audio;
  state.effects = effects;
  state.fonts = fonts;
  state.textStyles = textStyles;
  state.textEffects = textEffects;
  state.stickers = stickers;
  state.cornerStickers = cornerStickers;
  fillAudioCategories();
  fillFonts();
  fillCaptionStyles();
  updateCounts();
}

function filteredPersonalAssets() {
  const query = $("personalAssetSearch").value.trim().toLocaleLowerCase();
  const kind = $("personalAssetKindFilter").value;
  const status = $("personalAssetStatusFilter").value;
  return state.personalAssets.items.filter((item) => {
    if (kind && item.kind !== kind) return false;
    if (status === "active" && (item.deleted || item.enabled === false)) return false;
    if (status === "disabled" && (item.deleted || item.enabled !== false)) return false;
    if (status === "deleted" && !item.deleted) return false;
    const haystack = [item.name, item.original_name, item.identity].join(" ").toLocaleLowerCase();
    return !query || haystack.includes(query);
  });
}

function renderPersonalAssets() {
  const body = $("personalAssetRows");
  body.replaceChildren();
  const filtered = filteredPersonalAssets();
  const pageCount = Math.max(1, Math.ceil(filtered.length / state.personalAssets.pageSize));
  state.personalAssets.page = Math.min(Math.max(1, state.personalAssets.page), pageCount);
  const start = (state.personalAssets.page - 1) * state.personalAssets.pageSize;
  const items = filtered.slice(start, start + state.personalAssets.pageSize);
  $("personalAssetEmpty").classList.toggle("hidden", filtered.length > 0);
  $("personalAssetPageSummary").textContent = filtered.length
    ? `共 ${filtered.length} 项，当前 ${start + 1}-${start + items.length}`
    : "0 项";
  $("personalAssetPageNumber").textContent = `${state.personalAssets.page} / ${pageCount}`;
  $("personalAssetPreviousBtn").disabled = state.personalAssets.page <= 1;
  $("personalAssetNextBtn").disabled = state.personalAssets.page >= pageCount;

  items.forEach((item) => {
    const row = document.createElement("tr");
    row.classList.toggle("deleted", item.deleted);

    const nameCell = document.createElement("td");
    const name = document.createElement("div");
    name.className = "personal-asset-name";
    const title = document.createElement("strong");
    title.textContent = item.name || item.original_name || item.identity;
    const identity = document.createElement("small");
    identity.textContent = item.identity;
    identity.title = item.identity;
    name.append(title, identity);
    nameCell.append(name);

    const kindCell = document.createElement("td");
    const kindLabel = document.createElement("span");
    kindLabel.className = "personal-asset-kind";
    kindLabel.textContent = PERSONAL_ASSET_KIND_LABELS[item.kind] || item.kind;
    kindCell.append(kindLabel);

    const sourceCell = document.createElement("td");
    const sourceLabel = document.createElement("span");
    sourceLabel.className = "personal-asset-source-label";
    sourceLabel.textContent = item.kind === "template"
      ? "母版库"
      : (item.library_scope === "personal" ? "本机采集" : "基础素材库");
    sourceCell.append(sourceLabel);

    const statusCell = document.createElement("td");
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "personal-asset-enabled";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = item.enabled !== false;
    enabled.disabled = item.deleted;
    const enabledText = document.createElement("span");
    enabledText.textContent = item.deleted ? "回收站（7天后清理）" : (enabled.checked ? "启用" : "停用");
    enabled.addEventListener("change", async () => {
      enabled.disabled = true;
      try {
        await updatePersonalAsset(item, { enabled: enabled.checked });
      } catch (error) {
        enabled.checked = !enabled.checked;
        setPersonalAssetMessage(error.message, true);
      } finally {
        enabled.disabled = false;
      }
    });
    enabledLabel.append(enabled, enabledText);
    statusCell.append(enabledLabel);

    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "personal-asset-row-actions";
    if (item.preview_url) {
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "secondary-button";
      preview.textContent = "预览";
      preview.addEventListener("click", () => showPersonalAssetPreview(item));
      actions.append(preview);
    }
    const action = document.createElement("button");
    action.type = "button";
    action.className = item.deleted ? "secondary-button" : "danger-outline";
    action.textContent = item.deleted ? "恢复" : "删除";
    action.addEventListener("click", () => {
      if (item.deleted) restorePersonalAsset(item);
      else trashPersonalAsset(item);
    });
    actions.append(action);
    actionCell.append(actions);
    row.append(nameCell, kindCell, sourceCell, statusCell, actionCell);
    body.append(row);
  });
}

async function loadPersonalAssets(message = "") {
  if (state.personalAssets.loading) return;
  state.personalAssets.loading = true;
  try {
    const data = await apiFetch("/api/local-assets?include_deleted=true");
    state.personalAssets.items = Array.isArray(data.items) ? data.items : [];
    state.personalAssets.loaded = true;
    renderPersonalAssets();
    if (message) setPersonalAssetMessage(message);
  } catch (error) {
    setPersonalAssetMessage(error.message, true);
  } finally {
    state.personalAssets.loading = false;
  }
}

async function updatePersonalAsset(item, payload) {
  await apiFetch(`/api/local-assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await Promise.all([
    loadPersonalAssets(),
    item.kind === "template" ? refreshMothers() : refreshGenerationAssetLibraries(),
  ]);
  setPersonalAssetMessage(`已${payload.enabled ? "启用" : "停用"}：${item.name}`);
}

async function trashPersonalAsset(item) {
  if (!window.confirm(`确定删除“${item.name}”吗？将停止参与生成并保留 7 天，之后自动删除实际文件。`)) return;
  try {
    await apiFetch(`/api/local-assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}`, {
      method: "DELETE",
    });
    await Promise.all([
      loadPersonalAssets(),
      item.kind === "template" ? refreshMothers() : refreshGenerationAssetLibraries(),
    ]);
    setPersonalAssetMessage(`已移入回收站：${item.name}`);
  } catch (error) {
    setPersonalAssetMessage(error.message, true);
  }
}

async function restorePersonalAsset(item) {
  try {
    await apiFetch(`/api/local-assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}/restore`, {
      method: "POST",
    });
    await Promise.all([
      loadPersonalAssets(),
      item.kind === "template" ? refreshMothers() : refreshGenerationAssetLibraries(),
    ]);
    setPersonalAssetMessage(`已恢复：${item.name}`);
  } catch (error) {
    setPersonalAssetMessage(error.message, true);
  }
}

function showPersonalAssetPreview(item) {
  $("personalAssetPreviewTitle").textContent = item.name;
  $("personalAssetPreviewMeta").textContent = `${PERSONAL_ASSET_KIND_LABELS[item.kind] || item.kind} · ${item.identity}`;
  const body = $("personalAssetPreviewBody");
  body.replaceChildren();
  if (item.preview_type === "audio") {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = item.preview_url;
    body.append(audio);
  } else if (item.preview_type === "font") {
    const family = `PersonalAssetPreview_${Math.random().toString(36).slice(2)}`;
    const style = document.createElement("style");
    style.textContent = `@font-face { font-family: "${family}"; src: url("${item.preview_url}"); font-display: swap; }`;
    const sample = document.createElement("div");
    sample.className = "personal-asset-font-sample";
    sample.style.fontFamily = `"${family}", sans-serif`;
    sample.textContent = "人生没有白走的路，每一步都算数 123";
    body.append(style, sample);
  } else {
    const image = document.createElement("img");
    image.src = item.preview_url;
    image.alt = item.name;
    body.append(image);
  }
  $("personalAssetPreviewDialog").showModal();
}

async function refreshPersonalAssetDrafts() {
  const button = $("refreshPersonalAssetDraftsBtn");
  button.disabled = true;
  setPersonalAssetMessage("正在读取本机剪映草稿...");
  try {
    state.collector.drafts = await collectorFetch("/api/drafts");
    state.collector.connected = true;
    renderCollectorDrafts();
    setCollectorStatus("本机草稿工具已连接", "ok");
    setPersonalAssetMessage(`扫描完成，找到 ${state.collector.drafts.length} 个草稿。`);
  } catch (error) {
    setCollectorStatus("本机草稿工具连接失败", "bad");
    setPersonalAssetMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function resetCollectorPolicies() {
  $("collectorPolicyAudio").value = "keep";
  $("collectorPolicyEffects").value = "keep";
  $("collectorPolicyTextStyle").value = "keep";
  $("collectorPolicyTextEffects").value = "keep";
}

function invalidateCollectorPlan() {
  state.collector.plan = null;
  $("collectorPlanSummary").classList.add("hidden");
  $("collectorPlanDetails").classList.add("hidden");
  $("uploadCollectorPlanBtn").disabled = true;
}

function collectorDependencyKindLabel(kind) {
  return {
    audio: "背景音乐",
    sound_effect: "音效",
    video_effect: "视频特效",
    video_adjustment: "画面调节 / 色彩校正",
    text_effect: "花字资源",
    text_template_resource: "复合文字模板资源",
    font: "字幕字体",
    video: "视频素材",
    resource: "本地资源",
  }[kind] || kind || "本地资源";
}

function collectorDependencyStatusLabel(item) {
  if (item.decision === "blocked_missing" || item.status === "missing") return "本机文件缺失";
  if (item.decision === "blocked_external" || item.status === "external") return "不是可上传的本地文件";
  if (item.decision === "reuse_library") return "素材库复用";
  if (item.decision === "upload") return "需要上传";
  return item.decision_reason || item.status || "待处理";
}

function collectorDependencyAdvice(item) {
  const kind = String(item.kind || "");
  if (kind === "font") {
    return "保留原样：先将同一字体提取到字体库并重新分析；不要求原字体：把“字幕 / 文字样式”改为替换。";
  }
  if (kind === "audio" || kind === "sound_effect") {
    return "保留原素材：在剪映中重新下载或恢复该音频；后续会换音乐：把“BGM / 音效”改为替换或删除。";
  }
  if (kind === "video_effect") {
    return "保留原特效：在剪映中重新下载该特效；后续会换特效：把“视频特效”改为替换或删除。";
  }
  if (kind === "video_adjustment") {
    return "这是视频片段自身的画面调节，始终随母版保留；请在剪映中重新下载或恢复该资源，然后重新分析。";
  }
  if (kind === "text_effect") {
    return "保留原花字：在剪映中重新下载该资源；否则把“花字”改为替换或删除。";
  }
  return "请在剪映中恢复该文件或重新链接素材，然后重新分析草稿。";
}

function collectorIssueNode(item, { potential = false } = {}) {
  const node = document.createElement("article");
  node.className = "collector-issue";
  const title = document.createElement("div");
  title.className = "collector-issue-title";
  const kind = document.createElement("strong");
  kind.textContent = collectorDependencyKindLabel(item.kind);
  const badge = document.createElement("span");
  badge.className = "collector-issue-badge";
  badge.textContent = collectorDependencyStatusLabel(item);
  title.append(kind, badge);
  const reason = document.createElement("div");
  reason.className = "collector-issue-reason";
  const references = Array.isArray(item.references) ? item.references.length : 0;
  reason.textContent = potential
    ? `分析发现这个依赖不可用；选择保留时会阻塞上传${references ? `，影响 ${references} 处草稿引用` : ""}。`
    : `${item.decision_reason || "该依赖当前无法随母版迁移"}${references ? `；影响 ${references} 处草稿引用` : ""}。`;
  const path = document.createElement("div");
  path.className = "collector-issue-path";
  path.textContent = item.original_path || item.path || "草稿中没有记录具体路径";
  path.title = path.textContent;
  const advice = document.createElement("div");
  advice.className = "collector-issue-advice";
  advice.textContent = `处理建议：${collectorDependencyAdvice(item)}`;
  node.append(title, reason, path, advice);
  return node;
}

function renderCollectorDependencyProblems(dependencies) {
  const container = $("collectorDependencyProblems");
  const problems = dependencies.filter((item) => ["missing", "external"].includes(item.status));
  container.replaceChildren();
  container.classList.toggle("hidden", !problems.length);
  if (!problems.length) return;
  const heading = document.createElement("div");
  heading.className = "collector-detail-heading";
  const title = document.createElement("strong");
  title.textContent = "分析发现的潜在依赖问题";
  const meta = document.createElement("span");
  meta.textContent = `${problems.length} 项 · 是否阻塞取决于下方保留策略`;
  heading.append(title, meta);
  const list = document.createElement("div");
  list.className = "collector-issue-list";
  list.replaceChildren(...problems.map((item) => collectorIssueNode(item, { potential: true })));
  container.append(heading, list);
}

function renderCollectorPlanDetails(plan) {
  const container = $("collectorPlanDetails");
  const list = $("collectorPlanIssueList");
  const dependencies = Array.isArray(plan.dependencies) ? plan.dependencies : [];
  const blocked = dependencies.filter((item) => ["blocked_missing", "blocked_external"].includes(item.decision));
  const summary = plan.summary || {};
  container.classList.remove("hidden");
  container.classList.toggle("ready", !blocked.length);
  list.replaceChildren();
  if (blocked.length) {
    $("collectorPlanDetailsTitle").textContent = "具体阻塞问题";
    $("collectorPlanDetailsMeta").textContent = `${blocked.length} 项 · 处理后重新生成上传清单`;
    list.replaceChildren(...blocked.map((item) => collectorIssueNode(item)));
    return;
  }
  $("collectorPlanDetailsTitle").textContent = "上传清单可以继续";
  $("collectorPlanDetailsMeta").textContent = "没有阻塞问题";
  const item = document.createElement("article");
  item.className = "collector-issue";
  const title = document.createElement("div");
  title.className = "collector-issue-title";
  const strong = document.createElement("strong");
  strong.textContent = "依赖处理完成";
  title.append(strong);
  const detail = document.createElement("div");
  detail.className = "collector-issue-reason";
  detail.textContent = `随母版上传 ${summary.upload_count || 0} 项，素材库复用 ${summary.reuse_library_count || 0} 项，因替换或删除跳过 ${summary.skipped_count || 0} 项。`;
  item.append(title, detail);
  list.append(item);
}

function renderCollectorReport(report) {
  const draft = report.draft || {};
  const summary = report.summary || {};
  const counts = summary.slot_counts || {};
  const textSlots = Array.isArray(report.editable_slots?.texts) ? report.editable_slots.texts : [];
  const flowerTextCount = textSlots.filter((item) => item.has_flower_text).length;
  const ordinaryTextCount = Math.max(0, Number(counts.texts || 0) - flowerTextCount);
  const canvas = draft.canvas || {};
  const dependencies = report.dependencies || [];
  $("collectorAnalysisTitle").textContent = draft.name || "草稿分析";
  $("collectorAnalysisMeta").textContent = `${formatDuration(draft.duration_us)} · ${canvas.width || 0} × ${canvas.height || 0} · ${draft.track_count || 0} 条轨道${draft.was_decrypted ? " · 已自动解密副本" : ""}`;
  const packaging = $("collectorPackagingStatus");
  packaging.textContent = summary.ready_for_packaging ? "依赖检查通过" : "存在必要素材缺失";
  packaging.className = `inline-status ${summary.ready_for_packaging ? "ok" : "bad"}`;
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  $("collectorWarnings").replaceChildren(...warnings.map((warning) => {
    const item = document.createElement("div");
    item.textContent = warning;
    return item;
  }));
  $("collectorWarnings").classList.toggle("hidden", !warnings.length);
  renderCollectorDependencyProblems(dependencies);
  $("collectorMetrics").replaceChildren(
    collectorMetric("BGM / 音效", counts.audio || 0),
    collectorMetric("视频特效", counts.video_effects || 0),
    collectorMetric("普通文字", ordinaryTextCount),
    collectorMetric("花字", flowerTextCount),
    collectorMetric("复合文字模板", counts.text_templates || 0),
    collectorMetric("本地素材依赖", dependencies.length),
  );
  const extractableFontCount = dependencies.filter(
    (item) => item.kind === "font" && item.exists && item.status !== "central_library",
  ).length;
  $("extractCollectorFontsBtn").disabled = extractableFontCount === 0;
  $("extractCollectorFontsBtn").textContent = extractableFontCount
    ? `提取本草稿字体（${extractableFontCount}）`
    : "字体已在素材库或没有字体";
  resetCollectorPolicies();
  invalidateCollectorPlan();
  $("collectorAnalysis").classList.remove("hidden");
}

async function analyzeCollectorDraft() {
  const draftPath = $("collectorDraftSelect").value;
  if (!draftPath) {
    setCollectorMessage("请选择一个本机草稿。", true);
    return;
  }
  const button = $("analyzeCollectorDraftBtn");
  button.disabled = true;
  button.textContent = "正在分析...";
  setCollectorMessage();
  try {
    const report = await collectorFetch("/api/drafts/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_dir: draftPath, hash_mode: "small_files" }),
    });
    state.collector.report = report;
    renderCollectorReport(report);
    setCollectorMessage("草稿分析完成，请确认保留策略后生成上传清单。", false);
  } catch (error) {
    setCollectorMessage(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "分析草稿";
  }
}

async function buildCollectorPlan() {
  const reportId = state.collector.report?.report_id;
  if (!reportId) return;
  const button = $("buildCollectorPlanBtn");
  button.disabled = true;
  try {
    const plan = await collectorFetch("/api/upload-plans", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        report_id: reportId,
        policies: {
          audio: $("collectorPolicyAudio").value,
          video_effects: $("collectorPolicyEffects").value,
          text_style: $("collectorPolicyTextStyle").value,
          text_effects: $("collectorPolicyTextEffects").value,
          text_templates: "keep",
        },
      }),
    });
    state.collector.plan = plan;
    const summary = plan.summary || {};
    $("collectorPlanSummary").replaceChildren(
      collectorMetric("需要上传", `${summary.upload_count || 0} 项`),
      collectorMetric("预计大小", formatBytes(summary.upload_size_bytes || 0)),
      collectorMetric("素材库复用", `${summary.reuse_library_count || 0} 项`),
      collectorMetric("阻塞问题", `${summary.blocked_count || 0} 项`),
    );
    $("collectorPlanSummary").classList.remove("hidden");
    renderCollectorPlanDetails(plan);
    $("uploadCollectorPlanBtn").disabled = !summary.ready_for_upload;
    setCollectorMessage(summary.ready_for_upload ? "上传清单已生成，可以上传母版。" : "上传清单存在阻塞问题，请检查缺失素材。", !summary.ready_for_upload);
  } catch (error) {
    setCollectorMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function refreshMothers(preferredTemplateId = "") {
  state.templates = await apiFetch("/api/templates");
  fillMothers();
  if (preferredTemplateId && state.templates.some((item) => item.template_id === preferredTemplateId)) {
    $("motherSelect").value = preferredTemplateId;
  }
  updateMotherMeta();
}

async function uploadCollectorPlan() {
  const planId = state.collector.plan?.plan_id;
  if (!planId) return;
  const button = $("uploadCollectorPlanBtn");
  button.disabled = true;
  button.textContent = "正在打包并上传...";
  try {
    const result = await collectorFetch(`/api/upload-plans/${encodeURIComponent(planId)}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: window.location.origin,
        template_name: state.collector.report?.draft?.name || "",
      }),
    });
    const template = result.server_result?.template || {};
    await refreshMothers(template.template_id || "");
    setCollectorMessage(`上传完成，已自动选中母版：${template.name || state.collector.report?.draft?.name || "未命名"}。母版将在 48 小时后自动清理。`);
    $("collectorPanel").open = false;
    const motherInput = document.querySelector('input[name="sourceMode"][value="mother"]');
    if (motherInput) motherInput.checked = true;
    setWorkspaceMode("generate");
  } catch (error) {
    setCollectorMessage(error.message, true);
  } finally {
    button.textContent = "上传并使用这个母版";
    button.disabled = !(state.collector.plan?.summary?.ready_for_upload);
  }
}

async function extractCollectorFonts() {
  const draftPath = $("collectorDraftSelect").value;
  if (!draftPath) return;
  const button = $("extractCollectorFontsBtn");
  button.disabled = true;
  try {
    const result = await collectorFetch("/api/drafts/extract-fonts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_dir: draftPath }),
    });
    setCollectorMessage(`字体提取完成：新复制 ${result.copied_count || 0} 个，已存在 ${result.existing_count || 0} 个，本机缺失 ${result.missing_count || 0} 个。`);
  } catch (error) {
    setCollectorMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function sourceMode() {
  return document.querySelector('input[name="sourceMode"]:checked')?.value || "mother";
}

function workspaceMode() {
  return document.querySelector('input[name="workspaceMode"]:checked')?.value || "upload";
}

function processingMode() {
  return document.querySelector('input[name="processingMode"]:checked')?.value || "shared";
}

function isLocalMode() {
  return processingMode() === "local" && state.localFileAccess;
}

function normalizedServerUrl(value) {
  const text = String(value || "").trim().replace(/\/+$/, "");
  let parsed;
  try { parsed = new URL(text); } catch { throw new Error("工作台地址不正确，请完整粘贴负责人发来的地址"); }
  if (!["http:", "https:"].includes(parsed.protocol) || !["/", "/app"].includes(parsed.pathname) || parsed.search || parsed.hash) {
    throw new Error("工作台地址不正确，请完整粘贴负责人发来的地址");
  }
  return parsed.origin;
}

function localWorkspaceUrl() {
  return "http://127.0.0.1:8010/app";
}

function saveSharedWorkspaceUrls() {
  try {
    window.localStorage.setItem(SHARED_WORKSPACES_STORAGE_KEY, JSON.stringify(state.sharedWorkspaceUrls));
    window.localStorage.removeItem(SHARED_WORKSPACE_STORAGE_KEY);
  } catch { /* Browser storage may be disabled. */ }
}

async function probeSharedWorkspace(serverUrl) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch(`${serverUrl}/api/health`, {
      cache: "no-store",
      mode: "cors",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const health = await response.json();
    const activeJobs = Number(health.active_jobs || 0);
    return {
      url: serverUrl,
      online: Boolean(health.ok),
      activeJobs,
      pendingJobs: Number(health.pending_jobs || 0),
      status: activeJobs ? "busy" : "idle",
    };
  } catch {
    return { url: serverUrl, online: false, activeJobs: Number.MAX_SAFE_INTEGER, pendingJobs: 0, status: "offline" };
  } finally {
    window.clearTimeout(timeout);
  }
}

function renderSharedWorkspaceStatuses() {
  const container = $("sharedWorkspaceStatusList");
  container.replaceChildren();
  if (!state.sharedWorkspaceUrls.length) {
    const empty = document.createElement("span");
    empty.className = "shared-workspace-status offline";
    empty.textContent = "尚未添加其他工作台";
    container.appendChild(empty);
    return;
  }
  state.sharedWorkspaceStatuses.forEach((item, index) => {
    const badge = document.createElement("span");
    badge.className = `shared-workspace-status ${item.status}`;
    const label = item.online
      ? (item.activeJobs ? `忙碌 · ${item.activeJobs} 个任务` : "空闲")
      : "离线";
    badge.textContent = `工作台 ${index + 1}：${label}`;
    badge.title = item.url;
    container.appendChild(badge);
  });
}

async function refreshSharedWorkspaceStatuses() {
  state.sharedWorkspaceStatuses = await Promise.all(state.sharedWorkspaceUrls.map(probeSharedWorkspace));
  renderSharedWorkspaceStatuses();
  return state.sharedWorkspaceStatuses;
}

async function configureSharedWorkspace() {
  const supplied = window.prompt(
    "请粘贴负责人发来的工作台地址，每行一个：",
    state.sharedWorkspaceUrls.map((url) => `${url}/app`).join("\n"),
  );
  if (supplied === null) return false;
  const values = supplied.split(/[\n,;，；]+/).map((item) => item.trim()).filter(Boolean);
  state.sharedWorkspaceUrls = [...new Set(values.map(normalizedServerUrl))];
  saveSharedWorkspaceUrls();
  await refreshSharedWorkspaceStatuses();
  setMessage(`已保存 ${state.sharedWorkspaceUrls.length} 个其他工作台。`);
  return true;
}

async function openSharedMachine() {
  try {
    if (!state.sharedWorkspaceUrls.length) {
      if (!await configureSharedWorkspace()) {
        throw new Error("尚未连接其他工作台");
      }
    }
    const statuses = await refreshSharedWorkspaceStatuses();
    const available = statuses
      .filter((item) => item.online)
      .sort((left, right) => left.activeJobs - right.activeJobs || left.pendingJobs - right.pendingJobs);
    if (!available.length) throw new Error("其他工作台当前都离线，请确认公用电脑已经启动");
    const serverUrl = available[0].url;
    if (serverUrl === window.location.origin) {
      throw new Error("当前已经在这个工作台上");
    }
    window.location.assign(`${serverUrl}/app`);
  } catch (error) {
    const expected = state.localFileAccess ? "local" : "shared";
    const input = document.querySelector(`input[name="processingMode"][value="${expected}"]`);
    if (input) input.checked = true;
    setMessage(error.message);
  }
}

async function handleProcessingModeChange(event) {
  if (event.target.value === "shared" && state.localFileAccess) {
    await openSharedMachine();
    return;
  }
  if (event.target.value === "local" && !state.localFileAccess) {
    window.location.assign("/api/auth/handoff-to?target=local&next=/app");
    return;
  }
  updateProcessingModeUi();
}

function updateProcessingModeUi() {
  const local = isLocalMode();
  $("publicVideoPicker").classList.toggle("hidden", local);
  $("localVideoPicker").classList.toggle("hidden", !local);
  $("localOutputPanel").classList.toggle("hidden", !local);
  $("videoSourceModeLabel").textContent = local ? "选择本机视频" : "上传视频";
  $("sourceModeHelp").textContent = local
    ? "本机视频不上传，直接读取原文件并控制这台电脑的剪映。"
    : "上传视频或母版，由当前公用处理服务排队生成。";
  updateSourcePreview();
  if (state.lastBatch) renderResults(state.lastBatch);
}

function updateWorkspaceUi() {
  const mode = workspaceMode();
  const generating = mode === "generate";
  const excel = mode === "excel";
  $("uploadDraftWorkspace").classList.toggle("hidden", mode !== "upload");
  $("personalAssetsWorkspace").classList.toggle("hidden", mode !== "assets");
  $("excelBatchWorkspace").classList.toggle("hidden", !excel);
  $("digitalHumanWorkspace").classList.toggle("hidden", mode !== "digital_human");
  document.querySelectorAll(".generation-workspace").forEach((section) => {
    if (section.id === "resultsSection") {
      const hasResults = Boolean(state.lastBatch?.jobs?.length);
      section.classList.toggle("hidden", (!generating && !excel) || !hasResults);
    } else {
      section.classList.toggle("hidden", !generating);
    }
  });
  if (generating) updateSourceUi();
  if (mode === "assets" && !state.personalAssets.loaded && !state.personalAssets.loading) {
    void loadPersonalAssets();
  }
  if (mode === "digital_human") {
    void refreshDigitalHumanTasks();
    if (!state.digitalHumanPollTimer) {
      state.digitalHumanPollTimer = window.setInterval(() => {
        if (workspaceMode() === "digital_human") void refreshDigitalHumanTasks({ quiet: true });
      }, 15000);
    }
  } else if (state.digitalHumanPollTimer) {
    window.clearInterval(state.digitalHumanPollTimer);
    state.digitalHumanPollTimer = null;
  }
}

function digitalHumanStatusLabel(task) {
  return {
    AUTO_READY: "可自动后期",
    MANUAL_READY: "等待人工粗剪",
    WAITING_VIDEO: "数字人生成中",
    PARTIAL_FAILED: "部分片段失败",
    FAILED: "生成失败",
    CANCELLED: "已取消",
  }[task.status] || task.status || "等待处理";
}

function digitalHumanReason(task) {
  if (task.status === "AUTO_READY") return "单条文本语音视频，可带精确字幕直接导入。";
  if (task.manual_edit_reason === "UPLOADED_AUDIO") return "上传音频任务，请下载原始片段并人工粗剪。";
  if (task.manual_edit_reason === "SEGMENTED_VIDEO") return "多片段图生视频，请人工检查衔接并粗剪。";
  if (task.status === "WAITING_VIDEO") return "任务尚在生成，完成后这里会自动更新。";
  return "请检查数字人网站中的任务状态。";
}

function renderDigitalHumanTasks() {
  const list = $("digitalHumanTaskList");
  list.replaceChildren();
  if (!state.digitalHumanTasks.length) {
    const empty = document.createElement("div");
    empty.className = "source-note";
    empty.textContent = "当前账号还没有数字人后处理任务。";
    list.append(empty);
    return;
  }
  state.digitalHumanTasks.forEach((task) => {
    const card = document.createElement("article");
    card.className = "digital-human-task-card";
    const heading = document.createElement("div");
    heading.className = "digital-human-task-heading";
    const title = document.createElement("div");
    const h3 = document.createElement("h3");
    h3.textContent = `${task.row_key || "数字人任务"} · ${task.batch_name || "视频生成任务"}`;
    const meta = document.createElement("p");
    meta.textContent = `${task.input_mode === "text" ? "文本生成语音" : "上传音频"} · ${task.source?.videos?.length || 0} 个视频片段`;
    title.append(h3, meta);
    const badge = document.createElement("span");
    badge.className = `digital-human-badge${task.status === "AUTO_READY" ? " auto" : ""}`;
    badge.textContent = digitalHumanStatusLabel(task);
    heading.append(title, badge);
    const reason = document.createElement("div");
    reason.className = "digital-human-caption-note";
    reason.textContent = digitalHumanReason(task);
    const actions = document.createElement("div");
    actions.className = "digital-human-task-actions";
    if (task.status === "AUTO_READY") {
      const importButton = document.createElement("button");
      importButton.type = "button";
      importButton.className = "primary";
      importButton.textContent = "一键导入工作台";
      importButton.addEventListener("click", () => importDigitalHumanTask(task, importButton));
      actions.append(importButton);
    }
    (task.source?.videos || []).forEach((video) => {
      if (video.status !== "SUCCESS") return;
      const link = document.createElement("a");
      link.href = `/api/digital-human/tasks/${encodeURIComponent(task.item_id)}/videos/${Number(video.index)}`;
      link.textContent = `下载原始片段 ${video.index}`;
      actions.append(link);
    });
    card.append(heading, reason, actions);
    list.append(card);
  });
}

async function refreshDigitalHumanTasks({ quiet = false } = {}) {
  const button = $("refreshDigitalHumanTasksBtn");
  if (!quiet) {
    button.disabled = true;
    $("digitalHumanTaskMessage").textContent = "正在从数字人网站读取当前账号的任务...";
  }
  try {
    const result = await apiFetch("/api/digital-human/tasks?limit=50");
    state.digitalHumanTasks = Array.isArray(result.tasks) ? result.tasks : [];
    renderDigitalHumanTasks();
    $("digitalHumanTaskMessage").textContent = `已连接 ${result.source_url}，读取到 ${state.digitalHumanTasks.length} 个任务。`;
    $("digitalHumanTaskMessage").classList.remove("error");
  } catch (error) {
    $("digitalHumanTaskMessage").textContent = `读取失败：${error.message}`;
    $("digitalHumanTaskMessage").classList.add("error");
  } finally {
    button.disabled = false;
  }
}

async function importDigitalHumanTask(task, button) {
  button.disabled = true;
  button.textContent = "正在下载视频...";
  try {
    const result = await apiFetch(`/api/digital-human/tasks/${encodeURIComponent(task.item_id)}/import`, { method: "POST" });
    state.localVideo = result.media;
    state.digitalHumanSourceItemId = task.item_id;
    state.digitalHumanCaptionCues = Array.isArray(result.captions?.cues) ? result.captions.cues : [];
    $("localVideoFileName").textContent = `${result.media.filename} · ${formatBytes(result.media.size)}`;
    const workspaceInput = document.querySelector('input[name="workspaceMode"][value="generate"]');
    const sourceInput = document.querySelector('input[name="sourceMode"][value="video"]');
    const localInput = document.querySelector('input[name="processingMode"][value="local"]');
    if (workspaceInput) workspaceInput.checked = true;
    if (sourceInput) sourceInput.checked = true;
    if (localInput && !localInput.disabled) localInput.checked = true;
    if (result.captions?.text) {
      $("captionText").value = result.captions.text;
      $("useCaptions").checked = true;
    }
    updateWorkspaceUi();
    updateSourceUi();
    updateCounts();
    updateSourcePreview();
    setMessage(state.digitalHumanCaptionCues.length
      ? `已导入数字人视频和 ${state.digitalHumanCaptionCues.length} 条精确字幕，请选择 BGM、字幕字体和导出目录。`
      : "已导入数字人视频，请继续选择 BGM 和导出设置。", false);
  } catch (error) {
    window.alert(`导入失败：${error.message}`);
    button.disabled = false;
    button.textContent = "一键导入工作台";
  }
}

function setWorkspaceMode(mode) {
  const input = document.querySelector(`input[name="workspaceMode"][value="${mode}"]`);
  if (input) input.checked = true;
  updateWorkspaceUi();
}

function selectedMother() {
  return state.templates.find((item) => item.template_id === $("motherSelect").value) || null;
}

function availableMothers() {
  return state.templates.filter(
    (item) => item.import_info?.source === "local_collector" && item.enabled !== false && !item.deleted,
  );
}

function formatDuration(microseconds) {
  const seconds = Math.max(0, Math.round(Number(microseconds || 0) / 1000000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function validItems(items) {
  return items.filter((item) => !item.error && item.enabled !== false && !item.deleted);
}

function validFonts() {
  return state.fonts.filter(
    (item) => item.available && item.resource_id && item.path && item.enabled !== false && !item.deleted,
  );
}

function assetsInCategory() {
  const categoryId = $("audioCategory").value;
  return state.audio.assets.filter(
    (item) => item.available
      && item.enabled !== false
      && !item.deleted
      && Array.isArray(item.category_ids)
      && item.category_ids.includes(categoryId),
  );
}

function setMessage(message = "") {
  $("validationMessage").textContent = message;
  $("validationMessage").classList.toggle("hidden", !message);
}

function syncMotherRestrictions() {
  const mother = sourceMode() === "mother";
  $("flowerRow").classList.toggle("hidden", mother);
  $("captionSettings").classList.toggle("hidden", mother);
  $("captionModeTitle").textContent = mother ? "替换字幕字体" : "添加字幕";
  $("textStyleNote").textContent = mother
    ? "只替换普通字幕的字体；字号、颜色、位置、内容和时间全部保持原样。"
    : "填写长文案后自动切分；字体可参与组合，其他字幕参数固定使用当前设置。";
  $("useAudio").disabled = false;
  $("useEffects").disabled = false;
  $("useCaptions").disabled = false;
}

function updateSourceUi() {
  const mother = sourceMode() === "mother";
  $("videoSourcePanel").classList.toggle("hidden", mother);
  $("motherSourcePanel").classList.toggle("hidden", !mother);
  syncMotherRestrictions();
  updateProcessingModeUi();
  updateSourcePreview();
  updateCounts();
}

function updateSourcePreview() {
  const video = $("sourcePreview");
  if (state.videoUrl) URL.revokeObjectURL(state.videoUrl);
  state.videoUrl = "";
  let src = "";
  if (sourceMode() === "mother") {
    const mother = selectedMother();
    if (mother) src = `/api/templates/${encodeURIComponent(mother.template_id)}/preview-video`;
  } else {
    if (isLocalMode()) {
      src = state.localVideo?.preview_url || "";
    } else {
      const file = $("videoFile").files[0];
      if (file) {
        state.videoUrl = URL.createObjectURL(file);
        src = state.videoUrl;
      }
    }
  }
  if (src) {
    video.src = src;
    video.load();
    $("previewEmpty").classList.add("hidden");
  } else {
    video.removeAttribute("src");
    video.load();
    $("previewEmpty").classList.remove("hidden");
  }
}

function syncSingleCoverTimeFromPreview(event) {
  if (!$("useCover").checked) return;
  const preview = $("sourcePreview");
  if (!preview.currentSrc || !Number.isFinite(preview.currentTime)) return;
  $("coverFrameTimeSeconds").value = preview.currentTime.toFixed(2);
  if (event?.type === "seeked") updateCounts();
}

function fillMothers() {
  const select = $("motherSelect");
  select.replaceChildren();
  const mothers = availableMothers();
  if (!mothers.length) {
    select.append(new Option("还没有剪辑母版，请先到“上传草稿”中导入", ""));
  } else {
    mothers.forEach((item) => {
      const expiresAt = item.expires_at ? new Date(item.expires_at) : null;
      const expiry = expiresAt && !Number.isNaN(expiresAt.getTime())
        ? ` · ${new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(expiresAt)} 到期`
        : "";
      select.append(new Option(`${item.name || item.template_id}${expiry}`, item.template_id));
    });
  }
  updateMotherMeta();
}

function updateMotherMeta() {
  const mother = selectedMother();
  if (!mother) {
    $("motherMeta").textContent = "请先选择已有母版；没有母版时可切换到“上传草稿”导入本机剪映草稿。";
  } else {
    const summary = mother.summary || {};
    const expiresAt = mother.expires_at ? new Date(mother.expires_at) : null;
    const expiry = expiresAt && !Number.isNaN(expiresAt.getTime())
      ? ` · 将在 ${new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(expiresAt)} 自动清理`
      : "";
    $("motherMeta").textContent = `${formatDuration(mother.duration_us)} · ${mother.track_count || 0} 条轨道 · ${summary.text_count || 0} 段普通文字${expiry}；复合文字模板保持原样。`;
  }
  syncMotherRestrictions();
  updateSourcePreview();
  updateCounts();
}

function fillAudioCategories() {
  const select = $("audioCategory");
  select.replaceChildren();
  const categories = state.audio.categories.filter((item) => Number(item.asset_count || 0) > 0);
  if (!categories.length) select.append(new Option("音乐库还没有可用分类", ""));
  categories.forEach((item) => select.append(new Option(`${item.name}（${item.asset_count}）`, item.id)));
}

function fillFonts() {
  const select = $("fontSelection");
  select.replaceChildren();
  const fonts = validFonts();
  if (!fonts.length) {
    select.append(new Option("字体库还没有可用字体", ""));
    return;
  }
  select.append(new Option(`全部字体参与组合（${fonts.length}）`, "__all__"));
  fonts.forEach((font) => select.append(new Option(font.name || font.identity, font.identity)));
  updateFontPreview();
}

function fillCaptionStyles() {
  const select = $("captionStylePreset");
  select.replaceChildren();
  select.append(new Option("自定义字幕参数", ""));
  validItems(state.textStyles).forEach((style) => {
    select.append(new Option(style.name || style.path, style.path));
  });
}

function selectedFonts() {
  const fonts = validFonts();
  const identity = $("fontSelection").value;
  if (identity === "__all__") return fonts;
  return fonts.filter((font) => font.identity === identity);
}

async function updateFontPreview() {
  const identity = $("fontSelection").value;
  const font = validFonts().find((item) => item.identity === identity);
  const sample = $("fontPreviewSample");
  const name = $("fontPreviewName");
  if (!font) {
    name.textContent = identity === "__all__" ? "选择单个字体后可查看真实效果" : "字体预览不可用";
    sample.style.fontFamily = "inherit";
    return;
  }

  let style = $("fontPreviewStyle");
  if (!style) {
    style = document.createElement("style");
    style.id = "fontPreviewStyle";
    document.head.append(style);
  }
  const previewUrl = `/api/assets/fonts/${encodeURIComponent(font.identity)}/file`;
  style.textContent = `@font-face { font-family: "JydSelectedFontPreview"; src: url("${previewUrl}"); font-display: swap; }`;
  name.textContent = `${font.name} · 字体预览`;
  sample.style.fontFamily = '"JydSelectedFontPreview", sans-serif';
  try {
    await document.fonts.load('24px "JydSelectedFontPreview"');
  } catch {
    name.textContent = `${font.name} · 预览加载失败`;
  }
}

async function applyCoverPreviewFont(font, styleId, familyName, targets) {
  for (const target of targets) target.style.fontFamily = "inherit";
  if (!font) return;
  let style = $(styleId);
  if (!style) {
    style = document.createElement("style");
    style.id = styleId;
    document.head.append(style);
  }
  const previewUrl = `/api/assets/fonts/${encodeURIComponent(font.identity)}/file`;
  style.textContent = `@font-face { font-family: "${familyName}"; src: url("${previewUrl}"); font-display: swap; }`;
  for (const target of targets) target.style.fontFamily = `"${familyName}", sans-serif`;
  try { await document.fonts.load(`24px "${familyName}"`); } catch { /* Keep the fallback font. */ }
}

function normalizedCoverConfig(config = {}, enabled = true) {
  return {
    enabled: Boolean(config.enabled ?? enabled),
    frameTimeSeconds: Number(config.frameTimeSeconds ?? 0),
    textLine1: String(config.textLine1 ?? "默认文本"),
    textLine2: String(config.textLine2 ?? "默认文本"),
    frameScale: 1,
    frameOffsetX: Number(config.frameOffsetX ?? 0),
    frameOffsetY: Number(config.frameOffsetY ?? 0),
    fontIdentity: String(config.fontIdentity ?? ""),
    overlayX: Number(config.overlayX ?? 0.5),
    overlayY: Number(config.overlayY ?? 0.68),
    overlayWidth: Number(config.overlayWidth ?? 1),
    overlayHeight: Number(config.overlayHeight ?? 0.36),
    overlayAlpha: Number(config.overlayAlpha ?? 0.5),
    line1X: Number(config.line1X ?? 0),
    line1Y: Number(config.line1Y ?? -0.28),
    line2X: Number(config.line2X ?? 0),
    line2Y: Number(config.line2Y ?? -0.55),
    line1Size: Number(config.line1Size ?? 12),
    line2Size: Number(config.line2Size ?? 12),
    line1Color: String(config.line1Color ?? "#FFFFFF"),
    line2Color: String(config.line2Color ?? "#FFFFFF"),
  };
}

function singleCoverConfig() {
  const config = normalizedCoverConfig(state.singleCover || {}, $("useCover").checked);
  config.enabled = $("useCover").checked;
  config.frameTimeSeconds = Number($("coverFrameTimeSeconds").value || 0);
  config.textLine1 = $("coverTextLine1").value;
  config.textLine2 = $("coverTextLine2").value;
  return config;
}

function coverJobConfig(config) {
  const value = normalizedCoverConfig(config, true);
  const result = {
    enabled: true,
    frame_time_seconds: value.frameTimeSeconds,
    frame_source: "preview_material",
    frame_count: 3,
    text_line_1: value.textLine1.trim(),
    text_line_2: value.textLine2.trim(),
    frame_scale: 1,
    frame_offset_x: value.frameOffsetX,
    frame_offset_y: value.frameOffsetY,
    overlay_x_ratio: value.overlayX,
    overlay_y_ratio: value.overlayY,
    overlay_width_ratio: value.overlayWidth,
    overlay_height_ratio: value.overlayHeight,
    overlay_alpha: value.overlayAlpha,
    line_1_x: value.line1X,
    line_1_y: value.line1Y,
    line_2_x: value.line2X,
    line_2_y: value.line2Y,
    line_1_size: value.line1Size,
    line_2_size: value.line2Size,
    line_1_color: value.line1Color,
    line_2_color: value.line2Color,
  };
  const font = validFonts().find((item) => item.identity === value.fontIdentity);
  if (font) {
    result.font = {
      font_id: font.resource_id,
      font_path: font.path,
      font_title: font.name,
    };
  }
  return result;
}

function applyCoverPreview(config, { video = null, shade, line1, line2 } = {}) {
  const value = normalizedCoverConfig(config, true);
  if (video) {
    video.style.transform = "none";
    video.style.objectPosition = `${(value.frameOffsetX + 1) * 50}% ${(1 - value.frameOffsetY) * 50}%`;
  }
  shade.style.left = `${value.overlayX * 100}%`;
  shade.style.top = `${value.overlayY * 100}%`;
  shade.style.width = `${value.overlayWidth * 100}%`;
  shade.style.height = `${value.overlayHeight * 100}%`;
  shade.style.background = `rgba(0, 0, 0, ${value.overlayAlpha})`;
  shade.style.transform = "translate(-50%, -50%)";
  for (const [element, text, x, y, size, color] of [
    [line1, value.textLine1, value.line1X, value.line1Y, value.line1Size, value.line1Color],
    [line2, value.textLine2, value.line2X, value.line2Y, value.line2Size, value.line2Color],
  ]) {
    element.textContent = text.trim() || "默认文本";
    element.style.left = `${(x + 1) * 50}%`;
    element.style.top = `${(1 - y) * 50}%`;
    element.style.fontSize = `${size * 2}px`;
    element.style.color = color;
  }
}

function updateSingleCoverPreview() {
  const config = singleCoverConfig();
  $("singleCoverLivePreview").classList.toggle("hidden", !config.enabled);
  applyCoverPreview(config, {
    shade: $("singleCoverPreviewShade"),
    line1: $("singleCoverPreviewText1"),
    line2: $("singleCoverPreviewText2"),
  });
  const font = validFonts().find((item) => item.identity === config.fontIdentity)
    || ($("useCaptions").checked ? selectedFonts()[0] : null);
  void applyCoverPreviewFont(
    font,
    "singleCoverPreviewFontStyle",
    "JydSingleCoverPreviewFont",
    [$("singleCoverPreviewText1"), $("singleCoverPreviewText2")],
  );
}

function updateBatchCoverPreview() {
  syncRangeNumberInputs();
  const row = activeBatchCoverRow();
  const config = coverEditorConfigFromControls();
  state.coverEditorConfig = config;
  applyCoverPreview(config, {
    video: $("batchCoverPreview"),
    shade: $("batchCoverPreviewShade"),
    line1: $("batchCoverPreviewText1"),
    line2: $("batchCoverPreviewText2"),
  });
  $("batchCoverFrameOffsetXValue").textContent = String(Math.round(config.frameOffsetX * 100));
  $("batchCoverFrameOffsetYValue").textContent = String(Math.round(config.frameOffsetY * 100));
  $("batchCoverOverlayYValue").textContent = String(Math.round((config.overlayY - 0.5) * 200));
  $("batchCoverOverlayAlphaValue").textContent = `${Math.round(config.overlayAlpha * 100)}%`;
  const selectedFont = validFonts().find((item) => item.identity === config.fontIdentity);
  const font = selectedFont || (row
    ? excelSelectionCandidates("font", row.font)[0]
    : ($("useCaptions").checked ? selectedFonts()[0] : null));
  void applyCoverPreviewFont(
    font,
    "batchCoverPreviewFontStyle",
    "JydBatchCoverPreviewFont",
    [$("batchCoverPreviewText1"), $("batchCoverPreviewText2")],
  );
}

function fullCombinationCounts(candidateCounts) {
  const total = candidateCounts.reduce((value, count) => value * Math.max(1, count), 1);
  return { rawTotal: total, total, removed: 0 };
}

function selectedVariantRatios() {
  const ratios = [];
  if ($("variantRatioSquare").checked) ratios.push("1:1");
  if ($("variantRatioThreeFour").checked) ratios.push("3:4");
  return ratios;
}

function updateVisualCropPreview() {
  const source = $("sourcePreview");
  const preview = $("visualCropPreview");
  const src = source.currentSrc || source.getAttribute("src") || "";
  if (src && preview.getAttribute("src") !== src) {
    preview.src = src;
    preview.load();
  }
  if (src && Number.isFinite(source.currentTime) && preview.readyState >= 1) {
    const targetTime = Math.min(source.currentTime, Math.max(0, (preview.duration || source.currentTime + 1) - 0.01));
    if (Math.abs(preview.currentTime - targetTime) > 0.08) preview.currentTime = targetTime;
  }
  const ratio = $("cropPreviewRatio").value || "1:1";
  const zoom = Number($("cropZoom").value || 100) / 100;
  const offset = Number($("cropOffsetY").value || 0) / 100;
  const baseHeight = ratio === "3:4" ? 75 : 56.25;
  const height = baseHeight / zoom;
  const width = 100 / zoom;
  const availableY = 100 - height;
  const top = Math.max(0, Math.min(availableY, availableY / 2 + offset * availableY));
  const guide = $("visualCropGuide");
  guide.style.width = `${width}%`;
  guide.style.height = `${height}%`;
  guide.style.left = `${(100 - width) / 2}%`;
  guide.style.top = `${top}%`;
  $("visualCropGuideLabel").textContent = `${ratio} 裁剪区域`;
  $("cropOffsetYValue").textContent = String(Number($("cropOffsetY").value || 0));
  $("cropZoomValue").textContent = `${Math.round(zoom * 100)}%`;
}

function syncRangeNumberInputs() {
  document.querySelectorAll('input[type="range"][data-number-input-id]').forEach((range) => {
    const numberInput = document.getElementById(range.dataset.numberInputId);
    if (numberInput && numberInput !== document.activeElement) numberInput.value = range.value;
    if (numberInput) numberInput.disabled = range.disabled;
  });
}

function enhanceRangeInputs() {
  document.querySelectorAll('input[type="range"]').forEach((range) => {
    if (!range.id || range.dataset.numberInputId) return;
    const wrapper = document.createElement("span");
    wrapper.className = "range-with-number";
    const numberInput = document.createElement("input");
    numberInput.type = "number";
    numberInput.id = `${range.id}Number`;
    numberInput.className = "range-number-input";
    numberInput.min = range.min;
    numberInput.max = range.max;
    numberInput.step = range.step || "1";
    numberInput.value = range.value;
    numberInput.setAttribute("aria-label", `${range.closest("label")?.querySelector("span")?.textContent?.trim() || range.id}手动输入`);
    range.dataset.numberInputId = numberInput.id;
    range.parentNode.insertBefore(wrapper, range);
    wrapper.append(range, numberInput);
    range.addEventListener("input", () => {
      numberInput.value = range.value;
    });
    const applyNumberValue = (clamp) => {
      if (numberInput.value === "") return;
      let value = Number(numberInput.value);
      if (!Number.isFinite(value)) return;
      const minimum = Number(range.min);
      const maximum = Number(range.max);
      if (clamp) {
        if (Number.isFinite(minimum)) value = Math.max(minimum, value);
        if (Number.isFinite(maximum)) value = Math.min(maximum, value);
        numberInput.value = String(value);
      } else if ((Number.isFinite(minimum) && value < minimum) || (Number.isFinite(maximum) && value > maximum)) {
        return;
      }
      range.value = String(value);
      range.dispatchEvent(new Event("input", { bubbles: true }));
    };
    numberInput.addEventListener("input", () => applyNumberValue(false));
    numberInput.addEventListener("change", () => applyNumberValue(true));
  });
}

function updateRangeOutputs() {
  syncRangeNumberInputs();
  $("bgmVolumeValue").textContent = `${$("bgmVolume").value}%`;
  $("originalVolumeValue").textContent = `${$("originalVolume").value}%`;
  $("cornerStickerOpacityValue").textContent = `${$("cornerStickerOpacity").value}%`;
  $("batchBgmVolumeValue").textContent = `${$("batchBgmVolume").value}%`;
  $("batchOriginalVolumeValue").textContent = `${$("batchOriginalVolume").value}%`;
  $("batchCropOffsetYValue").textContent = String($("batchCropOffsetY").value);
  $("batchCropZoomValue").textContent = `${$("batchCropZoom").value}%`;
}

function selectedVariantColors() {
  const colors = [];
  for (let index = 1; index <= 4; index += 1) {
    if ($(`useVariantColor${index}`).checked) colors.push($(`variantColor${index}`).value);
  }
  return [...new Set(colors.map((color) => color.toUpperCase()))];
}

function visualLayoutCount() {
  return selectedVariantRatios().length * selectedVariantColors().length;
}

function dimensionCounts() {
  const counts = [];
  const errors = [];
  let coreChangeCount = 0;
  if ($("useCover").checked) {
    const frameTime = Number($("coverFrameTimeSeconds").value);
    const previewDuration = Number($("sourcePreview").duration);
    if (!Number.isFinite(frameTime) || frameTime < 0) {
      errors.push("封面画面时间必须是大于或等于 0 的数字");
    } else if (Number.isFinite(previewDuration) && previewDuration > 0 && frameTime >= previewDuration) {
      errors.push("封面画面时间必须小于视频时长");
    }
    if (!$("coverTextLine1").value.trim() || !$("coverTextLine2").value.trim()) {
      errors.push("封面的两行文字都不能为空");
    }
  }
  if ($("useAudio").checked) {
    coreChangeCount += 1;
    const count = assetsInCategory().length;
    counts.push(count);
    if (!count) errors.push("当前音乐分类没有可用音乐");
  }
  if (!(Number($("bgmVolume").value) >= 0 && Number($("bgmVolume").value) <= 100)) errors.push("BGM 音量必须在 0% 到 100% 之间");
  if (!(Number($("originalVolume").value) >= 0 && Number($("originalVolume").value) <= 150)) errors.push("原视频声音必须在 0% 到 150% 之间");
  if ($("useEffects").checked) {
    coreChangeCount += 1;
    const count = validItems(state.effects).length;
    counts.push(count);
    if (!count) errors.push("特效库中没有可用特效");
  }
  if ($("useStickers").checked) {
    coreChangeCount += 1;
    const count = validItems(state.stickers).length;
    counts.push(count);
    if (!count) errors.push("全屏贴纸库中没有可用贴纸");
  }
  if ($("useVisualVariant").checked) {
    coreChangeCount += 3;
    const interval = Number($("mirrorIntervalSeconds").value);
    const layoutCount = visualLayoutCount();
    const stickerCount = validItems(state.cornerStickers).length;
    counts.push(1, layoutCount, stickerCount);
    if (!Number.isFinite(interval) || interval <= 0 || interval > 3600) errors.push("镜像间隔必须是 1 到 3600 秒");
    if (!selectedVariantRatios().length) errors.push("画面变化套装至少选择一个裁剪比例");
    if (!selectedVariantColors().length) errors.push("画面变化套装至少选择一个背景颜色");
    if (!stickerCount) errors.push("四角贴纸需要贴纸库中至少有一个可用贴纸");
    if (!(Number($("cornerStickerOpacity").value) >= 0 && Number($("cornerStickerOpacity").value) <= 100)) errors.push("四角贴纸透明度必须在 0% 到 100% 之间");
  }
  if ($("useCaptions").checked) {
    const count = selectedFonts().length;
    counts.push(count);
    if (!count) errors.push("请选择可用字体");
    if (sourceMode() === "video" && !$("captionText").value.trim()) errors.push("添加字幕需要填写长文案");
  }
  if (sourceMode() === "video" && $("useTextEffects").checked) {
    const count = validItems(state.textEffects).length;
    counts.push(count);
    if (!count) errors.push("花字库中没有可用样式");
    if (!$("flowerText").value.trim()) errors.push("开启花字后需要填写花字内容");
  }
  if (coreChangeCount < 2) {
    errors.push("请至少开启两个核心变化项；画面变化套装本身包含镜像、裁剪填色和四角贴纸三项");
  }
  const combination = fullCombinationCounts(counts);
  const requestedLimit = Number($("generationLimit").value);
  if (!Number.isInteger(requestedLimit) || requestedLimit < 1 || requestedLimit > 500) {
    errors.push("本次生成数量必须是 1 到 500 之间的整数");
  }
  const selectedTotal = Number.isInteger(requestedLimit) && requestedLimit > 0
    ? Math.min(combination.total, requestedLimit)
    : 0;
  return { ...combination, coreChangeCount, requestedLimit, selectedTotal, errors };
}

function updateCounts() {
  updateRangeOutputs();
  updateVisualCropPreview();
  $("audioCount").textContent = `${assetsInCategory().length} 首可用`;
  $("effectCount").textContent = `${validItems(state.effects).length} 个可用`;
  $("stickerCount").textContent = `${validItems(state.stickers).length} 个可用`;
  $("cornerStickerCount").textContent = `${validItems(state.cornerStickers).length} 个可用`;
  $("fontCount").textContent = `${validFonts().length} 种字体可用`;
  $("textEffectCount").textContent = `${validItems(state.textEffects).length} 种可用`;
  const combination = dimensionCounts();
  $("combinationCount").textContent = String(combination.total);
  $("combinationFilterNote").textContent = combination.coreChangeCount >= 2
    ? `本次从 ${combination.total} 种完整组合中随机抽取 ${combination.selectedTotal} 种且不重复；每个结果改变 ${combination.coreChangeCount} 个核心元素`
    : `当前只有 ${combination.coreChangeCount} 个核心变化项，至少需要 2 个`;
  $("generateBtn").textContent = combination.selectedTotal
    ? `开始生成 ${combination.selectedTotal} 个`
    : "开始生成";
  setMessage(combination.errors.join("；"));
  $("audioCategory").disabled = !$("useAudio").checked;
  document.querySelectorAll("#visualVariantSettings input, #visualVariantSettings select").forEach((control) => {
    control.disabled = !$("useVisualVariant").checked;
  });
  $("fontSelection").disabled = !$("useCaptions").checked;
  ["captionStylePreset", "captionSize", "captionColor", "captionPosition", "captionText"].forEach((id) => {
    $(id).disabled = !$("useCaptions").checked;
  });
  $("coverSettings").classList.toggle("hidden", !$("useCover").checked);
}

function audioCandidates() {
  const volume = Number($("bgmVolume").value || 25) / 100;
  return assetsInCategory().map((asset) => ({
    id: asset.identity,
    label: asset.name || asset.identity,
    append: {
      audios: [{
        type: "add",
        library_identity: asset.identity,
        selection_mode: "specific",
        target_start_us: 0,
        target_duration_us: 0,
        fit_to_video: true,
        volume,
      }],
    },
  }));
}

function effectCandidates() {
  return validItems(state.effects).map((effect) => ({
    id: effect.path,
    label: effect.name || effect.effect_name || effect.path,
    append: {
      effects: [{
        effect_json_path: effect.path,
        target_video_track_index: 0,
        target_video_segment_index: 0,
        start_us: -1,
        duration_us: 0,
      }],
    },
  }));
}

function stickerCandidates() {
  return validItems(state.stickers).map((sticker) => ({
    id: sticker.identity || sticker.path,
    label: sticker.name || sticker.path,
    append: {
      stickers: [{
        sticker_json_path: sticker.path,
        start_us: 0,
        duration_us: 0,
      }],
    },
  }));
}

function mirrorCandidates() {
  const interval = Number($("mirrorIntervalSeconds").value);
  return [{
    id: `mirror-${interval}`,
    label: `每 ${interval} 秒镜像一次`,
    short_name: `镜${interval}`,
    patch: { visual_variant: { enabled: true, mirror_interval_seconds: interval } },
  }];
}

function layoutCandidates() {
  const candidates = [];
  for (const ratio of selectedVariantRatios()) {
    for (const color of selectedVariantColors()) {
      candidates.push({
        id: `layout-${ratio}-${color.replace("#", "")}`,
        label: `${ratio} + ${color}`,
        short_name: `${ratio === "1:1" ? "方" : "竖"}${color.slice(1, 2)}`,
        patch: {
          visual_variant: {
            enabled: true,
            crop_ratio: ratio,
            background_color: color,
            face_centered: true,
            face_sample_count: 3,
            crop_offset_y: Number($("cropOffsetY").value || 0) / 100,
            crop_zoom: Number($("cropZoom").value || 100) / 100,
          },
        },
      });
    }
  }
  return candidates;
}

function cornerStickerCandidates() {
  const stickers = validItems(state.cornerStickers);
  const visibleRatio = 0.05;
  const scale = 0.1;
  const opacity = Number($("cornerStickerOpacity").value || 50) / 100;
  const corners = ["top_left", "top_right", "bottom_left", "bottom_right"];
  return stickers.map((sticker, startIndex) => {
    const selected = corners.map((corner, offset) => {
      const item = stickers[(startIndex + offset) % stickers.length];
      return {
        sticker_json_path: item.path,
        start_us: 0,
        duration_us: 0,
        corner,
        visible_ratio: visibleRatio,
        scale,
        opacity,
      };
    });
    return {
      id: `corners-${sticker.identity || sticker.path}-${visibleRatio}-${scale}-${opacity}`,
      label: selected.map((item, index) => `${corners[index]}:${stickers[(startIndex + index) % stickers.length].name || "贴纸"}`).join(" / "),
      short_name: sticker.name || "角贴",
      append: { stickers: selected },
    };
  });
}

function fontCandidates() {
  const mother = sourceMode() === "mother";
  return selectedFonts().map((font) => ({
    id: font.identity,
    label: font.name || font.identity,
    patch: mother
      ? { existing_text_font: { font_id: font.resource_id, font_path: font.path, font_title: font.name } }
      : { captions: { font_id: font.resource_id, font_path: font.path, font_title: font.name } },
  }));
}

function flowerCandidates() {
  const text = $("flowerText").value.trim();
  return validItems(state.textEffects).map((effect) => ({
    id: effect.identity || effect.path,
    label: effect.name || effect.path,
    patch: {
      texts: [{
        type: "add",
        scope: "top",
        text,
        start_us: 0,
        duration_us: 0,
        text_effect_json_path: effect.path,
        apply_clip: false,
      }],
    },
  }));
}

async function uploadVideo(file) {
  const name = encodeURIComponent(file.name || "video.mp4");
  return apiFetch(`/api/media/video?filename=${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
}

async function selectLocalVideo() {
  const button = $("selectLocalVideoBtn");
  button.disabled = true;
  setMessage("正在打开本机视频选择框...");
  try {
    const selected = await collectorFetch("/api/local/select-media", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_kind: "video" }),
    });
    if (selected.cancelled) return;
    const registered = await apiFetch("/api/local/media-reference", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "video", path: selected.path }),
    });
    state.localVideo = { ...selected, ...registered };
    state.digitalHumanCaptionCues = [];
    state.digitalHumanSourceItemId = "";
    $("localVideoFileName").textContent = `${selected.name} · ${formatBytes(selected.size)}`;
    setMessage();
    updateSourcePreview();
  } catch (error) {
    setMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

async function selectLocalOutputFolder() {
  const button = $("selectLocalOutputFolderBtn");
  button.disabled = true;
  const status = $("localOutputStatus");
  status.textContent = "正在打开文件夹选择窗口；如果没有出现在前台，请查看 Windows 任务栏。";
  status.classList.remove("error");
  status.classList.add("pending");
  try {
    let selected;
    try {
      selected = await apiFetch("/api/local/select-output-folder", { method: "POST" });
    } catch (error) {
      if (error.status !== 404) throw error;
      selected = await collectorFetch("/api/local/select-output-folder", { method: "POST" });
    }
    if (selected.cancelled) {
      status.textContent = "已取消选择，生成前仍需指定导出文件夹。";
      return;
    }
    state.localOutputFolder = selected.path;
    $("localOutputFolder").value = selected.path;
    window.localStorage.setItem(LOCAL_OUTPUT_FOLDER_STORAGE_KEY, selected.path);
    status.textContent = `视频将保存到：${selected.path}`;
  } catch (error) {
    status.textContent = `选择失败：${error.message}`;
    status.classList.add("error");
  } finally {
    status.classList.remove("pending");
    button.disabled = false;
  }
}

function buildBaseJob(source) {
  const job = {
    schema: "jyd.render_job.v1",
    source,
    output: { skip_export: false },
    texts: [],
    text_templates: [],
    audios: [],
    effects: [],
    original_video_volume: Number($("originalVolume").value || 100) / 100,
    export: { resolution: "1080P", framerate: "30fps", timeout: 1200 },
  };
  if (isLocalMode() && state.localOutputFolder) {
    job.output.output_dir = state.localOutputFolder;
  }
  if ($("useCover").checked) {
    job.cover = coverJobConfig(singleCoverConfig());
  }
  if (sourceMode() === "video" && $("useCaptions").checked) {
    job.captions = {
      text: $("captionText").value.trim(),
      start_us: 0,
      duration_us: 0,
      max_chars: 16,
      size: Number($("captionSize").value || 8),
      color: $("captionColor").value || "#FFFFFF",
      transform_x: 0,
      transform_y: Number($("captionPosition").value),
      line_max_width: 0.82,
    };
    if (state.digitalHumanCaptionCues.length) {
      job.captions.cues = state.digitalHumanCaptionCues;
      delete job.captions.start_us;
      delete job.captions.duration_us;
      delete job.captions.max_chars;
    }
    if ($("captionStylePreset").value) {
      job.captions.style_json_path = $("captionStylePreset").value;
    }
  }
  if (sourceMode() === "mother") {
    job.remove_existing_audio = $("useAudio").checked;
    job.remove_existing_effects = $("useEffects").checked;
  }
  return job;
}

function buildDimensions() {
  const dimensions = [];
  if ($("useAudio").checked) dimensions.push({ key: "bgm", label: "音乐", mode: "product", candidates: audioCandidates() });
  if ($("useEffects").checked) dimensions.push({ key: "effect", label: "特效", mode: "product", candidates: effectCandidates() });
  if ($("useStickers").checked) dimensions.push({ key: "sticker", label: "贴纸", mode: "product", candidates: stickerCandidates() });
  if ($("useVisualVariant").checked) {
    dimensions.push({ key: "mirror", label: "分段镜像", mode: "fixed", candidates: mirrorCandidates() });
    dimensions.push({ key: "layout", label: "裁剪填色", mode: "product", candidates: layoutCandidates() });
    dimensions.push({ key: "corner_sticker", label: "四角贴纸", mode: "product", candidates: cornerStickerCandidates() });
  }
  if ($("useCaptions").checked) {
    const candidates = fontCandidates();
    dimensions.push({ key: "font", label: "字体", mode: candidates.length > 1 ? "product" : "fixed", candidates });
  }
  if (sourceMode() === "video" && $("useTextEffects").checked) {
    dimensions.push({ key: "flower", label: "花字", mode: "product", candidates: flowerCandidates() });
  }
  return dimensions;
}

function setExcelMessage(message = "", isError = true) {
  const element = $("excelBatchMessage");
  element.textContent = message;
  element.className = `message${isError ? "" : " success"}`;
  element.classList.toggle("hidden", !message);
}

function normalizedExcelText(value) {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[“”"'【】\[\]\s]/g, "")
    .replace(/：/g, ":");
}

function excelOption(value, label, aliases = []) {
  return { value, label, aliases: [label, ...aliases].map(normalizedExcelText) };
}

function validAudioAssets() {
  return state.audio.assets.filter(
    (item) => item.available && item.enabled !== false && !item.deleted,
  );
}

function excelSelectionOptions(kind) {
  const options = [excelOption("none", "不替换", ["保留", "不处理", "关闭"] )];
  if (kind === "audio") {
    const assets = validAudioAssets();
    if (assets.length) options.push(excelOption("all", "全部轮换", ["全部音乐轮换"]));
    for (const category of state.audio.categories || []) {
      const count = assets.filter((asset) => Array.isArray(asset.category_ids) && asset.category_ids.includes(category.id)).length;
      if (!count) continue;
      const name = category.name || category.id;
      options.push(excelOption(`category:${category.id}`, `分类轮换：${name}`, [`从${name}中轮换`, `轮换:${name}`]));
    }
    for (const asset of assets) {
      const name = asset.name || asset.identity;
      options.push(excelOption(`specific:${asset.identity}`, `固定：${name}`, [`固定${name}`, name]));
    }
    return options;
  }

  const source = {
    effect: validItems(state.effects),
    sticker: validItems(state.stickers),
    font: validFonts(),
  }[kind] || [];
  if (source.length) options.push(excelOption("all", "全部轮换"));
  for (const item of source) {
    const identity = kind === "font" ? item.identity : (item.identity || item.path);
    const name = item.name || item.effect_name || identity;
    options.push(excelOption(`specific:${identity}`, `固定：${name}`, [`固定${name}`, name]));
  }
  return options;
}

function fillBatchDefaultControls() {
  const configs = [
    ["batchDefaultAudio", "audio", "all"],
    ["batchDefaultEffect", "effect", "all"],
    ["batchDefaultSticker", "sticker", "all"],
    ["batchDefaultFont", "font", "all"],
  ];
  for (const [id, kind, preferred] of configs) {
    const select = $(id);
    if (!select) continue;
    const previous = select.value;
    const options = excelSelectionOptions(kind);
    select.replaceChildren(...options.map((item) => new Option(item.label, item.value)));
    const next = [previous, preferred, "none"].find((value) => options.some((item) => item.value === value));
    select.value = next || options[0]?.value || "none";
  }
  updateBatchVisualSettings();
}

function nextBatchRowId() {
  state.batchRowSequence += 1;
  return `batch-row-${state.batchRowSequence}`;
}

function batchDefaults() {
  return {
    audio: $("batchDefaultAudio").value || "none",
    effect: $("batchDefaultEffect").value || "none",
    sticker: $("batchDefaultSticker").value || "none",
    font: $("batchDefaultFont").value || "none",
    visual: $("batchDefaultVisual").value || "none",
    visualConfig: batchVisualConfigFromControls(),
    coverEnabled: $("batchDefaultCover").value === "enabled",
    bgmVolume: Number($("batchBgmVolume").value || 25) / 100,
    originalVolume: Number($("batchOriginalVolume").value || 100) / 100,
    count: Number($("batchDefaultCount").value),
  };
}

function normalizedBatchCoverConfig(config = {}, enabled = false) {
  return normalizedCoverConfig(config, enabled);
}

function batchVisualConfigFromControls() {
  const ratios = [];
  if ($("batchVariantRatioSquare").checked) ratios.push("1:1");
  if ($("batchVariantRatioThreeFour").checked) ratios.push("3:4");
  const colors = [];
  for (let index = 1; index <= 4; index += 1) {
    if ($(`batchUseVariantColor${index}`).checked) colors.push($(`batchVariantColor${index}`).value.toUpperCase());
  }
  return {
    mirrorIntervalSeconds: Number($("batchMirrorIntervalSeconds").value),
    ratios,
    colors: [...new Set(colors)],
    visibleRatio: 0.05,
    stickerScale: 0.1,
    stickerOpacity: 0.5,
    cropOffsetY: 0,
    cropZoom: 1,
  };
}

function normalizedBatchVisualConfig(config = {}) {
  return {
    mirrorIntervalSeconds: Number(config.mirrorIntervalSeconds ?? 10),
    ratios: Array.isArray(config.ratios) ? [...config.ratios] : ["1:1", "3:4"],
    colors: Array.isArray(config.colors) ? [...new Set(config.colors.map((color) => String(color).toUpperCase()))] : ["#000000", "#FFFFFF", "#DBE7F5", "#F2DDDD"],
    visibleRatio: Number(config.visibleRatio ?? 0.05),
    stickerScale: Number(config.stickerScale ?? 0.1),
    stickerOpacity: Number(config.stickerOpacity ?? 0.5),
    cropOffsetY: Number(config.cropOffsetY ?? 0),
    cropZoom: Number(config.cropZoom ?? 1),
  };
}

function updateBatchVisualSettings() {
  const enabled = $("batchDefaultVisual").value === "enabled";
  $("batchVisualSettings").classList.toggle("hidden", !enabled);
  $("batchVariantColorSettings").classList.toggle("hidden", !enabled);
}

function activeBatchVisualRow() {
  return state.excelRows.find((row) => row.clientId === state.batchVisualRowId) || null;
}

function batchVisualPreviewTemplate() {
  return activeBatchVisualRow();
}

function ensureBatchVisualPreviewSource() {
  const row = batchVisualPreviewTemplate();
  const source = row?.templateId ? `/api/templates/${encodeURIComponent(row.templateId)}/preview-video` : "";
  const preview = $("batchVisualPreviewVideo");
  if (preview.getAttribute("src") === source) return;
  if (source) preview.src = source;
  else preview.removeAttribute("src");
  preview.load();
}

function updateBatchVisualPreview() {
  updateRangeOutputs();
  state.batchVisualEditorConfig = normalizedBatchVisualConfig({
    ...(state.batchVisualEditorConfig || {}),
    cropOffsetY: Number($("batchCropOffsetY").value || 0) / 100,
    cropZoom: Number($("batchCropZoom").value || 100) / 100,
  });
  const ratio = $("batchCropPreviewRatio").value || "1:1";
  const zoom = Number($("batchCropZoom").value || 100) / 100;
  const offset = Number($("batchCropOffsetY").value || 0) / 100;
  const baseHeight = ratio === "3:4" ? 75 : 56.25;
  const height = baseHeight / zoom;
  const width = 100 / zoom;
  const availableY = 100 - height;
  const guide = $("batchVisualCropGuide");
  guide.style.width = `${width}%`;
  guide.style.height = `${height}%`;
  guide.style.left = `${(100 - width) / 2}%`;
  guide.style.top = `${Math.max(0, Math.min(availableY, availableY / 2 + offset * availableY))}%`;
  $("batchVisualCropGuideLabel").textContent = `${ratio} 裁剪区域`;
}

function setBatchVisualEditorControls(config) {
  const value = normalizedBatchVisualConfig(config);
  $("batchCropPreviewRatio").value = value.ratios[0] || "1:1";
  $("batchCropOffsetY").value = String(value.cropOffsetY * 100);
  $("batchCropZoom").value = String(value.cropZoom * 100);
  state.batchVisualEditorConfig = value;
}

function openBatchVisualPreview(event) {
  const row = state.excelRows.find((item) => item.clientId === event.currentTarget.dataset.rowId);
  if (!row?.templateId) return;
  state.batchVisualRowId = row.clientId;
  setBatchVisualEditorControls(row.visualConfig);
  $("batchVisualPreviewTitle").textContent = "人物裁剪预览";
  $("batchVisualPreviewDescription").textContent = `${row.templateName || "当前母版"}：蓝框表示裁剪范围，生成时会先自动定位人脸，再应用这里的微调。`;
  ensureBatchVisualPreviewSource();
  updateBatchVisualPreview();
  $("batchVisualPreviewDialog").classList.remove("hidden");
  $("batchVisualPreviewDialog").querySelector(".dialog-panel").scrollTop = 0;
}

function closeBatchVisualPreview() {
  const preview = $("batchVisualPreviewVideo");
  preview.pause();
  preview.removeAttribute("src");
  preview.load();
  state.batchVisualRowId = "";
  state.batchVisualEditorConfig = null;
  $("batchVisualPreviewDialog").classList.add("hidden");
}

function saveBatchVisualPreview() {
  const row = activeBatchVisualRow();
  if (!row) return closeBatchVisualPreview();
  row.visualConfig = normalizedBatchVisualConfig(state.batchVisualEditorConfig || row.visualConfig);
  closeBatchVisualPreview();
  renderExcelRows();
}

function createBatchRow(mother = null) {
  const defaults = batchDefaults();
  const cover = normalizedBatchCoverConfig({}, defaults.coverEnabled);
  return {
    clientId: nextBatchRowId(),
    rowNumber: state.excelRows.length + 1,
    enabled: true,
    templateId: mother?.template_id || "",
    templateName: mother?.name || mother?.template_id || "",
    ...defaults,
    cover,
    importErrors: {},
  };
}

function resolveExcelSelection(rawValue, kind) {
  const options = excelSelectionOptions(kind);
  const normalized = normalizedExcelText(rawValue);
  if (!normalized) return { value: "none", error: "" };
  const match = options.find((option) => option.aliases.includes(normalized));
  if (match) return { value: match.value, error: "" };
  return { value: "none", error: `无法识别“${rawValue}”` };
}

function resolveExcelTemplate(rawValue) {
  const normalized = normalizedExcelText(rawValue);
  if (!normalized) return { value: "", error: "请选择剪辑母版" };
  const match = availableMothers().find((item) =>
    [item.template_id, item.name].some((value) => normalizedExcelText(value) === normalized),
  );
  return match
    ? { value: match.template_id, error: "" }
    : { value: "", error: `找不到母版“${rawValue}”` };
}

function excelEnabled(value) {
  const normalized = normalizedExcelText(value);
  if (["否", "不启用", "禁用", "false", "0", "no"].includes(normalized)) return false;
  return true;
}

function importExcelRow(raw) {
  const template = resolveExcelTemplate(raw.template);
  const audio = resolveExcelSelection(raw.audio, "audio");
  const effect = resolveExcelSelection(raw.effect, "effect");
  const sticker = resolveExcelSelection(raw.sticker, "sticker");
  const font = resolveExcelSelection(raw.font, "font");
  const countText = String(raw.count ?? "").trim();
  const count = countText ? Number(countText) : null;
  const importErrors = {};
  for (const [field, result] of Object.entries({ template, audio, effect, sticker, font })) {
    if (result.error) importErrors[field] = result.error;
  }
  if (countText && (!Number.isInteger(count) || count < 1 || count > 500)) {
    importErrors.count = "生成数量必须是 1 到 500 的整数";
  }
  return {
    rowNumber: Number(raw.row_number || 0),
    enabled: excelEnabled(raw.enabled),
    templateId: template.value,
    templateName: String(raw.template || "").trim(),
    audio: audio.value,
    effect: effect.value,
    sticker: sticker.value,
    font: font.value,
    visual: "none",
    visualConfig: normalizedBatchVisualConfig(),
    coverEnabled: false,
    cover: normalizedBatchCoverConfig(),
    bgmVolume: 0.25,
    originalVolume: 1.0,
    count,
    importErrors,
  };
}

function excelSelectionCandidates(kind, value) {
  if (value === "none") return [];
  if (kind === "audio") {
    const assets = validAudioAssets();
    if (value === "all") return assets;
    if (value.startsWith("category:")) {
      const categoryId = value.slice("category:".length);
      return assets.filter((asset) => Array.isArray(asset.category_ids) && asset.category_ids.includes(categoryId));
    }
    if (value.startsWith("specific:")) {
      const identity = value.slice("specific:".length);
      return assets.filter((asset) => asset.identity === identity);
    }
  }
  const source = {
    effect: validItems(state.effects),
    sticker: validItems(state.stickers),
    font: validFonts(),
  }[kind] || [];
  if (value === "all") return source;
  if (value.startsWith("specific:")) {
    const identity = value.slice("specific:".length);
    return source.filter((item) => (kind === "font" ? item.identity : (item.identity || item.path)) === identity);
  }
  return [];
}

function validateExcelRow(row) {
  if (!row.enabled) return { errors: [], warnings: [], combinationTotal: 0, requestedCount: 0, actualCount: 0 };
  const errors = Object.values(row.importErrors || {});
  const warnings = [];
  if (!availableMothers().some((item) => item.template_id === row.templateId)) errors.push("请选择可用剪辑母版");
  const defaultCount = Number($("batchDefaultCount").value);
  if (!Number.isInteger(defaultCount) || defaultCount < 1 || defaultCount > 500) {
    errors.push("页面默认数量必须是 1 到 500 的整数");
  }
  const requestedCount = row.count == null ? defaultCount : Number(row.count);
  if (!Number.isInteger(requestedCount) || requestedCount < 1 || requestedCount > 500) {
    errors.push("本行生成数量必须是 1 到 500 的整数");
  }
  if (!(Number(row.bgmVolume) >= 0 && Number(row.bgmVolume) <= 1)) errors.push("BGM 音量必须在 0% 到 100% 之间");
  if (!(Number(row.originalVolume) >= 0 && Number(row.originalVolume) <= 1.5)) errors.push("原视频声音必须在 0% 到 150% 之间");

  const coreKinds = ["audio", "effect", "sticker"];
  const coreChangeCount = coreKinds.filter((kind) => row[kind] !== "none").length + (row.visual === "enabled" ? 3 : 0);
  if (coreChangeCount < 2) errors.push("背景音乐、视频特效、全屏贴纸或画面套装合计至少变化两项");

  let combinationTotal = 1;
  for (const kind of [...coreKinds, "font"]) {
    if (row[kind] === "none") continue;
    const candidates = excelSelectionCandidates(kind, row[kind]);
    if (!candidates.length) errors.push(`${{ audio: "背景音乐", effect: "视频特效", sticker: "全屏贴纸", font: "字幕字体" }[kind]}没有可用候选项`);
    if (row[kind] === "all" || row[kind].startsWith("category:")) combinationTotal *= Math.max(1, candidates.length);
  }
  if (row.visual === "enabled") {
    const config = normalizedBatchVisualConfig(row.visualConfig);
    const stickerCount = validItems(state.cornerStickers).length;
    if (!Number.isFinite(config.mirrorIntervalSeconds) || config.mirrorIntervalSeconds < 1 || config.mirrorIntervalSeconds > 3600) {
      errors.push("画面套装镜像间隔必须是 1 到 3600 秒");
    }
    if (!config.ratios.length) errors.push("画面套装至少选择一个裁剪比例");
    if (!config.colors.length) errors.push("画面套装至少选择一个背景颜色");
    if (!stickerCount) errors.push("画面套装需要贴纸库中至少有一个可用贴纸");
    if (!(config.stickerScale > 0 && config.stickerScale <= 1)) errors.push("四角贴纸大小必须在 1% 到 100% 之间");
    if (!(config.stickerOpacity >= 0 && config.stickerOpacity <= 1)) errors.push("四角贴纸透明度必须在 0% 到 100% 之间");
    if (!(config.cropOffsetY >= -1 && config.cropOffsetY <= 1)) errors.push("人物上下微调超出范围");
    if (!(config.cropZoom >= 1 && config.cropZoom <= 1.4)) errors.push("裁剪缩放必须在 100% 到 140% 之间");
    combinationTotal *= Math.max(1, config.ratios.length * config.colors.length);
    combinationTotal *= Math.max(1, stickerCount);
  }
  const cover = normalizedBatchCoverConfig(row.cover, row.coverEnabled);
  if (cover.enabled) {
    if (!Number.isFinite(cover.frameTimeSeconds) || cover.frameTimeSeconds < 0) {
      errors.push("封面画面时间必须是大于或等于 0 的数字");
    }
    if (!cover.textLine1.trim() || !cover.textLine2.trim()) {
      errors.push("封面的两行文字都不能为空");
    }
  }
  const actualCount = errors.length ? 0 : Math.min(combinationTotal, requestedCount);
  if (!errors.length && requestedCount > combinationTotal) {
    warnings.push(`只有 ${combinationTotal} 种不同组合，实际生成 ${actualCount} 个`);
  }
  return { errors, warnings, combinationTotal, requestedCount, actualCount };
}

function excelBatchSummary() {
  const evaluations = state.excelRows.map(validateExcelRow);
  const enabledCount = state.excelRows.filter((row) => row.enabled).length;
  const errorCount = evaluations.reduce((total, item) => total + (item.errors.length ? 1 : 0), 0);
  const videoCount = evaluations.reduce((total, item) => total + item.actualCount, 0);
  const selectedCount = state.excelRows.filter((row) => state.batchSelectedRowIds.has(row.clientId)).length;
  return { evaluations, enabledCount, selectedCount, errorCount, videoCount };
}

function optionSelect(options, value, field, rowIndex, className = "") {
  const select = document.createElement("select");
  select.className = className;
  select.dataset.field = field;
  select.dataset.rowIndex = String(rowIndex);
  for (const option of options) select.append(new Option(option.label, option.value));
  select.value = value;
  select.addEventListener("change", updateExcelRowFromControl);
  return select;
}

function updateExcelRowFromControl(event) {
  const control = event.currentTarget;
  const rowIndex = Number(control.dataset.rowIndex);
  const field = control.dataset.field;
  const row = state.excelRows[rowIndex];
  if (!row || !field) return;
  if (field === "count") row.count = control.value.trim() ? Number(control.value) : null;
  else if (field === "coverEnabled") {
    row.coverEnabled = control.value === "enabled";
    row.cover = normalizedBatchCoverConfig({ ...row.cover, enabled: row.coverEnabled }, row.coverEnabled);
  } else row[field] = control.value;
  if (field === "visual" && row.visual === "enabled" && !row.visualConfig) {
    row.visualConfig = normalizedBatchVisualConfig(batchVisualConfigFromControls());
  }
  if (field === "templateId") {
    const mother = availableMothers().find((item) => item.template_id === row.templateId);
    row.templateName = mother?.name || mother?.template_id || "";
    row.cover = normalizedBatchCoverConfig({ ...row.cover, frameTimeSeconds: 0 }, row.coverEnabled);
  }
  if (row.importErrors) delete row.importErrors[field === "templateId" ? "template" : field];
  renderExcelRows();
}

function updateBatchRowSelection(event) {
  const rowId = event.currentTarget.dataset.rowId;
  if (!rowId) return;
  if (event.currentTarget.checked) state.batchSelectedRowIds.add(rowId);
  else state.batchSelectedRowIds.delete(rowId);
  renderExcelRows();
}

function updateBatchVisualOpacity(event) {
  const control = event.currentTarget;
  const row = state.excelRows[Number(control.dataset.rowIndex)];
  if (!row) return;
  const rawValue = Number(control.value);
  const opacityPercent = Number.isFinite(rawValue) ? Math.max(0, Math.min(100, rawValue)) : 50;
  row.visualConfig = normalizedBatchVisualConfig({
    ...row.visualConfig,
    stickerOpacity: opacityPercent / 100,
  });
  renderExcelRows();
}

function tableCell(child) {
  const cell = document.createElement("td");
  if (typeof child === "string") cell.textContent = child;
  else cell.append(child);
  return cell;
}

function renderExcelRows() {
  const body = $("excelTaskRows");
  body.replaceChildren();
  state.excelRows.forEach((row, index) => {
    if (!row.clientId) row.clientId = nextBatchRowId();
    row.rowNumber = index + 1;
    row.enabled = true;
  });
  const summary = excelBatchSummary();
  const mothers = availableMothers().map((item) => ({ value: item.template_id, label: item.name || item.template_id }));
  if (!state.excelRows.length) {
    const emptyRow = document.createElement("tr");
    const emptyCell = document.createElement("td");
    emptyCell.colSpan = 10;
    emptyCell.className = "batch-empty-row";
    emptyCell.textContent = "还没有任务。请在上方批量上传母版，或者点击“添加已有母版”。";
    emptyRow.append(emptyCell);
    body.append(emptyRow);
  }
  state.excelRows.forEach((row, rowIndex) => {
    const evaluation = summary.evaluations[rowIndex];
    const tableRow = document.createElement("tr");
    tableRow.classList.toggle("selected-row", state.batchSelectedRowIds.has(row.clientId));
    tableRow.classList.toggle("error-row", evaluation.errors.length > 0);

    const selected = document.createElement("input");
    selected.type = "checkbox";
    selected.checked = state.batchSelectedRowIds.has(row.clientId);
    selected.className = "excel-enabled";
    selected.dataset.rowId = row.clientId;
    selected.setAttribute("aria-label", `选择任务 ${rowIndex + 1}`);
    selected.addEventListener("change", updateBatchRowSelection);

    const templateOptions = [{ value: "", label: "请选择母版" }, ...mothers];
    const template = optionSelect(templateOptions, row.templateId, "templateId", rowIndex, "excel-template-select");
    const audio = optionSelect(excelSelectionOptions("audio"), row.audio, "audio", rowIndex);
    const effect = optionSelect(excelSelectionOptions("effect"), row.effect, "effect", rowIndex);
    const sticker = optionSelect(excelSelectionOptions("sticker"), row.sticker, "sticker", rowIndex);
    const font = optionSelect(excelSelectionOptions("font"), row.font, "font", rowIndex);
    const visualCell = document.createElement("div");
    visualCell.className = "batch-visual-cell";
    const visual = optionSelect([
      { value: "none", label: "不使用" },
      { value: "enabled", label: "使用套装" },
    ], row.visual || "none", "visual", rowIndex, "batch-visual-select");
    const visualConfig = normalizedBatchVisualConfig(row.visualConfig);
    const visualActions = document.createElement("div");
    visualActions.className = "batch-visual-edit-row";
    const cropButton = document.createElement("button");
    cropButton.type = "button";
    cropButton.className = "batch-visual-edit secondary-button";
    cropButton.textContent = `裁剪：${Math.round(visualConfig.cropOffsetY * 100)} · ${Math.round(visualConfig.cropZoom * 100)}%`;
    cropButton.title = "调整这个母版的人物上下位置和裁剪缩放";
    cropButton.disabled = row.visual !== "enabled" || !row.templateId;
    cropButton.dataset.rowId = row.clientId;
    cropButton.addEventListener("click", openBatchVisualPreview);
    const opacityField = document.createElement("label");
    opacityField.className = "batch-corner-opacity";
    const opacityLabel = document.createElement("span");
    opacityLabel.textContent = "贴纸透明度";
    const opacityValue = document.createElement("span");
    opacityValue.className = "batch-corner-opacity-value";
    const opacityInput = document.createElement("input");
    opacityInput.type = "number";
    opacityInput.min = "0";
    opacityInput.max = "100";
    opacityInput.step = "1";
    opacityInput.value = String(Math.round(visualConfig.stickerOpacity * 100));
    opacityInput.disabled = row.visual !== "enabled";
    opacityInput.dataset.rowIndex = String(rowIndex);
    opacityInput.setAttribute("aria-label", `任务 ${rowIndex + 1} 四角贴纸透明度`);
    opacityInput.addEventListener("change", updateBatchVisualOpacity);
    opacityValue.append(opacityInput, document.createTextNode("%"));
    opacityField.append(opacityLabel, opacityValue);
    visualActions.append(cropButton, opacityField);
    visualCell.append(visual, visualActions);

    const coverCell = document.createElement("div");
    coverCell.className = "batch-cover-cell";
    const cover = normalizedBatchCoverConfig(row.cover, row.coverEnabled);
    const coverMode = optionSelect([
      { value: "none", label: "不制作" },
      { value: "enabled", label: "制作封面" },
    ], cover.enabled ? "enabled" : "none", "coverEnabled", rowIndex, "batch-cover-select");
    const coverButton = document.createElement("button");
    coverButton.type = "button";
    coverButton.className = "batch-cover-edit secondary-button";
    coverButton.textContent = cover.enabled ? `${cover.frameTimeSeconds.toFixed(2)} 秒 · 设置` : "设置画面";
    coverButton.disabled = !cover.enabled || !row.templateId;
    coverButton.dataset.rowId = row.clientId;
    coverButton.addEventListener("click", openBatchCoverEditor);
    coverCell.append(coverMode, coverButton);

    const count = document.createElement("input");
    count.type = "number";
    count.min = "1";
    count.max = "500";
    count.step = "1";
    count.value = row.count == null ? "" : String(row.count);
    count.placeholder = String($("batchDefaultCount").value || 20);
    count.className = "excel-count";
    count.dataset.field = "count";
    count.dataset.rowIndex = String(rowIndex);
    count.addEventListener("change", updateExcelRowFromControl);

    const status = document.createElement("div");
    status.className = `excel-row-status${evaluation.errors.length ? " error" : ""}`;
    if (evaluation.errors.length) status.textContent = evaluation.errors.join("；");
    else status.textContent = evaluation.warnings[0] || `可生成 ${evaluation.actualCount} 个（${evaluation.combinationTotal} 种组合）`;

    tableRow.append(
      tableCell(selected), tableCell(template), tableCell(audio),
      tableCell(effect), tableCell(sticker), tableCell(font), tableCell(visualCell), tableCell(coverCell), tableCell(count), tableCell(status),
    );
    body.append(tableRow);
  });

  $("excelRowCount").textContent = String(state.excelRows.length);
  $("excelEnabledCount").textContent = String(summary.selectedCount);
  $("excelVideoCount").textContent = String(summary.videoCount);
  $("excelErrorCount").textContent = String(summary.errorCount);
  $("batchSelectedRowCount").textContent = `已选 ${summary.selectedCount} 个任务`;
  $("applyBatchDefaultsBtn").disabled = summary.selectedCount === 0;
  $("deleteSelectedBatchRowsBtn").disabled = summary.selectedCount === 0;
  const selectAll = $("selectAllBatchRows");
  selectAll.checked = state.excelRows.length > 0 && summary.selectedCount === state.excelRows.length;
  selectAll.indeterminate = summary.selectedCount > 0 && summary.selectedCount < state.excelRows.length;
  const overLimit = summary.videoCount > 500;
  $("excelSubmitSummary").textContent = overLimit
    ? `预计 ${summary.videoCount} 个，超过单批上限 500 个`
    : `共 ${summary.enabledCount} 个任务，预计生成 ${summary.videoCount} 个视频`;
  $("submitExcelBatchBtn").disabled = !summary.enabledCount || summary.errorCount > 0 || !summary.videoCount || overLimit;
}

function setBatchCoverMessage(message = "", isError = false) {
  const element = $("batchCoverMessage");
  element.textContent = message;
  element.className = `message${isError ? " error" : ""}`;
  element.classList.toggle("hidden", !message);
}

function activeBatchCoverRow() {
  return state.excelRows.find((row) => row.clientId === state.batchCoverRowId) || null;
}

function fillCoverFontOptions(selectedIdentity = "") {
  const select = $("batchCoverFont");
  const options = [new Option("跟随字幕字体", "")];
  for (const font of validFonts()) {
    options.push(new Option(font.name || font.identity, font.identity));
  }
  select.replaceChildren(...options);
  select.value = validFonts().some((font) => font.identity === selectedIdentity) ? selectedIdentity : "";
}

function setCoverEditorControls(config) {
  const value = normalizedCoverConfig(config, true);
  $("batchCoverTextLine1").value = value.textLine1;
  $("batchCoverTextLine2").value = value.textLine2;
  fillCoverFontOptions(value.fontIdentity);
  $("batchCoverFrameOffsetX").value = String(value.frameOffsetX * 100);
  $("batchCoverFrameOffsetY").value = String(value.frameOffsetY * 100);
  $("batchCoverOverlayWidth").value = String(value.overlayWidth * 100);
  $("batchCoverOverlayHeight").value = String(value.overlayHeight * 100);
  $("batchCoverOverlayY").value = String((value.overlayY - 0.5) * 200);
  $("batchCoverOverlayAlpha").value = String(value.overlayAlpha * 100);
  $("batchCoverLine1X").value = String(value.line1X * 100);
  $("batchCoverLine1Y").value = String(value.line1Y * 100);
  $("batchCoverLine2X").value = String(value.line2X * 100);
  $("batchCoverLine2Y").value = String(value.line2Y * 100);
  $("batchCoverLine1Size").value = String(value.line1Size);
  $("batchCoverLine2Size").value = String(value.line2Size);
  $("batchCoverLine1Color").value = value.line1Color;
  $("batchCoverLine2Color").value = value.line2Color;
  state.coverEditorConfig = value;
}

function coverEditorConfigFromControls() {
  const previous = normalizedCoverConfig(state.coverEditorConfig || {}, true);
  return normalizedCoverConfig({
    ...previous,
    frameTimeSeconds: Number($("batchCoverFrameTimeSeconds").value || 0),
    textLine1: $("batchCoverTextLine1").value,
    textLine2: $("batchCoverTextLine2").value,
    frameScale: 1,
    frameOffsetX: Number($("batchCoverFrameOffsetX").value || 0) / 100,
    frameOffsetY: Number($("batchCoverFrameOffsetY").value || 0) / 100,
    fontIdentity: $("batchCoverFont").value,
    overlayWidth: Number($("batchCoverOverlayWidth").value || 100) / 100,
    overlayHeight: Number($("batchCoverOverlayHeight").value || 36) / 100,
    overlayY: 0.5 + Number($("batchCoverOverlayY").value || 36) / 200,
    overlayAlpha: Number($("batchCoverOverlayAlpha").value || 50) / 100,
    line1X: Number($("batchCoverLine1X").value || 0) / 100,
    line1Y: Number($("batchCoverLine1Y").value || -28) / 100,
    line2X: Number($("batchCoverLine2X").value || 0) / 100,
    line2Y: Number($("batchCoverLine2Y").value || -55) / 100,
    line1Size: Number($("batchCoverLine1Size").value || 12),
    line2Size: Number($("batchCoverLine2Size").value || 12),
    line1Color: $("batchCoverLine1Color").value.toUpperCase(),
    line2Color: $("batchCoverLine2Color").value.toUpperCase(),
  }, true);
}

function updateBatchCoverTimeDisplay(time) {
  const safeTime = Number.isFinite(time) && time >= 0 ? time : 0;
  $("batchCoverFrameTimeSeconds").value = safeTime.toFixed(2);
  $("batchCoverCurrentTime").textContent = `${safeTime.toFixed(2)} 秒`;
}

function syncBatchCoverTimeFromPreview() {
  const preview = $("batchCoverPreview");
  if (preview.currentSrc && Number.isFinite(preview.currentTime)) {
    updateBatchCoverTimeDisplay(preview.currentTime);
  }
}

function openBatchCoverEditor(event) {
  const row = state.excelRows.find((item) => item.clientId === event.currentTarget.dataset.rowId);
  if (!row?.templateId) return;
  state.batchCoverRowId = row.clientId;
  state.coverEditorMode = "batch";
  const cover = normalizedBatchCoverConfig(row.cover, true);
  $("batchCoverMotherName").textContent = `${row.templateName || "当前母版"}：拖动视频选择画面，同一母版生成的所有视频使用这一张封面。`;
  setCoverEditorControls(cover);
  updateBatchCoverTimeDisplay(cover.frameTimeSeconds);
  updateBatchCoverPreview();
  setBatchCoverMessage();
  const preview = $("batchCoverPreview");
  preview.src = `/api/templates/${encodeURIComponent(row.templateId)}/preview-video`;
  preview.dataset.initialTime = String(cover.frameTimeSeconds);
  preview.load();
  $("batchCoverDialog").classList.remove("hidden");
}

function openSingleCoverEditor() {
  const previewSource = $("sourcePreview");
  if (!previewSource.currentSrc) {
    setMessage("请先选择并加载母版或视频预览");
    return;
  }
  state.batchCoverRowId = "";
  state.coverEditorMode = "single";
  const cover = singleCoverConfig();
  setCoverEditorControls(cover);
  updateBatchCoverTimeDisplay(cover.frameTimeSeconds);
  $("batchCoverMotherName").textContent = "调整一次后，本次来源生成的所有视频共用这个封面布局。";
  const preview = $("batchCoverPreview");
  preview.src = previewSource.currentSrc;
  preview.dataset.initialTime = String(cover.frameTimeSeconds);
  preview.load();
  updateBatchCoverPreview();
  setBatchCoverMessage();
  $("batchCoverDialog").classList.remove("hidden");
}

function closeBatchCoverEditor() {
  state.batchCoverRowId = "";
  state.coverEditorMode = "";
  state.coverEditorConfig = null;
  const preview = $("batchCoverPreview");
  preview.pause();
  preview.removeAttribute("src");
  preview.load();
  $("batchCoverDialog").classList.add("hidden");
  setBatchCoverMessage();
}

function saveBatchCoverEditor() {
  const row = activeBatchCoverRow();
  const cover = coverEditorConfigFromControls();
  const frameTimeSeconds = cover.frameTimeSeconds;
  const textLine1 = cover.textLine1.trim();
  const textLine2 = cover.textLine2.trim();
  const duration = Number($("batchCoverPreview").duration);
  if (!Number.isFinite(frameTimeSeconds) || frameTimeSeconds < 0) {
    setBatchCoverMessage("封面画面时间必须是大于或等于 0 的数字。", true);
    return;
  }
  if (Number.isFinite(duration) && duration > 0 && frameTimeSeconds >= duration) {
    setBatchCoverMessage("封面画面时间必须小于视频时长。", true);
    return;
  }
  if (!textLine1 || !textLine2) {
    setBatchCoverMessage("封面的两行文字都不能为空。", true);
    return;
  }
  cover.enabled = true;
  cover.textLine1 = textLine1;
  cover.textLine2 = textLine2;
  if (state.coverEditorMode === "single") {
    state.singleCover = cover;
    $("coverFrameTimeSeconds").value = frameTimeSeconds.toFixed(2);
    $("coverTextLine1").value = textLine1;
    $("coverTextLine2").value = textLine2;
    updateSingleCoverPreview();
  } else if (row) {
    row.coverEnabled = true;
    row.cover = cover;
  }
  closeBatchCoverEditor();
  if (row) renderExcelRows();
}

function addBatchTask(mother = null, { select = true } = {}) {
  if (mother && state.excelRows.some((row) => row.templateId === mother.template_id)) {
    const existing = state.excelRows.find((row) => row.templateId === mother.template_id);
    if (select) state.batchSelectedRowIds.add(existing.clientId);
    return existing;
  }
  const row = createBatchRow(mother);
  state.excelRows.push(row);
  if (select) state.batchSelectedRowIds.add(row.clientId);
  return row;
}

function addEmptyBatchTask() {
  addBatchTask();
  renderExcelRows();
  setExcelMessage("已添加一行，请选择已有母版。", false);
}

function applyBatchDefaultsToSelected() {
  const defaults = batchDefaults();
  if (!Number.isInteger(defaults.count) || defaults.count < 1 || defaults.count > 500) {
    setExcelMessage("每个母版生成数量必须是 1 到 500 的整数。", true);
    return;
  }
  let changed = 0;
  for (const row of state.excelRows) {
    if (!state.batchSelectedRowIds.has(row.clientId)) continue;
    const previousCover = normalizedBatchCoverConfig(row.cover, row.coverEnabled);
    const previousVisual = normalizedBatchVisualConfig(row.visualConfig);
    Object.assign(row, defaults, {
      visualConfig: normalizedBatchVisualConfig({
        ...defaults.visualConfig,
        visibleRatio: previousVisual.visibleRatio,
        stickerScale: previousVisual.stickerScale,
        stickerOpacity: previousVisual.stickerOpacity,
        cropOffsetY: previousVisual.cropOffsetY,
        cropZoom: previousVisual.cropZoom,
      }),
      cover: normalizedBatchCoverConfig({ ...previousCover, enabled: defaults.coverEnabled }, defaults.coverEnabled),
    });
    row.importErrors = {};
    changed += 1;
  }
  renderExcelRows();
  setExcelMessage(`已把统一设置应用到 ${changed} 个任务。`, false);
}

function deleteSelectedBatchRows() {
  const selectedCount = state.batchSelectedRowIds.size;
  if (!selectedCount) return;
  if (!window.confirm(`确定从本批次移除 ${selectedCount} 个任务吗？`)) return;
  state.excelRows = state.excelRows.filter((row) => !state.batchSelectedRowIds.has(row.clientId));
  state.batchSelectedRowIds.clear();
  renderExcelRows();
  setExcelMessage(`已移除 ${selectedCount} 个任务。`, false);
}

async function uploadCollectorDraftForExcel(draft) {
  const report = await collectorFetch("/api/drafts/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draft_dir: draft.path, hash_mode: "small_files" }),
  });
  const plan = await collectorFetch("/api/upload-plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      report_id: report.report_id,
      policies: {
        audio: $("collectorPolicyAudio").value,
        video_effects: $("collectorPolicyEffects").value,
        text_style: $("collectorPolicyTextStyle").value,
        text_effects: $("collectorPolicyTextEffects").value,
        text_templates: "keep",
      },
    }),
  });
  if (!plan.summary?.ready_for_upload) {
    throw new Error(`${draft.name} 的上传清单存在 ${plan.summary?.blocked_count || 1} 个阻塞问题`);
  }
  return collectorFetch(`/api/upload-plans/${encodeURIComponent(plan.plan_id)}/upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server_url: window.location.origin,
      template_name: draft.name || "",
      template_lifecycle: "excel_batch_once",
    }),
  });
}

function renderExcelLocalDrafts() {
  const list = $("excelLocalDraftList");
  list.replaceChildren();
  const drafts = state.collector.drafts || [];
  if (!drafts.length) {
    const empty = document.createElement("p");
    empty.textContent = "本机草稿目录中没有找到可上传草稿。";
    list.append(empty);
  }
  for (const draft of drafts) {
    const option = document.createElement("label");
    option.className = "excel-local-draft-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.excelSelectedDraftPaths.has(draft.path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.excelSelectedDraftPaths.add(draft.path);
      else state.excelSelectedDraftPaths.delete(draft.path);
      updateExcelDraftSelectionSummary();
    });
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = draft.name || "未命名草稿";
    const meta = document.createElement("small");
    meta.textContent = `${draft.encryption_status === "plain" ? "明文" : "自动解密"} · ${formatDuration(draft.duration_us || 0)}`;
    text.append(name, meta);
    option.append(checkbox, text);
    list.append(option);
  }
  $("excelLocalDraftCount").textContent = `${drafts.length} 个本机草稿`;
  updateExcelDraftSelectionSummary();
}

function updateExcelDraftSelectionSummary() {
  const count = state.excelSelectedDraftPaths.size;
  $("excelSelectedDraftCount").textContent = `已选 ${count} 个`;
  $("uploadSelectedExcelDraftsBtn").disabled = count === 0;
  const total = (state.collector.drafts || []).length;
  $("selectAllExcelDrafts").checked = total > 0 && count === total;
  $("selectAllExcelDrafts").indeterminate = count > 0 && count < total;
}

async function loadExcelLocalDrafts() {
  const button = $("loadExcelLocalDraftsBtn");
  button.disabled = true;
  try {
    if (!state.collector.connected) await connectCollector({ quiet: true });
    if (!state.collector.connected) throw new Error("请先启动本机草稿采集工具");
    state.collector.drafts = await collectorFetch("/api/drafts");
    state.excelSelectedDraftPaths = new Set(
      state.collector.drafts
        .filter((draft) => state.excelSelectedDraftPaths.has(draft.path))
        .map((draft) => draft.path),
    );
    renderExcelLocalDrafts();
    setExcelMessage(`已读取 ${state.collector.drafts.length} 个本机草稿，可以多选上传。`, false);
  } catch (error) {
    setExcelMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function openExcelMotherUploadPanel() {
  const panel = $("excelMotherUploadPanel");
  panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  if (!state.collector.drafts.length) await loadExcelLocalDrafts();
  else renderExcelLocalDrafts();
}

async function uploadSelectedExcelDrafts() {
  const selected = (state.collector.drafts || []).filter((draft) => state.excelSelectedDraftPaths.has(draft.path));
  if (!selected.length) return;
  if (!window.confirm(`将批量处理 ${selected.length} 个本机草稿，并自动生成 ${selected.length} 行任务。已经上传过的同名母版会直接复用，是否继续？`)) return;
  const button = $("uploadSelectedExcelDraftsBtn");
  button.disabled = true;
  try {
    const needsUpload = selected.filter((draft) => !resolveExcelTemplate(draft.name).value);
    for (let index = 0; index < needsUpload.length; index += 1) {
      button.textContent = `正在上传 ${index + 1} / ${needsUpload.length}`;
      setExcelMessage(`正在分析并上传母版：${needsUpload[index].name}`, false);
      const upload = await uploadCollectorDraftForExcel(needsUpload[index]);
      const templateId = upload?.server_result?.template?.template_id;
      if (templateId) state.excelTemporaryTemplateIds.add(templateId);
    }
    await refreshMothers();
    let added = 0;
    for (const draft of selected) {
      const mother = availableMothers().find(
        (item) => normalizedExcelText(item.name) === normalizedExcelText(draft.name),
      );
      if (!mother) continue;
      const existed = state.excelRows.some((row) => row.templateId === mother.template_id);
      addBatchTask(mother);
      if (!existed) added += 1;
    }
    renderExcelRows();
    const reused = selected.length - needsUpload.length;
    setExcelMessage(
      `已生成 ${added} 行任务：新上传 ${needsUpload.length} 个母版${reused ? `，复用 ${reused} 个已有母版` : ""}。现在可以统一设置参数。`,
      false,
    );
  } catch (error) {
    setExcelMessage(error.message, true);
  } finally {
    button.textContent = "上传并生成任务行";
    button.disabled = state.excelSelectedDraftPaths.size === 0;
  }
}

function excelAudioCandidate(asset, volume = 0.25) {
  return {
    id: asset.identity,
    label: asset.name || asset.identity,
    append: { audios: [{
      type: "add", library_identity: asset.identity, selection_mode: "specific",
      target_start_us: 0, target_duration_us: 0, fit_to_video: true, volume,
    }] },
  };
}

function excelEffectCandidate(effect) {
  return {
    id: effect.path,
    label: effect.name || effect.effect_name || effect.path,
    append: { effects: [{
      effect_json_path: effect.path, target_video_track_index: 0,
      target_video_segment_index: 0, start_us: -1, duration_us: 0,
    }] },
  };
}

function excelStickerCandidate(sticker) {
  return {
    id: sticker.identity || sticker.path,
    label: sticker.name || sticker.path,
    append: { stickers: [{ sticker_json_path: sticker.path, start_us: 0, duration_us: 0 }] },
  };
}

function excelFontCandidate(font) {
  return {
    id: font.identity,
    label: font.name || font.identity,
    patch: { existing_text_font: { font_id: font.resource_id, font_path: font.path, font_title: font.name } },
  };
}

function excelVisualDimensions(row) {
  if (row.visual !== "enabled") return [];
  const config = normalizedBatchVisualConfig(row.visualConfig);
  const stickers = validItems(state.cornerStickers);
  const corners = ["top_left", "top_right", "bottom_left", "bottom_right"];
  const mirror = [{
    id: `mirror-${config.mirrorIntervalSeconds}`,
    label: `每 ${config.mirrorIntervalSeconds} 秒镜像一次`,
    short_name: `镜${config.mirrorIntervalSeconds}`,
    patch: { visual_variant: { enabled: true, mirror_interval_seconds: config.mirrorIntervalSeconds } },
  }];
  const layouts = config.ratios.flatMap((ratio) => config.colors.map((color) => ({
    id: `layout-${ratio}-${color.replace("#", "")}`,
    label: `${ratio} + ${color}`,
    short_name: `${ratio === "1:1" ? "方" : "竖"}${color.slice(1, 2)}`,
    patch: {
      visual_variant: {
        enabled: true,
        crop_ratio: ratio,
        background_color: color,
        face_centered: true,
        face_sample_count: 3,
        crop_offset_y: config.cropOffsetY,
        crop_zoom: config.cropZoom,
      },
    },
  })));
  const cornerStickers = stickers.map((sticker, startIndex) => {
    const selected = corners.map((corner, offset) => {
      const item = stickers[(startIndex + offset) % stickers.length];
      return {
        sticker_json_path: item.path,
        start_us: 0,
        duration_us: 0,
        corner,
        visible_ratio: config.visibleRatio,
        scale: config.stickerScale,
        opacity: config.stickerOpacity,
      };
    });
    return {
      id: `corners-${sticker.identity || sticker.path}-${config.visibleRatio}-${config.stickerScale}-${config.stickerOpacity}`,
      label: selected.map((item, index) => `${corners[index]}:${stickers[(startIndex + index) % stickers.length].name || "贴纸"}`).join(" / "),
      short_name: sticker.name || "角贴",
      append: { stickers: selected },
    };
  });
  return [
    { key: "mirror", label: "分段镜像", mode: "fixed", candidates: mirror },
    { key: "layout", label: "裁剪填色", mode: "product", candidates: layouts },
    { key: "corner_sticker", label: "四角贴纸", mode: "product", candidates: cornerStickers },
  ];
}

function excelDimension(kind, value, row = null) {
  if (value === "none") return null;
  const candidates = excelSelectionCandidates(kind, value).map((item) => {
    if (kind === "audio") return excelAudioCandidate(item, Number(row?.bgmVolume ?? 0.25));
    return ({ effect: excelEffectCandidate, sticker: excelStickerCandidate, font: excelFontCandidate }[kind](item));
  });
  return {
    key: { audio: "bgm", effect: "effect", sticker: "sticker", font: "font" }[kind],
    label: { audio: "音乐", effect: "特效", sticker: "贴纸", font: "字体" }[kind],
    mode: value.startsWith("specific:") ? "fixed" : "product",
    candidates,
  };
}

function buildExcelSubmitRow(row, evaluation) {
  const dimensions = ["audio", "effect", "sticker", "font"]
    .map((kind) => excelDimension(kind, row[kind], row))
    .filter(Boolean);
  dimensions.push(...excelVisualDimensions(row));
  const job = {
    schema: "jyd.render_job.v1",
    source: { type: "template", template_id: row.templateId, preserve_original_video: true },
    output: { skip_export: false },
    texts: [], text_templates: [], audios: [], effects: [],
    original_video_volume: Number(row.originalVolume ?? 1),
    remove_existing_audio: row.audio !== "none",
    remove_existing_effects: row.effect !== "none",
    export: { resolution: "1080P", framerate: "30fps", timeout: 1200 },
  };
  const cover = normalizedBatchCoverConfig(row.cover, row.coverEnabled);
  if (cover.enabled) {
    job.cover = coverJobConfig(cover);
  }
  return {
    enabled: row.enabled,
    row_number: row.rowNumber,
    task_name: `第${row.rowNumber}行-${availableMothers().find((item) => item.template_id === row.templateId)?.name || row.templateName || "母版"}`,
    job,
    dimensions,
    selection: { mode: "random", limit: evaluation.requestedCount },
  };
}

async function submitExcelBatch() {
  const summary = excelBatchSummary();
  if (!summary.enabledCount || summary.errorCount || !summary.videoCount || summary.videoCount > 500) {
    renderExcelRows();
    setExcelMessage("请先修正表格中的配置错误。", true);
    return;
  }
  const button = $("submitExcelBatchBtn");
  button.disabled = true;
  setExcelMessage();
  $("excelProgressPanel").classList.remove("hidden");
  $("excelProgressBar").style.width = "0";
  $("excelProgressText").textContent = "正在创建任务";
  $("excelProgressCounts").textContent = `0 / ${summary.videoCount}`;
  state.selectedResultJobIds.clear();
  state.lastBatch = null;
  state.terminalResultsShown = false;
  setResultMessage();
  try {
    const rows = state.excelRows
      .map((row, index) => ({ row, evaluation: summary.evaluations[index] }))
      .filter((item) => item.row.enabled)
      .map((item) => buildExcelSubmitRow(item.row, item.evaluation));
    const usedTemplateIds = new Set(rows.map((row) => row.job?.source?.template_id).filter(Boolean));
    const temporaryTemplateIds = availableMothers()
      .filter((mother) => mother.import_info?.lifecycle === "excel_batch_once")
      .map((mother) => mother.template_id)
      .concat(Array.from(state.excelTemporaryTemplateIds))
      .filter((templateId, index, values) => usedTemplateIds.has(templateId) && values.indexOf(templateId) === index);
    const result = await apiFetch("/api/render/excel-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rows,
        max_jobs: 500,
        source_filename: "网页批量任务",
        temporary_template_ids: temporaryTemplateIds,
      }),
    });
    rememberBatch(result.batch_id);
    clearPolling();
    const finished = await pollBatch();
    if (!finished) state.pollTimer = setInterval(pollBatch, 2000);
  } catch (error) {
    button.disabled = false;
    setExcelMessage(error.message, true);
  }
}

function clearPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function activeProgressElements() {
  const excel = workspaceMode() === "excel";
  return {
    panel: $(excel ? "excelProgressPanel" : "progressPanel"),
    bar: $(excel ? "excelProgressBar" : "progressBar"),
    text: $(excel ? "excelProgressText" : "progressText"),
    counts: $(excel ? "excelProgressCounts" : "progressCounts"),
  };
}

function setSubmitButtonsDisabled(disabled) {
  $("generateBtn").disabled = disabled;
  if (disabled) $("submitExcelBatchBtn").disabled = true;
  else if (state.excelRows.length) renderExcelRows();
  else $("submitExcelBatchBtn").disabled = true;
}

function formatRemainingTime(value) {
  if (value === null || value === undefined) return "首个视频完成后显示预计时间";
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  if (seconds < 60) return `预计还需 ${seconds} 秒`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `预计还需 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `预计还需 ${hours} 小时 ${minutes % 60} 分钟`;
}

function setResultMessage(message = "", isError = false) {
  const element = $("resultActionMessage");
  element.textContent = message;
  element.classList.toggle("hidden", !message);
  element.classList.toggle("error", isError);
}

function returnToEditor() {
  const excelBatch = (state.lastBatch?.jobs || []).some((job) => Number(job.variant?.excel_row_number || 0) > 0);
  setWorkspaceMode(excelBatch ? "excel" : "generate");
  $(excelBatch ? "excelBatchWorkspace" : "generateVideoWorkspace")
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function rememberBatch(batchId) {
  state.batchId = batchId || "";
  if (state.batchId) localStorage.setItem(LAST_BATCH_STORAGE_KEY, state.batchId);
  else localStorage.removeItem(LAST_BATCH_STORAGE_KEY);
  const url = new URL(window.location.href);
  if (state.batchId) url.searchParams.set("batch", state.batchId);
  else url.searchParams.delete("batch");
  window.history.replaceState(null, "", url);
}

function closeRecentBatches() {
  $("recentBatchesDialog").classList.add("hidden");
}

function formatBatchCreatedAt(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function recentBatchStatusText(batch) {
  const completed = Number(batch.counts?.completed || 0);
  const failed = Number(batch.counts?.failed || 0);
  const running = Number(batch.counts?.running || 0);
  const pending = Number(batch.counts?.pending || 0);
  const parts = [`成功 ${completed}`];
  if (failed) parts.push(`失败 ${failed}`);
  if (running) parts.push(`处理中 ${running}`);
  if (pending) parts.push(`排队 ${pending}`);
  return parts.join("，");
}

async function openBatch(batchId) {
  setWorkspaceMode("generate");
  clearPolling();
  rememberBatch(batchId);
  state.lastBatch = null;
  state.selectedResultJobIds.clear();
  state.terminalResultsShown = false;
  closeRecentBatches();
  $("progressPanel").classList.remove("hidden");
  $("generateBtn").disabled = true;
  const finished = await pollBatch();
  if (!finished) state.pollTimer = setInterval(pollBatch, 2000);
}

async function deleteRecentBatch(batch) {
  const total = Number(batch.total || 0);
  const confirmed = window.confirm(
    `确定永久删除这个批次吗？\n\n将删除 ${total} 个任务的记录、MP4、生成草稿和临时目录，删除后不能恢复。`,
  );
  if (!confirmed) return;
  try {
    await apiFetch(`/api/admin/batches/${encodeURIComponent(batch.batch_id)}`, {
      method: "DELETE",
    });
    if (state.batchId === batch.batch_id) {
      clearPolling();
      rememberBatch("");
      state.lastBatch = null;
      state.selectedResultJobIds.clear();
      $("progressPanel").classList.add("hidden");
      $("resultsSection").classList.add("hidden");
    }
    await showRecentBatches();
  } catch (error) {
    if (error.status === 401) {
      window.location.href = `/local-admin/login?next=${encodeURIComponent("/app")}`;
      return;
    }
    window.alert(`删除失败：${error.message}`);
  }
}

async function showRecentBatches() {
  const dialog = $("recentBatchesDialog");
  const list = $("recentBatchesList");
  dialog.classList.remove("hidden");
  list.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "recent-batch-empty";
  loading.textContent = "正在读取任务记录...";
  list.append(loading);
  try {
    const batches = await apiFetch("/api/recent-batches?limit=30");
    list.replaceChildren();
    if (!batches.length) {
      const empty = document.createElement("div");
      empty.className = "recent-batch-empty";
      empty.textContent = "还没有可恢复的任务记录。";
      list.append(empty);
      return;
    }
    for (const batch of batches) {
      const row = document.createElement("article");
      row.className = "recent-batch-row";
      const main = document.createElement("div");
      main.className = "recent-batch-main";
      const title = document.createElement("strong");
      title.textContent = `${formatBatchCreatedAt(batch.created_at)} · ${batch.total || 0} 个任务`;
      const summary = document.createElement("span");
      summary.textContent = `${recentBatchStatusText(batch)} · 可预览 ${batch.available_outputs || 0} 个`;
      const id = document.createElement("span");
      id.textContent = `编号 ${batch.batch_id}`;
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "查看结果";
      open.addEventListener("click", () => openBatch(batch.batch_id));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "recent-batch-delete";
      remove.textContent = "删除记录";
      remove.disabled = ["pending", "running"].includes(batch.status);
      remove.title = remove.disabled ? "任务仍在处理，暂时不能删除" : "删除整个批次及其临时文件";
      remove.addEventListener("click", () => deleteRecentBatch(batch));
      const actions = document.createElement("div");
      actions.className = "recent-batch-actions";
      actions.append(open, remove);
      main.append(title, summary, id);
      row.append(main, actions);
      list.append(row);
    }
  } catch (error) {
    list.replaceChildren();
    const failed = document.createElement("div");
    failed.className = "recent-batch-empty";
    failed.textContent = `读取失败：${error.message}`;
    list.append(failed);
  }
}

function selectableResultJobs(batch) {
  return (batch?.jobs || []).filter(
    (job) => job.status === "completed" && job.result?.exported && !job.output_deleted,
  );
}

function updateResultToolbar(batch = state.lastBatch) {
  if (!batch) return;
  state.lastBatch = batch;
  const selectable = selectableResultJobs(batch);
  const selectableIds = new Set(selectable.map((job) => job.job_id));
  for (const jobId of state.selectedResultJobIds) {
    if (!selectableIds.has(jobId)) state.selectedResultJobIds.delete(jobId);
  }
  const selectedCount = state.selectedResultJobIds.size;
  const selectAll = $("selectAllResults");
  selectAll.disabled = !selectable.length;
  selectAll.checked = selectable.length > 0 && selectedCount === selectable.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < selectable.length;
  $("selectedResultCount").textContent = `已选 ${selectedCount} 个`;
  $("downloadSelectedBtn").disabled = selectedCount === 0;
  $("deleteSelectedBtn").disabled = selectedCount === 0;
  $("cancelBatchBtn").disabled = Number(batch.counts?.pending || 0) === 0;
  $("retryFailedBtn").disabled = Number(batch.counts?.failed || 0) === 0;
  $("resultSelectionControl").classList.toggle("hidden", isLocalMode());
  $("downloadSelectedBtn").classList.toggle("hidden", isLocalMode());
  $("deleteSelectedBtn").classList.toggle("hidden", isLocalMode());
  const expirations = (batch.jobs || [])
    .filter((job) => !job.output_deleted && job.expires_at)
    .map((job) => new Date(job.expires_at))
    .filter((value) => !Number.isNaN(value.getTime()))
    .sort((left, right) => left - right);
  const retentionNote = $("resultRetentionNote");
  if (isLocalMode()) {
    retentionNote.textContent = state.localOutputFolder
      ? `视频已直接保存到：${state.localOutputFolder}`
      : "视频已直接保存到本机所选文件夹。";
    retentionNote.classList.remove("hidden");
  } else if (expirations.length) {
    const formatted = new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(expirations[0]);
    retentionNote.textContent = workspaceMode() === "excel"
      ? `临时结果最早将在 ${formatted} 到期，请及时下载。批量任务上传的母版仅供本批次使用，批次成功或取消后自动删除。`
      : `临时结果最早将在 ${formatted} 到期，请及时下载。普通上传的剪辑母版保留 48 小时，永久素材库不会自动删除。`;
    retentionNote.classList.remove("hidden");
  } else {
    retentionNote.textContent = "";
    retentionNote.classList.add("hidden");
  }
}

function renderResults(batch) {
  state.lastBatch = batch;
  updateResultToolbar(batch);
  const grid = $("resultGrid");
  grid.replaceChildren();
  for (const job of batch.jobs || []) {
    const card = document.createElement("article");
    card.className = "result-card";
    const completed = job.status === "completed" && job.result?.exported && !job.output_deleted;
    if (state.selectedResultJobIds.has(job.job_id)) card.classList.add("selected");
    if (completed) {
      if (isLocalMode()) {
        const placeholder = document.createElement("div");
        placeholder.className = "result-video result-placeholder";
        placeholder.textContent = "已导出到本机";
        card.append(placeholder);
      } else {
        const video = document.createElement("video");
        video.className = "result-video";
        video.controls = true;
        video.preload = "metadata";
        video.src = `/api/jobs/${job.job_id}/preview`;
        card.append(video);
      }
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "result-video result-placeholder";
      placeholder.textContent = job.output_deleted
        ? "输出已删除"
        : job.status === "failed"
          ? "生成失败"
          : job.status === "cancelled"
            ? "任务已取消"
            : "等待导出";
      card.append(placeholder);
    }
    const body = document.createElement("div");
    body.className = "result-body";
    const title = document.createElement("div");
    title.className = "result-title";
    if (completed && !isLocalMode()) {
      const selectLabel = document.createElement("label");
      selectLabel.className = "result-checkbox";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedResultJobIds.has(job.job_id);
      checkbox.setAttribute("aria-label", `选择 ${job.variant?.display_name || `视频 ${job.index}`}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedResultJobIds.add(job.job_id);
        else state.selectedResultJobIds.delete(job.job_id);
        card.classList.toggle("selected", checkbox.checked);
        updateResultToolbar();
      });
      selectLabel.append(checkbox);
      title.append(selectLabel);
    }
    const name = document.createElement("strong");
    name.textContent = job.variant?.display_name || `视频 ${job.index}`;
    name.title = name.textContent;
    const status = document.createElement("span");
    const displayStatus = job.output_deleted ? "deleted" : job.status;
    status.className = `result-state ${displayStatus}`;
    status.textContent = {
      pending: "排队中",
      running: "处理中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
      deleted: "已删除",
    }[displayStatus] || displayStatus;
    title.append(name, status);
    const summary = document.createElement("div");
    summary.className = "result-summary";
    summary.textContent = job.variant?.summary || "未使用变化元素";
    body.append(title, summary);
    if (completed && isLocalMode()) {
      const actions = document.createElement("div");
      actions.className = "recent-batch-actions";
      for (const [action, label] of [["play", "打开视频"], ["reveal", "打开所在文件夹"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await apiFetch(`/api/local/jobs/${job.job_id}/open`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action }),
            });
          } catch (error) {
            setResultMessage(error.message, true);
          } finally {
            button.disabled = false;
          }
        });
        actions.append(button);
      }
      body.append(actions);
    } else if (completed) {
      const download = document.createElement("a");
      download.className = "result-download";
      download.href = `/api/jobs/${job.job_id}/download`;
      download.textContent = "下载 MP4";
      body.append(download);
    } else if (job.error) {
      const error = document.createElement("div");
      error.className = "result-error";
      error.textContent = job.error;
      body.append(error);
    }
    card.append(body);
    grid.append(card);
  }
  $("resultsSection").classList.toggle(
    "hidden",
    !["generate", "excel"].includes(workspaceMode()) || !(batch.jobs || []).length,
  );
}

async function pollBatch() {
  try {
    const batch = await apiFetch(`/api/batches/${state.batchId}`);
    const finished = Number(batch.finished || 0);
    const total = Number(batch.total || 0);
    const progress = activeProgressElements();
    progress.panel.classList.remove("hidden");
    progress.bar.style.width = `${total ? Math.round((finished / total) * 100) : 0}%`;
    progress.text.textContent = {
      completed: "全部生成完成",
      failed: "批次已结束，存在失败项",
      cancelled: "未开始的任务已取消",
    }[batch.status] || "正在生成视频";
    const details = [`${finished} / ${total}`];
    if (Number(batch.counts?.failed || 0)) details.push(`失败 ${batch.counts.failed}`);
    if (Number(batch.counts?.cancelled || 0)) details.push(`取消 ${batch.counts.cancelled}`);
    if (!['completed', 'failed', 'cancelled'].includes(batch.status)) {
      details.push(formatRemainingTime(batch.estimated_remaining_seconds));
    }
    progress.counts.textContent = details.join(" · ");
    renderResults(batch);
    if (["completed", "failed", "cancelled"].includes(batch.status)) {
      clearPolling();
      setSubmitButtonsDisabled(false);
      const completed = Number(batch.counts?.completed || 0);
      const failed = Number(batch.counts?.failed || 0);
      const cancelled = Number(batch.counts?.cancelled || 0);
      const parts = [`本批次已结束：成功 ${completed} 个`];
      if (failed) parts.push(`失败 ${failed} 个`);
      if (cancelled) parts.push(`取消 ${cancelled} 个`);
      parts.push(completed ? "成功视频仍可正常预览和下载" : "可点击“重试失败项”再次处理");
      setResultMessage(`${parts.join("，")}。`, completed === 0 && failed > 0);
      if (!state.terminalResultsShown) {
        state.terminalResultsShown = true;
        $("resultsSection").scrollIntoView({ behavior: "smooth", block: "start" });
      }
      return true;
    }
    return false;
  } catch (error) {
    setSubmitButtonsDisabled(false);
    if (error.status === 404) {
      clearPolling();
      rememberBatch("");
      if (workspaceMode() === "excel") setExcelMessage("之前的批次记录已不存在，请重新创建。", true);
      else setMessage("之前的批次记录已不存在，请重新生成。");
      return true;
    }
    if (workspaceMode() === "excel") setExcelMessage(`暂时无法读取任务进度：${error.message}。页面会继续重试。`, true);
    else setMessage(`暂时无法读取任务进度：${error.message}。页面会继续重试。`);
    return false;
  }
}

async function downloadSelectedResults() {
  const jobIds = [...state.selectedResultJobIds];
  if (!jobIds.length) return;
  $("downloadSelectedBtn").disabled = true;
  setResultMessage("正在打包选中的视频...");
  try {
    const result = await apiFetch(`/api/batches/${state.batchId}/downloads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_ids: jobIds }),
    });
    setResultMessage(`已打包 ${result.count} 个视频，浏览器将开始下载。`);
    const link = document.createElement("a");
    link.href = result.url;
    link.click();
  } catch (error) {
    setResultMessage(error.message, true);
  } finally {
    updateResultToolbar();
  }
}

async function deleteSelectedResults() {
  const jobIds = [...state.selectedResultJobIds];
  if (!jobIds.length || !window.confirm(`确定删除选中的 ${jobIds.length} 个 MP4 和生成草稿吗？`)) return;
  $("deleteSelectedBtn").disabled = true;
  setResultMessage("正在删除临时输出...");
  try {
    const result = await apiFetch(`/api/batches/${state.batchId}/delete-outputs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_ids: jobIds }),
    });
    state.selectedResultJobIds.clear();
    setResultMessage(`已删除 ${result.deleted.length} 个受管输出。`);
    await pollBatch();
  } catch (error) {
    setResultMessage(error.message, true);
  } finally {
    updateResultToolbar();
  }
}

async function cancelPendingResults() {
  if (!state.batchId || !window.confirm("确定取消这个批次中所有尚未开始的任务吗？正在处理的任务会继续完成。")) return;
  $("cancelBatchBtn").disabled = true;
  try {
    const result = await apiFetch(`/api/batches/${state.batchId}/cancel`, { method: "POST" });
    setResultMessage(`已取消 ${result.cancelled_now || 0} 个尚未开始的任务。`);
    await pollBatch();
  } catch (error) {
    setResultMessage(error.message, true);
  }
}

async function retryFailedResults() {
  if (!state.batchId) return;
  $("retryFailedBtn").disabled = true;
  try {
    const result = await apiFetch(`/api/batches/${state.batchId}/retry-failed`, { method: "POST" });
    clearPolling();
    rememberBatch(result.batch_id);
    state.lastBatch = null;
    state.selectedResultJobIds.clear();
    state.terminalResultsShown = false;
    setResultMessage(`已创建失败重试批次，共 ${result.total} 个任务。`);
    $("progressPanel").classList.remove("hidden");
    const finished = await pollBatch();
    if (!finished) state.pollTimer = setInterval(pollBatch, 2000);
  } catch (error) {
    setResultMessage(error.message, true);
    updateResultToolbar();
  }
}

async function submitBatch() {
  const combination = dimensionCounts();
  if (combination.errors.length) {
    setMessage(combination.errors.join("；"));
    return;
  }
  $("generateBtn").disabled = true;
  setMessage();
  $("progressPanel").classList.remove("hidden");
  $("progressText").textContent = "正在准备素材";
  $("progressCounts").textContent = `0 / ${combination.selectedTotal}`;
  $("progressBar").style.width = "0";
  state.selectedResultJobIds.clear();
  state.lastBatch = null;
  state.terminalResultsShown = false;
  setResultMessage();
  try {
    if (isLocalMode() && !state.localOutputFolder) {
      throw new Error("请先选择视频导出文件夹");
    }
    let source;
    if (sourceMode() === "mother") {
      const mother = selectedMother();
      if (!mother) throw new Error("请选择一个剪辑母版");
      source = { type: "template", template_id: mother.template_id, preserve_original_video: true };
    } else {
      if (isLocalMode()) {
        if (!state.localVideo?.media_id) throw new Error("请选择一个本机视频文件");
        source = { type: "video", media_id: state.localVideo.media_id };
      } else {
        const file = $("videoFile").files[0];
        if (!file) throw new Error("请选择一个视频文件");
        const media = await uploadVideo(file);
        source = { type: "video", media_id: media.media_id };
      }
    }
    const result = await apiFetch("/api/render/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job: buildBaseJob(source),
        dimensions: buildDimensions(),
        selection: { mode: "random", limit: combination.selectedTotal },
        max_jobs: 500,
      }),
    });
    rememberBatch(result.batch_id);
    clearPolling();
    const finished = await pollBatch();
    if (!finished) state.pollTimer = setInterval(pollBatch, 2000);
  } catch (error) {
    $("generateBtn").disabled = false;
    setMessage(error.message);
  }
}

async function loadData() {
  const savedCollectorUrl = localStorage.getItem(COLLECTOR_URL_STORAGE_KEY);
  $("collectorUrl").value = savedCollectorUrl || defaultCollectorUrl();
  void connectCollector({ quiet: true });

  const sessionPromise = apiFetch("/api/auth/session")
    .then((session) => {
      if (session.user?.display_name) {
        $("currentUserLabel").textContent = `${session.user.display_name}（${session.username}）`;
      } else if (session.username) {
        $("currentUserLabel").textContent = session.username;
      } else if (session.auth_center_online === false) {
        $("currentUserLabel").textContent = "账号中心离线";
      } else {
        $("currentUserLabel").textContent = session.authenticated ? "已登录" : "未登录";
      }
      return session;
    })
    .catch((error) => {
      $("currentUserLabel").textContent = "账号状态异常";
      console.warn("账号状态读取失败", error);
      return null;
    });

  let health;
  try {
    health = await apiFetch("/api/health");
    state.localFileAccess = Boolean(health.local_file_access);
    const configuredSharedUrl = String(health.shared_processor_url || "").trim();
    if (configuredSharedUrl) {
      try {
        const configuredOrigin = normalizedServerUrl(configuredSharedUrl);
        if (!state.sharedWorkspaceUrls.includes(configuredOrigin)) state.sharedWorkspaceUrls.push(configuredOrigin);
        saveSharedWorkspaceUrls();
      } catch { /* Ignore obsolete packaged addresses. */ }
    }
    const localModeInput = document.querySelector('input[name="processingMode"][value="local"]');
    const sharedModeInput = document.querySelector('input[name="processingMode"][value="shared"]');
    localModeInput.disabled = false;
    sharedModeInput.disabled = false;
    if (state.localFileAccess) localModeInput.checked = true;
    else sharedModeInput.checked = true;
    $("localAssetsWorkspaceChoice").classList.toggle("hidden", !state.localFileAccess);
    $("sharedWorkspaceActions").classList.toggle("hidden", !state.localFileAccess);
    if (state.localFileAccess) await refreshSharedWorkspaceStatuses();
    if (!state.localFileAccess && workspaceMode() === "assets") {
      setWorkspaceMode("upload");
    }
    $("localProcessingChoice").title = state.localFileAccess
      ? "当前为本机独立服务"
      : "只有安装在本机的独立服务才能直接访问本机文件";
    const onlineCount = Number(health.online_agents || 0);
    const busyCount = Number(health.busy_agents || 0);
    if (!health.ok) {
      $("apiStatus").textContent = "服务异常";
      $("apiStatus").className = "status bad";
    } else if (!onlineCount) {
      $("apiStatus").textContent = "等待处理机";
      $("apiStatus").className = "status bad";
    } else {
      const onlineLabel = health.execution_mode === "embedded"
        ? "本机处理机在线"
        : `${onlineCount} 台处理机在线`;
      $("apiStatus").textContent = `${onlineLabel}${busyCount ? ` · ${busyCount} 台忙碌` : ""}`;
      $("apiStatus").className = "status ok";
    }
  } catch (error) {
    $("apiStatus").textContent = "网站服务离线";
    $("apiStatus").className = "status bad";
    setMessage(error.message);
    await sessionPromise;
    return;
  }

  const initializationWarnings = [];
  if (state.localFileAccess) {
    try {
      const localConfig = await apiFetch("/api/local/config");
      state.personalLibraryRoot = localConfig.personal_library_root || "";
      if (state.personalLibraryRoot) {
        try {
          await collectorFetch("/api/config/personal-library-root", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ personal_library_root: state.personalLibraryRoot }),
          });
        } catch (error) {
          console.warn("个人素材目录暂未同步到采集器", error);
        }
      }
    } catch (error) {
      initializationWarnings.push(`本机配置（${error.message}）`);
    }
  }

  const assetRequests = [
    { label: "母版库", key: "templates", fallback: [], request: apiFetch("/api/templates") },
    { label: "音乐库", key: "audio", fallback: { categories: [], assets: [] }, request: apiFetch("/api/audio-library") },
    { label: "特效库", key: "effects", fallback: [], request: apiFetch("/api/assets/effects") },
    { label: "字体库", key: "fonts", fallback: [], request: apiFetch("/api/assets/fonts") },
    { label: "字幕样式库", key: "textStyles", fallback: [], request: apiFetch("/api/assets/text-styles") },
    { label: "花字库", key: "textEffects", fallback: [], request: apiFetch("/api/assets/text-effects") },
    { label: "全屏贴纸库", key: "stickers", fallback: [], request: apiFetch("/api/assets/stickers") },
    { label: "四角贴纸库", key: "cornerStickers", fallback: [], request: apiFetch("/api/assets/corner-stickers") },
  ];
  const assetResults = await Promise.allSettled(assetRequests.map((item) => item.request));
  assetResults.forEach((result, index) => {
    const definition = assetRequests[index];
    if (result.status === "fulfilled") {
      state[definition.key] = result.value;
    } else {
      state[definition.key] = definition.fallback;
      initializationWarnings.push(`${definition.label}（${result.reason?.message || "加载失败"}）`);
    }
  });
  if (initializationWarnings.length) {
    setMessage(`部分内容暂未加载：${initializationWarnings.join("、")}。本机处理机仍可正常使用。`);
  }

  fillMothers();
  fillAudioCategories();
  fillFonts();
  fillCaptionStyles();
  fillBatchDefaultControls();
  renderExcelRows();
  if (state.localFileAccess) {
    const savedOutputFolder = window.localStorage.getItem(LOCAL_OUTPUT_FOLDER_STORAGE_KEY) || "";
    if (savedOutputFolder) {
      state.localOutputFolder = savedOutputFolder;
      $("localOutputFolder").value = savedOutputFolder;
      $("localOutputStatus").textContent = `已恢复上次导出文件夹：${savedOutputFolder}`;
    }
  }
  updateSourceUi();
  updateProcessingModeUi();
  updateWorkspaceUi();
  const requestedBatchId = new URLSearchParams(window.location.search).get("batch") || "";
  if (requestedBatchId) {
    try {
      await openBatch(requestedBatchId);
    } catch (error) {
      setMessage(error.message);
    }
  }
  await sessionPromise;
}

function bindEvents() {
  document.querySelectorAll('input[name="workspaceMode"]').forEach((input) => input.addEventListener("change", updateWorkspaceUi));
  document.querySelectorAll('input[name="sourceMode"]').forEach((input) => input.addEventListener("change", () => {
    if (input.checked && input.value !== "video") {
      state.digitalHumanCaptionCues = [];
      state.digitalHumanSourceItemId = "";
    }
    updateSourceUi();
  }));
  document.querySelectorAll('input[name="processingMode"]').forEach((input) => input.addEventListener("change", handleProcessingModeChange));
  $("configureSharedWorkspaceBtn").addEventListener("click", () => {
    configureSharedWorkspace().catch((error) => setMessage(error.message));
  });
  $("refreshSharedWorkspacesBtn").addEventListener("click", () => {
    refreshSharedWorkspaceStatuses().catch((error) => setMessage(error.message));
  });
  $("videoFile").addEventListener("change", () => {
    state.digitalHumanCaptionCues = [];
    state.digitalHumanSourceItemId = "";
    const file = $("videoFile").files[0];
    $("videoFileName").textContent = file?.name || "选择 MP4 视频";
    updateSourcePreview();
  });
  $("selectLocalVideoBtn").addEventListener("click", selectLocalVideo);
  $("selectLocalOutputFolderBtn").addEventListener("click", selectLocalOutputFolder);
  $("motherSelect").addEventListener("change", updateMotherMeta);
  $("refreshMothersBtn").addEventListener("click", async () => {
    const button = $("refreshMothersBtn");
    button.disabled = true;
    try {
      await refreshMothers($("motherSelect").value);
    } catch (error) {
      setMessage(`刷新母版失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
  $("saveCollectorConfigBtn").addEventListener("click", saveCollectorConfig);
  $("refreshCollectorDraftsBtn").addEventListener("click", refreshCollectorDrafts);
  $("refreshPersonalAssetDraftsBtn").addEventListener("click", refreshPersonalAssetDrafts);
  $("analyzeCollectorDraftBtn").addEventListener("click", analyzeCollectorDraft);
  $("uploadPersonalAssetsBtn").addEventListener("click", collectPersonalAssets);
  $("refreshPersonalAssetsBtn").addEventListener("click", () => loadPersonalAssets());
  $("personalAssetSearch").addEventListener("input", () => {
    state.personalAssets.page = 1;
    renderPersonalAssets();
  });
  ["personalAssetKindFilter", "personalAssetStatusFilter"].forEach((id) => {
    $(id).addEventListener("change", () => {
      state.personalAssets.page = 1;
      renderPersonalAssets();
    });
  });
  $("personalAssetPreviousBtn").addEventListener("click", () => {
    state.personalAssets.page = Math.max(1, state.personalAssets.page - 1);
    renderPersonalAssets();
  });
  $("personalAssetNextBtn").addEventListener("click", () => {
    state.personalAssets.page += 1;
    renderPersonalAssets();
  });
  $("closePersonalAssetPreviewBtn").addEventListener("click", () => {
    $("personalAssetPreviewDialog").close();
  });
  $("personalAssetPreviewDialog").addEventListener("click", (event) => {
    if (event.target === $("personalAssetPreviewDialog")) {
      $("personalAssetPreviewDialog").close();
    }
  });
  $("buildCollectorPlanBtn").addEventListener("click", buildCollectorPlan);
  $("uploadCollectorPlanBtn").addEventListener("click", uploadCollectorPlan);
  $("extractCollectorFontsBtn").addEventListener("click", extractCollectorFonts);
  $("collectorDraftSearch").addEventListener("input", (event) => {
    state.collector.query = event.target.value.trim().toLowerCase();
    renderCollectorDrafts();
  });
  ["collectorPolicyAudio", "collectorPolicyEffects", "collectorPolicyTextStyle", "collectorPolicyTextEffects"].forEach(
    (id) => $(id).addEventListener("change", invalidateCollectorPlan),
  );
  $("collectorUrl").addEventListener("change", () => {
    localStorage.setItem(COLLECTOR_URL_STORAGE_KEY, collectorBaseUrl());
    void connectCollector();
  });
  $("audioCategory").addEventListener("change", updateCounts);
  ["useAudio", "useEffects", "useStickers", "useVisualVariant", "useCaptions", "useTextEffects", "useCover"].forEach((id) => $(id).addEventListener("change", updateCounts));
  [
    "mirrorIntervalSeconds",
    "variantRatioSquare",
    "variantRatioThreeFour",
    "useVariantColor1",
    "variantColor1",
    "useVariantColor2",
    "variantColor2",
    "useVariantColor3",
    "variantColor3",
    "useVariantColor4",
    "variantColor4",
    "cornerStickerOpacity",
    "cropPreviewRatio",
    "cropOffsetY",
    "cropZoom",
    "bgmVolume",
    "originalVolume",
  ].forEach((id) => $(id).addEventListener("input", updateCounts));
  $("fontSelection").addEventListener("change", () => {
    updateCounts();
    updateFontPreview();
    updateSingleCoverPreview();
  });
  $("captionText").addEventListener("input", () => {
    state.digitalHumanCaptionCues = [];
    state.digitalHumanSourceItemId = "";
    updateCounts();
  });
  $("refreshDigitalHumanTasksBtn").addEventListener("click", () => refreshDigitalHumanTasks());
  $("flowerText").addEventListener("input", updateCounts);
  ["coverFrameTimeSeconds", "coverTextLine1", "coverTextLine2"].forEach((id) => $(id).addEventListener("input", () => {
    updateCounts();
    updateSingleCoverPreview();
  }));
  $("useCover").addEventListener("change", updateSingleCoverPreview);
  $("editSingleCoverBtn").addEventListener("click", openSingleCoverEditor);
  $("useCaptions").addEventListener("change", updateSingleCoverPreview);
  $("usePreviewTimeBtn").addEventListener("click", () => {
    const preview = $("sourcePreview");
    if (!preview.currentSrc || !Number.isFinite(preview.currentTime)) {
      setMessage("请先选择并加载母版或视频预览");
      return;
    }
    $("coverFrameTimeSeconds").value = preview.currentTime.toFixed(2);
    updateCounts();
  });
  ["seeking", "seeked", "timeupdate"].forEach((eventName) => {
    $("sourcePreview").addEventListener(eventName, (event) => {
      syncSingleCoverTimeFromPreview(event);
      updateVisualCropPreview();
    });
  });
  $("sourcePreview").addEventListener("loadedmetadata", updateVisualCropPreview);
  $("visualCropPreview").addEventListener("loadedmetadata", updateVisualCropPreview);
  $("resetCropAdjustBtn").addEventListener("click", () => {
    $("cropOffsetY").value = "0";
    $("cropZoom").value = "100";
    updateCounts();
  });
  $("coverFrameTimeSeconds").addEventListener("change", () => {
    const preview = $("sourcePreview");
    const time = Number($("coverFrameTimeSeconds").value);
    if (preview.currentSrc && Number.isFinite(time) && time >= 0) preview.currentTime = time;
  });
  $("generationLimit").addEventListener("input", updateCounts);
  $("loadExcelLocalDraftsBtn").addEventListener("click", loadExcelLocalDrafts);
  $("selectAllExcelDrafts").addEventListener("change", (event) => {
    state.excelSelectedDraftPaths = new Set(
      event.target.checked ? (state.collector.drafts || []).map((draft) => draft.path) : [],
    );
    renderExcelLocalDrafts();
  });
  $("uploadSelectedExcelDraftsBtn").addEventListener("click", uploadSelectedExcelDrafts);
  $("addBatchTaskBtn").addEventListener("click", addEmptyBatchTask);
  $("applyBatchDefaultsBtn").addEventListener("click", applyBatchDefaultsToSelected);
  $("deleteSelectedBatchRowsBtn").addEventListener("click", deleteSelectedBatchRows);
  $("batchDefaultVisual").addEventListener("change", updateBatchVisualSettings);
  [
    "batchBgmVolume", "batchOriginalVolume", "batchCropOffsetY", "batchCropZoom",
  ].forEach((id) => $(id).addEventListener("input", () => {
    updateRangeOutputs();
    if (state.batchVisualRowId) updateBatchVisualPreview();
  }));
  $("batchCropPreviewRatio").addEventListener("change", updateBatchVisualPreview);
  $("closeBatchVisualPreviewBtn").addEventListener("click", closeBatchVisualPreview);
  $("finishBatchVisualPreviewBtn").addEventListener("click", saveBatchVisualPreview);
  $("resetBatchCropBtn").addEventListener("click", () => {
    $("batchCropOffsetY").value = "0";
    $("batchCropZoom").value = "100";
    updateBatchVisualPreview();
  });
  $("batchVisualPreviewDialog").addEventListener("click", (event) => {
    if (event.target === $("batchVisualPreviewDialog")) closeBatchVisualPreview();
  });
  $("selectAllBatchRows").addEventListener("change", (event) => {
    state.batchSelectedRowIds = new Set(event.target.checked ? state.excelRows.map((row) => row.clientId) : []);
    renderExcelRows();
  });
  $("batchDefaultCount").addEventListener("input", () => {
    if (state.excelRows.length) renderExcelRows();
  });
  $("submitExcelBatchBtn").addEventListener("click", submitExcelBatch);
  $("closeBatchCoverBtn").addEventListener("click", closeBatchCoverEditor);
  $("cancelBatchCoverBtn").addEventListener("click", closeBatchCoverEditor);
  $("saveBatchCoverBtn").addEventListener("click", saveBatchCoverEditor);
  [
    "batchCoverTextLine1", "batchCoverTextLine2", "batchCoverFont",
    "batchCoverFrameOffsetX", "batchCoverFrameOffsetY", "batchCoverOverlayWidth",
    "batchCoverOverlayHeight", "batchCoverOverlayY", "batchCoverOverlayAlpha",
    "batchCoverLine1X", "batchCoverLine1Y", "batchCoverLine2X", "batchCoverLine2Y",
    "batchCoverLine1Size", "batchCoverLine2Size", "batchCoverLine1Color", "batchCoverLine2Color",
  ].forEach((id) => {
    $(id).addEventListener("input", updateBatchCoverPreview);
  });
  $("batchCoverDialog").addEventListener("click", (event) => {
    if (event.target === $("batchCoverDialog")) closeBatchCoverEditor();
  });
  $("batchCoverPreview").addEventListener("loadedmetadata", () => {
    const preview = $("batchCoverPreview");
    const initialTime = Number(preview.dataset.initialTime || 0);
    if (Number.isFinite(initialTime) && initialTime >= 0 && (!preview.duration || initialTime < preview.duration)) {
      preview.currentTime = initialTime;
    }
  });
  ["seeking", "seeked", "timeupdate"].forEach((eventName) => {
    $("batchCoverPreview").addEventListener(eventName, syncBatchCoverTimeFromPreview);
  });
  $("batchCoverFrameTimeSeconds").addEventListener("change", () => {
    const preview = $("batchCoverPreview");
    const time = Number($("batchCoverFrameTimeSeconds").value);
    if (preview.currentSrc && Number.isFinite(time) && time >= 0) preview.currentTime = time;
  });
  $("sourcePreview").addEventListener("error", () => $("previewEmpty").classList.remove("hidden"));
  $("sourcePreview").addEventListener("loadedmetadata", () => $("previewEmpty").classList.add("hidden"));
  $("generateBtn").addEventListener("click", submitBatch);
  $("selectAllResults").addEventListener("change", (event) => {
    state.selectedResultJobIds.clear();
    if (event.target.checked) {
      selectableResultJobs(state.lastBatch).forEach((job) => state.selectedResultJobIds.add(job.job_id));
    }
    renderResults(state.lastBatch);
  });
  $("downloadSelectedBtn").addEventListener("click", downloadSelectedResults);
  $("deleteSelectedBtn").addEventListener("click", deleteSelectedResults);
  $("cancelBatchBtn").addEventListener("click", cancelPendingResults);
  $("retryFailedBtn").addEventListener("click", retryFailedResults);
  $("backToEditorBtn").addEventListener("click", returnToEditor);
  $("recentBatchesBtn").addEventListener("click", showRecentBatches);
  $("siteLogoutBtn").addEventListener("click", async () => {
    $("siteLogoutBtn").disabled = true;
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } finally {
      window.location.assign("/login");
    }
  });
  $("closeRecentBatchesBtn").addEventListener("click", closeRecentBatches);
  $("recentBatchesDialog").addEventListener("click", (event) => {
    if (event.target === $("recentBatchesDialog")) closeRecentBatches();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeRecentBatches();
      if (!$("batchCoverDialog").classList.contains("hidden")) closeBatchCoverEditor();
      if (!$("batchVisualPreviewDialog").classList.contains("hidden")) closeBatchVisualPreview();
    }
  });
}

enhanceRangeInputs();
bindEvents();
loadData();
