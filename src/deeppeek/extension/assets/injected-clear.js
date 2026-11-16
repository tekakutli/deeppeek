// =====================================================================
// injected-clear.js — composer clearing (STABILITY TIER 3, VOLATILE)
// =====================================================================
//
// Removes attached file chips and empties the textarea.  This is
// the most volatile file in the tree because it depends on:
//
//   · the chip selector `.ds-animated-size-item` — a DeepSeek
//     internal class name
//   · the remove-button being the last `div[tabindex="0"]` inside
//     the chip
//   · React attaching `__reactProps$…` to that button and its
//     `onClick` being the actual removal path (a DOM `.click()`
//     does not trigger React's state update)
//
// When DeepSeek reworks its chip UI, this file needs editing.
// Isolating it means the volatile bit can be updated without
// touching the composer-typing path (injected-composer.js), which
// is stable by comparison.
//
// STRICT MODE (2026-11-15)
// ========================
// When `opts.strict_composer` is true, `doClear` resolves the
// textarea via `__dsx__.resolveTargetComposer({strict_composer: true})`
// instead of the old `document.querySelector('textarea')`.  This
// ensures we clear the SPECIFIC composer we bound to, not whatever
// textarea happens to be first in the DOM.
//
// `injected-clear.js` loads AFTER `injected-composer.js` in
// content.js's PAGE_WORLD_SCRIPTS, so `resolveTargetComposer` is
// guaranteed to be defined by the time `doClear` runs at runtime.
//
// Exports on `window.__dsx__`:
//
//   doClear(opts) → {success, files_removed, files_remaining, ...}
//
// Bridge entry point (defined on `window`):
//
//   window.__dsx_clear_json(argsJson)
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  async function doClear(opts) {
    const strict = !!(opts && opts.strict_composer);

    const result = {
      success: false,
      files_removed: 0,
      files_remaining: 0,
      textarea_cleared: false,
      textarea_found: false,
      composer_source: strict ? "strict" : "default",
    };

    // ---- 1. Clear the textarea ----
    let ta = null;
    if (strict && typeof window.__dsx__.resolveTargetComposer === "function") {
      const resolved = window.__dsx__.resolveTargetComposer({
        strict_composer: true,
      });
      ta = resolved.el || null;
      result.composer_source = resolved.source || "strict-no-match";
    } else {
      ta = document.querySelector("textarea");
    }

    if (ta) {
      result.textarea_found = true;
      ta.focus();
      try {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")
          .set.call(ta, "");
        ta.dispatchEvent(new InputEvent("input", {
          bubbles: true, cancelable: true,
          inputType: "deleteContentBackward",
        }));
        ta.dispatchEvent(new Event("change", { bubbles: true }));
        result.textarea_cleared = (ta.value === "");
      } catch (e) {
        console.warn("[deeppeek:clear] textarea clear failed:", e);
      }
    }

    // ---- 2. Remove file chips by invoking React's onClick ----
    let guard = 0;
    while (guard++ < 50) {
      const chip = document.querySelector(".ds-animated-size-item");
      if (!chip) break;

      const tabbables = chip.querySelectorAll('div[tabindex="0"]');
      if (tabbables.length === 0) break;

      const removeBtn = tabbables[tabbables.length - 1];
      const propsKey = Object.keys(removeBtn)
        .find(k => k.startsWith("__reactProps$"));

      if (propsKey && removeBtn[propsKey]
          && typeof removeBtn[propsKey].onClick === "function") {
        try {
          removeBtn[propsKey].onClick({
            preventDefault: () => {},
            stopPropagation: () => {},
            target: removeBtn,
            currentTarget: removeBtn,
            nativeEvent: new MouseEvent("click"),
          });
          result.files_removed++;
        } catch (e) {
          console.warn("[deeppeek:clear] onClick failed:", e);
          break;
        }
      } else {
        try {
          removeBtn.click();
          result.files_removed++;
        } catch (_) {
          break;
        }
      }

      // Yield one event-loop tick so React can flush the removal.
      await new Promise(r => setTimeout(r, 0));
    }

    result.files_remaining =
      document.querySelectorAll(".ds-animated-size-item").length;
    result.success = result.files_remaining === 0
      && (result.textarea_cleared || !result.textarea_found);
    return result;
  }

  window.__dsx__.doClear = doClear;

  window.__dsx_clear_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch (_) {}
    const opts = (args.opts && typeof args.opts === "object")
      ? args.opts : {};
    let result;
    try {
      result = await doClear(opts);
    } catch (e) {
      result = { success: false, reason: `doClear threw: ${e.message}` };
    }
    console.log("[deeppeek:clear] result:", result);
    return JSON.stringify(result);
  };
})();