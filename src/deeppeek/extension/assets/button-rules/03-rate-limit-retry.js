// =====================================================================
// button-rules/03-rate-limit-retry.js — "Retry" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   END of the same capsule row that 01-default-capsule
//            targets.
//   Which:   capsule rows whose grandparent carries a `<SPAN>` with
//            the exact error text "Messages too frequent. Try again
//            later." — i.e. rows that belong to DeepSeek's rate-
//            limit notice banner, not to a normal assistant message.
//   Verify:  __dsx.buttons.debug(true) → red outline on the error
//            row only, "rate-limit-retry" label above the clone.
//            No label on normal message rows.
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Delegates entirely to `__dsx__.resume.fullPipeline(e, el, "rate-limit-retry")`.
//   Identical behaviour to 02-continue-btn — same resume pipeline,
//   same container picker, same worker.  The only difference is
//   WHERE the button appears (error banner vs. Continue row).
//
//   Shift-click opens a container picker.
//   Plain click resumes in the current tab's container.
//
// FILTER STRUCTURE
// ----------------
//   Verified against DeepSeek's current build:
//
//     <notice _60a11b2>                 ← row.parentElement.parentElement
//       ├── <SPAN _1ce76f5>error text</SPAN>
//       └── <div _425ea0b>
//             └── <div .ds-flex>        ← the capsule row
//                   └── [native] [native] [injected…]
//
//   The check is `notice.children` contains a `<SPAN>` whose trimmed
//   textContent equals NEEDLE.  This excludes sibling capsule rows
//   that happen to share the visible virtual list (Probe 1 showed
//   those as false-positives for a looser ancestor-innerText check).
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js and _resume.js.
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/03-rate-limit-retry] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.capsule || !window.__dsx__.resume) {
    console.error(
      "[DSX:button-rules/03-rate-limit-retry] shared helpers not " +
      "loaded — are _capsule.js and _resume.js listed before 03- " +
      "in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const C = window.__dsx__.capsule;

  // Exact text of DeepSeek's rate-limit notice.  If DeepSeek changes
  // the wording (punctuation, localization), edit this string and
  // re-package.  The `rowQualifies` predicate below does an exact
  // match against the notice's SPAN child — no substring matching.
  const NEEDLE = "Messages too frequent. Try again later.";

  const rowQualifies = (row) => {
    if (!C.isCapsuleRow(row)) return false;

    // The notice banner is 2 levels up from the capsule row:
    //   row → rowWrapper → notice
    // and the notice's direct <SPAN> child carries the message.
    const rowWrapper = row.parentElement;
    const notice = rowWrapper && rowWrapper.parentElement;
    if (!notice) return false;

    return [...notice.children].some(c =>
      c.tagName === "SPAN"
      && c.textContent.trim() === NEEDLE
    );
  };

  B.rule({
    name: "rate-limit-retry",
    position: "end",
    template: () => {
      for (const row of document.querySelectorAll(".ds-flex")) {
        const pill = C.makeTextPill(row, "Retry");
        if (pill) return pill;
      }
      return null;
    },
    target: ".ds-flex",
    filter: rowQualifies,
    decorate: (clone) => {
      clone.setAttribute(
        "title",
        `[DSX] rate-limit-retry — Resume in the current container ` +
        `(shift-click to pick a different container)`);
    },
    onClick: async (e, el) => {
      await window.__dsx__.resume.fullPipeline(e, el, "rate-limit-retry");
    },
  });
})();
