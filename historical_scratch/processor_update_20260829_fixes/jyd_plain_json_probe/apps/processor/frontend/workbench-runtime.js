(() => {
  let leaseId = "";
  let reconnectTimer = null;
  let heartbeatTimer = null;
  let closing = false;

  const closeLease = (id) => {
    if (!id) return;
    const url = `/api/runtime/pages/${encodeURIComponent(id)}/close`;
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([], { type: "application/json" }));
    } else {
      fetch(url, { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => {});
    }
  };

  const scheduleReconnect = () => {
    clearInterval(heartbeatTimer);
    leaseId = "";
    if (!closing) reconnectTimer = setTimeout(connect, 2000);
  };

  const heartbeat = async () => {
    const id = leaseId;
    if (!id || closing) return;
    try {
      const response = await fetch(`/api/runtime/pages/${encodeURIComponent(id)}`, {
        method: "POST",
        credentials: "same-origin",
      });
      if (!response.ok) scheduleReconnect();
    } catch (_) {
      scheduleReconnect();
    }
  };

  const connect = async () => {
    if (closing) return;
    try {
      const response = await fetch("/api/runtime/pages", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`runtime lease failed: ${response.status}`);
      const payload = await response.json();
      const id = String(payload.lease_id || "");
      if (!id) throw new Error("runtime lease id missing");
      if (closing) {
        closeLease(id);
        return;
      }
      leaseId = id;
      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(heartbeat, 10000);
    } catch (_) {
      scheduleReconnect();
    }
  };

  window.addEventListener("pagehide", () => {
    closing = true;
    clearTimeout(reconnectTimer);
    clearInterval(heartbeatTimer);
    closeLease(leaseId);
    leaseId = "";
  });
  connect();
})();
