// =====================================================================
// button-rules/02-continue-btn.js — "Resume" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   START of the wrapper <div> around the "Continue" button.
//   Which:   any DIV whose direct children include a
//            `.ds-button[role="button"]` with textContent "Continue".
//   Verify:  __dsx.buttons.debug(true) → teal outline on the row,
//            "continue-btn" label above the clone.
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Delegates entirely to `__dsx__.resume.fullPipeline(e, el, "continue-btn")`.
//   See button-rules/_resume.js for the full pipeline (extract,
//   format, pick container, save payload, open tab, worker types
//   into the new tab).
//
//   Shift-click opens a container picker.
//   Plain click resumes in the current tab's container.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _resume.js.
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/02-continue-btn] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.resume) {
    console.error(
      "[DSX:button-rules/02-continue-btn] shared resume pipeline " +
      "not loaded — is _resume.js listed before 02- in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const MARKER_ATTR = "data-dsx-injected";

  const isContinueBtn = (el) => el && el.nodeType === 1
    && !el.hasAttribute(MARKER_ATTR)
    && el.matches(".ds-button[role='button']")
    && (el.innerText || "").trim() === "Continue";

  B.rule({
    name: "continue-btn",
    position: "start",
    template: () => {
      for (const b of document.querySelectorAll(".ds-button[role='button']")) {
        if (!isContinueBtn(b)) continue;
        const base = b.cloneNode(true);
        base.removeAttribute(MARKER_ATTR);
        const content = base.querySelector(".ds-button__content");
        if (content) content.textContent = "Resume";
        return base;
      }
      return null;
    },
    target: () => {
      const rows = new Set();
      for (const b of document.querySelectorAll(".ds-button[role='button']")) {
        if (!isContinueBtn(b)) continue;
        const p = b.parentElement;
        if (p && p.tagName === "DIV") rows.add(p);
      }
      return [...rows];
    },
    filter: (row) => [...row.children].some(isContinueBtn),
    decorate: (clone) => {
      clone.setAttribute(
        "title",
        `[DSX] continue-btn — Resume in the current container ` +
        `(shift-click to pick a different container)`);
    },
    onClick: async (e, el) => {
      await window.__dsx__.resume.fullPipeline(e, el, "continue-btn");
    },
  });
})();
