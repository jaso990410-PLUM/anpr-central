(() => {
    "use strict";

    const root = document.querySelector("[data-dashboard-live]");
    const cameraSelect = root?.querySelector("[data-live-camera-select]");
    const video = root?.querySelector("[data-dashboard-live-video]");
    const shell = root?.querySelector("[data-live-video-shell]");
    const startButton = root?.querySelector("[data-live-start]");
    const stopButton = root?.querySelector("[data-live-stop]");
    const status = root?.querySelector("[data-dashboard-live-status]");
    const message = root?.querySelector("[data-live-message]");
    const dedicatedLink = root?.querySelector("[data-live-dedicated]");
    if (!cameraSelect || !video || !shell || !startButton || !stopButton || !status) return;

    const selectedCamera = () => cameraSelect.options[cameraSelect.selectedIndex];
    const setStatus = (label, tone) => {
        status.textContent = label;
        status.className = `status-badge status-badge--${tone}`;
    };
    const stop = () => {
        video.pause();
        video.removeAttribute("src");
        video.load();
        shell.hidden = true;
        startButton.disabled = false;
        stopButton.disabled = true;
        cameraSelect.disabled = false;
        setStatus("IDLE", "neutral");
    };
    const start = () => {
        const option = selectedCamera();
        if (!option?.dataset.streamUrl) return;
        shell.hidden = false;
        cameraSelect.disabled = true;
        startButton.disabled = true;
        stopButton.disabled = false;
        dedicatedLink.href = option.dataset.viewUrl;
        message.textContent = `Connecting to ${option.textContent.trim()}...`;
        setStatus("CONNECTING", "warning");
        video.src = option.dataset.streamUrl;
        video.play().catch(() => {
            message.textContent = "Press play if browser autoplay is disabled.";
        });
    };

    video.addEventListener("playing", () => {
        setStatus("LIVE", "online");
        message.textContent = "Live stream active. Stop it when monitoring is complete.";
    });
    video.addEventListener("error", () => {
        setStatus("UNAVAILABLE", "critical");
        message.textContent = "The live stream could not be opened. Check FFmpeg and the camera media source.";
        startButton.disabled = false;
        stopButton.disabled = false;
    });
    startButton.addEventListener("click", start);
    stopButton.addEventListener("click", stop);
    cameraSelect.addEventListener("change", stop);
    document.addEventListener("visibilitychange", () => {
        if (document.hidden && video.hasAttribute("src")) stop();
    });
    window.addEventListener("pagehide", stop);
})();
