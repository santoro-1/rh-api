const form = document.querySelector("#loginForm");
const errorBox = document.querySelector("#loginError");
const loginButton = document.querySelector("#loginButton");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.classList.add("hidden");
  loginButton.disabled = true;
  try {
    const params = new URLSearchParams(window.location.search);
    const response = await fetch("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value,
        next: params.get("next") || "/app/assets",
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `登录失败：HTTP ${response.status}`);
    window.location.replace(payload.next || "/app/assets");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
    loginButton.disabled = false;
  }
});
