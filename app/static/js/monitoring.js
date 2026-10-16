(() => {
  "use strict";
  const list = document.querySelector("[data-monitor-event-list]");
  const banner = document.querySelector("[data-monitor-new-events]");
  const countNode = document.querySelector("[data-monitor-new-count]");
  if (!list || !banner || !countNode || !list.dataset.latestAt) return;
  let cursor = list.dataset.latestAt;
  const poll = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(`/api/monitoring/events?after=${encodeURIComponent(cursor)}`, {headers: {Accept: "application/json"}});
      if (!response.ok) return;
      const payload = await response.json();
      if (payload.items && payload.items.length) {
        cursor = payload.items[payload.items.length - 1].occurred_at;
        countNode.textContent = String(Number(countNode.textContent || "0") + payload.items.length);
        banner.hidden = false;
      }
    } catch (_) {
      // Monitoring UI failure must not affect operational workflows.
    }
  };
  window.setInterval(poll, 5000);
})();
