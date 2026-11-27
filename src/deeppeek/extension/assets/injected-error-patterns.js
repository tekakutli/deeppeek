// =====================================================================
// injected-error-patterns.js — registry + detector for DeepSeek error
// banners (STABILITY TIER 2)
// =====================================================================
//
// WHY
// ---
// When DeepSeek can't serve a request — rate limit, quota exhaustion,
// model unavailable, context length, etc. — it renders a notice banner
// in the chat UI instead of an assistant message.  The watch flow
// (injected-watch.js) would otherwise see "no new content" and, via
// its fallback path, return the *previous* turn's assistant message
// as if it were the current reply.  The daemon then happily streams
// stale content to aider.
//
// This module detects those banners and returns a structured error
// that injected-watch.js / bg-handlers-read.js can relay.  The
// daemon's bridge raises a BridgeError, so the client gets a real
// error instead of a bogus reply.
//
// REGISTRY
// --------
// `window.__dsx__.errorPatterns` is an array of entries:
//
//   {
//     name:      short identifier (e.g. "rate-limit")
//     category:  coarse class (e.g. "rate_limit", "quota_exhausted")
//     match:     (text) => bool — called with the banner's trimmed
//                <SPAN> text
//     hint:      optional human-readable hint for the daemon's error
//                message
//     retryable: optional bool — daemon currently doesn't retry on
//                BridgeError, but this field lets future code decide
//   }
//
// Adding a new error = one entry.  No other file needs to change.
//
// DETECTION ANCHOR
// ----------------
// Both 03-rate-limit-retry.js and 04-length-limit-reached.js key off
// the same structural shape:
//
//   <notice>
//     ├── <SPAN>error text</SPAN>
//     └── <div>
//           └── <div .ds-flex>
//                 └── [capsule button(s)]
//
// `detectErrorBanner(root)` scans for `<SPAN>` elements whose trimmed
// text matches a pattern, then verifies:
//   · the SPAN's parent also contains at least one
//     `.ds-button--capsule` (the retry/continue affordance)
//   · the SPAN is NOT inside a `.ds-message` (defense against user
//     pasting the error text into the chat)
//
// This is intentionally looser than `C.isCapsuleRow()` — DeepSeek has
// already shipped notice rows with 1 vs. 2 capsule buttons, and the
// tighter check has bitten us before (see 04-length-limit-reached.js
// header comment).
//
// Exposed API:
//   window.__dsx__.errorPatterns
//   window.__dsx__.detectErrorBanner(root?)  → error object | null
//   window.__dsx_detect_error_json(argsJson) → JSON string (bridge)
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // ---------- registry ---------------------------------------------
  //
  // Match functions are free to use exact equality, startsWith, or
  // regex.  Prefer startsWith when the message has a stable prefix
  // and a rewordable suffix ("Length limit reached. Please start a
  // new chat.").

  const patterns = [
    {
      name: "rate-limit",
      category: "rate_limit",
      retryable: true,
      hint: "DeepSeek is rate-limiting this account; wait a moment " +
            "before retrying.",
      match: (t) => t === "Messages too frequent. Try again later.",
    },
    {
      name: "usage-limit",
      category: "quota_exhausted",
      retryable: false,
      hint: "DeepSeek quota is exhausted for this account; wait for " +
            "the quota to reset.",
      match: (t) => t.startsWith("Usage limit reached"),
    },
    {
      name: "length-limit",
      category: "length_limit",
      retryable: false,
      hint: "Conversation length limit reached; start a new chat.",
      match: (t) => t.startsWith("Length limit reached"),
    },
    {
      name: "model-unavailable",
      category: "model_unavailable",
      retryable: true,
      hint: "DeepSeek reports the model is unavailable; retry shortly.",
      match: (t) => /\bmodel\b.*\bunavailable\b/i.test(t),
    },
  ];

  // ---------- detector ---------------------------------------------

  function detectErrorBanner(root) {
    root = root || document;

    // Narrow the scan to the chat container when called on document.
    // Inlined here rather than referencing __dsx__.config so the
    // detector still works if config is ever renamed.
    if (root === document) {
      const cc = document.querySelector(
        ".ds-virtual-list-visible-items, " +
        ".chat-messages-container, .chat-messages");
      if (cc) root = cc;
    }

    const spans = root.querySelectorAll("span");
    for (const span of spans) {
      const text = (span.textContent || "").trim();
      if (!text) continue;

      for (const pattern of patterns) {
        let matched = false;
        try { matched = pattern.match(text); }
        catch (_) { matched = false; }
        if (!matched) continue;

        // Structural check: the SPAN's parent must contain a capsule
        // button.  Every notice banner seen so far has one (the
        // "Retry" / "Continue" affordance); message bubbles don't.
        const parent = span.parentElement;
        if (!parent) continue;
        if (!parent.querySelector(".ds-button--capsule")) continue;

        // Defense: reject matches inside a normal message bubble.
        // A user pasting "Usage limit reached..." into the chat
        // would otherwise trigger a false positive.
        if (span.closest(".ds-message")) continue;

        return {
          pattern: pattern.name,
          category: pattern.category,
          retryable: !!pattern.retryable,
          hint: pattern.hint || "",
          text: text,
        };
      }
    }
    return null;
  }

  // ---------- exports ----------------------------------------------

  window.__dsx__.errorPatterns = patterns;
  window.__dsx__.detectErrorBanner = detectErrorBanner;

  // Bridge entry point for content.js → page world.  Returns
  // { success: true, error: <obj|null> }.
  window.__dsx_detect_error_json = function (argsJson) {
    // argsJson is accepted but unused today; kept for symmetry with
    // the other bridge entry points (__dsx_startWatch, etc.).
    try {
      const err = detectErrorBanner(document);
      return JSON.stringify({ success: true, error: err });
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `detect-error threw: ${e.message}`,
      });
    }
  };
})();