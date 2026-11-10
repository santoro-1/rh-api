document.addEventListener("DOMContentLoaded", () => {
  const logoutButton = document.querySelector("#adminLogoutBtn");
  if (!logoutButton) return;
  logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    try {
      await fetch("/api/admin/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } finally {
      window.location.replace("/local-admin/login");
    }
  });
});
