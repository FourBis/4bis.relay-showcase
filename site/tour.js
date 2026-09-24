(() => {
  "use strict";
  const tabs = [...document.querySelectorAll("[data-capability]")];
  function selectCapability(name, focus = false) {
    for (const tab of tabs) {
      const selected = tab.dataset.capability === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) {
        tab.focus({ preventScroll: true });
        tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectCapability(tab.dataset.capability));
    tab.addEventListener("keydown", event => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectCapability(tabs[next].dataset.capability, true);
    });
  });
  document.querySelectorAll("[data-capability-target]").forEach(link => {
    link.addEventListener("click", () => selectCapability(link.dataset.capabilityTarget, true));
  });
  selectCapability("models");

  const dialog = document.getElementById("screenshot-dialog");
  const image = document.getElementById("screenshot-image");
  const caption = document.getElementById("screenshot-caption");
  const close = document.getElementById("screenshot-close");
  let opener;
  document.querySelectorAll(".screenshot-open").forEach(trigger => {
    trigger.addEventListener("click", () => {
      opener = trigger;
      caption.dataset.es = trigger.dataset.captionEs;
      caption.dataset.en = trigger.dataset.captionEn;
      caption.textContent = document.documentElement.lang === "en" ? caption.dataset.en : caption.dataset.es;
      image.alt = caption.textContent;
      image.src = trigger.dataset.image;
      dialog.showModal();
      dialog.querySelector(".screenshot-viewport").scrollTo(0, 0);
      close.focus();
    });
  });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => opener?.focus({ preventScroll: true }));
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
})();
