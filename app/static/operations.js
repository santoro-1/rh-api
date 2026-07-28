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
        document.hidden ? 5000 : 2000,
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
