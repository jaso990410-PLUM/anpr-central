(() => {
    const editor = document.querySelector("[data-busy-period-editor]");
    if (!editor) return;

    const list = editor.querySelector("[data-busy-period-list]");
    const empty = editor.querySelector("[data-busy-period-empty]");
    const output = editor.querySelector("[data-busy-period-value]");
    const dialog = editor.querySelector("[data-busy-period-dialog]");
    const day = dialog?.querySelector("[data-dialog-day]");
    const start = dialog?.querySelector("[data-dialog-start]");
    const end = dialog?.querySelector("[data-dialog-end]");
    const error = dialog?.querySelector("[data-dialog-error]");
    const form = editor.closest("form");
    const canEdit = editor.dataset.canEdit === "true";
    const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

    const rows = () => [...list.querySelectorAll("[data-busy-period-row]")];
    const serialize = () => {
        output.value = rows().map((item) => `${item.dataset.day},${item.dataset.start},${item.dataset.end}`).join("\n");
        empty.hidden = rows().length > 0;
    };
    const showError = (message) => {
        error.textContent = message;
        error.hidden = !message;
    };
    const resetDialog = () => {
        day.value = "";
        start.value = "";
        end.value = "";
        showError("");
    };
    const overlaps = (selectedDay, selectedStart, selectedEnd) => rows().some((item) => (
        item.dataset.day === selectedDay
        && selectedStart < item.dataset.end
        && selectedEnd > item.dataset.start
    ));
    const createRow = (selectedDay, selectedStart, selectedEnd) => {
        const element = document.createElement("div");
        element.className = "busy-period-row";
        element.dataset.busyPeriodRow = "";
        element.dataset.day = selectedDay;
        element.dataset.start = selectedStart;
        element.dataset.end = selectedEnd;
        element.innerHTML = `<div><strong>${weekdays[Number(selectedDay)]}</strong><span class="data-text">· ${selectedStart}–${selectedEnd}</span></div><button class="button-quiet busy-period-remove" type="button" data-remove-busy-period aria-label="Remove peak-hour period">Remove</button>`;
        return element;
    };
    const confirmPeriod = () => {
        if (!day.value || !start.value || !end.value) {
            showError("Select a weekday, start time, and end time.");
            return;
        }
        if (start.value >= end.value) {
            showError("End time must be later than start time.");
            return;
        }
        if (overlaps(day.value, start.value, end.value)) {
            showError("This period overlaps another period on the same day.");
            return;
        }
        list.insertBefore(createRow(day.value, start.value, end.value), empty);
        serialize();
        dialog.close();
        resetDialog();
    };

    editor.addEventListener("click", (event) => {
        if (!canEdit) return;
        if (event.target.closest("[data-add-busy-period]")) {
            resetDialog();
            dialog.showModal();
            day.focus();
            return;
        }
        if (event.target.closest("[data-cancel-busy-period]")) {
            dialog.close();
            resetDialog();
            return;
        }
        if (event.target.closest("[data-confirm-busy-period]")) {
            confirmPeriod();
            return;
        }
        const remove = event.target.closest("[data-remove-busy-period]");
        if (remove) {
            remove.closest("[data-busy-period-row]").remove();
            serialize();
        }
    });
    dialog?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            confirmPeriod();
        }
    });
    form?.addEventListener("submit", serialize);
    serialize();
})();
