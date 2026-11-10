const form = document.querySelector("#loginForm");
const button = document.querySelector("#loginButton");
const message = document.querySelector("#message");

if (new URLSearchParams(window.location.search).get("center") === "offline") {
  message.textContent = "统一账号中心暂时无法连接，请检查当前电脑的网络连接。";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  message.textContent = "正在登录...";
  const next = new URLSearchParams(window.location.search).get("next") || "/app";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#username").value.trim(),
        password: document.querySelector("#password").value,
        next,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "登录失败");
    window.location.assign(data.next || "/app");
  } catch (error) {
    message.textContent = error.message;
    button.disabled = false;
  }
});
