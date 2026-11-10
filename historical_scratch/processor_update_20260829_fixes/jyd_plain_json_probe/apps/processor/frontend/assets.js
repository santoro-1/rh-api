const state = { items: [], audioCategories: [], users: [], page: 1, pageSize: 30 };
const $ = (id) => document.getElementById(id);
const KIND_LABELS = {
  audio: "音乐",
  font: "字体",
  effect: "视频特效",
  sticker: "全屏贴纸",
  text_effect: "花字",
  text_style: "字幕样式",
  text_template: "复合文字模板",
  template: "剪辑母版",
};

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* Keep raw text. */ }
  if (!response.ok) {
    const detail = data && typeof data === "object" ? data.detail : data;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
  }
  return data;
}

function setMessage(message = "", error = false) {
  const node = $("message");
  node.textContent = message;
  node.classList.toggle("hidden", !message);
  node.classList.toggle("error", error);
}

function renderUsers() {
  const body = $("userRows");
  body.replaceChildren();
  $("userCount").textContent = `${state.users.length} 个账号`;
  $("userEmptyState").classList.toggle("hidden", state.users.length > 0);
  for (const user of state.users) {
    const row = document.createElement("tr");
    const accountCell = document.createElement("td");
    accountCell.className = "user-account-cell";
    const username = document.createElement("strong");
    username.textContent = user.username;
    const created = document.createElement("small");
    created.textContent = user.created_at ? `创建于 ${user.created_at.replace("T", " ")}` : user.user_id;
    accountCell.append(username, created);

    const displayCell = document.createElement("td");
    const displayInput = document.createElement("input");
    displayInput.className = "user-display-input";
    displayInput.type = "text";
    displayInput.maxLength = 80;
    displayInput.value = user.display_name || "";
    displayInput.placeholder = "可填写姓名或备注";
    displayCell.append(displayInput);

    const statusCell = document.createElement("td");
    const statusLabel = document.createElement("label");
    statusLabel.className = "user-status";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = user.enabled !== false;
    const statusText = document.createElement("span");
    statusText.textContent = enabled.checked ? "允许登录" : "已停用";
    enabled.addEventListener("change", async () => {
      enabled.disabled = true;
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: enabled.checked }),
        });
        await loadUsers(`${user.username} 已${enabled.checked ? "启用" : "停用"}`);
      } catch (error) {
        enabled.checked = !enabled.checked;
        setMessage(error.message, true);
      } finally {
        enabled.disabled = false;
      }
    });
    statusLabel.append(enabled, statusText);
    statusCell.append(statusLabel);

    const passwordCell = document.createElement("td");
    const passwordInput = document.createElement("input");
    passwordInput.className = "user-password-input";
    passwordInput.type = "password";
    passwordInput.minLength = 6;
    passwordInput.maxLength = 128;
    passwordInput.autocomplete = "new-password";
    passwordInput.placeholder = "不修改可留空";
    passwordCell.append(passwordInput);

    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "保存";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_name: displayInput.value.trim(),
            password: passwordInput.value,
          }),
        });
        passwordInput.value = "";
        await loadUsers(`已保存账号：${user.username}`);
      } catch (error) {
        setMessage(error.message, true);
      } finally {
        save.disabled = false;
      }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`确定删除内测账号“${user.username}”吗？该账号会立即退出。`)) return;
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, { method: "DELETE" });
        await loadUsers(`已删除账号：${user.username}`);
      } catch (error) {
        setMessage(error.message, true);
      }
    });
    actions.append(save, remove);
    actionCell.append(actions);
    row.append(accountCell, displayCell, statusCell, passwordCell, actionCell);
    body.append(row);
  }
}

async function loadUsers(message = "") {
  const data = await apiFetch("/api/admin/users");
  state.users = Array.isArray(data.users) ? data.users : [];
  renderUsers();
  const form = $("createUserForm");
  if (data.managed_remotely) {
    [...form.elements].forEach((element) => { element.disabled = true; });
    $("userCount").textContent = "统一账号中心管理";
    setMessage(`账号统一在 ${data.auth_server_url}/admin 管理`);
  }
  if (message) setMessage(message);
}

async function createUser(event) {
  event.preventDefault();
  const button = $("createUserBtn");
  button.disabled = true;
  try {
    const user = await apiFetch("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("newUsername").value.trim(),
        display_name: $("newUserDisplayName").value.trim(),
        password: $("newUserPassword").value,
      }),
    });
    $("createUserForm").reset();
    await loadUsers(`已新增内测账号：${user.username}`);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function filteredItems() {
  const query = $("searchInput").value.trim().toLocaleLowerCase();
  const kind = $("kindFilter").value;
  const status = $("statusFilter").value;
  return state.items.filter((item) => {
    if (kind && item.kind !== kind) return false;
    if (status === "active" && (item.deleted || item.enabled === false)) return false;
    if (status === "disabled" && (item.deleted || item.enabled !== false)) return false;
    if (status === "deleted" && !item.deleted) return false;
    const haystack = [item.name, item.original_name, item.identity, item.category]
      .join(" ").toLocaleLowerCase();
    return !query || haystack.includes(query);
  });
}

function option(value, label, selected = false) {
  const node = new Option(label, value, selected, selected);
  return node;
}

function buildCategoryControl(item) {
  if (item.kind === "audio") {
    const select = document.createElement("select");
    select.className = "category-input";
    const current = Array.isArray(item.category_ids) ? item.category_ids[0] || "unclassified" : "unclassified";
    state.audioCategories.forEach((category) => {
      select.append(option(category.id, category.name, category.id === current));
    });
    return select;
  }
  const input = document.createElement("input");
  input.className = "category-input";
  input.type = "text";
  input.maxLength = 80;
  input.placeholder = "可选管理标签";
  input.value = item.category || "";
  return input;
}

function renderRows() {
  const body = $("assetRows");
  body.replaceChildren();
  const filtered = filteredItems();
  const pageCount = Math.max(1, Math.ceil(filtered.length / state.pageSize));
  state.page = Math.min(Math.max(1, state.page), pageCount);
  const start = (state.page - 1) * state.pageSize;
  const items = filtered.slice(start, start + state.pageSize);
  $("emptyState").classList.toggle("hidden", filtered.length > 0);
  $("pageSummary").textContent = filtered.length
    ? `共 ${filtered.length} 项，当前 ${start + 1}-${start + items.length}`
    : "0 项";
  $("pageNumber").textContent = `${state.page} / ${pageCount}`;
  $("previousPageBtn").disabled = state.page <= 1;
  $("nextPageBtn").disabled = state.page >= pageCount;
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.dataset.kind = item.kind;
    row.dataset.identity = item.identity;
    row.classList.toggle("deleted", item.deleted);

    const nameCell = document.createElement("td");
    nameCell.className = "name-cell";
    const nameInput = document.createElement("input");
    nameInput.className = "name-input";
    nameInput.type = "text";
    nameInput.maxLength = 120;
    nameInput.value = item.name || "";
    nameInput.disabled = item.deleted;
    const identity = document.createElement("small");
    identity.title = item.identity;
    identity.textContent = item.original_name !== item.name
      ? `原名：${item.original_name} · ${item.identity}`
      : item.identity;
    nameCell.append(nameInput, identity);

    const kindCell = document.createElement("td");
    const kindLabel = document.createElement("span");
    kindLabel.className = "kind-label";
    kindLabel.textContent = KIND_LABELS[item.kind] || item.kind;
    kindCell.append(kindLabel);

    const categoryCell = document.createElement("td");
    const categoryControl = buildCategoryControl(item);
    categoryControl.disabled = item.deleted;
    categoryCell.append(categoryControl);

    const enabledCell = document.createElement("td");
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "enabled-label";
    const enabled = document.createElement("input");
    enabled.className = "enabled-input";
    enabled.type = "checkbox";
    enabled.checked = item.enabled !== false;
    enabled.disabled = item.deleted;
    const enabledText = document.createElement("span");
    enabledText.textContent = item.deleted ? "已删除" : (enabled.checked ? "启用" : "停用");
    enabled.addEventListener("change", () => { enabledText.textContent = enabled.checked ? "启用" : "停用"; });
    enabledLabel.append(enabled, enabledText);
    enabledCell.append(enabledLabel);

    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "预览";
    preview.addEventListener("click", () => showPreview(item));
    actions.append(preview);
    if (item.deleted) {
      const restore = document.createElement("button");
      restore.type = "button";
      restore.textContent = "恢复";
      restore.addEventListener("click", () => restoreAsset(item));
      actions.append(restore);
    } else {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "primary";
      save.textContent = "保存";
      save.addEventListener("click", () => saveAsset(item, row));
      const trash = document.createElement("button");
      trash.type = "button";
      trash.className = "danger";
      trash.textContent = "移入回收站";
      trash.addEventListener("click", () => trashAsset(item));
      actions.append(save, trash);
    }
    actionCell.append(actions);

    row.append(nameCell, kindCell, categoryCell, enabledCell, actionCell);
    body.append(row);
  });
}

function renderSummary() {
  $("assetTotal").textContent = String(state.items.length);
  const counts = {};
  state.items.forEach((item) => { counts[item.kind] = (counts[item.kind] || 0) + 1; });
  $("kindCounts").replaceChildren(...Object.entries(KIND_LABELS).map(([kind, label]) => {
    const node = document.createElement("span");
    node.textContent = `${label} ${counts[kind] || 0}`;
    return node;
  }));
}

async function loadAssets(message = "") {
  try {
    const data = await apiFetch("/api/admin/assets?include_deleted=true");
    state.items = Array.isArray(data.items) ? data.items : [];
    state.audioCategories = Array.isArray(data.audio_categories) ? data.audio_categories : [];
    $("apiStatus").textContent = "处理机在线";
    $("apiStatus").className = "status ok";
    renderSummary();
    renderRows();
    setMessage(message);
  } catch (error) {
    $("apiStatus").textContent = "处理机离线";
    $("apiStatus").className = "status bad";
    setMessage(error.message, true);
  }
}

async function saveAsset(item, row) {
  const payload = {
    name: row.querySelector(".name-input").value.trim(),
    enabled: row.querySelector(".enabled-input").checked,
  };
  const categoryControl = row.querySelector(".category-input");
  if (item.kind === "audio") {
    payload.audio_category_ids = categoryControl.value === "unclassified" ? [] : [categoryControl.value];
  } else {
    payload.category = categoryControl.value.trim();
  }
  try {
    await apiFetch(`/api/admin/assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadAssets(`已保存：${payload.name || item.original_name}`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function trashAsset(item) {
  if (!window.confirm(`将“${item.name}”移入回收站？生成页将立即停止使用该素材。`)) return;
  try {
    await apiFetch(`/api/admin/assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}`, { method: "DELETE" });
    await loadAssets(`已移入回收站：${item.name}`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function restoreAsset(item) {
  try {
    await apiFetch(`/api/admin/assets/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.identity)}/restore`, { method: "POST" });
    await loadAssets(`已恢复：${item.name}`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

function showPreview(item) {
  $("previewTitle").textContent = item.name;
  $("previewMeta").textContent = `${KIND_LABELS[item.kind] || item.kind} · ${item.identity}`;
  const body = $("previewBody");
  body.replaceChildren();
  if (item.preview_type === "audio" && item.preview_url) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = item.preview_url;
    body.append(audio);
  } else if (item.preview_type === "video" && item.preview_url) {
    const video = document.createElement("video");
    video.controls = true;
    video.src = item.preview_url;
    body.append(video);
  } else if (item.preview_type === "image" && item.preview_url) {
    const image = document.createElement("img");
    image.src = item.preview_url;
    image.alt = item.name;
    body.append(image);
  } else if (item.preview_type === "font" && item.preview_url) {
    const family = `AssetPreview_${Math.random().toString(36).slice(2)}`;
    const style = document.createElement("style");
    style.textContent = `@font-face { font-family: "${family}"; src: url("${item.preview_url}"); font-display: swap; }`;
    const sample = document.createElement("div");
    sample.className = "font-sample";
    sample.style.fontFamily = `"${family}", sans-serif`;
    sample.textContent = "人生没有白走的路，每一步都算数 123";
    body.append(style, sample);
  } else {
    const metadata = document.createElement("pre");
    metadata.className = "metadata-preview";
    metadata.textContent = JSON.stringify(item, null, 2);
    body.append(metadata);
  }
  $("previewDialog").showModal();
}

async function createAudioCategory() {
  const name = $("newAudioCategory").value.trim();
  if (!name) {
    setMessage("请输入音乐分类名称", true);
    return;
  }
  try {
    await apiFetch("/api/audio-library/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    $("newAudioCategory").value = "";
    await loadAssets(`已新增音乐分类：${name}`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

[$("searchInput"), $("kindFilter"), $("statusFilter")].forEach((node) => {
  node.addEventListener(node.tagName === "INPUT" ? "input" : "change", () => {
    state.page = 1;
    renderRows();
  });
});
$("previousPageBtn").addEventListener("click", () => { state.page -= 1; renderRows(); });
$("nextPageBtn").addEventListener("click", () => { state.page += 1; renderRows(); });
$("createAudioCategoryBtn").addEventListener("click", createAudioCategory);
$("createUserForm").addEventListener("submit", createUser);
$("closePreviewBtn").addEventListener("click", () => $("previewDialog").close());
Promise.all([loadUsers(), loadAssets()]).catch((error) => setMessage(error.message, true));
