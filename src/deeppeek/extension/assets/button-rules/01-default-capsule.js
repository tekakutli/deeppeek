// =====================================================================
// button-rules/01-default-capsule.js — "Copy Thoughts" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   END of the icon capsule row.
//   Which:   any `.ds-flex` whose direct non-injected children are
//            ALL `.ds-button--capsule.ds-button--xs[role="button"]`.
//   Verify:  __dsx.buttons.debug(true) → red outline on the row,
//            "default-capsule" label above the clone.
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Extracts the last Thought Process, attaches it as
//   thought_process.md, copies the markdown to the clipboard, and
//   types the follow-up text — all in memory.
//
// USES SHARED HELPER
// ------------------
//   `__dsx__.capsule.makeTextPill(row, "Copy Thoughts")` — same
//   capsule-to-text-pill transform 03-rate-limit-retry uses.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js and _resume.js.
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/01-default-capsule] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.capsule) {
    console.error(
      "[DSX:button-rules/01-default-capsule] shared capsule helpers " +
      "not loaded — is _capsule.js listed before 01- in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const MARKER_ATTR = "data-dsx-injected";
  const C = window.__dsx__.capsule;

  // ---- helpers --------------------------------------------------

  const b64Of = (s) => btoa(unescape(encodeURIComponent(s)));

  const closeFences = (md) => {
    if (!md) return md;
    let inFence = false;
    for (const line of md.split("\n")) {
      const m = line.match(/^ {0,3}(`{3,})/);
      if (!m) continue;
      if (m[1].length === 3) inFence = !inFence;
    }
    return inFence ? md + "\n```" : md;
  };

  const formatThought = (thinking) => {
    const closed = closeFences(thinking);
    const quoted = closed.split("\n").map(ln => "> " + ln).join("\n");
    return "### Thought Process\n\n" + quoted + "\n";
  };

  const FOLLOW_UP_TEXT =
    "you had already thought about this, but you stopped at the end";

  const withBusy = async (btn, fn) => {
    const prevPe = btn.style.pointerEvents;
    const prevOp = btn.style.opacity;
    btn.style.pointerEvents = "none";
    btn.style.opacity = "0.5";
    try { return await fn(); }
    finally {
      btn.style.pointerEvents = prevPe;
      btn.style.opacity = prevOp;
    }
  };

  // ---- the pipeline ---------------------------------------------

  const runLastThoughtPipeline = async () => {
    if (typeof window.__dsx_extract !== "function") {
      throw new Error("__dsx_extract unavailable — is injected-extract.js loaded?");
    }
    const extractJson = await window.__dsx_extract(
      JSON.stringify({ include_forked: false }));
    const data = JSON.parse(extractJson);
    const messages = data.messages || [];
    if (messages.length === 0) {
      throw new Error("conversation is empty");
    }

    let thinking = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "assistant"
          && typeof m.thinking === "string"
          && m.thinking.trim()) {
        thinking = m.thinking;
        break;
      }
    }
    if (!thinking) {
      throw new Error(
        "no Thought Process found — no assistant message in this " +
        "conversation has a non-empty thinking field");
    }

    const thoughtMd = formatThought(thinking);

    if (typeof window.__dsx_attach_json !== "function") {
      throw new Error("__dsx_attach_json unavailable — is injected-attach.js loaded?");
    }
    if (typeof window.__dsx_type_json !== "function") {
      throw new Error("__dsx_type_json unavailable — is injected-composer.js loaded?");
    }

    const attachArgs = {
      files: [{
        name: "thought_process.md",
        mime: "text/markdown",
        base64: b64Of(thoughtMd),
      }],
      opts: { delay_ms: 0 },
    };
    const typeArgs = { text: FOLLOW_UP_TEXT, opts: {} };

    const clipboardPromise = (async () => {
      try {
        if (!navigator.clipboard
            || typeof navigator.clipboard.writeText !== "function") {
          return { ok: false, reason: "navigator.clipboard unavailable" };
        }
        await navigator.clipboard.writeText(thoughtMd);
        return { ok: true };
      } catch (err) {
        return { ok: false, reason: String((err && err.message) || err) };
      }
    })();

    const [clipboardRes, attachJson, typeJson] = await Promise.all([
      clipboardPromise,
      window.__dsx_attach_json(JSON.stringify(attachArgs)),
      window.__dsx_type_json(JSON.stringify(typeArgs)),
    ]);

    const attachRes = JSON.parse(attachJson);
    const typeRes = JSON.parse(typeJson);

    const failures = [];
    if (!attachRes.success) {
      failures.push(`attach: ${attachRes.reason || "(no reason)"}`);
    }
    if (!typeRes.success) {
      failures.push(`type: ${typeRes.reason || "(no reason)"}`);
    }

    return {
      ok: failures.length === 0,
      failures,
      clipboard: clipboardRes,
      stats: {
        messages: messages.length,
        thoughtChars: thinking.length,
        fileChars: thoughtMd.length,
        followUpChars: FOLLOW_UP_TEXT.length,
      },
    };
  };

  // ============================================================
  // Rule: default-capsule — "Copy Thoughts"
  // ============================================================
  B.rule({
    name: "default-capsule",
    position: "end",
    template: () => {
      for (const row of document.querySelectorAll(".ds-flex")) {
        const pill = C.makeTextPill(row, "Copy Thoughts");
        if (pill) return pill;
      }
      return null;
    },
    target: ".ds-flex",
    filter: (row) => C.isCapsuleRow(row),
    onClick: async (e, el) => {
      try {
        const result = await withBusy(el, runLastThoughtPipeline);
        if (result.ok) {
          const clip = result.clipboard;
          const clipMsg = clip.ok
            ? "copied to clipboard"
            : `clipboard not updated (${clip.reason})`;
          console.log(
            `%c[DSX:default-capsule] OK — attached thought_process.md ` +
            `(${result.stats.fileChars} chars from ${result.stats.thoughtChars} ` +
            `thinking chars) + typed follow-up (${result.stats.followUpChars} chars) ` +
            `— ${clipMsg}`,
            "color:magenta;font-weight:bold");
        } else {
          console.warn(
            `[DSX:default-capsule] partial failure:\n  ` +
            result.failures.join("\n  "));
          if (!result.clipboard.ok) {
            console.warn(
              `[DSX:default-capsule] clipboard: ${result.clipboard.reason}`);
          }
        }
      } catch (err) {
        console.error(
          `[DSX:default-capsule] threw: ${err && err.message || err}`);
      }
    },
  });
})();
