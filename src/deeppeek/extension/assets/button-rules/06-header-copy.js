// =====================================================================
// button-rules/06-header-copy.js — "Copy Chat" button (header)
// =====================================================================
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Extracts the full conversation via __dsx_extract, formats it as
//   markdown (identical to `deeppeek conversation` output), copies it
//   to the clipboard.  Same pipeline 05-copy-turn.js had before
//   turn-scoping was added.
//
// INJECTION POINT
// ---------------
//   Where:   END of the nearest `.the-header` ancestor of the chat-
//            title element.
//   Which:   `.the-header` elements that contain the title selector.
//            DeepSeek nests two `.the-header` divs; the inner one
//            (5 children) is the row we append to, and it's the
//            nearest ancestor, so the walk-up lands on it first.
//   Verify:  __dsx.buttons.debug(true) → teal label "header-copy"
//            above the clone.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js (uses makeTextPillFromNative).
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/06-header-copy] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.capsule) {
    console.error(
      "[DSX:button-rules/06-header-copy] shared capsule helpers not " +
      "loaded — is _capsule.js listed before 06- in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const C = window.__dsx__.capsule;

  // Same selector as injected.js's config.titleSelector.  Duplicated
  // inline so the rule is self-contained (no ordering dependency on
  // injected.js populating config first — it always does, but explicit
  // is safer).
  const TITLE_SEL = ".afa34042.e37a04e4.e0a1edb7";

  // ---- markdown formatting (mirrors _resume.js::formatConversation) ---

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

  const formatConversation = (data) => {
    const title = data.title;
    const url = data.url;
    const messages = data.messages || [];
    let header = "";
    if (title && url) {
      const escaped = title.replace(/\\/g, "\\\\").replace(/\]/g, "\\]");
      header = `# [${escaped}](${url})\n\n`;
    } else if (title) {
      header = `# ${title}\n\n`;
    }
    const parts = [];
    for (const m of messages) {
      if (m.role === "user") {
        parts.push(`## User\n\n${m.content}`);
        continue;
      }
      const sub = [];
      if (m.thinking && m.thinking.trim()) {
        const closed = closeFences(m.thinking);
        const quoted = closed.split("\n").map(ln => "> " + ln).join("\n");
        sub.push(`### Thought Process\n\n${quoted}`);
      }
      if (m.content && m.content.trim()) {
        sub.push(closeFences(m.content));
      }
      if (sub.length) parts.push(`## Assistant\n\n${sub.join("\n\n")}`);
    }
    return header + parts.join("\n\n---\n\n");
  };

  // ---- the pipeline --------------------------------------------

  const runCopyPipeline = async () => {
    if (typeof window.__dsx_extract !== "function") {
      throw new Error(
        "__dsx_extract unavailable — is injected-extract.js loaded?");
    }
    const extractJson = await window.__dsx_extract(
      JSON.stringify({ include_forked: false }));
    const data = JSON.parse(extractJson);
    if (!data.messages || data.messages.length === 0) {
      throw new Error("conversation is empty");
    }
    const md = formatConversation(data);
    if (!navigator.clipboard
        || typeof navigator.clipboard.writeText !== "function") {
      throw new Error("navigator.clipboard unavailable");
    }
    await navigator.clipboard.writeText(md);
    return { chars: md.length, messages: data.messages.length };
  };

  // ---- rule registration ---------------------------------------

  B.rule({
    name: "header-copy",
    position: "end",
    target: () => {
      const title = document.querySelector(TITLE_SEL);
      if (!title) return [];
      // Walk up to the nearest .the-header ancestor of the title.
      // DeepSeek nests two .the-header elements (outer wrapper +
      // inner row); the inner one is the row we want, and it's the
      // nearest ancestor, so the first match wins.
      let el = title.parentElement;
      for (let i = 0; i < 8 && el; i++) {
        if (el.classList && el.classList.contains("the-header")) {
          return [el];
        }
        el = el.parentElement;
      }
      // Fallback: append to the title's own parent.
      return title.parentElement ? [title.parentElement] : [];
    },
    filter: (row) => !!row.querySelector(TITLE_SEL),
    template: () => {
      // No capsule buttons live in the header, so clone a native
      // capsule from anywhere on the page (same pattern as
      // 04-length-limit-reached.js) and relabel it.
      const natives = document.querySelectorAll(C.CAPSULE_SEL);
      for (const nat of natives) {
        const pill = C.makeTextPillFromNative(nat, "Copy Chat");
        if (pill) return pill;
      }
      return null;
    },
    decorate: (clone) => {
      clone.setAttribute(
        "title",
        `[DSX] header-copy — copy the whole conversation to clipboard ` +
        `(markdown, same format as \`deeppeek conversation\`)`);
    },
    onClick: async (e, el) => {
      const prevPe = el.style.pointerEvents;
      const prevOp = el.style.opacity;
      el.style.pointerEvents = "none";
      el.style.opacity = "0.5";
      try {
        const result = await runCopyPipeline();
        console.log(
          `%c[DSX:header-copy] OK — copied ${result.chars} chars ` +
          `(${result.messages} messages) to clipboard`,
          "color:cyan;font-weight:bold");
      } catch (err) {
        console.error(
          `[DSX:header-copy] failed: ${err && err.message || err}`);
      } finally {
        el.style.pointerEvents = prevPe;
        el.style.opacity = prevOp;
      }
    },
  });
})();
