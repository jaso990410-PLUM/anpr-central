(() => {
  const form = document.querySelector("[data-response-profile]");
  if (!form) return;
  const now = new Date();
  const weekOne = new Date(now.getFullYear(), 0, 1);
  const weekNumber = Math.ceil((((now - weekOne) / 86400000) + weekOne.getDay() + 1) / 7);
  const samples = {
    BLANK: "",
    PLATE: "PFQ5217",
    DECISION: form.dataset.responseKind,
    ACCESS_STATUS: form.dataset.responseKind === "ALLOW" ? "WHITELIST_ACTIVE" : (form.dataset.responseKind === "DENY" ? "BLACKLIST_ACTIVE" : "NOT_LISTED"),
    CAMERA_NAME: "Main Entry",
    CAMERA_CODE: "MAIN-ENTRY-01",
    SITE_NAME: "Main Site",
    SITE_CODE: "MAIN",
    DIRECTION: "Entry",
    CURRENT_DATE: now.toLocaleDateString("en-GB"),
    CURRENT_TIME: now.toLocaleTimeString("en-GB"),
    CURRENT_WEEKDAY: now.toLocaleDateString("en-GB", { weekday: "long" }),
    CURRENT_WEEK_NUMBER: `Week ${String(weekNumber).padStart(2, "0")}`,
    CURRENT_DATETIME: `${now.toLocaleDateString("en-GB")} ${now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`,
  };
  const rows = [...form.querySelectorAll("[data-response-row]")];
  const speakerEnabled = form.querySelector("[data-speaker-enabled]");
  const previewScreen = form.querySelector("[data-response-preview-screen]");

  const refreshOverflow = () => {
    form.querySelectorAll("[data-response-preview-row]").forEach((row) => {
      row.classList.toggle("is-overflowing", row.scrollWidth > previewScreen.clientWidth);
    });
  };

  const refresh = () => {
    rows.forEach((row, index) => {
      const type = row.querySelector("[data-response-row-type]");
      const source = row.querySelector("[data-response-row-source]");
      const custom = row.querySelector("[data-response-row-custom]");
      const isCustom = type.value === "CUSTOM";
      source.hidden = isCustom; source.disabled = isCustom;
      custom.hidden = !isCustom; custom.disabled = !isCustom;
      form.querySelector(`[data-response-preview-row="${index + 1}"]`).textContent = isCustom ? custom.value : (samples[source.value] || "");
    });
    refreshOverflow();

    const brightness = Number(form.querySelector("[data-response-brightness]").value);
    const scrollSpeed = Number(form.querySelector("[data-response-scroll-speed]").value);
    previewScreen.style.setProperty("--led-opacity", String(brightness / 5));
    previewScreen.style.setProperty("--led-glow", `${0.5 + brightness * 1.1}px`);
    previewScreen.style.setProperty("--scroll-duration", `${12 - scrollSpeed * 1.5}s`);
    form.querySelector("[data-response-brightness-output]").textContent = String(brightness);
    form.querySelector("[data-response-scroll-speed-output]").textContent = String(scrollSpeed);

    const traffic = form.querySelector('[name="traffic_state"]:checked').value;
    const green = traffic === "GREEN";
    const greenGroup = form.querySelector("[data-green-duration]");
    const greenInput = greenGroup.querySelector("input");
    greenGroup.hidden = !green; greenInput.disabled = !green;
    form.querySelector("[data-green-not-applicable]").hidden = green;
    const light = form.querySelector("[data-response-preview-light]");
    light.textContent = traffic;
    light.setAttribute("aria-label", `Traffic indicator: ${traffic}`);
    light.className = `peripheral-preview__light peripheral-preview__light--${traffic.toLowerCase()}`;

    const messageGroup = form.querySelector("[data-speaker-message]");
    const messageInput = messageGroup.querySelector("input");
    messageGroup.hidden = !speakerEnabled.checked; messageInput.disabled = !speakerEnabled.checked;
    form.querySelector("[data-speaker-disabled]").hidden = speakerEnabled.checked;

    const displaySeconds = Number(form.querySelector('[name="display_seconds"]').value) || 0;
    const greenSeconds = green ? (Number(greenInput.value) || 0) : 0;
    form.querySelector("[data-preview-traffic]").textContent = traffic.charAt(0) + traffic.slice(1).toLowerCase();
    form.querySelector("[data-preview-display-time]").textContent = `${displaySeconds} seconds`;
    form.querySelector("[data-preview-green-time]").textContent = green ? `${greenSeconds} seconds` : "Not applicable";
    form.querySelector("[data-preview-effective-time]").textContent = `Approximately ${Math.max(displaySeconds, greenSeconds, 1)} seconds`;
    form.querySelector("[data-preview-speaker]").textContent = speakerEnabled.checked ? (messageInput.value.replaceAll("{plate}", samples.PLATE) || "Enabled") : "Disabled";
  };

  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  window.addEventListener("resize", refreshOverflow);
  refresh();
  document.fonts?.ready.then(refreshOverflow);
})();
