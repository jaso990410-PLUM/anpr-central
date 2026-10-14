(() => {
    const video = document.querySelector("[data-event-video]");
    const fallback = document.querySelector("[data-play-fallback]");
    const loading = document.querySelector("[data-video-loading]");

    const play = () => {
        if (!video) return;
        const attempt = video.play();
        if (attempt && typeof attempt.catch === "function") {
            attempt.catch(() => {
                if (fallback) fallback.hidden = false;
            });
        }
    };

    video?.addEventListener("loadedmetadata", () => {
        if (loading) loading.hidden = true;
        play();
    });
    video?.addEventListener("playing", () => {
        if (fallback) fallback.hidden = true;
    });
    video?.addEventListener("error", () => {
        if (loading) loading.textContent = "Event video could not be loaded.";
    });
    fallback?.addEventListener("click", play);

    document.querySelector("[data-replay]")?.addEventListener("click", () => {
        if (!video) return;
        video.currentTime = 0;
        play();
    });
    document.querySelector("[data-replay-recognition]")?.addEventListener("click", () => {
        if (!video) return;
        const recognitionAt = Number(video.dataset.recognitionOffset || 0);
        video.currentTime = Math.max(0, recognitionAt - 2);
        play();
    });

    const dialog = document.querySelector("[data-protect-dialog]");
    document.querySelector("[data-open-protect]")?.addEventListener("click", () => dialog?.showModal());
    document.querySelector("[data-close-protect]")?.addEventListener("click", () => dialog?.close());
    dialog?.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
    });
})();
