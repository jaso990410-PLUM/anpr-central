(() => {
    "use strict";

    const statusRoot = document.querySelector("[data-live-status]");
    const recognitionRoot = document.querySelector("[data-live-recognitions]");
    if (!statusRoot && !recognitionRoot) return;

    const indicators = () => document.querySelectorAll("[data-live-indicator]");
    const setIndicator = (state) => {
        indicators().forEach((element) => {
            element.textContent = state;
            element.className = `status-badge status-badge--${state === "LIVE" ? "online" : state === "PAUSED" ? "neutral" : "warning"}`;
        });
    };
    const interval = Math.max(3, Math.min(30, Number((statusRoot || recognitionRoot).dataset.pollSeconds) || 5)) * 1000;
    let failureCount = 0;
    let statusBusy = false;
    let recognitionBusy = false;
    let statusTimer;
    let recognitionTimer;

    const delay = () => failureCount ? Math.min(30000, interval * Math.pow(2, Math.min(failureCount, 3))) : interval;
    const schedule = (kind, milliseconds = delay()) => {
        if (document.hidden) return;
        if (kind === "status" && statusRoot) statusTimer = window.setTimeout(pollStatus, milliseconds);
        if (kind === "recognition" && recognitionRoot) recognitionTimer = window.setTimeout(pollRecognitions, milliseconds);
    };
    const succeeded = () => { failureCount = 0; setIndicator("LIVE"); };
    const failed = () => { failureCount += 1; setIndicator("RETRYING"); };
    const localTime = (value) => value ? new Date(value).toLocaleString(undefined, { hour12: false }) : "Never";

    async function pollStatus() {
        if (statusBusy || document.hidden) return;
        statusBusy = true;
        try {
            const response = await fetch("/api/status/live", { credentials: "same-origin", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("status request failed");
            const payload = await response.json();
            document.querySelectorAll("[data-mqtt-status]").forEach((element) => {
                element.textContent = payload.mqtt.connected ? "MQTT CONNECTED" : "MQTT DISCONNECTED";
                element.className = `status-badge status-badge--${payload.mqtt.connected ? "online" : "offline"}`;
            });
            document.querySelectorAll("[data-broker-endpoint]").forEach((element) => { element.textContent = payload.mqtt.broker; });
            document.querySelectorAll("[data-cameras-online]").forEach((element) => { element.textContent = String(payload.cameras_online); });
            payload.cameras.forEach((camera) => {
                const row = document.querySelector(`[data-camera-id="${CSS.escape(camera.id)}"]`);
                if (!row) return;
                const badge = row.querySelector("[data-camera-state]");
                if (badge) {
                    badge.textContent = camera.state;
                    badge.className = `status-badge status-badge--${camera.state.toLowerCase().replaceAll("_", "-")}`;
                }
                const seen = row.querySelector("[data-camera-last-seen]");
                if (seen) seen.textContent = localTime(camera.last_seen);
            });
            succeeded();
        } catch (_error) {
            failed();
        } finally {
            statusBusy = false;
            schedule("status");
        }
    }

    const textCell = (field, value, className = "") => {
        const cell = document.createElement("td");
        if (className) cell.className = className;
        const node = document.createElement(field === "plate" ? "strong" : "span");
        node.dataset.field = field;
        node.textContent = value;
        if (field === "plate") node.className = "plate-value";
        if (field === "direction") node.className = `status-badge status-badge--${value.toLowerCase()}`;
        if (field === "status") node.className = `status-badge status-badge--${value === "Complete" ? "online" : "warning"}`;
        if (field === "source") node.className = `status-badge status-badge--${value === "Live" ? "online" : "neutral"}`;
        cell.append(node);
        return cell;
    };
    const renderAccess = (cell, event) => {
        cell.replaceChildren();
        const badge = document.createElement("span");
        badge.className = `status-badge status-badge--${event.access_badge_tone || "neutral"}`;
        badge.dataset.accessBadge = "";
        badge.textContent = event.access_label;
        cell.append(badge);
        if (event.access_sync_status) {
            const sync = document.createElement("small");
            sync.className = "recognition-access__sync";
            sync.dataset.accessSync = "";
            sync.textContent = `Camera sync: ${event.access_sync_status}`;
            cell.append(sync);
        }
        if (recognitionRoot?.dataset.canManageAccess !== "true") return;
        const actions = document.createElement("span");
        actions.className = "recognition-access__actions";
        actions.dataset.accessActions = "";
        const addLink = (label, href) => {
            const link = document.createElement("a");
            link.className = "table-link";
            link.href = href;
            link.textContent = label;
            actions.append(link);
        };
        if (event.access_entry_id) {
            addLink("Edit Access", `/access-list/${encodeURIComponent(event.access_entry_id)}`);
        } else {
            const id = encodeURIComponent(event.id);
            addLink("Add to Whitelist", `/access-list/new?recognition_id=${id}&list_type=ALLOW`);
            addLink("Add to Blacklist", `/access-list/new?recognition_id=${id}&list_type=BLOCK`);
        }
        cell.append(actions);
    };
    const accessCell = (event) => {
        const cell = document.createElement("td");
        cell.dataset.field = "access";
        renderAccess(cell, event);
        return cell;
    };
    const updateImages = (cell, event) => {
        cell.replaceChildren();
        [["Plate Crop", event.plate_image_url], ["Scene", event.scene_image_url]].forEach(([label, url]) => {
            if (!url) return;
            if (cell.childNodes.length) cell.append(" · ");
            const link = document.createElement("a");
            link.className = "table-link image-action";
            link.href = url;
            link.textContent = label;
            link.dataset.recognitionImage = "";
            link.dataset.imageLabel = `${label} · ${event.plate}`;
            cell.append(link);
        });
        if (!cell.childNodes.length) cell.textContent = "Unavailable";
    };
    const upsertRow = (event) => {
        const body = document.querySelector("[data-recognition-rows]");
        if (!body) return;
        document.querySelector("[data-empty-row]")?.remove();
        let row = body.querySelector(`[data-event-id="${CSS.escape(event.id)}"]`);
        if (!row) {
            row = document.createElement("tr");
            row.dataset.eventId = event.id;
            row.append(
                textCell("plate", event.plate), textCell("camera", event.camera),
                textCell("site", event.site), textCell("direction", event.direction),
                textCell("source", event.source_label),
                accessCell(event),
                textCell("confidence", event.confidence_display, "data-text"),
                textCell("time", event.captured_display, "data-text"),
                textCell("status", event.status), textCell("images", "Unavailable")
            );
            body.prepend(row);
        }
        ["plate", "camera", "site", "direction", "source", "confidence", "time", "status"].forEach((field) => {
            const node = row.querySelector(`[data-field="${field}"]`);
            if (node) node.textContent = field === "time" ? event.captured_display : field === "confidence" ? event.confidence_display : field === "source" ? event.source_label : event[field];
        });
        const direction = row.querySelector('[data-field="direction"]');
        if (direction) direction.className = `status-badge status-badge--${event.direction_code.toLowerCase()}`;
        const status = row.querySelector('[data-field="status"]');
        if (status) status.className = `status-badge status-badge--${event.status === "Complete" ? "online" : "warning"}`;
        const source = row.querySelector('[data-field="source"]');
        if (source) source.className = `status-badge status-badge--${event.source === "LIVE" ? "online" : "neutral"}`;
        const access = row.querySelector('[data-field="access"]');
        if (access) renderAccess(access, event);
        const imageCell = row.querySelector('[data-field="images"]')?.closest("td") || row.lastElementChild;
        if (imageCell) updateImages(imageCell, event);
        while (body.querySelectorAll("tr[data-event-id]").length > 100) body.querySelector("tr[data-event-id]:last-of-type")?.remove();
    };
    const updateLatest = (event) => {
        if (!event) return;
        document.querySelector("[data-latest-recognition]")?.removeAttribute("hidden");
        document.querySelector("[data-latest-empty]")?.setAttribute("hidden", "");
        const fields = { plate: event.plate, camera: event.camera_name || event.camera, direction: event.direction, access: event.access_label, time: event.captured_time_display };
        Object.entries(fields).forEach(([name, value]) => {
            const node = document.querySelector(`[data-latest-${name}]`);
            if (node) node.textContent = value;
        });
        const access = document.querySelector("[data-latest-access]");
        if (access) access.className = `status-badge status-badge--${event.access_badge_tone || "neutral"}`;
        if (event.plate_image_url) {
            let image = document.querySelector("[data-latest-image]");
            if (!image) {
                image = document.createElement("img");
                image.dataset.latestImage = "";
                image.alt = "Latest recognized plate";
                document.querySelector(".latest-recognition__image")?.replaceChildren(image);
            }
            image.src = event.plate_image_url;
        }
    };

    async function pollRecognitions() {
        if (recognitionBusy || document.hidden) return;
        recognitionBusy = true;
        try {
            const cursor = recognitionRoot.dataset.cursor;
            const parameters = new URLSearchParams();
            if (cursor) parameters.set("after", cursor);
            if (recognitionRoot.dataset.source) parameters.set("source", recognitionRoot.dataset.source);
            const query = parameters.toString();
            const url = `/api/recognitions/live${query ? `?${query}` : ""}`;
            const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("recognition request failed");
            const payload = await response.json();
            payload.events.forEach(upsertRow);
            (payload.access_updates || []).forEach((update) => {
                const cell = document.querySelector(`[data-event-id="${CSS.escape(update.id)}"] [data-field="access"]`);
                if (cell) renderAccess(cell, update);
            });
            updateLatest(payload.latest);
            if (payload.cursor) recognitionRoot.dataset.cursor = payload.cursor;
            succeeded();
        } catch (_error) {
            failed();
        } finally {
            recognitionBusy = false;
            schedule("recognition");
        }
    }

    document.addEventListener("visibilitychange", () => {
        window.clearTimeout(statusTimer); window.clearTimeout(recognitionTimer);
        if (document.hidden) setIndicator("PAUSED");
        else { failureCount = 0; setIndicator("LIVE"); schedule("status", 0); schedule("recognition", 0); }
    });
    schedule("status", 0);
    schedule("recognition", 0);

    const imageViewer = document.querySelector("[data-image-viewer]");
    if (imageViewer) {
        document.addEventListener("click", (event) => {
            const action = event.target.closest("[data-recognition-image]");
            if (!action) return;
            event.preventDefault();
            const image = imageViewer.querySelector("[data-image-viewer-content]");
            const title = imageViewer.querySelector("[data-image-viewer-title]");
            const label = action.dataset.imageLabel || "Recognition image";
            image.src = action.href;
            image.alt = label;
            title.textContent = label;
            imageViewer.showModal();
        });
        imageViewer.querySelector("[data-image-viewer-close]")?.addEventListener("click", () => imageViewer.close());
        imageViewer.addEventListener("click", (event) => {
            if (event.target === imageViewer) imageViewer.close();
        });
    }
})();
