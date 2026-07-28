(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const cloneTab = document.getElementById("voice-clone-tab");
  const mixTab = document.getElementById("voice-mix-tab");
  const clonePanel = document.getElementById("voice-clone-panel");
  const mixPanel = document.getElementById("voice-mix-panel");
  const message = document.getElementById("voice-form-message");

  function selectMethod(method) {
    const clone = method === "clone";
    cloneTab.classList.toggle("active", clone);
    mixTab.classList.toggle("active", !clone);
    clonePanel.classList.toggle("hidden", !clone);
    mixPanel.classList.toggle("hidden", clone);
  }

  async function submitForm(form) {
    message.classList.add("hidden");
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "正在创建任务…";
    try {
      const response = await fetch("/api/voice-creations", {
        method: "POST",
        headers: {"X-CSRF-Token": csrfToken},
        body: new FormData(form),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "声音制作任务创建失败");
      location.href = "/voices?watch=1";
    } catch (error) {
      message.textContent = error.message || "声音制作任务创建失败";
      message.classList.remove("hidden");
      button.disabled = false;
      button.textContent = original;
    }
  }

  cloneTab.addEventListener("click", () => selectMethod("clone"));
  mixTab.addEventListener("click", () => selectMethod("mix"));
  document.getElementById("voice-clone-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget);
  });
  document.getElementById("voice-mix-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget);
  });

  const weightA = document.getElementById("voice-weight-a");
  weightA.addEventListener("input", () => {
    const value = Math.max(1, Math.min(99, Number(weightA.value) || 50));
    document.getElementById("voice-weight-b").textContent = `${100 - value}%`;
  });

  document.querySelectorAll(".save-voice-button").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "正在提交保存…";
      try {
        const response = await fetch(
          `/api/voice-creations/${button.dataset.taskId}/save`,
          {
            method: "POST",
            headers: {"X-CSRF-Token": csrfToken},
          },
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "保存请求失败");
        location.href = "/voices?watch=1";
      } catch (error) {
        message.textContent = error.message || "保存请求失败";
        message.classList.remove("hidden");
        button.disabled = false;
        button.textContent = "保存为可用音色";
      }
    });
  });
})();
