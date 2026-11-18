// =====================================================================
// button-rules/05-copy-turn.js — "Copy Turn" button (v5)
// =====================================================================
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Extracts the full conversation via __dsx_extract, resolves which
//   extracted message the clicked pill belongs to (nearest preceding
//   .ds-markdown, content-matched), then copies ONLY the turn that
//   contains that message to the clipboard — the user prompt that
//   started the turn plus the assistant response(s) up to the next
//   user prompt.
//
//   Equivalent CLI form, for the last turn:
//     deeppeek conversation --last 1
//
// WHY "NEAREST PRECEDING .ds-markdown"
// ------------------------------------
//   On the current DeepSeek build, the action row (which holds the
//   pill) is a SIBLING of the message body, not a descendant.  The
//   pill's ancestor chain never crosses a .ds-message, .ds-markdown,
//   or [data-role] — verified via __dsx__.copyTurn.probe().  So
//   `closest(".ds-message")` is always null.
//
//   The message body IS rendered just before the action row in
//   document order; the resolver grabs the nearest preceding
//   .ds-markdown and content-matches it against extracted messages
//   (checking both `content` and `thinking`).
//
// WHAT'S IN A TURN
// ----------------
//     start = nearest user message at or before the resolved index
//     end   = one before the next user message, or the last message
//   Multi-part assistant responses (legal but rare) are included
//   whole.  A user message sent but not yet answered becomes its
//   own single-message turn.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js and 01-default-capsule.js.
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/05-copy-turn] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }
  if (!window.__dsx__.capsule) {
    console.error(
      "[DSX:button-rules/05-copy-turn] shared capsule helpers not " +
      "loaded — is _capsule.js listed before 05- in _manifest.json?");
    return;
  }

  const B = window.__dsx__.buttons;
  const C = window.__dsx__.capsule;

  const MARKDOWN_SEL = ".ds-markdown, .markdown-body, .prose";

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

  // Formats ANY slice of messages with the same structure as the full
  // conversation formatter.  Header is included for provenance; the
  // block structure (## User / ## Assistant / ### Thought Process)
  // matches the CLI byte-for-byte — so a single-turn copy is exactly
  // the corresponding slice of `deeppeek conversation -o`.
  const formatMessages = (msgs, title, url) => {
    let header = "";
    if (title && url) {
      const escaped = title.replace(/\\/g, "\\\\").replace(/\]/g, "\\]");
      header = `# [${escaped}](${url})\n\n`;
    } else if (title) {
      header = `# ${title}\n\n`;
    }
    const parts = [];
    for (const m of msgs) {
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

  // ---- content comparison --------------------------------------

  const normalize = (s) => (s || "")
    .replace(/[`*_>#~\[\]()!-]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

  const prefixSim = (a, b, n = 120) => {
    const x = normalize(a).slice(0, n);
    const y = normalize(b).slice(0, n);
    if (!x || !y) return 0;
    let i = 0;
    const len = Math.min(x.length, y.length);
    while (i < len && x[i] === y[i]) i++;
    return i / Math.max(x.length, y.length);
  };

  // ---- DOM helpers ---------------------------------------------

  const nearestPrecedingMarkdown = (el) => {
    const all = document.querySelectorAll(MARKDOWN_SEL);
    let best = null;
    for (const md of all) {
      const pos = md.compareDocumentPosition(el);
      const follows = !!(pos & Node.DOCUMENT_POSITION_FOLLOWING);
      const containedBy = !!(pos & Node.DOCUMENT_POSITION_CONTAINED_BY);
      if (follows && !containedBy) best = md;
    }
    return best;
  };

  // ---- position resolution -------------------------------------

  const _turnOf = (msgs, index) =>
    msgs.slice(0, index + 1).filter(m => m.role === "user").length;

  const _totalTurns = (msgs) =>
    msgs.filter(m => m.role === "user").length;

  const resolvePosition = (pill, msgs) => {
    if (!msgs || !msgs.length) {
      return { ok: false, reason: "no extracted messages" };
    }

    const md = nearestPrecedingMarkdown(pill);
    if (!md) {
      return {
        ok: false,
        reason: "no .ds-markdown precedes the pill in document order",
      };
    }
    const domText = (md.innerText || "").trim();
    if (!domText) {
      return {
        ok: false,
        reason: "nearest preceding .ds-markdown is empty",
        mdClasses: md.className || null,
      };
    }

    const candidates = [];
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const sc = m.content ? prefixSim(domText, m.content) : 0;
      const st = m.thinking ? prefixSim(domText, m.thinking) : 0;
      const score = Math.max(sc, st);
      if (score > 0.4) {
        candidates.push({
          i,
          role: m.role,
          score,
          field: sc >= st ? "content" : "thinking",
        });
      }
    }

    if (!candidates.length) {
      return {
        ok: false,
        reason: "no extracted message matched the preceding .ds-markdown",
        domTextHead: domText.slice(0, 100),
        mdClasses: md.className || null,
      };
    }

    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0];
    const tied = candidates.filter(c => best.score - c.score < 0.05);
    // Tie-break: prefer the most recent extracted message.  When
    // content is duplicated the pill the user clicked almost
    // certainly belongs to the newer message.
    const chosen = tied.length > 1 ? tied[tied.length - 1] : best;

    return {
      ok: true,
      index: chosen.i,
      humanIndex: chosen.i + 1,
      turn: _turnOf(msgs, chosen.i),
      total: msgs.length,
      totalTurns: _totalTurns(msgs),
      role: chosen.role,
      score: chosen.score,
      matchedField: chosen.field,
      ambiguous: tied.length > 1,
      method: "nearest-preceding-markdown",
      mdClasses: md.className || null,
    };
  };

  // ---- segment extraction -------------------------------------

  // Turn containing `atIndex`:
  //   start = nearest user message at or before atIndex
  //   end   = one before the next user message, or last message
  //
  // To copy ONLY the clicked message instead of the whole turn,
  // replace both return statements with
  //   return { start: atIndex, end: atIndex, singleMessage: true };
  const findTurnRange = (msgs, atIndex) => {
    if (atIndex < 0 || atIndex >= msgs.length) return null;

    let start = atIndex;
    while (start > 0 && msgs[start].role !== "user") start--;

    if (msgs[start].role !== "user") {
      // No user message at or before this index.  Copy just the
      // one message so nothing is silently lost.
      return { start: atIndex, end: atIndex, singleMessage: true };
    }

    let end = msgs.length - 1;
    for (let j = atIndex + 1; j < msgs.length; j++) {
      if (msgs[j].role === "user") { end = j - 1; break; }
    }

    return { start, end, singleMessage: false };
  };

  // ---- the pipeline --------------------------------------------

  const runCopyPipeline = async (pill) => {
    if (typeof window.__dsx_extract !== "function") {
      throw new Error(
        "__dsx_extract unavailable — is injected-extract.js loaded?");
    }
    const extractJson = await window.__dsx_extract(
      JSON.stringify({ include_forked: false }));
    const data = JSON.parse(extractJson);
    const msgs = data.messages || [];
    if (!msgs.length) throw new Error("conversation is empty");

    const position = resolvePosition(pill, msgs);
    if (!position.ok) {
      // v1 silently fell back to "last assistant" on failure — that
      // was the original bug.  v5 fails loudly instead.
      const err = new Error(
        `could not resolve position: ${position.reason}`);
      err.positionDiagnostic = position;
      throw err;
    }

    const range = findTurnRange(msgs, position.index);
    if (!range) {
      throw new Error(
        `could not determine turn range for message ${position.humanIndex}`);
    }

    const segment = msgs.slice(range.start, range.end + 1);
    const md = formatMessages(segment, data.title, data.url);

    if (!navigator.clipboard
        || typeof navigator.clipboard.writeText !== "function") {
      throw new Error("navigator.clipboard unavailable");
    }
    await navigator.clipboard.writeText(md);

    return {
      ok: true,
      chars: md.length,
      segmentMessages: segment.length,
      position,
      range,
      totalMessages: msgs.length,
    };
  };

  // ---- rule registration ---------------------------------------

  const RATE_LIMIT_NEEDLE = "Messages too frequent. Try again later.";

  const isRateLimitRow = (row) => {
    const rowWrapper = row.parentElement;
    const notice = rowWrapper && rowWrapper.parentElement;
    if (!notice) return false;
    return [...notice.children].some(c =>
      c.tagName === "SPAN" && c.textContent.trim() === RATE_LIMIT_NEEDLE);
  };

  B.rule({
    name: "copy-turn",
    position: "end",
    template: () => {
      for (const row of document.querySelectorAll(".ds-flex")) {
        const pill = C.makeTextPill(row, "Copy Turn");
        if (pill) return pill;
      }
      return null;
    },
    target: ".ds-flex",
    filter: (row) => C.isCapsuleRow(row) && !isRateLimitRow(row),
    decorate: (clone) => {
      clone.setAttribute(
        "title",
        `[DSX] copy-turn — copy THIS turn (user prompt + assistant ` +
        `response containing this message) to clipboard`);
    },
    onClick: async (e, el) => {
      const prevPe = el.style.pointerEvents;
      const prevOp = el.style.opacity;
      el.style.pointerEvents = "none";
      el.style.opacity = "0.5";
      try {
        const result = await runCopyPipeline(el);
        const p = result.position;
        console.log(
          `%c[DSX:copy-turn] OK — copied turn ${p.turn}/${p.totalTurns} ` +
          `(${result.segmentMessages} message(s), ${result.chars} chars) ` +
          `to clipboard — resolved from message ${p.humanIndex}/${p.total} ` +
          `(${p.role}, match=${p.score.toFixed(2)}, ${p.method}` +
          `${p.ambiguous ? ", ambiguous" : ""})`,
          "color:cyan;font-weight:bold");
      } catch (err) {
        console.error(
          `[DSX:copy-turn] failed: ${err && err.message || err}`);
        if (err && err.positionDiagnostic) {
          console.warn(
            "[DSX:copy-turn] position diagnostic:", err.positionDiagnostic);
        }
      } finally {
        el.style.pointerEvents = prevPe;
        el.style.opacity = prevOp;
      }
    },
  });

  // ---- console API ---------------------------------------------

  if (!window.__dsx__.copyTurn) window.__dsx__.copyTurn = {};

  window.__dsx__.copyTurn.resolvePosition = resolvePosition;
  window.__dsx__.copyTurn.nearestPrecedingMarkdown = nearestPrecedingMarkdown;
  window.__dsx__.copyTurn.findTurnRange = findTurnRange;

  // Preview what WOULD be copied, without touching the clipboard.
  // Right-click any pill → Inspect → in console:
  //   await __dsx__.copyTurn.preview($0)
  window.__dsx__.copyTurn.preview = async (pill) => {
    if (typeof window.__dsx_extract !== "function") {
      return { error: "__dsx_extract unavailable" };
    }
    const data = JSON.parse(await window.__dsx_extract(
      JSON.stringify({ include_forked: false })));
    const msgs = data.messages || [];
    const position = resolvePosition(pill, msgs);
    if (!position.ok) return { position, preview: null };
    const range = findTurnRange(msgs, position.index);
    if (!range) return { position, range, preview: null };
    const segment = msgs.slice(range.start, range.end + 1);
    const md = formatMessages(segment, data.title, data.url);
    return {
      position,
      range,
      messageCount: segment.length,
      chars: md.length,
      preview: md.slice(0, 400) + (md.length > 400 ? "\n[…]" : ""),
    };
  };

  window.__dsx__.copyTurn.probe = async () => {
    if (typeof window.__dsx_extract !== "function") {
      return { error: "__dsx_extract unavailable" };
    }
    const data = JSON.parse(await window.__dsx_extract(
      JSON.stringify({ include_forked: false })));
    const msgs = data.messages || [];
    const marks = [...document.querySelectorAll(MARKDOWN_SEL)];
    return {
      extractedCount: msgs.length,
      renderedMarkdownCount: marks.length,
      virtualized: marks.length < msgs.length,
      marks: marks.map((md, ordinal) => {
        const text = (md.innerText || "").trim();
        const sc = { content: 0, thinking: 0, idx: -1 };
        for (let i = 0; i < msgs.length; i++) {
          const m = msgs[i];
          const c = m.content ? prefixSim(text, m.content) : 0;
          const t = m.thinking ? prefixSim(text, m.thinking) : 0;
          if (c > sc.content) { sc.content = c; sc.idx = i; }
          if (t > sc.thinking) { sc.thinking = t; }
        }
        return {
          domOrdinal: ordinal,
          classes: md.className || null,
          textHead: text.slice(0, 60).replace(/\s+/g, " "),
          bestExtractedIndex: sc.idx,
          bestContentScore: +sc.content.toFixed(3),
          bestThinkingScore: +sc.thinking.toFixed(3),
        };
      }),
    };
  };
})();
