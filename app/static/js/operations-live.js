(() => {
    "use strict";
    const root = document.querySelector("[data-lane-console]");
    if (!root) return;

    const siteId = root.dataset.siteId;
    const cameraId = root.dataset.cameraId;
    const interval = Math.max(3000, Number(root.dataset.pollSeconds || 5) * 1000);
    const consoleStatus = root.querySelector("[data-console-status]");
    const video = root.querySelector("[data-lane-live-video]");
    const liveStatus = root.querySelector("[data-lane-live-status]");
    const liveMessage = root.querySelector("[data-lane-live-message]");
    const sourceUrl = video?.dataset.streamUrl || "";
    let reconnectAttempts = 0;
    let reconnectTimer = null;

    const setBadge = (node, text, tone) => {
        if (!node) return;
        node.textContent = text;
        node.className = `status-badge status-badge--${tone}`;
    };
    const field = (selector, value, fallback = "Not available") => {
        const node = root.querySelector(selector);
        if (node) node.textContent = value || fallback;
    };
    const setLink = (selector, url) => {
        const node = root.querySelector(selector);
        if (!node) return;
        node.hidden = !url;
        if (url) node.href = url;
    };
    const renderImage = (container, url, alt, placeholder) => {
        if (!container) return;
        container.replaceChildren();
        if (!url) {
            const message = document.createElement("span");
            message.textContent = placeholder;
            container.append(message);
            return;
        }
        const image = document.createElement("img");
        image.src = url;
        image.alt = alt;
        image.addEventListener("error", () => renderImage(container, null, alt, placeholder), { once: true });
        container.append(image);
    };

    function connectLive() {
        if (!video || !sourceUrl || document.hidden) return;
        window.clearTimeout(reconnectTimer);
        setBadge(liveStatus, "CONNECTING", "warning");
        if (liveMessage) liveMessage.textContent = reconnectAttempts ? "Reconnecting to lane camera…" : "Connecting to lane camera…";
        const separator = sourceUrl.includes("?") ? "&" : "?";
        video.src = `${sourceUrl}${separator}attempt=${reconnectAttempts}`;
        video.load();
        video.play().catch(() => {
            if (liveMessage) liveMessage.textContent = "Press play if browser autoplay is disabled.";
        });
    }
    function scheduleReconnect() {
        if (!video || document.hidden || reconnectTimer) return;
        reconnectAttempts += 1;
        const delay = Math.min(30000, 2000 * (2 ** Math.min(reconnectAttempts - 1, 4)));
        setBadge(liveStatus, "RECONNECTING", "warning");
        if (liveMessage) liveMessage.textContent = `Live stream interrupted. Retrying in ${Math.round(delay / 1000)} seconds…`;
        reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connectLive();
        }, delay);
    }
    video?.addEventListener("playing", () => {
        reconnectAttempts = 0;
        setBadge(liveStatus, "LIVE", "online");
        if (liveMessage) liveMessage.textContent = "Live lane stream active";
    });
    video?.addEventListener("error", scheduleReconnect);
    video?.addEventListener("ended", scheduleReconnect);
    if (video) connectLive();
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && video && video.paused) connectLive();
    });

    function renderLatest(latest) {
        const empty = root.querySelector("[data-latest-empty]");
        const content = root.querySelector("[data-latest-content]");
        if (empty) empty.hidden = Boolean(latest);
        if (content) content.hidden = !latest;
        if (!latest) {
            setBadge(root.querySelector("[data-latest-lane]"), "WAITING", "neutral");
            root.querySelector("[data-latest-open-barrier]")?.setAttribute("hidden", "");
            return;
        }
        setBadge(root.querySelector("[data-latest-lane]"), latest.lane_role, "neutral");
        field("[data-latest-plate]", latest.plate);
        field("[data-latest-correction]", latest.corrected ? `Corrected from ${latest.raw_plate}` : "", "");
        const decision = root.querySelector("[data-latest-decision]");
        setBadge(decision, latest.decision, latest.decision === "ALLOW" ? "online" : latest.decision === "DENY" ? "critical" : "warning");
        field("[data-latest-profile]", latest.profile, "No access profile");
        field("[data-latest-reason]", latest.reason);
        field("[data-latest-lane-time]", `${latest.camera} · ${latest.captured}`);
        field("[data-latest-access]", latest.access);
        field("[data-latest-session]", latest.session_status);
        field("[data-latest-entry]", latest.entry_time);
        field("[data-latest-exit]", latest.exit_time);
        field("[data-latest-stay]", latest.stay);
        field("[data-latest-barrier]", String(latest.barrier_state || "UNKNOWN").replaceAll("_", " "));
        field("[data-latest-watch]", latest.watch, "No watch flag");
        renderImage(root.querySelector("[data-latest-scene]"), latest.scene_url, `Scene for ${latest.plate}`, "Snapshot unavailable");
        renderImage(root.querySelector("[data-latest-plate-image]"), latest.plate_url, `Plate crop for ${latest.plate}`, "Plate crop unavailable");
        setLink("[data-latest-event-video]", latest.event_video_url);
        setLink("[data-latest-recording]", latest.recording_url);
        setLink("[data-latest-details]", latest.operations_url);
        const openButton = root.querySelector("[data-latest-open-barrier]");
        if (openButton) openButton.hidden = false;
        const barrierForm = root.querySelector("[data-console-barrier-form]");
        if (barrierForm) barrierForm.action = `/operations/events/${encodeURIComponent(latest.id)}/open-barrier`;
        field("[data-barrier-plate]", latest.plate);
        field("[data-barrier-decision]", latest.decision);
    }

    const statusBadge = (text, tone) => {
        const node = document.createElement("span");
        node.className = `status-badge status-badge--${tone}`;
        node.textContent = text;
        return node;
    };
    function renderRecent(events) {
        const strip = root.querySelector("[data-recent-access-strip]");
        if (!strip) return;
        strip.replaceChildren();
        if (!events.length) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.innerHTML = "<strong>No recent access records</strong><span>Events for this lane will appear here.</span>";
            strip.append(empty);
            return;
        }
        events.forEach((event) => {
            const card = document.createElement("a");
            card.className = "recent-access-card";
            card.href = `/operations/events/${encodeURIComponent(event.id)}`;
            card.dataset.eventId = event.id;
            const image = document.createElement("div");
            image.className = "recent-access-image";
            renderImage(image, event.scene_url, `Scene for ${event.plate}`, "Snapshot unavailable");
            const body = document.createElement("div");
            body.className = "recent-access-card__body";
            const plateLine = document.createElement("div");
            const plate = document.createElement("strong");
            plate.className = "plate-value";
            plate.textContent = event.plate;
            plateLine.append(plate);
            if (event.corrected) {
                const correction = document.createElement("small");
                correction.textContent = `Corrected from ${event.raw_plate}`;
                plateLine.append(correction);
            }
            const lane = document.createElement("span");
            lane.textContent = `${event.camera} · ${event.lane_role}`;
            const captured = document.createElement("time");
            captured.textContent = event.captured;
            const badges = document.createElement("div");
            badges.append(statusBadge(event.decision, event.decision === "ALLOW" ? "online" : event.decision === "DENY" ? "critical" : "warning"));
            if (event.source === "OFFLINE_RECOVERY") badges.append(statusBadge("RECOVERED", "neutral"));
            if (event.watch) badges.append(statusBadge("WATCH", "critical"));
            body.append(plateLine, lane, captured, badges);
            card.append(image, body);
            strip.append(card);
        });
    }

    async function poll() {
        if (!siteId || !cameraId || document.hidden) {
            window.setTimeout(poll, interval);
            return;
        }
        try {
            const url = `/api/operations/console?site=${encodeURIComponent(siteId)}&camera=${encodeURIComponent(cameraId)}`;
            const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("console polling failed");
            const payload = await response.json();
            field("[data-stat-entry]", String(payload.statistics.entries), "0");
            field("[data-stat-exit]", String(payload.statistics.exits), "0");
            field("[data-stat-on-site]", String(payload.statistics.on_site), "0");
            renderLatest(payload.latest);
            renderRecent(payload.recent);
            setBadge(consoleStatus, "LIVE MODE", "online");
        } catch (_error) {
            setBadge(consoleStatus, "UPDATES RETRYING", "warning");
        } finally {
            window.setTimeout(poll, interval);
        }
    }
    if (siteId && cameraId) window.setTimeout(poll, interval);

    const selector = root.querySelector("[data-lane-selector]");
    root.querySelector("[data-console-site]")?.addEventListener("change", () => {
        const camera = root.querySelector("[data-console-camera]");
        if (camera) camera.disabled = true;
        selector?.requestSubmit();
    });
    root.querySelector("[data-console-camera]")?.addEventListener("change", () => selector?.requestSubmit());
    root.querySelectorAll("[data-open-dialog]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.openDialog)?.showModal()));
    root.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
    root.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); }));
})();
