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
    const result = {
      success: false,
      files_removed: 0,
      files_remaining: 0,
      textarea_cleared: false,
      textarea_found: false
    };

    // 1. Clear the textarea using the native setter.
    const ta = document.querySelector('textarea');
    if (ta) {
      result.textarea_found = true;
      ta.focus();
      try {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')
          .set.call(ta, '');
        ta.dispatchEvent(new InputEvent('input', {
          bubbles: true, cancelable: true, inputType: 'deleteContentBackward',
        }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        result.textarea_cleared = (ta.value === '');
      } catch (e) {
        console.warn('[deeppeek:clear] textarea clear failed:', e);
      }
    }

    // 2. Remove file chips by invoking React's onClick.
    let guard = 0;
    while (guard++ < 50) {
      const chip = document.querySelector('.ds-animated-size-item');
      if (!chip) break;

      const tabbables = chip.querySelectorAll('div[tabindex="0"]');
      if (tabbables.length === 0) break;

      const removeBtn = tabbables[tabbables.length - 1];
      const propsKey = Object.keys(removeBtn).find(k => k.startsWith('__reactProps$'));

      if (propsKey && removeBtn[propsKey] && typeof removeBtn[propsKey].onClick === 'function') {
        try {
          removeBtn[propsKey].onClick({
            preventDefault: () => {},
            stopPropagation: () => {},
            target: removeBtn,
            currentTarget: removeBtn,
            nativeEvent: new MouseEvent('click')
          });
          result.files_removed++;
        } catch (e) {
          console.warn('[deeppeek:clear] onClick failed:', e);
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

    result.files_remaining = document.querySelectorAll('.ds-animated-size-item').length;
    result.success = result.files_remaining === 0 && (result.textarea_cleared || !result.textarea_found);
    return result;
  }

  window.__dsx__.doClear = doClear;

  window.__dsx_clear_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || '{}'); } catch (_) {}
    const opts = (args.opts && typeof args.opts === 'object') ? args.opts : {};
    let result;
    try {
      result = await doClear(opts);
    } catch (e) {
      result = { success: false, reason: `doClear threw: ${e.message}` };
    }
    console.log('[deeppeek:clear] result:', result);
    return JSON.stringify(result);
  };
})();