(() => {
    "use strict";
    const root = document.querySelector("[data-playback-workspace]");
    const dataNode = document.getElementById("playback-workspace-data");
    if (!root || !dataNode) return;
    const data = JSON.parse(dataNode.textContent);
    const dayStart = Date.parse(data.dayStart);
    const dayEnd = Date.parse(data.dayEnd);
    const timeline = root.querySelector("[data-timeline]");
    const playhead = root.querySelector("[data-playhead]");
    const playheadLabel = root.querySelector("[data-playhead-label]");
    const selectedTime = document.querySelector("[data-selected-time]");
    const scale = root.querySelector("[data-timeline-scale]");
    const gapNotice = root.querySelector("[data-gap-notice]");
    const players = [...root.querySelectorAll("[data-playback-video]")];
    const zoomSelect = root.querySelector("[data-timeline-zoom]");
    const zoomWindowLabel = root.querySelector("[data-zoom-window-label]");
    const eventLeadInSelect = root.querySelector("[data-event-lead-in]");
    const zoomDurations = { "24h": dayEnd - dayStart, "6h": 21600000, "1h": 3600000, "15m": 900000 };
    const minimumViewDuration = 60000;
    const maximumViewDuration = dayEnd - dayStart;
    let masterTime = Math.min(dayEnd - 1, Math.max(dayStart, Date.parse(data.playhead)));
    let selectedEvent = data.event;
    let playing = false;
    let syncing = false;
    let dragging = false;
    let viewStart = dayStart;
    let viewEnd = dayEnd;
    let loadGeneration = 0;
    let eventCueAt = null;
    let eventCueShown = false;
    let eventPrerollMs = Number(data.eventPrerollSeconds || 10) * 1000;
    let customZoom = data.windowMinutes !== null && data.windowMinutes !== undefined;
    let viewDuration = customZoom
        ? Math.min(maximumViewDuration, Math.max(minimumViewDuration, Number(data.windowMinutes) * 60000))
        : (zoomDurations[data.zoom] || zoomDurations["24h"]);

    const cameraData = new Map(data.cameras.map((camera) => [camera.id, camera]));
    const formatDateTime = (epoch) => new Intl.DateTimeFormat("en-GB", {
        timeZone: data.timezone, day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date(epoch)).replace(",", " ·");
    const formatTime = (epoch) => new Intl.DateTimeFormat("en-GB", {
        timeZone: data.timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date(epoch));
    const iso = (epoch) => new Date(epoch).toISOString();
    const clamp = (value, lower, upper) => Math.min(upper, Math.max(lower, value));
    const rangesFor = (cameraId) => (cameraData.get(cameraId)?.coverage || []).map((range) => ({ start: Date.parse(range.start), end: Date.parse(range.end) }));
    const containingRange = (cameraId, epoch) => rangesFor(cameraId).find((range) => epoch >= range.start && epoch < range.end);

    function formatWindow(duration) {
        const minutes = Math.max(1, Math.round(duration / 60000));
        if (minutes < 60) return `${minutes} min window`;
        const hours = minutes / 60;
        return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} hr window`;
    }

    function syncZoomControls() {
        if (zoomWindowLabel) zoomWindowLabel.textContent = `${formatWindow(viewDuration)} · wheel to zoom`;
        if (customZoom && zoomSelect) zoomSelect.value = "";
    }

    function setView() {
        if (viewDuration >= maximumViewDuration) {
            viewStart = dayStart;
            viewEnd = dayEnd;
        } else {
            viewStart = clamp(masterTime - viewDuration / 2, dayStart, dayEnd - viewDuration);
            viewEnd = viewStart + viewDuration;
        }
    }

    function position(epoch) {
        return (epoch - viewStart) / (viewEnd - viewStart) * 100;
    }

    function renderScale() {
        if (!scale) return;
        scale.replaceChildren();
        const tickCount = 4;
        for (let index = 0; index <= tickCount; index += 1) {
            const tick = document.createElement("span");
            const epoch = viewStart + (viewEnd - viewStart) * index / tickCount;
            tick.style.left = `${index / tickCount * 100}%`;
            tick.textContent = formatTime(epoch);
            scale.append(tick);
        }
    }

    function renderTimeline(recenter = true) {
        if (recenter) setView();
        syncZoomControls();
        renderScale();
        root.querySelectorAll("[data-coverage-row]").forEach((row) => {
            const cameraId = row.dataset.coverageRow;
            const coverageLayer = row.querySelector("[data-coverage-layer]");
            const markerLayer = row.querySelector("[data-marker-layer]");
            coverageLayer.replaceChildren();
            markerLayer.replaceChildren();
            rangesFor(cameraId).forEach((range) => {
                const start = Math.max(range.start, viewStart);
                const end = Math.min(range.end, viewEnd);
                if (end <= start) return;
                const bar = document.createElement("span");
                bar.className = "timeline-coverage";
                bar.style.left = `${position(start)}%`;
                bar.style.width = `${position(end) - position(start)}%`;
                bar.title = `Recording ${formatTime(start)}–${formatTime(end)}`;
                coverageLayer.append(bar);
            });
            data.markers.filter((marker) => marker.camera_id === cameraId).forEach((marker) => {
                const epoch = Date.parse(marker.at);
                if (epoch < viewStart || epoch > viewEnd) return;
                const button = document.createElement("button");
                button.type = "button";
                button.className = `timeline-marker${marker.id === selectedEvent ? " is-selected" : ""}`;
                button.style.left = `${position(epoch)}%`;
                button.dataset.eventId = marker.id;
                button.setAttribute("aria-label", `${marker.plate}, ${marker.local_time}, ${marker.camera}, ${marker.decision}`);
                button.title = `${marker.plate}\n${marker.local_time} · ${marker.camera}\n${marker.decision}${marker.source === "OFFLINE_RECOVERY" ? " · Offline recovery" : ""}${marker.corrected ? `\nCorrected from ${marker.raw_plate}` : ""}`;
                button.addEventListener("click", (event) => {
                    event.stopPropagation();
                    selectMarker(marker);
                });
                button.addEventListener("dblclick", () => window.location.assign(marker.clip_url));
                markerLayer.append(button);
            });
        });
        updatePlayhead();
    }

    function updatePlayhead() {
        const percent = clamp(position(masterTime), 0, 100);
        if (playhead) playhead.style.left = `calc(7rem + (100% - 7rem) * ${percent / 100})`;
        if (playheadLabel) playheadLabel.textContent = formatTime(masterTime);
        if (selectedTime) selectedTime.textContent = formatDateTime(masterTime);
    }

    function updateUrl() {
        const url = new URL(window.location.href);
        url.searchParams.set("date", new Intl.DateTimeFormat("en-CA", { timeZone: data.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(masterTime)));
        url.searchParams.set("time", iso(masterTime));
        url.searchParams.set("zoom", zoomSelect?.value || "24h");
        url.searchParams.set("lead_in", String(eventPrerollMs / 1000));
        if (customZoom) url.searchParams.set("window_minutes", String(Math.max(1, Math.round(viewDuration / 60000))));
        else url.searchParams.delete("window_minutes");
        url.searchParams.delete("start");
        url.searchParams.delete("end");
        if (selectedEvent) url.searchParams.set("event", selectedEvent); else url.searchParams.delete("event");
        window.history.replaceState({}, "", url);
    }

    function showGap(message) {
        if (!gapNotice) return;
        gapNotice.textContent = message;
        gapNotice.hidden = false;
    }

    function clearGap() {
        if (gapNotice) gapNotice.hidden = true;
    }

    function loadPlayers(epoch, autoplay = false, cueAt = null) {
        const generation = ++loadGeneration;
        masterTime = clamp(epoch, dayStart, dayEnd - 1);
        eventCueAt = Number.isFinite(cueAt) ? cueAt : null;
        eventCueShown = false;
        clearGap();
        const advancedEnd = data.advancedEnd ? Date.parse(data.advancedEnd) : null;
        const readyPlayers = new Set();
        const settledPlayers = new Set();
        const availablePlayers = [];
        let preparationConfigured = false;
        const toggle = root.querySelector("[data-play-toggle]");
        playing = false;
        if (toggle) {
            toggle.textContent = autoplay ? "Preparing…" : "Play";
            toggle.disabled = true;
        }

        function finishPreparation() {
            if (!preparationConfigured || generation !== loadGeneration || settledPlayers.size !== availablePlayers.length) return;
            if (toggle) toggle.disabled = false;
            if (!readyPlayers.size) {
                playing = false;
                if (toggle) toggle.textContent = "Play";
                showGap("The selected recording could not be prepared.");
                return;
            }
            readyPlayers.forEach((video) => {
                const stage = video.closest("[data-player-stage]");
                stage.querySelector("[data-player-message]").hidden = true;
                stage.querySelector("[data-player-state]").textContent = "Recorded";
            });
            if (!autoplay) {
                if (toggle) toggle.textContent = "Play";
                return;
            }
            playing = true;
            if (toggle) toggle.textContent = "Pause";
            if (eventCueAt !== null) {
                const seconds = Math.max(0, Math.round((eventCueAt - masterTime) / 1000));
                showGap(`Event ready · recognition moment in ${seconds} second${seconds === 1 ? "" : "s"}`);
            }
            readyPlayers.forEach((video) => video.play().catch(() => {}));
        }

        players.forEach((video) => {
            const cameraId = video.dataset.cameraId;
            const stage = video.closest("[data-player-stage]");
            const message = stage.querySelector("[data-player-message]");
            const state = stage.querySelector("[data-player-state]");
            const range = containingRange(cameraId, masterTime);
            if (!range) {
                video.pause();
                video.removeAttribute("src");
                video.load();
                message.textContent = `No recording available at ${formatTime(masterTime)}`;
                message.hidden = false;
                state.textContent = "No footage";
                return;
            }
            availablePlayers.push(video);
            const clipStart = masterTime;
            const clipEnd = Math.min(range.end, advancedEnd || Infinity, clipStart + 15 * 60 * 1000);
            delete video.dataset.mediaOrigin;
            video.dataset.windowStart = String(clipStart);
            video.dataset.windowEnd = String(clipEnd);
            video.playbackRate = Number(root.querySelector("[data-playback-speed]")?.value || 1);
            message.textContent = eventCueAt === null ? "Preparing recording…" : "Preparing event footage…";
            message.hidden = false;
            state.textContent = "Preparing";
            video.addEventListener("loadedmetadata", () => {
                if (generation !== loadGeneration) return;
                video.dataset.mediaOrigin = String(video.seekable.length ? video.seekable.start(0) : video.currentTime);
            }, { once: true });
            video.addEventListener("canplay", () => {
                if (generation !== loadGeneration) return;
                if (!video.dataset.mediaOrigin) {
                    video.dataset.mediaOrigin = String(video.seekable.length ? video.seekable.start(0) : video.currentTime);
                }
                readyPlayers.add(video);
                settledPlayers.add(video);
                finishPreparation();
            }, { once: true });
            video.addEventListener("error", () => {
                if (generation !== loadGeneration) return;
                message.textContent = "Recording could not be prepared";
                message.hidden = false;
                state.textContent = "Unavailable";
                settledPlayers.add(video);
                finishPreparation();
            }, { once: true });
            video.src = `/media/cameras/${encodeURIComponent(cameraId)}/playback?start=${encodeURIComponent(iso(clipStart))}&end=${encodeURIComponent(iso(clipEnd))}`;
            video.load();
        });
        preparationConfigured = true;
        if (eventCueAt !== null) {
            const seconds = eventPrerollMs / 1000;
            showGap(`Preparing event footage · playback will start ${seconds} seconds before recognition`);
        }
        if (!availablePlayers.length) finishPreparation();
        renderTimeline();
        updateUrl();
    }

    function seek(epoch, autoplay = playing) {
        // Historical playback is a sequential fragmented MP4 response. A native
        // seek may not be buffered yet, so reload a bounded stream at the exact
        // requested timestamp instead of moving only the visible playhead.
        loadPlayers(epoch, autoplay);
    }

    function selectMarker(marker) {
        selectedEvent = marker.id;
        root.querySelectorAll(".playback-event-row").forEach((row) => row.classList.toggle("is-selected", row.dataset.eventId === marker.id));
        const recognitionAt = Date.parse(marker.at);
        loadPlayers(recognitionAt - eventPrerollMs, true, recognitionAt);
    }

    function eventRelative(direction) {
        const ordered = [...data.markers].sort((left, right) => Date.parse(left.at) - Date.parse(right.at));
        const candidates = direction < 0 ? ordered.filter((item) => Date.parse(item.at) < masterTime).reverse() : ordered.filter((item) => Date.parse(item.at) > masterTime);
        if (candidates[0]) selectMarker(candidates[0]);
    }

    function timelineEpoch(clientX) {
        const track = timeline.querySelector(".timeline-track");
        const rect = track.getBoundingClientRect();
        return viewStart + clamp((clientX - rect.left) / rect.width, 0, 1) * (viewEnd - viewStart);
    }

    timeline?.addEventListener("pointerdown", (event) => {
        if (event.target.closest(".timeline-marker")) return;
        dragging = true;
        timeline.setPointerCapture(event.pointerId);
        masterTime = timelineEpoch(event.clientX);
        updatePlayhead();
    });
    timeline?.addEventListener("pointermove", (event) => {
        if (!dragging || !timeline.hasPointerCapture(event.pointerId)) return;
        masterTime = timelineEpoch(event.clientX);
        updatePlayhead();
    });
    timeline?.addEventListener("pointerup", (event) => {
        if (!dragging) return;
        dragging = false;
        loadPlayers(timelineEpoch(event.clientX), false);
    });
    timeline?.addEventListener("pointercancel", () => { dragging = false; });
    timeline?.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        seek(masterTime + (event.key === "ArrowRight" ? 10000 : -10000), false);
    });
    timeline?.addEventListener("wheel", (event) => {
        event.preventDefault();
        const track = timeline.querySelector(".timeline-track");
        if (!track) return;
        const rect = track.getBoundingClientRect();
        const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
        const anchor = viewStart + ratio * (viewEnd - viewStart);
        const nextDuration = clamp(viewDuration * Math.exp(event.deltaY * 0.0015), minimumViewDuration, maximumViewDuration);
        if (Math.abs(nextDuration - viewDuration) < 100) return;
        viewDuration = nextDuration;
        customZoom = true;
        viewStart = clamp(anchor - ratio * viewDuration, dayStart, dayEnd - viewDuration);
        viewEnd = viewStart + viewDuration;
        renderTimeline(false);
        updateUrl();
    }, { passive: false });

    zoomSelect?.addEventListener("change", (event) => {
        if (!zoomDurations[event.target.value]) return;
        viewDuration = zoomDurations[event.target.value];
        customZoom = false;
        renderTimeline();
        updateUrl();
    });
    eventLeadInSelect?.addEventListener("change", (event) => {
        eventPrerollMs = Number(event.target.value) * 1000;
        updateUrl();
    });
    root.querySelectorAll("[data-skip]").forEach((button) => button.addEventListener("click", () => seek(masterTime + Number(button.dataset.skip) * 1000)));
    root.querySelector("[data-previous-event]")?.addEventListener("click", () => eventRelative(-1));
    root.querySelector("[data-next-event]")?.addEventListener("click", () => eventRelative(1));
    root.querySelector("[data-play-toggle]")?.addEventListener("click", () => {
        if (root.querySelector("[data-play-toggle]").disabled) return;
        playing = !playing;
        players.forEach((video) => playing ? video.play().catch(() => {}) : video.pause());
        root.querySelector("[data-play-toggle]").textContent = playing ? "Pause" : "Play";
    });
    root.querySelector("[data-playback-speed]")?.addEventListener("change", (event) => players.forEach((video) => { video.playbackRate = Number(event.target.value); }));
    root.querySelector("[data-fullscreen]")?.addEventListener("click", () => root.querySelector("[data-video-grid]")?.requestFullscreen?.());

    players.forEach((video, index) => {
        video.addEventListener("timeupdate", () => {
            if (index !== 0 || syncing || !Number.isFinite(Number(video.dataset.windowStart))) return;
            syncing = true;
            const mediaOrigin = Number(video.dataset.mediaOrigin) || 0;
            masterTime = Number(video.dataset.windowStart) + Math.max(0, video.currentTime - mediaOrigin) * 1000;
            players.slice(1).forEach((peer) => {
                const peerStart = Number(peer.dataset.windowStart);
                const peerOrigin = Number(peer.dataset.mediaOrigin) || 0;
                const target = peerOrigin + (masterTime - peerStart) / 1000;
                if (Number.isFinite(target) && Math.abs(peer.currentTime - target) > 1.5 && target >= 0 && target <= peer.duration) peer.currentTime = target;
            });
            updatePlayhead();
            if (eventCueAt !== null && !eventCueShown && masterTime >= eventCueAt) {
                eventCueShown = true;
                const marker = data.markers.find((item) => item.id === selectedEvent);
                showGap(`Recognition moment${marker?.plate ? ` · ${marker.plate}` : ""}`);
                const cueGeneration = loadGeneration;
                window.setTimeout(() => {
                    if (cueGeneration === loadGeneration && eventCueShown) clearGap();
                }, 2500);
            }
            syncing = false;
        });
        video.addEventListener("ended", () => {
            if (index !== 0) return;
            const currentRange = containingRange(video.dataset.cameraId, masterTime);
            const windowEnd = Number(video.dataset.windowEnd);
            if (currentRange && Number.isFinite(windowEnd) && windowEnd < currentRange.end - 500) {
                loadPlayers(windowEnd, true);
                return;
            }
            const next = rangesFor(video.dataset.cameraId).find((range) => range.start >= (currentRange?.end || windowEnd || masterTime));
            if (next && playing) {
                showGap(`Recording gap: ${formatTime(currentRange.end)}–${formatTime(next.start)}. Skipping to next recorded segment.`);
                window.setTimeout(() => loadPlayers(next.start, true), 1200);
            }
        });
    });

    root.querySelectorAll("[data-event-time]").forEach((button) => button.addEventListener("click", () => {
        const marker = data.markers.find((item) => item.id === button.closest("[data-event-id]").dataset.eventId);
        if (marker) selectMarker(marker);
    }));
    document.querySelectorAll("[data-calendar-date]").forEach((button) => button.addEventListener("click", () => {
        const url = new URL(window.location.href);
        url.searchParams.set("date", button.dataset.calendarDate);
        ["time", "event", "start", "end"].forEach((name) => url.searchParams.delete(name));
        window.location.assign(url);
    }));
    document.querySelectorAll("[data-playback-camera]").forEach((checkbox) => checkbox.addEventListener("change", () => {
        const checked = [...document.querySelectorAll("[data-playback-camera]:checked")];
        if (!checked.length) { checkbox.checked = true; return; }
        if (checked.length > 4) { checkbox.checked = false; return; }
        const url = new URL(window.location.href);
        url.searchParams.delete("camera");
        checked.forEach((item) => url.searchParams.append("camera", item.value));
        url.searchParams.delete("event");
        window.location.assign(url);
    }));
    document.querySelector("[data-playback-site]")?.addEventListener("change", (event) => {
        const url = new URL(window.location.href);
        url.search = "";
        url.searchParams.set("site", event.target.value);
        window.location.assign(url);
    });
    root.querySelectorAll("[data-layout]").forEach((button) => button.addEventListener("click", () => {
        const requested = Number(button.dataset.layout);
        const options = [...document.querySelectorAll("[data-playback-camera]")];
        if (!options.length) return;
        options.forEach((option, index) => { option.checked = index < requested; });
        options[0].dispatchEvent(new Event("change"));
    }));

    renderTimeline();
    loadPlayers(masterTime, false);
})();
