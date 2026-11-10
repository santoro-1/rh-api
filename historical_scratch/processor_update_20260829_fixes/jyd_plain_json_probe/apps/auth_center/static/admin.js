const state = { users: [] };
const $ = (id) => document.getElementById(id);

async function apiFetch(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.location.replace("/admin/login");
    throw new Error("管理员登录已失效");
  }
  if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
  return data;
}

function showMessage(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}

function renderUsers() {
  const body = $("userRows");
  body.replaceChildren();
  $("userCount").textContent = `${state.users.length} 个账号`;
  $("emptyState").classList.toggle("hidden", state.users.length > 0);
  for (const user of state.users) {
    const row = document.createElement("tr");
    const account = document.createElement("td");
    account.innerHTML = `<strong></strong><small></small>`;
    account.querySelector("strong").textContent = user.username;
    account.querySelector("small").textContent = user.created_at ? `创建于 ${user.created_at.replace("T", " ")}` : "";

    const displayCell = document.createElement("td");
    const display = document.createElement("input");
    display.value = user.display_name || "";
    display.maxLength = 80;
    display.placeholder = "可填写姓名或备注";
    displayCell.append(display);

    const enabledCell = document.createElement("td");
    const enabledLabel = document.createElement("label");
    enabledLabel.className = "toggle";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = user.enabled !== false;
    const enabledText = document.createElement("span");
    enabledText.textContent = enabled.checked ? "已启用" : "已停用";
    enabled.addEventListener("change", async () => {
      enabled.disabled = true;
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: enabled.checked }),
        });
        await loadUsers(`${user.username} 已${enabled.checked ? "启用" : "停用"}`);
      } catch (error) {
        enabled.checked = !enabled.checked;
        showMessage(error.message, true);
      } finally { enabled.disabled = false; }
    });
    enabledLabel.append(enabled, enabledText);
    enabledCell.append(enabledLabel);

    const passwordCell = document.createElement("td");
    const password = document.createElement("input");
    password.type = "password";
    password.minLength = 8;
    password.maxLength = 128;
    password.placeholder = "不修改请留空";
    passwordCell.append(password);

    const actions = document.createElement("td");
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "保存";
    save.addEventListener("click", async () => {
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name: display.value.trim(), password: password.value }),
        });
        password.value = "";
        await loadUsers(`已保存账号：${user.username}`);
      } catch (error) { showMessage(error.message, true); }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      if (!confirm(`确定删除账号“${user.username}”吗？`)) return;
      try {
        await apiFetch(`/api/admin/users/${encodeURIComponent(user.user_id)}`, { method: "DELETE" });
        await loadUsers(`已删除账号：${user.username}`);
      } catch (error) { showMessage(error.message, true); }
    });
    actions.append(save, remove);
    row.append(account, displayCell, enabledCell, passwordCell, actions);
    body.append(row);
  }
}

async function loadUsers(message = "") {
  const data = await apiFetch("/api/admin/users");
  state.users = data.users || [];
  renderUsers();
  if (message) showMessage(message);
}

$("createUserForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const user = await apiFetch("/api/admin/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("newUsername").value.trim(),
        display_name: $("newDisplayName").value.trim(),
        password: $("newPassword").value,
      }),
    });
    event.target.reset();
    await loadUsers(`已新增账号：${user.username}`);
  } catch (error) { showMessage(error.message, true); }
});

$("logoutButton").addEventListener("click", async () => {
  await fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" });
  window.location.replace("/admin/login");
});

loadUsers().catch((error) => showMessage(error.message, true));
