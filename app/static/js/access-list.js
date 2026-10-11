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

(() => {
    "use strict";
    const root = document.querySelector("[data-access-entry-form]");
    if (!root) return;

    const profile = root.querySelector("[data-access-profile]");
    const modeInputs = [...root.querySelectorAll('input[name="max_stay_override_mode"]')];
    const customFields = root.querySelector("[data-custom-stay]");
    const customValue = root.querySelector('input[name="max_stay_override_value"]');
    const customUnit = root.querySelector('select[name="max_stay_override_unit"]');
    const classificationInputs = [...root.querySelectorAll('input[name="list_type"]')];
    const classificationContext = root.querySelector("[data-classification-context]");
    const siteSelector = root.querySelector("[data-site-selector]");
    const dailyToggle = root.querySelector("[data-daily-window-toggle]");
    const dailyFields = root.querySelector("[data-daily-window-fields]");

    const policyLabels = {
        USE_SITE_POLICY: "Use site policy",
        ALLOW: "Allow",
        REVIEW: "Require operator review",
        DENY: "Deny",
    };

    function formatMinutes(value) {
        const minutes = Number(value);
        if (!Number.isFinite(minutes) || minutes <= 0) return "Unlimited";
        if (minutes % 1440 === 0) {
            const days = minutes / 1440;
            return `${days} day${days === 1 ? "" : "s"}`;
        }
        if (minutes % 60 === 0) {
            const hours = minutes / 60;
            return `${hours} hour${hours === 1 ? "" : "s"}`;
        }
        return `${minutes} minute${minutes === 1 ? "" : "s"}`;
    }

    function setText(selector, value) {
        const element = root.querySelector(selector);
        if (element) element.textContent = value;
    }

    function selectedMode() {
        return modeInputs.find((input) => input.checked)?.value || "PROFILE";
    }

    function customMinutes() {
        const amount = Number(customValue?.value);
        if (!Number.isFinite(amount) || amount <= 0) return null;
        const multiplier = { MINUTES: 1, HOURS: 60, DAYS: 1440 }[customUnit?.value] || 1;
        return amount * multiplier;
    }

    function updateProfileSummary() {
        const option = profile?.selectedOptions[0];
        const profileMinutes = option?.dataset.maxStay || "";
        setText("[data-profile-name]", option?.textContent.trim() || "No access profile");
        setText("[data-profile-stay]", formatMinutes(profileMinutes));
        setText("[data-profile-normal]", policyLabels[option?.dataset.normalExit] || "Use site policy");
        setText("[data-profile-expired]", policyLabels[option?.dataset.expiredExit] || "Use site policy");
        setText("[data-profile-overstay]", policyLabels[option?.dataset.overstayExit] || "Use site policy");
        setText("[data-profile-blacklist]", policyLabels[option?.dataset.blacklistExit] || "Use site policy");
        setText("[data-effective-stay]", selectedMode() === "CUSTOM" ? formatMinutes(customMinutes()) : formatMinutes(profileMinutes));
    }

    function updateOverrideMode() {
        const custom = selectedMode() === "CUSTOM";
        if (customFields) customFields.hidden = !custom;
        if (customValue) {
            customValue.disabled = !custom;
            customValue.required = custom;
        }
        if (customUnit) customUnit.disabled = !custom;
        updateProfileSummary();
    }

    function updateClassificationContext() {
        if (!classificationContext) return;
        const blocked = classificationInputs.some((input) => input.checked && input.value === "BLOCK");
        classificationContext.textContent = blocked
            ? "Blacklist controls current authorization. The access profile still applies to Central stay history and profile-specific exit enforcement."
            : "Whitelist controls current authorization. The access profile independently controls Central stay and exit behavior.";
    }

    function updateDailyWindow() {
        if (!dailyToggle || !dailyFields) return;
        dailyFields.hidden = !dailyToggle.checked;
        dailyFields.querySelectorAll("input").forEach((input) => {
            input.disabled = !dailyToggle.checked;
            input.required = dailyToggle.checked;
        });
    }

    function updateProfilesForSite() {
        if (!profile || !siteSelector) return;
        const siteId = siteSelector.value;
        [...profile.options].forEach((option) => {
            if (!option.value) return;
            option.hidden = !siteId || option.dataset.siteId !== siteId;
            option.disabled = option.hidden;
        });
        if (profile.selectedOptions[0]?.disabled) profile.value = "";
        profile.disabled = !siteId;
        updateProfileSummary();
    }

    function siteLocalNow(timeZone) {
        if (timeZone) {
            try {
                const parts = Object.fromEntries(
                    new Intl.DateTimeFormat("en-CA", {
                        timeZone,
                        year: "numeric",
                        month: "2-digit",
                        day: "2-digit",
                        hour: "2-digit",
                        minute: "2-digit",
                        hourCycle: "h23",
                    }).formatToParts(new Date()).filter((part) => part.type !== "literal").map((part) => [part.type, part.value])
                );
                return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
            } catch (_error) {
                // Invalid timezone data falls back to the browser's local clock.
            }
        }
        const now = new Date();
        now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
        return now.toISOString().slice(0, 16);
    }

    profile?.addEventListener("change", updateProfileSummary);
    modeInputs.forEach((input) => input.addEventListener("change", updateOverrideMode));
    customValue?.addEventListener("input", updateProfileSummary);
    customUnit?.addEventListener("change", updateProfileSummary);
    classificationInputs.forEach((input) => input.addEventListener("change", updateClassificationContext));
    dailyToggle?.addEventListener("change", updateDailyWindow);
    siteSelector?.addEventListener("change", () => {
        root.dataset.siteTimezone = siteSelector.selectedOptions[0]?.dataset.timezone || "";
        updateProfilesForSite();
    });
    root.querySelector("[data-set-current-datetime]")?.addEventListener("click", () => {
        const input = root.querySelector('input[name="valid_from"]');
        if (input) input.value = siteLocalNow(root.dataset.siteTimezone);
    });

    if (siteSelector) {
        root.dataset.siteTimezone = siteSelector.selectedOptions[0]?.dataset.timezone || "";
        updateProfilesForSite();
    }
    updateOverrideMode();
    updateClassificationContext();
    updateDailyWindow();
})();
