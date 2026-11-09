// =====================================================================
// button-rules/04-length-limit-reached.js — "Retry" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   END of the capsule row that belongs to DeepSeek's
//            context-length notice banner.
//   Which:   capsule rows whose ancestor chain (up to 4 levels) holds
//            a <SPAN> whose trimmed text STARTS WITH
//            "Length limit reached" — i.e. rows under the
//            context-length notice, not rows under a normal
//            assistant message.
//
//            The current DeepSeek build renders the notice text as:
//
//              "Length limit reached. Please start a new chat."
//
//            The " Please start a new chat." suffix is DeepSeek's
//            action hint and is likely to be reworded between builds,
//            so the filter matches on the stable prefix with
//            startsWith() rather than requiring an exact match
//            against the full sentence.  startsWith (not includes)
//            keeps the check narrow — an assistant message that
//            happens to *quote* the phrase mid-sentence does not
//            qualify.
//
//   Verify:  __dsx__.buttons.debug(true) → coloured outline on the
//            notice row only, "length-limit-reached" label above
//            the clone.  No label on normal message rows.
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Delegates entirely to `__dsx__.resume.fullPipeline(e, el, "length-limit-reached")`.
//   Identical behaviour to 02-continue-btn and 03-rate-limit-retry —
//   same resume pipeline, same container picker, same worker.  The
//   only difference is WHERE the button appears (length-limit notice
//   vs. Continue row vs. rate-limit notice).
//
//   Shift-click opens a container picker.
//   Plain click resumes in the current tab's container.
//
// FILTER STRUCTURE — WHY IT WALKS UP 4 LEVELS
// -------------------------------------------
//   The rate-limit rule hard-codes `row.parentElement.parentElement`
//   as the notice, which works for that notice because the capsule
//   row sits exactly 2 levels below it:
//
//     <notice>
//       ├── <SPAN>…text…</SPAN>
//       └── <div>
//             └── <div .ds-flex>       ← row, 2 levels down
//
//   The context-length notice, however, wraps the row in one extra
//   container, so the row is 3 (or more) levels down:
//
//     <notice>
//       ├── <SPAN>…text…</SPAN>
//       └── <div>                      ← extra action-row wrapper
//             └── <div>
//                   └── <div .ds-flex> ← row, 3 levels down
//
//   Walking up to 4 ancestors covers both shapes without needing to
//   know which one we're in.  The check requires the ancestor to
//   have the notice <SPAN> as a *direct child* (not just somewhere
//   in its subtree), so it cannot false-match on a sibling capsule
//   row that happens to share an outer scroll wrapper with the
//   notice.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js and _resume.js (both shared helpers).
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/04-length-limit-reached] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.capsule || !window.__dsx__.resume) {
    console.error(
      "[DSX:button-rules/04-length-limit-reached] shared helpers not " +
      "loaded — are _capsule.js and _resume.js listed before 04- " +
      "in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const C = window.__dsx__.capsule;

  // Stable prefix of DeepSeek's context-length notice.  The full
  // on-screen string is currently
  //
  //   "Length limit reached. Please start a new chat."
  //
  // but the action-hint suffix is likely to be reworded, so match
  // on the prefix via startsWith.  If the *prefix* ever changes,
  // update this constant and re-package.
  const NEEDLE = "Length limit reached";

  // How many ancestors above the capsule row to search for the
  // notice.  4 covers both shapes observed so far (2-level rate-
  // limit, 3-level context-length) and leaves one level of slack
  // for future wrappers.
  const MAX_ANCESTOR_WALK = 4;

  const rowQualifies = (row) => {
    if (!C.isCapsuleRow(row)) return false;

    let n = row.parentElement;
    for (let i = 0; i < MAX_ANCESTOR_WALK && n; i++) {
      // The notice is the container whose *direct* children include
      // the text-bearing <SPAN>.  Requiring a direct child (rather
      // than a descendant) keeps the check tight — a broader
      // ancestor like the scroll wrapper would also contain the
      // SPAN further down, but it must not qualify, because every
      // capsule row in the visible list shares it.
      const hasNoticeSpan = [...n.children].some(c =>
        c.tagName === "SPAN"
        && c.textContent.trim().startsWith(NEEDLE)
      );
      if (hasNoticeSpan) return true;
      n = n.parentElement;
    }
    return false;
  };

  B.rule({
    name: "length-limit-reached",
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
        `[DSX] length-limit-reached — Resume in the current container ` +
        `(shift-click to pick a different container)`);
    },
    onClick: async (e, el) => {
      await window.__dsx__.resume.fullPipeline(e, el, "length-limit-reached");
    },
  });
})();
