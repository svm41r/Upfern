(() => {
  const key = "upfern.appearance";
  const choices = [
    ["ember", "Ember", "Warm red glass", "#af4b42", "#131013"],
    ["slate", "Slate", "Cool steel with apricot highlights", "#e9a476", "#121b24"],
    ["frost", "Frost", "Cool blue glass", "#4f9bdc", "#101621"],
    ["ocean", "Ocean", "Cool teal glass", "#34a89d", "#101b1b"],
    ["dusk", "Dusk", "Cool violet glass", "#9375d9", "#171222"],
    ["amber", "Amber", "Warm gold glass", "#c68236", "#1b1510"],
    ["mono", "Black & white", "Pure black with white text", "#ffffff", "#000000"],
  ];
  const apply = id => {
    const name = choices.some(([choice]) => choice === id) ? id : "slate";
    document.documentElement.dataset.appearance = name;
    return name;
  };
  try { apply(localStorage.getItem(key)); } catch { apply("slate"); }
  window.UpfernAppearance = {
    choices,
    current: () => document.documentElement.dataset.appearance,
    set(id) { const chosen = apply(id); try { localStorage.setItem(key, chosen); } catch {} },
  };
  addEventListener("storage", e => { if (e.key === key) apply(e.newValue); });
})();
