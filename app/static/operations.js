(() => {
  "use strict";

  // Service keys are API field names and DOM identifiers. Keep this list in
  // sync with LOG_FILES in app/routes/operations.py.
  const serviceKeys = ["web", "audio_worker", "video_worker", "launcher"];
  const logElements = new Map(
    [...document.querySelectorAll("[data-log-service]")].map((element) => [
      element.dataset.logService,
      element,
    ]),
  );
  const liveState = document.getElementById("live-log-state");
  const liveMessage = document.getElementById("live-log-message");
  const manualRefresh = document.getElementById("refresh-operations");
  // These variables control one browser-side polling loop. They are not task
  // state: requestInFlight prevents overlapping requests; nextPollTimer owns
  // the only scheduled refresh so visibility/manual refresh cannot duplicate it.
  let requestInFlight = false;
  let nextPollTimer = null;

  function setConnectionState(message, failed = false) {
    if (!liveState || !liveMessage) return;
    liveState.classList.toggle("failed", failed);
    liveMessage.textContent = message;
  }

  function appendLogLines(element, lines, cursor) {
    const wasNearBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 32;
    if (lines.length) {
      const existing =
        element.dataset.logEmpty === "1" ? [] : element.textContent.split("\n");
      // Bound browser memory while retaining enough context for diagnosis.
      element.textContent = [...existing, ...lines].slice(-300).join("\n");
      element.dataset.logEmpty = "0";
      if (wasNearBottom) element.scrollTop = element.scrollHeight;
    }
    element.dataset.logCursor = String(cursor);
  }

  function updateServiceCards(services) {
    for (const key of serviceKeys) {
      const online = Boolean(services[key]?.online);
      const card = document.querySelector(`[data-service-card="${key}"]`);
      const state = document.querySelector(`[data-service-state="${key}"]`);
      if (!card || !state) continue;
      state.textContent = online ? "正常" : "离线";
      card.classList.toggle("success", online);
      card.classList.toggle("danger", !online);
    }
  }

  function updateQueue(queue) {
    const bindings = {
      "audio-active-count": queue.audioActive,
      "video-active-count": queue.videoActive,
      "audio-status-counts": JSON.stringify(queue.audioCounts),
      "video-status-counts": JSON.stringify(queue.videoCounts),
    };
    for (const [id, value] of Object.entries(bindings)) {
      const element = document.getElementById(id);
      if (element) element.textContent = String(value);
    }
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "0 MB";
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  }

  function resourceSeverity(percent) {
    if (percent == null) return "";
    if (percent >= 90) return "danger";
    if (percent >= 70) return "review";
    return "success";
  }

  function updateResources(resources) {
    const values = {
      "resource-cpu":
        resources.cpuPercent == null ? "采集中" : `${resources.cpuPercent}%`,
      "resource-memory":
        resources.memory.usedPercent == null
          ? "未知"
          : `${resources.memory.usedPercent}%`,
      "resource-disk": `${resources.disk.usedPercent}%`,
      "resource-ffmpeg": `${resources.ffmpeg.processCount} 个`,
      "resource-memory-detail":
        `${formatBytes(resources.memory.usedBytes)} / ${formatBytes(resources.memory.totalBytes)}`,
      "resource-disk-detail":
        `${formatBytes(resources.disk.usedBytes)} / ${formatBytes(resources.disk.totalBytes)}`,
      "resource-project-memory":
        `项目进程约 ${formatBytes(resources.project.rssBytes)}`,
    };
    for (const [id, value] of Object.entries(values)) {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    }
    const percents = {
      cpu: resources.cpuPercent,
      memory: resources.memory.usedPercent,
      disk: resources.disk.usedPercent,
      ffmpeg: resources.ffmpeg.processCount ? 75 : 0,
    };
    for (const [key, percent] of Object.entries(percents)) {
      const card = document.querySelector(`[data-resource-card="${key}"]`);
      if (!card) continue;
      card.classList.remove("success", "review", "danger");
      card.classList.add(resourceSeverity(percent));
    }
  }

  async function pollOperations() {
    if (requestInFlight) return;
    requestInFlight = true;
    if (nextPollTimer) clearTimeout(nextPollTimer);
    try {
      const url = new URL("/admin/operations/updates", window.location.origin);
      for (const [key, element] of logElements) {
        url.searchParams.set(key, element.dataset.logCursor || "0");
      }
      const response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      updateServiceCards(payload.services);
      updateQueue(payload.queue);
      updateResources(payload.resources);
      for (const [key, chunk] of Object.entries(payload.logs)) {
        const element = logElements.get(key);
        if (element) appendLogLines(element, chunk.lines, chunk.cursor);
      }
      setConnectionState(`实时日志已连接 · ${new Date().toLocaleTimeString()}`);
    } catch (error) {
      setConnectionState("实时日志暂时断开，正在重连", true);
    } finally {
      requestInFlight = false;
      nextPollTimer = window.setTimeout(
        pollOperations,
        document.hidden ? 15000 : 5000,
      );
    }
  }

  manualRefresh?.addEventListener("click", (event) => {
    event.preventDefault();
    pollOperations();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollOperations();
  });
  pollOperations();
})();
