// =====================================================================
// button-rules/01-default-capsule.js — "Copy Thoughts" button
// =====================================================================
//
// Extracts the last Thought Process from the current conversation,
// attaches it as `thought_process.md`, copies the same markdown to
// the clipboard, and types the follow-up text — all in memory.
//
// Equivalent to:
//     deeppeek conversation -o /tmp/conversation.md
//       && deeppeek prompt last-thought
// plus a clipboard write, but the intermediate file never touches
// disk.
//
// Loaded by content.js's injectButtonRules() in the order set by
// button-rules/_manifest.json.  Must run after injected-buttons.js
// (which defines window.__dsx__.buttons).
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/01-default-capsule] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }

  const B = window.__dsx__.buttons;
  const MARKER_ATTR = "data-dsx-injected";

  // ---- helpers --------------------------------------------------

  const closeUnclosedFences = (md) => {
    if (!md) return md;
    let inFence = false;
    for (const line of md.split("\n")) {
      const m = line.match(/^ {0,3}(`{3,})/);
      if (!m) continue;
      if (m[1].length === 3) inFence = !inFence;
    }
    return inFence ? md + "\n```" : md;
  };

  // Mirrors formatter.py's _format_thinking(): prepend the header and
  // prefix every line with `> `.  Trailing newline matches the CLI's
  // last-thought output exactly.
  const formatThought = (thinking) => {
    const closed = closeUnclosedFences(thinking);
    const quoted = closed.split("\n").map(ln => "> " + ln).join("\n");
    return "### Thought Process\n\n" + quoted + "\n";
  };

  const b64Of = (s) => btoa(unescape(encodeURIComponent(s)));

  // Same default as prompts/last_thought.py's DEFAULT_COMPOSER_TEXT.
  const FOLLOW_UP_TEXT =
    "you had already thought about this, but you stopped at the end";

  // Disables the button visually while an async action runs; restores
  // in a finally so a thrown error can't permanently stick the button.
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
    // 1. extract conversation (in-memory; same extraction paths the
    //    CLI uses: API, then IndexedDB, then fiber)
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

    // 2. find the last assistant message that has non-empty thinking
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

    // 3. format as markdown (matches prompt last-thought's output)
    const thoughtMd = formatThought(thinking);

    // 4. attach + type + clipboard in parallel.
    //    attach & type mirror the CLI's broadcast_many semantics;
    //    clipboard is best-effort — a failure there does not fail
    //    the whole pipeline.
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
        return {
          ok: false,
          reason: String((err && err.message) || err),
        };
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
        const natives = [...row.children].filter(c =>
          c.nodeType === 1 && !c.hasAttribute(MARKER_ATTR));
        if (natives.length < 2) continue;
        if (!natives.every(k => k.matches(
            ".ds-button--capsule.ds-button--xs[role='button']"))) continue;
        const base = natives[0].cloneNode(true);
        // icon-only pill → text-labelled pill:
        //   · drop `--icon` so the pill isn't locked to a square
        //   · add `--min-width` so it grows to fit text (same class
        //     the code block's Copy button uses)
        //   · clear inline width + gap set by the --icon sizing
        //   · remove the icon container
        //   · append the label
        base.classList.remove("ds-button--icon");
        base.classList.add("ds-button--min-width");
        base.style.removeProperty("--dsl-button-width");
        base.style.removeProperty("--dsl-button-gap");
        const iconWrap = base.querySelector(".ds-button__icon-container");
        if (iconWrap) iconWrap.remove();
        const textEl = document.createElement("span");
        textEl.textContent = "Copy Thoughts";
        textEl.style.cssText =
          "white-space: nowrap; font-size: 12px; line-height: 1; " +
          "font-weight: 500; padding: 0 8px;";
        base.appendChild(textEl);
        return base;
      }
      return null;
    },
    target: ".ds-flex",
    filter: (row) => {
      const natives = [...row.children].filter(c =>
        c.nodeType === 1 && !c.hasAttribute(MARKER_ATTR));
      if (natives.length < 2) return false;
      return natives.every(k => k.matches(
        ".ds-button--capsule.ds-button--xs[role='button']"));
    },
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
