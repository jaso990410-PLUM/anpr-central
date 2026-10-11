(() => {
    "use strict";
    const live = document.querySelector("[data-operations-live]");
    const recent = document.querySelector("[data-operations-recent]");
    const ages = document.querySelectorAll("[data-event-age]");
    const formatAge = (seconds) => seconds < 60 ? `${seconds} seconds ago` : seconds < 3600 ? `${Math.floor(seconds / 60)} minutes ago` : `${Math.floor(seconds / 3600)} hours ago`;
    const updateAges = () => ages.forEach((node) => {
        const captured = Date.parse(node.dataset.capturedAt);
        if (Number.isFinite(captured)) node.textContent = formatAge(Math.max(0, Math.floor((Date.now() - captured) / 1000)));
    });
    updateAges();
    window.setInterval(updateAges, 1000);
    if (!recent || !live) return;
    const rows = recent.querySelector("[data-operations-rows]");
    const interval = 5000;
    const badge = (text, style) => {
        const node = document.createElement("span");
        node.className = `status-badge status-badge--${style}`;
        node.textContent = text;
        return node;
    };
    const renderRows = (events) => {
        if (!rows) return;
        const selectedId = rows.querySelector(".is-selected")?.dataset.eventId;
        rows.replaceChildren();
        if (!events.length) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.textContent = "No recent events";
            rows.append(empty);
            return;
        }
        events.forEach((event) => {
            const link = document.createElement("a");
            link.href = `/operations/events/${encodeURIComponent(event.id)}`;
            link.className = `recent-event${event.id === selectedId ? " is-selected" : ""}`;
            link.dataset.eventId = event.id;
            const plateLine = document.createElement("div");
            const plate = document.createElement("strong");
            plate.textContent = event.plate;
            plateLine.append(plate);
            if (event.corrected) {
                const raw = document.createElement("small");
                raw.textContent = `Raw ${event.raw_plate}`;
                plateLine.append(raw);
            }
            const camera = document.createElement("span");
            camera.textContent = `${event.camera} · ${event.lane_role}`;
            const captured = document.createElement("span");
            captured.textContent = event.captured;
            const statuses = document.createElement("div");
            statuses.append(badge(event.decision, event.decision === "ALLOW" ? "online" : event.decision === "DENY" ? "critical" : "warning"));
            if (event.exception) statuses.append(badge(event.exception, "warning"));
            if (event.watch) statuses.append(badge("WATCH", "critical"));
            link.append(plateLine, camera, captured, statuses);
            rows.append(link);
        });
    };
    async function poll() {
        if (document.hidden) return window.setTimeout(poll, interval);
        try {
            const response = await fetch(`/api/operations/recent${window.location.search}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("operations poll failed");
            const payload = await response.json();
            const known = new Set([...recent.querySelectorAll("[data-event-id]")].map((row) => row.dataset.eventId));
            live.textContent = payload.events.some((event) => !known.has(event.id)) ? "UPDATED" : "LIVE";
            renderRows(payload.events);
            live.className = "status-badge status-badge--online";
        } catch (_error) {
            live.textContent = "RETRYING";
            live.className = "status-badge status-badge--warning";
        } finally {
            window.setTimeout(poll, interval);
        }
    }
    window.setTimeout(poll, interval);
})();
