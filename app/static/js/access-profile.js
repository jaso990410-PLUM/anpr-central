(() => {
  const form = document.querySelector("[data-access-profile-form]");
  if (!form) return;
  const rules = JSON.parse(form.querySelector("[data-site-exit-rules]").textContent);
  const site = form.querySelector("[data-profile-site]");
  const mode = () => form.querySelector('[name="stay_limit_mode"]:checked').value;
  const monitor = form.querySelector('[name="overstay_monitoring_enabled"]');
  const grace = form.querySelector('[name="overstay_exit_grace_minutes"]');
  const value = form.querySelector('[name="maximum_stay_value"]');
  const unit = form.querySelector('[name="maximum_stay_unit"]');
  const labels = {ALLOW: "Allow", REVIEW: "Review", DENY: "Deny"};

  const effective = (select) => select.value === "USE_SITE_POLICY"
    ? ((rules[site.value] || {})[select.dataset.condition] || "REVIEW")
    : select.value;

  const refresh = () => {
    const limited = mode() === "LIMITED";
    form.querySelector("[data-stay-limited]").hidden = !limited;
    form.querySelector("[data-unlimited-note]").hidden = limited;
    monitor.disabled = !limited;
    const monitored = limited && monitor.checked;
    form.querySelector('[data-exit-condition="overstay"]').classList.toggle("is-inapplicable", !monitored);
    form.querySelector("[data-grace-row]").classList.toggle("is-inapplicable", !monitored);
    form.querySelector("[data-overstay-applicability]").textContent = monitored
      ? "Applies after the stay limit and grace period."
      : "Not applicable while overstay monitoring is inactive.";

    let restrictive = false;
    form.querySelectorAll("[data-policy-select]").forEach((select) => {
      const action = effective(select);
      restrictive ||= action === "REVIEW" || action === "DENY";
      const inherited = select.parentElement.querySelector("[data-inherited-rule]");
      inherited.textContent = select.value === "USE_SITE_POLICY" ? `Effective site rule: ${labels[action]}` : "";
      const summary = form.querySelector(`[data-summary-policy="${select.dataset.condition}"]`);
      summary.textContent = select.dataset.condition === "overstay" && monitored
        ? `${labels[action]} after ${grace.value || 0} min grace`
        : (select.dataset.condition === "overstay" && !monitored ? "Not applicable" : labels[action]);
    });
    form.querySelector("[data-restrictive-warning]").hidden = !restrictive;
    form.querySelector("[data-summary-stay]").textContent = limited ? `${value.value || "?"} ${unit.options[unit.selectedIndex].text.toLowerCase()}` : "Unlimited";
    form.querySelector("[data-summary-monitoring]").textContent = monitored ? "Enabled" : "Not applicable";
  };
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  refresh();
})();
