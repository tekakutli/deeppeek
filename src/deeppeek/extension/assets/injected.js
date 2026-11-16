// =====================================================================
// injected.js — page-world bootstrap (STABILITY TIER 1)
// =====================================================================
//
// SEND BUTTON DETECTION (2026-11-15, final)
// =========================================
// The Send button is `<div role="button" class="ds-button
// ds-button--primary ds-button--filled ds-button--circle
// ds-button--m ds-button--icon-relative-m [ds-button--disabled]">`
// with NO accessible label.  Disabled state adds `.ds-button--disabled`;
// enabled state adds `tabindex="0"`.
//
// Problem: the DEACTIVATED-state DOM contains MULTIPLE elements
// matching the button selector (user reported two).  A single-match
// find can land on a permanently-disabled decoy and hang.
//
// Solution: check ALL candidates.  `isAnySendButtonEnabled` returns
// true if any candidate is enabled.  `findSendButton` prefers the
// enabled candidate, then the first by proximity to the composer.
//
// `waitComposerReady` returns on the FIRST of three signals:
//   (a) Any send button candidate is enabled  ← fast path
//   (b) No button candidate exists at all     ← fallback for other
//       providers or transient DOM state
//   (c) Extended grace elapsed                ← last-resort floor
//
// Total worst case is bounded by --upload-timeout (default 30s), but
// typical return is 200–800ms (button-enabled at real upload
// completion) or ~3.5s (extended grace when button detection is
// dead but chip+floor still lets us proceed).
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  if (!window.__dsx__.state) {
    window.__dsx__.state = { boundComposer: null };
  }

  window.__dsx__.config = {
    chatContainerSelector:
      ".ds-virtual-list-visible-items, .chat-messages-container, .chat-messages",
    titleSelector: ".afa34042.e37a04e4.e0a1edb7",
  };

  // ---- shared el predicates (used by send-button code below) ----
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
    return r.width >= 5 && r.height >= 5;
  }

  window.__dsx__._isEditable = _isEditable;
  window.__dsx__._isVisible = _isVisible;

  // ---- title / session / role helpers ----
  window.__dsx__.getChatTitle = function () {
    const el = document.querySelector(window.__dsx__.config.titleSelector);
    if (el && el.textContent.trim()) {
      return el.textContent.trim();
    }
    return document.title.trim() || null;
  };

  window.__dsx__.getSessionIdFromURL = function () {
    const m = window.location.href.match(/\/s\/([0-9a-f-]{20,})/i);
    return m ? m[1] : null;
  };

  window.__dsx__.normalizeRoleFromIDB = function (raw) {
    if (typeof raw !== "string") return null;
    const s = raw.toLowerCase();
    if (s === "user" || s === "human" || s === "customer") return "user";
    if (s === "assistant" || s === "ai" || s === "bot" || s === "model") return "assistant";
    return "assistant";
  };

  window.__dsx__.isGenerating = function () {
    for (const btn of document.querySelectorAll('button, [role="button"]')) {
      const r = btn.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const label = (btn.getAttribute("aria-label") || "")
        .toLowerCase().trim();
      const text = (btn.textContent || "").toLowerCase().trim();
      const testid = (btn.getAttribute("data-testid") || "")
        .toLowerCase().trim();
      if (label === "stop" || label === "stop generating"
          || label === "停止" || label === "停止生成"
          || text === "stop" || text === "停止"
          || testid === "stop" || testid === "stop-button"
          || testid === "stop-generating") return true;
    }
    return false;
  };

  // ---- send button (DeepSeek-specific) ----
  const _DS_SEND_BUTTON_SEL =
    'div[role="button"].ds-button--primary.ds-button--circle' +
    '.ds-button--icon-relative-m';

  function _allSendButtonCandidates(composerEl) {
    const seen = new Set();

    // Composer-scoped walk first — usually the right neighborhood.
    if (composerEl && composerEl.parentElement) {
      let el = composerEl.parentElement;
      for (let i = 0; i < 15 && el; i++) {
        const found = el.querySelectorAll(_DS_SEND_BUTTON_SEL);
        if (found.length > 0) {
          for (const b of found) seen.add(b);
          break;
        }
        el = el.parentElement;
      }
    }

    // Fall back to page-wide if composer-scoped walk found nothing.
    if (seen.size === 0) {
      for (const b of document.querySelectorAll(_DS_SEND_BUTTON_SEL)) {
        seen.add(b);
      }
    }

    return [...seen].filter(_isVisible);
  }

  function isSendButtonEnabled(btn) {
    if (!btn) return false;
    if (btn.classList.contains("ds-button--disabled")) return false;
    if (btn.getAttribute("aria-disabled") === "true") return false;
    if (btn.hasAttribute("disabled")) return false;
    if (typeof btn.disabled === "boolean" && btn.disabled) return false;
    return true;
  }

  function isAnySendButtonEnabled(composerEl) {
    const candidates = _allSendButtonCandidates(composerEl);
    return candidates.some(isSendButtonEnabled);
  }

  function findSendButton(composerEl) {
    const candidates = _allSendButtonCandidates(composerEl);
    for (const b of candidates) {
      if (isSendButtonEnabled(b)) return b;
    }
    return candidates[0] || null;
  }

  // -------------------------------------------------------------------
  // waitComposerReady — return as soon as ANY readiness signal fires.
  //
  // Signals (evaluated every poll_ms):
  //   1. any send-button candidate enabled → "button" (fast path)
  //   2. no button candidates exist AND past floor → "floor-no-button"
  //   3. button candidates exist but none enabled AND past extended
  //      grace → "extended-floor" (give up on button detection)
  //
  // The chip check requires at least one chip present when
  // `require_chip` is true (default).  Prevents false-positive
  // return on the text-only case.
  // -------------------------------------------------------------------
  async function waitComposerReady(opts) {
    const timeoutMs = Number(opts && opts.timeout_ms) || 8000;
    const floorMs = Number(opts && opts.floor_ms) || 500;
    const extendedGraceMs = Number(opts && opts.extended_grace_ms) || 3000;
    const pollMs = Number(opts && opts.poll_ms) || 100;
    const composerEl = (opts && opts.composer_el) || null;
    const requireChip = !(opts && opts.require_chip === false);

    const startedAt = Date.now();
    let lastSig = null;

    while (Date.now() - startedAt < timeoutMs) {
      const elapsed = Date.now() - startedAt;

      const candidates = _allSendButtonCandidates(composerEl);
      const btnPresent = candidates.length > 0;
      const anyEnabled = candidates.some(isSendButtonEnabled);

      let chipCount = 0;
      try {
        chipCount = document.querySelectorAll(
          ".ds-animated-size-item").length;
      } catch (_) {}
      const chipOk = !requireChip || chipCount > 0;

      const pastFloor = elapsed >= floorMs;
      const pastExtended = elapsed >= floorMs + extendedGraceMs;

      let ready = false;
      let method = null;

      if (chipOk) {
        if (anyEnabled) {
          ready = true;
          method = "button";
        } else if (!btnPresent && pastFloor) {
          ready = true;
          method = "floor-no-button";
        } else if (btnPresent && !anyEnabled && pastExtended) {
          ready = true;
          method = "extended-floor";
        }
      }

      const sig = JSON.stringify({
        be: anyEnabled, bp: btnPresent, cc: chipCount, r: ready,
      });
      if (sig !== lastSig) {
        console.log("[deeppeek:ready]", {
          elapsed_ms: elapsed,
          any_enabled: anyEnabled,
          btn_present: btnPresent,
          n_btn_candidates: candidates.length,
          chip_count: chipCount,
          ready, method,
        });
        lastSig = sig;
      }

      if (ready) {
        return {
          success: true,
          method,
          waited_ms: elapsed,
          chips: chipCount,
          button_present: btnPresent,
          button_enabled: anyEnabled,
          n_candidates: candidates.length,
        };
      }

      await new Promise(r => setTimeout(r, pollMs));
    }

    const candidates = _allSendButtonCandidates(composerEl);
    return {
      success: false,
      waited_ms: Date.now() - startedAt,
      reason: `composer not ready within ${timeoutMs}ms`,
      chips: document.querySelectorAll('.ds-animated-size-item').length,
      button_present: candidates.length > 0,
      n_candidates: candidates.length,
    };
  }

  // ---- chip-based wait (legacy fallback for non-DeepSeek) ----
  function _describeChip(chip) {
    const classList = [...chip.classList];
    const loadingClass = classList.find(c =>
      /load|pend|upload|progress|spin/i.test(c));

    const spinner = chip.querySelector(
      'svg[class*="load"], svg[class*="spin"], ' +
      '[class*="progress"], [class*="spinner"]');

    let fiberStatus = null;
    try {
      const fk = Object.keys(chip).find(k => k.startsWith('__reactFiber$'));
      if (fk) {
        let fiber = chip[fk];
        let d = 0;
        while (fiber && d < 20) {
          const props = fiber.memoizedProps;
          if (props && typeof props === 'object') {
            for (const key of ['status', 'uploadStatus', 'state',
                              'loading', 'isUploading']) {
              const v = props[key];
              if (typeof v === "string"
                  && /upload|pend|progress|load/i.test(v)) {
                fiberStatus = { key, value: v };
                break;
              }
              if (typeof v === "boolean"
                  && /loading|uploading/i.test(key) && v) {
                fiberStatus = { key, value: v };
                break;
              }
            }
          }
          if (fiberStatus) break;
          fiber = fiber.return;
          d++;
        }
      }
    } catch (_) {}

    const opacity = parseFloat(getComputedStyle(chip).opacity);
    const styleIsUploading = opacity < 0.95;

    return {
      uploading: !!loadingClass || !!spinner || !!fiberStatus
                 || styleIsUploading,
      classList, loadingClass, hasSpinner: !!spinner,
      fiberStatus, opacity,
      signature: {
        cls: loadingClass || null, spin: !!spinner,
        fib: fiberStatus, op: Math.round(opacity * 10) / 10,
      },
    };
  }

  async function waitUploadComplete(opts) {
    const timeoutMs = Number(opts && opts.timeout_ms) || 30000;
    const minWaitMs = Number(opts && opts.min_wait_ms) || 500;
    const pollMs = Number(opts && opts.poll_ms) || 100;
    const startedAt = Date.now();
    let sawChip = false;
    let chipFirstSeenAt = null;

    while (Date.now() - startedAt < timeoutMs) {
      const chips = [...document.querySelectorAll('.ds-animated-size-item')];

      if (chips.length === 0) {
        if (Date.now() - startedAt < 5000) {
          await new Promise(r => setTimeout(r, pollMs));
          continue;
        }
        return {
          success: true, complete: true, chips: 0,
          waited_ms: Date.now() - startedAt, method: "chip",
          reason: "no chips after attach (assumed complete)",
        };
      }

      if (!sawChip) {
        sawChip = true;
        chipFirstSeenAt = Date.now();
      }

      const details = chips.map(_describeChip);
      const uploadingCount = details.filter(d => d.uploading).length;
      const sinceChipMs = Date.now() - chipFirstSeenAt;

      if (uploadingCount === 0 && sinceChipMs >= minWaitMs) {
        return {
          success: true, complete: true, chips: chips.length,
          waited_ms: Date.now() - startedAt, method: "chip",
          details,
        };
      }

      await new Promise(r => setTimeout(r, pollMs));
    }

    return {
      success: false, complete: false,
      chips: document.querySelectorAll('.ds-animated-size-item').length,
      waited_ms: Date.now() - startedAt, method: "chip",
      reason: `chip wait timeout after ${timeoutMs}ms`,
    };
  }

  window.__dsx__.findSendButton = findSendButton;
  window.__dsx__.isSendButtonEnabled = isSendButtonEnabled;
  window.__dsx__.isAnySendButtonEnabled = isAnySendButtonEnabled;
  window.__dsx__.waitComposerReady = waitComposerReady;
  window.__dsx__.waitUploadComplete = waitUploadComplete;
  window.__dsx__._describeChip = _describeChip;
  window.__dsx__._allSendButtonCandidates = _allSendButtonCandidates;
  window.__dsx__._DS_SEND_BUTTON_SEL = _DS_SEND_BUTTON_SEL;
})();