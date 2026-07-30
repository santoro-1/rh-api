(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function responseJson(response) {
    let body = {};
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok) {
      throw new Error(body.detail || "请求失败，请稍后重试");
    }
    return body;
  }

  const createForm = document.getElementById("long-audio-form");
  if (createForm) {
    createForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = document.getElementById("long-audio-submit");
      const status = document.getElementById("long-audio-upload-status");
      const error = document.getElementById("long-audio-create-error");
      submit.disabled = true;
      error.classList.add("hidden");
      status.textContent = "正在上传并检查音视频时长，请不要关闭页面……";
      try {
        const response = await fetch("/api/long-audio-projects", {
          method: "POST",
          headers: {"X-CSRF-Token": csrfToken},
          body: new FormData(createForm),
        });
        const body = await responseJson(response);
        window.location.assign(body.detailUrl);
      } catch (caught) {
        error.textContent = caught.message;
        error.classList.remove("hidden");
        status.textContent = "上传未完成，请检查提示后重试。";
        submit.disabled = false;
      }
    });
  }

  const dataElement = document.getElementById("long-audio-initial-data");
  if (!dataElement) return;

  let project = JSON.parse(dataElement.textContent || "{}");
  const projectId = project.projectId;
  const reviewPanel = document.getElementById("long-audio-review-panel");
  const processingPanel = document.getElementById("long-audio-processing-panel");
  const completedPanel = document.getElementById("long-audio-completed-panel");
  const failedPanel = document.getElementById("long-audio-failed-panel");
  const errorBox = document.getElementById("long-audio-error");
  const player = document.getElementById("long-audio-player");
  const body = document.getElementById("long-audio-segment-body");
  let playEnd = null;
  let pollTimer = null;

  const confidenceLabels = {
    high: "高",
    low: "低，请试听",
    reviewed: "已人工确认",
  };

  function formatSeconds(value) {
    const total = Math.max(Number(value) || 0, 0);
    const minutes = Math.floor(total / 60);
    return `${minutes}:${(total - minutes * 60).toFixed(1).padStart(4, "0")}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderSegments(segments) {
    body.innerHTML = "";
    segments.forEach((segment, index) => {
      const row = document.createElement("tr");
      row.dataset.index = String(index);
      const readonlyStart = "readonly";
      const readonlyEnd = index === segments.length - 1 ? "readonly" : "";
      row.innerHTML = `
        <td>${index + 1}</td>
        <td><input class="segment-start" type="number" step="0.01" value="${Number(segment.startSeconds).toFixed(3)}" ${readonlyStart}></td>
        <td><input class="segment-end" type="number" step="0.01" value="${Number(segment.endSeconds).toFixed(3)}" ${readonlyEnd}></td>
        <td class="segment-duration">${formatSeconds(Number(segment.endSeconds) - Number(segment.startSeconds))}</td>
        <td><span class="confidence ${escapeHtml(segment.confidence || "low")}">${escapeHtml(confidenceLabels[segment.confidence] || segment.confidence || "低，请试听")}</span></td>
        <td><textarea class="segment-script" rows="5">${escapeHtml(segment.scriptText || "")}</textarea></td>
        <td><button type="button" class="secondary segment-play">播放本段</button></td>
      `;
      body.appendChild(row);
    });

    body.querySelectorAll(".segment-end").forEach((input, index) => {
      input.addEventListener("change", () => {
        const rows = [...body.querySelectorAll("tr")];
        const end = Number(input.value);
        if (rows[index + 1]) {
          rows[index + 1].querySelector(".segment-start").value = end.toFixed(3);
        }
        updateDurations();
      });
    });
    body.querySelectorAll(".segment-play").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("tr");
        const start = Number(row.querySelector(".segment-start").value);
        const end = Number(row.querySelector(".segment-end").value);
        playEnd = end;
        player.currentTime = start;
        player.play();
      });
    });
    updateDurations();
  }

  function updateDurations() {
    body.querySelectorAll("tr").forEach((row) => {
      const start = Number(row.querySelector(".segment-start").value);
      const end = Number(row.querySelector(".segment-end").value);
      row.querySelector(".segment-duration").textContent = formatSeconds(end - start);
    });
  }

  player?.addEventListener("timeupdate", () => {
    if (playEnd !== null && player.currentTime >= playEnd) {
      player.pause();
      playEnd = null;
    }
  });

  function collectSegments() {
    return [...body.querySelectorAll("tr")].map((row) => ({
      startSeconds: Number(row.querySelector(".segment-start").value),
      endSeconds: Number(row.querySelector(".segment-end").value),
      scriptText: row.querySelector(".segment-script").value,
    }));
  }

  async function savePlan() {
    const response = await fetch(`/api/long-audio-projects/${projectId}/plan`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({segments: collectSegments()}),
    });
    return responseJson(response);
  }

  document.getElementById("long-audio-save")?.addEventListener("click", async () => {
    const error = document.getElementById("long-audio-plan-error");
    const status = document.getElementById("long-audio-save-status");
    error.classList.add("hidden");
    try {
      await savePlan();
      status.textContent = "调整已保存。切割点和脚本已标记为人工确认。";
    } catch (caught) {
      error.textContent = caught.message;
      error.classList.remove("hidden");
    }
  });

  document.getElementById("long-audio-confirm")?.addEventListener("click", async () => {
    const button = document.getElementById("long-audio-confirm");
    const error = document.getElementById("long-audio-plan-error");
    button.disabled = true;
    error.classList.add("hidden");
    try {
      await savePlan();
      const response = await fetch(`/api/long-audio-projects/${projectId}/confirm`, {
        method: "POST",
        headers: {"X-CSRF-Token": csrfToken},
      });
      await responseJson(response);
      await refreshProject();
    } catch (caught) {
      error.textContent = caught.message;
      error.classList.remove("hidden");
      button.disabled = false;
    }
  });

  function renderProject() {
    document.getElementById("long-audio-status-label").textContent = project.statusLabel;
    document.getElementById("long-audio-provider").textContent = project.alignmentProvider;
    document.getElementById("long-audio-segment-count").textContent =
      project.segments?.length ? `${project.segments.length} 段` : "等待分析";
    if (project.errorMessage) {
      errorBox.textContent = project.errorMessage;
      errorBox.classList.remove("hidden");
    } else {
      errorBox.classList.add("hidden");
    }

    const review = project.status === "REVIEW";
    const completed = project.status === "COMPLETED";
    const failed = project.status === "FAILED";
    const processing = ["PENDING_ANALYSIS", "ANALYZING", "PENDING_CUT", "CUTTING"].includes(project.status);
    reviewPanel.classList.toggle("hidden", !review);
    processingPanel.classList.toggle("hidden", !processing);
    completedPanel.classList.toggle("hidden", !completed);
    failedPanel.classList.toggle("hidden", !failed);
    if (review) renderSegments(project.segments || []);
    if (completed && project.batchId) {
      document.getElementById("long-audio-batch-link").href = `/batches/${project.batchId}`;
    }
    if (processing && !pollTimer) {
      pollTimer = window.setInterval(refreshProject, 5000);
    }
    if (!processing && pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function refreshProject() {
    try {
      const response = await fetch(`/api/long-audio-projects/${projectId}`);
      project = await responseJson(response);
      renderProject();
    } catch (_error) {
      // Keep the current page usable and retry on the next polling interval.
    }
  }

  renderProject();
})();
