// =====================================================================
// button-rules/04-length-limit-reached.js — "Retry" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   END of the row that belongs to DeepSeek's context-
//            length notice banner.
//   Which:   any `.ds-flex` row whose ancestor chain (up to 4
//            levels) holds a <SPAN> whose trimmed text STARTS WITH
//            "Length limit reached" — AND whose own children
//            include at least one native capsule button.
//
//            The current DeepSeek build renders the notice text as:
//
//              "Length limit reached. Please start a new chat."
//
//            and the notice's row contains exactly ONE native
//            capsule button (older builds had ≥2, matching a normal
//            assistant message row).  This rule used to delegate to
//            `C.isCapsuleRow`, which requires ≥2 natives, so it
//            silently stopped firing when DeepSeek trimmed the row
//            down to 1 button.  The filter below now accepts ≥1.
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

  // How many ancestors above the target row to search for the
  // notice.  4 covers both shapes observed so far (2-level rate-
  // limit, 3-level context-length) and leaves one level of slack
  // for future wrappers.
  const MAX_ANCESTOR_WALK = 4;

  const rowQualifies = (row) => {
    if (!row || row.nodeType !== 1) return false;

    // Require at least ONE native capsule button in the row.
    // Historically we required >=2 (via C.isCapsuleRow), matching
    // a normal assistant message row.  DeepSeek's current length-
    // limit notice renders only ONE button, so relax to >=1 while
    // still requiring it to be a capsule — that keeps us from
    // injecting into arbitrary .ds-flex wrappers that happen to
    // sit under a notice-shaped ancestor.
    const natives = [...row.children].filter(
      c => c.nodeType === 1 && !c.hasAttribute("data-dsx-injected"));
    if (natives.length === 0) return false;
    if (!natives.some(k => k.matches(C.CAPSULE_SEL))) return false;

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
      // Build the template from ANY native capsule on the page —
      // not necessarily inside a proper capsule row.  The template
      // element is only cloned as a shape; it doesn't need to be
      // in the target row.  This works even when the only native
      // capsule in the DOM is the single button inside the length-
      // limit notice itself.
      const natives = document.querySelectorAll(C.CAPSULE_SEL);
      for (const nat of natives) {
        const pill = C.makeTextPillFromNative(nat, "Retry");
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
