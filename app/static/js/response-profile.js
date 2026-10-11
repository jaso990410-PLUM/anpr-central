(() => {
  const form = document.querySelector("[data-response-profile]");
  if (!form) return;
  const samples = {
    BLANK: "",
    PLATE: "PFQ5217",
    DECISION: form.dataset.responseKind,
    ACCESS_STATUS: form.dataset.responseKind === "ALLOW" ? "WHITELIST_ACTIVE" : (form.dataset.responseKind === "DENY" ? "BLACKLIST_ACTIVE" : "NOT_LISTED"),
  };
  const rows = [...form.querySelectorAll("[data-response-row]")];
  const speakerEnabled = form.querySelector("[data-speaker-enabled]");

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

    const traffic = form.querySelector('[name="traffic_state"]:checked').value;
    const green = traffic === "GREEN";
    const greenGroup = form.querySelector("[data-green-duration]");
    const greenInput = greenGroup.querySelector("input");
    greenGroup.hidden = !green; greenInput.disabled = !green;
    form.querySelector("[data-green-not-applicable]").hidden = green;
    const light = form.querySelector("[data-response-preview-light]");
    light.textContent = traffic;
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
  refresh();
})();
