// =====================================================================
// injected-composer.js — typing + Enter (STABILITY TIER 2)
// =====================================================================
//
// ADAPTIVE ENTER (2026-11-15, fourth iteration)
// =============================================
// With `waitComposerReady` (attach) guaranteeing the button is
// enabled before enter fires, enter's precheck usually returns in
// 0ms.  The retry loop is preserved as a safety net for races.
//
// Precheck: `waitComposerReady` with `floor_ms: 0` returns as soon
// as the button is enabled or found-not-present.  If it times out,
// we proceed anyway (button detection could be a decoy at that
// moment) and rely on retries.
//
// Fallback: if all Enter attempts fail, click the send button
// directly.  With the multi-candidate `findSendButton`, this now
// works reliably.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};
  if (!window.__dsx__.state) {
    window.__dsx__.state = { boundComposer: null };
  }

  const STRICT_SELECTORS = [
    'textarea[placeholder*="Message DeepSeek"]',
    'textarea[data-testid="chat-input"]',
    '#chat-input',
  ];

  const COMPOSER_STRATEGIES = [
    'textarea[placeholder*="Message DeepSeek"]',
    'textarea[data-testid="chat-input"]',
    '#chat-input',
    'textarea.message-input-textarea',
    'textarea[placeholder="Ask Qwen"]',
    'textarea',
    'div[contenteditable][role="textbox"]',
  ];

  const ENTER_BACKOFF_MS = [0, 0, 300, 500];
  const ENTER_VERIFY_PER_ATTEMPT_MS = 500;
  const ENTER_POLL_INTERVAL_MS = 100;
  const ENTER_AFTER_LOOP_WAIT_MS = 1000;
  const ENTER_DEFAULT_ATTEMPTS = 3;
  const SEND_READY_PRECHECK_MS = 1500;
  const SEND_READY_PRECHECK_POLL_MS = 100;

  function _isEditable(el) {
    if (typeof window.__dsx__._isEditable === "function") {
      return window.__dsx__._isEditable(el);
    }
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "textarea") return true;
    return false;
  }

  function _isVisible(el) {
    if (typeof window.__dsx__._isVisible === "function") {
      return window.__dsx__._isVisible(el);
    }
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 5 && r.height >= 5;
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

  function _strictMatches() {
    const out = [];
    const seen = new Set();
    for (const sel of STRICT_SELECTORS) {
      for (const el of document.querySelectorAll(sel)) {
        if (seen.has(el)) continue;
        if (_isEditable(el) && _isVisible(el)) {
          out.push({ el, sel });
          seen.add(el);
        }
      }
      if (out.length) return out;
    }
    return out;
  }

  function _isGenerating() {
    if (typeof window.__dsx__.isGenerating === "function") {
      try { return !!window.__dsx__.isGenerating(); }
      catch (_) { return false; }
    }
    return false;
  }

  // Delegate to shared helper.
  function findSendButton(composerEl) {
    if (typeof window.__dsx__.findSendButton === "function") {
      return window.__dsx__.findSendButton(composerEl);
    }
    return null;
  }

  function _isSendEnabled(btn) {
    if (typeof window.__dsx__.isSendButtonEnabled === "function") {
      return window.__dsx__.isSendButtonEnabled(btn);
    }
    if (!btn) return false;
    return !btn.classList.contains("ds-button--disabled");
  }

  function findComposer(opts) {
    const strict = !!(opts && opts.strict_composer);
    const active = document.activeElement;
    const activeOk = _isEditable(active) && _isVisible(active);

    if (strict) {
      const matches = _strictMatches();
      if (matches.length === 0) {
        return {
          el: null, selector: null, source: "strict-no-match",
          active, chosen: null,
        };
      }
      const activeMatch = matches.find(m => m.el === active);
      const chosen = activeMatch || matches[0];
      return {
        el: chosen.el,
        selector: `strict: ${chosen.sel}`,
        source: activeMatch ? "strict-active" : "strict-selector",
        active, chosen: chosen.el,
      };
    }

    const strategy = _strategyMatches();
    const largest = _largestVisibleEditable();

    if (activeOk && strategy.includes(active)) {
      return {
        el: active,
        selector: "active-element (matches strategy)",
        source: "active-matches-strategy",
        active, chosen: active,
      };
    }
    if (activeOk && largest && active === largest) {
      return {
        el: active,
        selector: "active-element (largest editable)",
        source: "active-largest",
        active, chosen: active,
      };
    }
    if (strategy.length) {
      return {
        el: strategy[0],
        selector: "strategy: " +
                  (strategy[0].getAttribute("data-testid")
                   || strategy[0].placeholder
                   || strategy[0].tagName.toLowerCase()),
        source: "selector",
        active, chosen: strategy[0],
      };
    }
    if (largest) {
      return {
        el: largest,
        selector: "largest-editable",
        source: "largest-editable",
        active, chosen: largest,
      };
    }
    return {
      el: null, selector: null, source: "none",
      active, chosen: null,
    };
  }

  function setComposerValue(el, text) {
    if (el instanceof HTMLTextAreaElement
        || el instanceof HTMLInputElement) {
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

  async function _ensureFocus(el) {
    for (let i = 0; i < 5; i++) {
      if (document.activeElement === el) {
        return { ok: true, attempts: i, active: el };
      }
      try { el.click(); } catch (_) {}
      await new Promise(r => setTimeout(r, 50));
      if (document.activeElement === el) {
        return { ok: true, attempts: i + 1, active: el };
      }
      try { el.focus(); } catch (_) {}
      await new Promise(r => setTimeout(r, 50));
      if (document.activeElement === el) {
        return { ok: true, attempts: i + 1, active: el };
      }
    }
    return {
      ok: false, attempts: 5, active: document.activeElement,
    };
  }

  function resolveTargetComposer(opts) {
    const strict = !!(opts && opts.strict_composer);
    if (!strict) return findComposer();

    const state = window.__dsx__.state;
    const bound = state.boundComposer;
    if (bound && bound.el && bound.el.isConnected) {
      return {
        el: bound.el,
        selector: bound.selector,
        source: "boundComposer-cached",
        active: document.activeElement,
        chosen: bound.el,
      };
    }

    const found = findComposer({ strict_composer: true });
    if (found.el) {
      state.boundComposer = {
        el: found.el,
        selector: found.selector,
        boundAt: Date.now(),
      };
      return { ...found, source: "strict-rebind" };
    }
    return found;
  }

  async function bindComposer() {
    const deadline = Date.now() + 5000;
    let attempts = 0;
    while (Date.now() < deadline) {
      attempts++;
      const found = findComposer({ strict_composer: true });
      if (found.el) {
        window.__dsx__.state.boundComposer = {
          el: found.el,
          selector: found.selector,
          boundAt: Date.now(),
        };
        return {
          success: true,
          selector: found.selector,
          source: found.source,
          attempts,
        };
      }
      await new Promise(r => setTimeout(r, 200));
    }
    return {
      success: false,
      reason: `strict composer not found after 5s (${attempts} attempts)`,
      tried_selectors: STRICT_SELECTORS,
      attempts,
    };
  }

  function probeComposer() {
    const state = window.__dsx__.state;
    const bound = state.boundComposer;
    const el = (bound && bound.el && bound.el.isConnected)
      ? bound.el
      : (findComposer({ strict_composer: true }).el);

    const ta = el;
    const disabled = !!(ta && (ta.disabled || ta.readOnly));
    const present = !!ta && ta.isConnected;
    let chipCount = 0;
    try {
      chipCount = document.querySelectorAll(
        ".ds-animated-size-item").length;
    } catch (_) {}

    let sendBtnPresent = false;
    let sendBtnEnabled = false;
    let candidateCount = 0;
    try {
      if (typeof window.__dsx__._allSendButtonCandidates === "function") {
        const candidates = window.__dsx__._allSendButtonCandidates(el);
        candidateCount = candidates.length;
        sendBtnPresent = candidates.length > 0;
        sendBtnEnabled = candidates.some(_isSendEnabled);
      } else {
        const sb = findSendButton(el);
        if (sb) {
          sendBtnPresent = true;
          sendBtnEnabled = _isSendEnabled(sb);
        }
      }
    } catch (_) {}

    return {
      success: true,
      textarea_present: present,
      textarea_disabled: disabled,
      chip_count: chipCount,
      is_generating: _isGenerating(),
      send_button_present: sendBtnPresent,
      send_button_enabled: sendBtnEnabled,
      send_button_candidates: candidateCount,
      composer_source: bound && bound.el && bound.el.isConnected
        ? "boundComposer"
        : "strict-fresh",
    };
  }

  async function doType(text, opts) {
    const skipVerify = !!(opts && opts.skip_verify);
    const settleMs = skipVerify ? 0 : (Number(opts.settle_ms) || 300);
    const strict = !!(opts && opts.strict_composer);

    const found = strict
      ? resolveTargetComposer({ strict_composer: true })
      : findComposer();
    const el = found.el;
    if (!el) {
      return {
        success: false,
        reason: strict
          ? "strict composer not found"
          : "no composer found on page",
        composer_source: found.source,
        active_element_info: _describeEl(found.active),
      };
    }
    if (strict && _isGenerating()) {
      return {
        success: false,
        reason: "composer is read-only (model generating)",
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
      };
    }

    const before = el.value ?? el.textContent ?? "";
    if (!setComposerValue(el, text)) {
      return {
        success: false,
        reason: "could not set composer value",
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
      };
    }
    if (skipVerify) {
      return {
        success: true,
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
        value_before: before,
        value_after: text,
        react_accepted: true,
        skipped_verification: true,
      };
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

  async function doEnter(opts) {
    const strict = !!(opts && opts.strict_composer);
    const expected = typeof opts.expected_text === "string"
      ? opts.expected_text
      : null;
    const maxAttempts = Math.max(
      1, Math.min(10, Number(opts.enter_attempts)
                     || ENTER_DEFAULT_ATTEMPTS));

    const found = strict
      ? resolveTargetComposer({ strict_composer: true })
      : findComposer();
    const el = found.el;
    if (!el) {
      return {
        success: false,
        reason: strict
          ? "strict composer not found"
          : "no composer found on page",
        composer_source: found.source,
        active_element_info: _describeEl(found.active),
        focus_ok: false, focus_attempts: 0, attempts_used: 0,
      };
    }

    let before = el.value ?? el.textContent ?? "";
    let drift_recovered = false;
    if (strict && expected !== null && before !== expected) {
      const retype = await doType(expected, {
        skip_verify: false, settle_ms: 300,
        strict_composer: true,
      });
      if (!retype.success) {
        return {
          success: false,
          reason: `pre-enter retype failed: ${retype.reason}`,
          composer_selector: found.selector,
          composer_source: found.source,
          composer_element_info: _describeEl(el),
          active_element_info: _describeEl(found.active),
          value_before: before,
          expected_text: expected,
          focus_ok: false, focus_attempts: 0, attempts_used: 0,
        };
      }
      before = el.value ?? el.textContent ?? "";
      drift_recovered = true;
    }

    if (!before) {
      return {
        success: false,
        reason: "composer is empty — nothing to submit",
        composer_selector: found.selector,
        composer_source: found.source,
        composer_element_info: _describeEl(el),
        active_element_info: _describeEl(found.active),
        focus_ok: false, focus_attempts: 0, attempts_used: 0,
      };
    }

    // ---- precheck: is the composer ready right now? ----
    let precheck = { ready: false, waited_ms: 0 };
    if (typeof window.__dsx__.waitComposerReady === "function") {
      try {
        const w = await window.__dsx__.waitComposerReady({
          timeout_ms: SEND_READY_PRECHECK_MS,
          floor_ms: 0,
          extended_grace_ms: 500,
          poll_ms: SEND_READY_PRECHECK_POLL_MS,
          composer_el: el,
          require_chip: true,
        });
        precheck = {
          ready: !!w.success,
          method: w.method,
          waited_ms: w.waited_ms,
          button_enabled: w.button_enabled,
        };
      } catch (_) {}
    } else {
      // Fallback for older page-world
      const btn = findSendButton(el);
      precheck = {
        ready: _isSendEnabled(btn),
        method: "direct",
        waited_ms: 0,
        button_enabled: _isSendEnabled(btn),
      };
    }

    const msgCountBefore = _countMessages();
    const attemptsLog = [];
    let lastFocusResult = null;
    let lastActiveAtEnter = null;
    let lastValueAfter = before;
    let lastMsgCountAfter = msgCountBefore;

    const baseResult = {
      composer_selector: found.selector,
      composer_source: found.source,
      composer_element_info: _describeEl(el),
      active_element_info: _describeEl(found.active),
      value_before: before,
      drift_recovered,
      msg_count_before: msgCountBefore,
      precheck,
    };

    function _checkSuccess() {
      const curValue = el.value ?? el.textContent ?? "";
      const curMsgCount = _countMessages();
      const curCleared = curValue === "";
      const curNewMsg = curMsgCount > msgCountBefore;
      const curGen = _isGenerating();
      lastValueAfter = curValue;
      lastMsgCountAfter = curMsgCount;
      return {
        success: curCleared || curNewMsg || curGen,
        composer_cleared: curCleared,
        new_message_appeared: curNewMsg,
        is_generating: curGen,
      };
    }

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      if (attempt > 1) {
        const check = _checkSuccess();
        if (check.success) {
          return {
            ...baseResult,
            success: true, submitted: true,
            composer_cleared: check.composer_cleared,
            new_message_appeared: check.new_message_appeared,
            is_generating: check.is_generating,
            msg_count_after: lastMsgCountAfter,
            value_after: lastValueAfter,
            attempts_used: attempt - 1,
            attempt_log: attemptsLog,
            focus_ok: lastFocusResult?.ok,
            focus_attempts: lastFocusResult?.attempts,
            active_at_enter: lastActiveAtEnter,
            entered_via: `keypress (attempt ${attempt - 1})`,
          };
        }
        const backoff = ENTER_BACKOFF_MS[attempt] || 500;
        if (backoff > 0) {
          await new Promise(r => setTimeout(r, backoff));
        }
      }

      const focusResult = await _ensureFocus(el);
      lastFocusResult = focusResult;
      pressEnter(el);
      lastActiveAtEnter = _describeEl(document.activeElement);
      attemptsLog.push({
        attempt,
        focus_ok: focusResult.ok,
        focus_attempts: focusResult.attempts,
        at: Date.now(),
      });

      const deadline = Date.now() + ENTER_VERIFY_PER_ATTEMPT_MS;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, ENTER_POLL_INTERVAL_MS));
        const check = _checkSuccess();
        if (check.success) {
          return {
            ...baseResult,
            success: true, submitted: true,
            composer_cleared: check.composer_cleared,
            new_message_appeared: check.new_message_appeared,
            is_generating: check.is_generating,
            msg_count_after: lastMsgCountAfter,
            value_after: lastValueAfter,
            attempts_used: attempt,
            attempt_log: attemptsLog,
            focus_ok: focusResult.ok,
            focus_attempts: focusResult.attempts,
            active_at_enter: lastActiveAtEnter,
            entered_via: `keypress (attempt ${attempt})`,
          };
        }
      }
    }

    // ---- Send-button click fallback ----
    const sendBtn = findSendButton(el);
    if (sendBtn && _isSendEnabled(sendBtn)) {
      console.log(
        "[deeppeek:enter] all Enter attempts failed — clicking Send");
      try { sendBtn.click(); } catch (_) {}
      await new Promise(r => setTimeout(r, ENTER_VERIFY_PER_ATTEMPT_MS));
      const check = _checkSuccess();
      if (check.success) {
        return {
          ...baseResult,
          success: true, submitted: true,
          composer_cleared: check.composer_cleared,
          new_message_appeared: check.new_message_appeared,
          is_generating: check.is_generating,
          msg_count_after: lastMsgCountAfter,
          value_after: lastValueAfter,
          attempts_used: maxAttempts + 1,
          attempt_log: attemptsLog,
          focus_ok: lastFocusResult?.ok,
          focus_attempts: lastFocusResult?.attempts,
          active_at_enter: lastActiveAtEnter,
          entered_via: "send-button-click",
        };
      }
    }

    // ---- Final patient wait ----
    await new Promise(r => setTimeout(r, ENTER_AFTER_LOOP_WAIT_MS));
    const finalCheck = _checkSuccess();
    if (finalCheck.success) {
      return {
        ...baseResult,
        success: true, submitted: true,
        composer_cleared: finalCheck.composer_cleared,
        new_message_appeared: finalCheck.new_message_appeared,
        is_generating: finalCheck.is_generating,
        msg_count_after: lastMsgCountAfter,
        value_after: lastValueAfter,
        attempts_used: maxAttempts,
        attempt_log: attemptsLog,
        focus_ok: lastFocusResult?.ok,
        focus_attempts: lastFocusResult?.attempts,
        active_at_enter: lastActiveAtEnter,
        entered_via: `keypress (delayed after ${maxAttempts} attempts)`,
      };
    }

    return {
      ...baseResult,
      success: false, submitted: true,
      composer_cleared: false,
      new_message_appeared: false,
      is_generating: false,
      msg_count_after: lastMsgCountAfter,
      value_after: lastValueAfter,
      attempts_used: maxAttempts,
      attempt_log: attemptsLog,
      focus_ok: lastFocusResult?.ok,
      focus_attempts: lastFocusResult?.attempts,
      active_at_enter: lastActiveAtEnter,
      entered_via: "no-effect-after-retries-and-click",
      reason: `${maxAttempts} Enter attempt(s) + Send click all ` +
              `failed (precheck ready=${precheck.ready}, ` +
              `method=${precheck.method})`,
    };
  }

  window.__dsx__.findComposer = findComposer;
  window.__dsx__.findSendButton = findSendButton;
  window.__dsx__.doType = doType;
  window.__dsx__.doEnter = doEnter;
  window.__dsx__.resolveTargetComposer = resolveTargetComposer;
  window.__dsx__.bindComposer = bindComposer;
  window.__dsx__.probeComposer = probeComposer;

  window.__dsx_type_json = async function (argsJson) {
    let args;
    try { args = JSON.parse(argsJson); } catch (e) {
      return JSON.stringify({
        success: false, reason: `arg parse failed: ${e.message}`,
      });
    }
    const text = typeof args.text === "string" ? args.text : "";
    const opts = (args.opts && typeof args.opts === "object")
      ? args.opts : {};
    if (!text) {
      return JSON.stringify({ success: false, reason: "empty text" });
    }
    let result;
    try { result = await doType(text, opts); }
    catch (e) {
      result = { success: false, reason: `doType threw: ${e.message}` };
    }
    console.log("[deeppeek:type] result:", result);
    return JSON.stringify(result);
  };

  window.__dsx_enter_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch (_) {}
    const opts = (args.opts && typeof args.opts === "object")
      ? args.opts : {};
    let result;
    try { result = await doEnter(opts); }
    catch (e) {
      result = { success: false, reason: `doEnter threw: ${e.message}` };
    }
    console.log("[deeppeek:enter] result:", result);
    return JSON.stringify(result);
  };

  window.__dsx_bind_composer_json = async function (argsJson) {
    try {
      const r = await bindComposer();
      console.log("[deeppeek:bind-composer] result:", r);
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `bindComposer threw: ${e.message}`,
      });
    }
  };

  window.__dsx_probe_composer_json = async function (argsJson) {
    try {
      const r = probeComposer();
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `probeComposer threw: ${e.message}`,
      });
    }
  };
})();