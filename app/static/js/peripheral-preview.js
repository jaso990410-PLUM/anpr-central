(() => {
  const standard = {
    BLANK: "—",
    RECOGNIZED_PLATE: "WQT4120",
    ACCESS_STATUS: "Whitelist",
    CAMERA_NAME: "MAIN-ENTRY-01",
    SITE_NAME: "MAIN",
    CAMERA_ROLE: "Entry",
    CURRENT_DATE: new Date().toLocaleDateString("en-GB"),
    CURRENT_TIME: new Date().toLocaleTimeString("en-GB"),
  };

  document.querySelectorAll("[data-behavior]").forEach((section) => {
    const refresh = () => {
      const preview = section.querySelector("[data-preview]");
      if (!preview) return;
      const rows = preview.querySelectorAll(".peripheral-preview__row");
      const types = section.querySelectorAll("[data-row-type]");
      const sources = section.querySelectorAll("[data-row-source]");
      const texts = section.querySelectorAll("[data-row-text]");

      types.forEach((type, index) => {
        const custom = type.value === "CUSTOM";
        sources[index].closest("[data-row-source-field]").hidden = custom;
        texts[index].closest("[data-row-text-field]").hidden = !custom;
        rows[index].textContent = custom ? (texts[index].value || "—") : (standard[sources[index].value] || "—");
        rows[index].classList.toggle("is-overflowing", rows[index].scrollWidth > preview.querySelector(".peripheral-preview__screen").clientWidth);
      });

      const speakerEnabled = section.querySelector("[data-speaker-enabled]").checked;
      section.querySelectorAll("[data-speaker-setting]").forEach((field) => {
        field.hidden = !speakerEnabled;
      });

      const mode = section.dataset.behavior;
      const value = (suffix) => section.querySelector(`[name="${mode}_${suffix}"]`);
      const traffic = value("traffic_state").value;
      const brightness = Number(value("led_brightness").value);
      const scrollSpeed = Number(value("led_scroll_speed").value);
      const screen = preview.querySelector(".peripheral-preview__screen");
      screen.style.setProperty("--led-opacity", String(brightness / 5));
      screen.style.setProperty("--led-glow", `${0.5 + brightness * 1.1}px`);
      screen.style.setProperty("--scroll-duration", `${12 - scrollSpeed * 1.5}s`);
      section.querySelector("[data-led-brightness-output]").textContent = String(brightness);
      section.querySelector("[data-led-scroll-speed-output]").textContent = String(scrollSpeed);
      const lamp = preview.querySelector("[data-preview-lamp]");
      lamp.textContent = traffic;
      lamp.setAttribute("aria-label", `Traffic indicator: ${traffic}`);
      lamp.className = `peripheral-preview__light peripheral-preview__light--${traffic.toLowerCase()}`;
    };
    section.addEventListener("input", refresh);
    section.addEventListener("change", refresh);
    refresh();
  });
})();
