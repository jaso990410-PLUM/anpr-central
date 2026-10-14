(() => {
    const form = document.querySelector("[data-recording-form]");
    if (!form) return;

    const before = form.querySelector("[name='pre_event_seconds']");
    const after = form.querySelector("[name='post_event_seconds']");
    const total = form.querySelector("[data-clip-total]");
    const offsetSeconds = form.querySelector("[name='media_time_offset_seconds']");
    const offsetMilliseconds = form.querySelector("[data-offset-ms]");

    const updateTotal = () => {
        const seconds = Number(before?.value || 0) + Number(after?.value || 0);
        if (total) total.textContent = `${seconds} seconds`;
    };

    before?.addEventListener("input", updateTotal);
    after?.addEventListener("input", updateTotal);
    form.addEventListener("submit", () => {
        if (offsetSeconds && offsetMilliseconds) {
            offsetMilliseconds.value = String(Math.round(Number(offsetSeconds.value || 0) * 1000));
        }
    });
})();
