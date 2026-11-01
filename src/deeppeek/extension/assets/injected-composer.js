// =====================================================================
// injected-composer.js — typing + Enter (STABILITY TIER 2)
// =====================================================================
//
// Composer discovery and typing.  DeepSeek's composer is a
// textarea (or contenteditable) that React gates on a *trusted*
// input event, so the only way to make it accept a programmatic
// write is `document.execCommand('insertText')` on a focused,
// selected element.  A native value-setter + InputEvent does not
// mount the submit control.
//
// Enter is dispatched as keydown/keypress/keyup with key=Enter.
// DeepSeek's handler does not gate on `isTrusted`, so this works
// in isolation.  But third-party userscripts (notably
// "DeepSeek Anti-recall") DO intercept Enter keydown to preserve
// the textarea content, and that blocks DeepSeek's own submit
// handler — the 2026-10-31 log shows the keystroke is swallowed
// and the composer never clears.
//
// v6 fix: doEnter now measures `.ds-message` count before and
// after Enter.  Success = either the composer cleared OR a new
// message appeared.  If neither happened within 2 * verifyMs,
// fall back to clicking the Send button (found by aria-label /
// title proximity to the textarea).  The message-count check is
// what makes the fallback safe: if a new message appeared, we
// never click Send, so no double-submit.
//
// Composer targeting uses a priority chain
// (active-matches-strategy → active-largest → first strategy →
// largest visible editable).
//
// Exports on `window.__dsx__`:
//
//   findComposer()      → {el, selector, source, active, chosen}
//   findSendButton()    → button element or null
//   doType(text, opts)  → {success, composer_source, ...}
//   doEnter(opts)       → {success, composer_cleared, submitted, ...}
//
// Bridge entry points (defined on `window`):
//
//   window.__dsx_type_json(argsJson)
//   window.__dsx_enter_json(argsJson)
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const COMPOSER_STRATEGIES = [
    'textarea[placeholder*="Message DeepSeek"]',
    'textarea[data-testid="chat-input"]',
    '#chat-input',
    'div[contenteditable][role="textbox"]',
    'textarea',
  ];

  function _isEditable(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "textarea") return true;
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      return ["text", "search", "email", "url", "tel", "password"]
        .includes(t);
    }
    return false;
  }

  function _isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden")
      return false;
    if (parseFloat(style.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 50 && r.height >= 15;
  }

  function _describeEl(el) {
    if (!el) return null;
    return {
      tag: el.tagName || null,
      id: el.id || null,
      type: el.type || null,
      name: el.name || null,
      placeholder: el.placeholder || null,
      cls: (typeof el.className === "string")
        ? el.className.slice(0, 80) : null,
    };
  }

  function _largestVisibleEditable() {
    const candidates = [];
    for (const el of document.querySelectorAll(
        'textarea, input, [contenteditable="true"], [contenteditable=""]')) {
      if (_isEditable(el) && _isVisible(el)) candidates.push(el);
    }
    if (!candidates.length) return null;
    candidates.sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return (rb.width * rb.height) - (ra.width * ra.height);
    });
    return candidates[0];
  }

  function _strategyMatches() {
    const out = [];
    const seen = new Set();
    for (const sel of COMPOSER_STRATEGIES) {
      for (const el of document.querySelectorAll(sel)) {
        if (seen.has(el)) continue;
        if (_isEditable(el) && _isVisible(el)) {
          out.push(el);
          seen.add(el);
        }
      }
    }
    return out;
  }

  function findComposer() {
    const active = document.activeElement;
    const activeOk = _isEditable(active) && _isVisible(active);
    const strategy = _strategyMatches();
    const largest = _largestVisibleEditable();

    if (activeOk && strategy.includes(active)) {
      return { el: active,
               selector: "active-element (matches strategy)",
               source: "active-matches-strategy",
               active, chosen: active };
    }
    if (activeOk && largest && active === largest) {
      return { el: active,
               selector: "active-element (largest editable)",
               source: "active-largest",
               active, chosen: active };
    }
    if (strategy.length) {
      return { el: strategy[0],
               selector: "strategy: " +
                         (strategy[0].getAttribute("data-testid")
                          || strategy[0].placeholder
                          || strategy[0].tagName.toLowerCase()),
               source: "selector",
               active, chosen: strategy[0] };
    }
    if (largest) {
      return { el: largest,
               selector: "largest-editable",
               source: "largest-editable",
               active, chosen: largest };
    }
    return { el: null, selector: null, source: "none",
             active, chosen: null };
  }

  // Locate the Send button near the composer.  Walks up from the
  // textarea (up to 5 ancestors) and checks each ancestor's buttons
  // for aria-label="Send"/"发送", title="Send"/"发送", or
  // "send message" variants.  Used as a fallback when Enter is
  // swallowed by a userscript; only called AFTER msgCount has been
  // verified not to have grown, so it can't double-submit.
  function findSendButton() {
    const ta = document.querySelector("textarea");
    if (!ta) return null;
    let el = ta.parentElement;
    for (let i = 0; i < 5 && el; i++) {
      for (const btn of el.querySelectorAll(
          'button, [role="button"], div[role="button"]')) {
        const label = (btn.getAttribute("aria-label") || "")
          .toLowerCase().trim();
        const title = (btn.getAttribute("title") || "")
          .toLowerCase().trim();
        if (label === "send" || label === "发送"
            || title === "send" || title === "发送"
            || label === "send message" || title === "send message") {
          const rect = btn.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) return btn;
        }
      }
      el = el.parentElement;
    }
    return null;
  }

  function setComposerValue(el, text) {
    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
      el.focus();
      if (typeof el.select === "function") el.select();
      try {
        if (document.execCommand("insertText", false, text)) return true;
      } catch (_) { /* fall through */ }
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (!desc || !desc.set) return false;
      desc.set.call(el, text);
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true,
        inputType: "insertText", data: text,
      }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (el.isContentEditable) {
      el.focus();
      el.textContent = text;
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true,
        inputType: "insertText", data: text,
      }));
      return true;
    }
    return false;
  }

  function pressEnter(el) {
    const init = {
      key: "Enter", code: "Enter", keyCode: 13, which: 13,
      bubbles: true, cancelable: true, composed: true,
    };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keypress", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
  }

  function _countMessages() {
    try {
      return document.querySelectorAll(".ds-message").length;
    } catch (_) {
      return 0;
    }
  }

  async function doType(text, opts) {
    const skipVerify = !!(opts && opts.skip_verify);
    const settleMs = skipVerify ? 0 : (Number(opts.settle_ms) || 300);
    const found = findComposer();
    const el = found.el;
    if (!el) {
      return { success: false,
               reason: "no composer found on page",
               composer_source: "none",
               active_element_info: _describeEl(found.active) };
    }

    const before = el.value ?? el.textContent ?? "";
    if (!setComposerValue(el, text)) {
      return { success: false,
               reason: "could not set composer value",
               composer_selector: found.selector,
               composer_source: found.source,
               composer_element_info: _describeEl(el),
               active_element_info: _describeEl(found.active) };
    }
    if (skipVerify) {
      return { success: true,
               composer_selector: found.selector,
               composer_source: found.source,
               composer_element_info: _describeEl(el),
               active_element_info: _describeEl(found.active),
               value_before: before,
               value_after: text,
               react_accepted: true,
               skipped_verification: true };
    }
    await new Promise((r) => setTimeout(r, settleMs));

    const after = el.value ?? el.textContent ?? "";
    const react_accepted = after === text;

    return {
      success: react_accepted,
      composer_selector: found.selector,
      composer_source: found.source,
      composer_element_info: _describeEl(el),
      active_element_info: _describeEl(found.active),
      value_before: before,
      value_after: after,
      react_accepted,
      reason: react_accepted
        ? undefined
        : "React state did not accept the value",
    };
  }

  // v6: Enter now has three states checked — composer cleared,
  // new message appeared, or neither (fall back to Send click).
  // The message-count check is the double-submit guard: only when
  // no new message has appeared AND the composer still has text do
  // we touch the Send button.
  async function doEnter(opts) {
    const verifyMs = Number(opts.verify_ms) || 700;
    const found = findComposer();
    const el = found.el;
    if (!el) {
      return { success: false,
               reason: "no composer found on page",
               composer_source: "none",
               active_element_info: _describeEl(found.active) };
    }

    const before = el.value ?? el.textContent ?? "";
    if (!before) {
      return {
        success: false,
        reason: "composer is empty — nothing to submit",
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
      };
    }

    const msgCountBefore = _countMessages();

    el.focus();
    pressEnter(el);
    await new Promise((r) => setTimeout(r, verifyMs));

    let after = el.value ?? el.textContent ?? "";
    let composerCleared = after === "";
    let msgCountAfter = _countMessages();
    let newMessageAppeared = msgCountAfter > msgCountBefore;

    if (composerCleared || newMessageAppeared) {
      return {
        success: true,
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
        value_before: before,
        value_after: after,
        submitted: true,
        composer_cleared: composerCleared,
        new_message_appeared: newMessageAppeared,
        msg_count_before: msgCountBefore,
        msg_count_after: msgCountAfter,
        entered_via: "keypress",
      };
    }

    // Second verify window — Enter can lag on slow pages or when
    // a userscript delays the event chain.
    await new Promise((r) => setTimeout(r, verifyMs));
    after = el.value ?? el.textContent ?? "";
    composerCleared = after === "";
    msgCountAfter = _countMessages();
    newMessageAppeared = msgCountAfter > msgCountBefore;

    if (composerCleared || newMessageAppeared) {
      return {
        success: true,
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
        value_before: before,
        value_after: after,
        submitted: true,
        composer_cleared: composerCleared,
        new_message_appeared: newMessageAppeared,
        msg_count_before: msgCountBefore,
        msg_count_after: msgCountAfter,
        entered_via: "keypress (delayed)",
      };
    }

    // Enter had no effect.  Try the Send button.
    const sendBtn = findSendButton();
    if (sendBtn) {
      try { sendBtn.click(); } catch (_) {}
      await new Promise((r) => setTimeout(r, verifyMs));
      after = el.value ?? el.textContent ?? "";
      composerCleared = after === "";
      msgCountAfter = _countMessages();
      newMessageAppeared = msgCountAfter > msgCountBefore;

      return {
        success: composerCleared || newMessageAppeared,
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
        value_before: before,
        value_after: after,
        submitted: true,
        composer_cleared: composerCleared,
        new_message_appeared: newMessageAppeared,
        msg_count_before: msgCountBefore,
        msg_count_after: msgCountAfter,
        entered_via: "send-button-fallback",
        reason: (composerCleared || newMessageAppeared)
          ? undefined
          : "Send click had no effect either — userscript likely " +
            "intercepting both Enter and Send click",
      };
    }

    return {
      success: false,
      composer_selector: found.selector,
      composer_source: found.source,
      composer_element_info: _describeEl(el),
      active_element_info: _describeEl(found.active),
      value_before: before,
      value_after: after,
      submitted: true,
      composer_cleared: false,
      new_message_appeared: false,
      msg_count_before: msgCountBefore,
      msg_count_after: msgCountAfter,
      entered_via: "keypress (no effect, no send button found)",
      reason: "Enter fired but neither composer cleared nor new " +
              "message appeared; no Send button found to fall back",
    };
  }

  window.__dsx__.findComposer = findComposer;
  window.__dsx__.findSendButton = findSendButton;
  window.__dsx__.doType = doType;
  window.__dsx__.doEnter = doEnter;

  window.__dsx_type_json = async function (argsJson) {
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    const text = typeof args.text === "string" ? args.text : "";
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    if (!text) {
      return JSON.stringify({ success: false, reason: "empty text" });
    }
    let result;
    try {
      result = await doType(text, opts);
    } catch (e) {
      result = { success: false, reason: `doType threw: ${e.message}` };
    }
    console.log("[deeppeek:type] result:", result);
    return JSON.stringify(result);
  };

  window.__dsx_enter_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch (_) {}
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    let result;
    try {
      result = await doEnter(opts);
    } catch (e) {
      result = { success: false, reason: `doEnter threw: ${e.message}` };
    }
    console.log("[deeppeek:enter] result:", result);
    return JSON.stringify(result);
  };
})();
