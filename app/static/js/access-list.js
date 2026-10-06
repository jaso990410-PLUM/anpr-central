(() => {
    "use strict";
    const root = document.querySelector("[data-access-sync]");
    if (!root) return;
    const interval = Math.max(3, Math.min(30, Number(root.dataset.pollSeconds) || 5)) * 1000;
    let busy = false;
    let timer;
    const schedule = (delay = interval) => {
        if (!document.hidden) timer = window.setTimeout(poll, delay);
    };
    const localTime = (value) => value ? new Date(value).toLocaleString(undefined, { hour12: false }) : "Never";
    async function poll() {
        if (busy || document.hidden) return;
        busy = true;
        try {
            const response = await fetch(`/access-list/${encodeURIComponent(root.dataset.entryId)}/sync-status`, { credentials: "same-origin", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("sync status request failed");
            const payload = await response.json();
            const overall = root.querySelector("[data-access-overall]");
            if (overall) overall.textContent = payload.overall;
            payload.cameras.forEach((camera) => {
                const row = root.querySelector(`[data-sync-id="${CSS.escape(camera.id)}"]`);
                if (!row) return;
                const status = row.querySelector("[data-sync-status]");
                if (status) {
                    status.textContent = camera.status;
                    status.className = `status-badge status-badge--${camera.status_code === "VERIFIED" ? "online" : ["PENDING", "SYNCING", "ACKNOWLEDGED", "PARTIALLY_VERIFIED"].includes(camera.status_code) ? "warning" : "offline"}`;
                }
                const verified = row.querySelector("[data-sync-verified]");
                if (verified) verified.textContent = localTime(camera.last_verified);
                const note = row.querySelector("[data-sync-note]");
                if (note) note.textContent = camera.note;
            });
        } catch (_error) {
            // Server-rendered state remains available; retry at a bounded cadence.
        } finally {
            busy = false;
            schedule();
        }
    }
    document.addEventListener("visibilitychange", () => {
        window.clearTimeout(timer);
        if (!document.hidden) schedule(0);
    });
    schedule(0);
})();
