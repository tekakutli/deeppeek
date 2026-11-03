// =====================================================================
// button-rules/_capsule.js — shared helpers for capsule-row buttons
// =====================================================================
//
// Loaded FIRST (via button-rules/_manifest.json) so `__dsx__.capsule`
// exists before 01-default-capsule.js and 03-rate-limit-retry.js run.
// Files prefixed with `_` are shared helpers — they define namespaces
// on `window.__dsx__` but do NOT register rules.
//
// Exported API (window.__dsx__.capsule):
//   CAPSULE_SEL               CSS selector for a capsule-xs icon button
//   isNativeCapsule(el)       true if el is a non-injected <button>
//   isCapsuleRow(row)         true if row's native children are ALL
//                             capsule-xs icon buttons, ≥2 of them
//   makeTextPill(row, label)  returns a fresh clone of the row's first
//                             native capsule transformed into a
//                             text-labelled pill; null if row isn't a
//                             capsule row
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};
  if (window.__dsx__.capsule) return;   // idempotent

  const MARKER_ATTR = "data-dsx-injected";
  const CAPSULE_SEL = ".ds-button--capsule.ds-button--xs[role='button']";

  const isNativeCapsule = (c) =>
    c.nodeType === 1 && !c.hasAttribute(MARKER_ATTR);

  const isCapsuleRow = (row) => {
    if (!row || row.nodeType !== 1) return false;
    const natives = [...row.children].filter(isNativeCapsule);
    if (natives.length < 2) return false;
    return natives.every(k => k.matches(CAPSULE_SEL));
  };

  const makeTextPill = (row, label) => {
    if (!isCapsuleRow(row)) return null;
    const natives = [...row.children].filter(isNativeCapsule);
    const base = natives[0].cloneNode(true);
    base.removeAttribute(MARKER_ATTR);
    // icon-only pill → text-labelled pill:
    //   · drop `--icon` so the pill isn't locked to a square
    //   · add `--min-width` so it grows to fit text (same class
    //     the code block's Copy button uses)
    //   · clear inline width + gap set by the --icon sizing
    //   · remove the icon container
    //   · append the label
    base.classList.remove("ds-button--icon");
    base.classList.add("ds-button--min-width");
    base.style.removeProperty("--dsl-button-width");
    base.style.removeProperty("--dsl-button-gap");
    const iconWrap = base.querySelector(".ds-button__icon-container");
    if (iconWrap) iconWrap.remove();
    const textEl = document.createElement("span");
    textEl.textContent = label;
    textEl.style.cssText =
      "white-space: nowrap; font-size: 12px; line-height: 1; " +
      "font-weight: 500; padding: 0 8px;";
    base.appendChild(textEl);
    return base;
  };

  window.__dsx__.capsule = {
    CAPSULE_SEL,
    isNativeCapsule,
    isCapsuleRow,
    makeTextPill,
  };
})();
