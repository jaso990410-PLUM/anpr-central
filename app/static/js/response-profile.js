(() => {
  document.querySelectorAll("[data-response-row]").forEach((row) => {
    const type = row.querySelector("[data-response-row-type]");
    const source = row.querySelector("[data-response-row-source]");
    const custom = row.querySelector("[data-response-row-custom]");
    const refresh = () => {
      const isCustom = type.value === "CUSTOM";
      source.hidden = isCustom;
      source.disabled = isCustom;
      custom.hidden = !isCustom;
      custom.disabled = !isCustom;
    };
    type.addEventListener("change", refresh);
    refresh();
  });
})();
