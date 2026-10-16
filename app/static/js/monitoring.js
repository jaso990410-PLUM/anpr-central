(() => {
  "use strict";
  const list = document.querySelector("[data-monitor-event-list]");
  const banner = document.querySelector("[data-monitor-new-events]");
  const countNode = document.querySelector("[data-monitor-new-count]");
  const pluralNode = document.querySelector("[data-monitor-new-plural]");
  if (!list || !banner || !countNode || !list.dataset.latestAt) return;
  let cursor = list.dataset.latestAt;
  const poll = async () => {
    if (document.hidden) return;
    try {
      const attention = list.dataset.view === "attention" ? "&attention_only=true" : "";
      const response = await fetch(`/api/monitoring/events?after=${encodeURIComponent(cursor)}${attention}`, {headers: {Accept: "application/json"}});
      if (!response.ok) return;
      const payload = await response.json();
      if (payload.items && payload.items.length) {
        cursor = payload.items[payload.items.length - 1].occurred_at;
        const count = Number(countNode.textContent || "0") + payload.items.length;
        countNode.textContent = String(count);
        if (pluralNode) pluralNode.hidden = count === 1;
        banner.hidden = false;
      }
    } catch (_) {
      // Monitoring UI failure must not affect operational workflows.
    }
  };
  window.setInterval(poll, 5000);
})();
