// =====================================================================
// button-rules/05-copy-turn.js — "Copy Turn" button (v7, hijack-aware)
// =====================================================================
//
// TWO MODES
// ---------
//   1. NORMAL (default) — click copies the turn to the clipboard.
//   2. HIJACK — click POSTs the turn's markdown to the SSE server
//      started by `deeppeek hijack_turn`, which writes it to a .md
//      file.  Clipboard is untouched.
//
//   Mode is toggled from outside by the background script, which
//   receives `hijack-turn-install` / `hijack-turn-uninstall` SSE
//   events and forwards them via content.js as postMessage:
//       window.postMessage({ dsx_hijack_turn_set: { port: <n>|null } })
//   Manual toggle for debugging:
//       __dsx__.copyTurn.setHijack(8765)      // on
//       __dsx__.copyTurn.setHijack(null)      // off
//
// RESOLVER + FALLBACK
// -------------------
//   Unchanged from v6: nearest preceding .ds-markdown in document
//   order, content-matched against extracted messages, with the
//   incomplete-last-message fallback guarded so a mid-conversation
//   failure still errors loudly instead of copying the wrong turn.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after _capsule.js, _resume.js, and 01-default-capsule.js.
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

  // ---- hijack mode state ---------------------------------------
  //
  // `active` gates the copy path; `port` is where the background
  // POSTs the download ack.  Set from outside (see header comment).
  const hijackState = { active: false, port: null };

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || typeof d !== "object") return;
    if (!d.dsx_hijack_turn_set) return;
    const port = d.dsx_hijack_turn_set.port;
    hijackState.active = !!port;
    hijackState.port = port || null;
    console.log(
      `[DSX:copy-turn] hijack mode ` +
      `${hijackState.active ? "ON" : "OFF"}` +
      (port ? ` (port ${port})` : ""));
  });

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

  // Returns the OUTERMOST `.ds-markdown`-family element preceding
  // `el` in document order.  DeepSeek nests per-code-block wrappers
  // (and sometimes `.markdown-body` / `.prose`) INSIDE the message's
  // `.ds-markdown`, and a naive "last match" picks the final code
  // block — whose innerText is a single snippet, not the message.
  // Collapsing to outermost ensures we compare against the full
  // message body, which is what `prefixSim` needs to clear 0.4.
  const nearestPrecedingMarkdown = (el) => {
    const all = [...document.querySelectorAll(MARKDOWN_SEL)];

    // Elements that precede `el` and are not ancestors of it.
    const preceding = all.filter(md => {
      const pos = md.compareDocumentPosition(el);
      const follows = !!(pos & Node.DOCUMENT_POSITION_FOLLOWING);
      const containedBy = !!(pos & Node.DOCUMENT_POSITION_CONTAINED_BY);
      return follows && !containedBy;
    });

    // Keep only the outermost ones — drop anything contained by
    // another preceding element.
    const outermost = preceding.filter(md =>
      !preceding.some(other => other !== md && other.contains(md))
    );

    return outermost[outermost.length - 1] || null;
  };

  const hasMarkdownAfter = (pill) => {
    for (const md of document.querySelectorAll(MARKDOWN_SEL)) {
      if (pill.compareDocumentPosition(md)
          & Node.DOCUMENT_POSITION_FOLLOWING) return true;
    }
    return false;
  };

  const getPreviousMarkdown = (md) => {
    if (!md) return null;
    const all = [...document.querySelectorAll(MARKDOWN_SEL)];
    const idx = all.indexOf(md);
    return idx > 0 ? all[idx - 1] : null;
  };

  const resolveText = (text, msgs) => {
    if (!text) return -1;
    let best = { i: -1, score: 0 };
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const sc = m.content ? prefixSim(text, m.content) : 0;
      const st = m.thinking ? prefixSim(text, m.thinking) : 0;
      const score = Math.max(sc, st);
      if (score > best.score) best = { i, score };
    }
    return best.score > 0.4 ? best.i : -1;
  };

  // ---- position resolution -------------------------------------

  const _turnOf = (msgs, index) =>
    msgs.slice(0, index + 1).filter(m => m.role === "user").length;

  const _totalTurns = (msgs) =>
    msgs.filter(m => m.role === "user").length;

  // Resolves the message index the pill belongs to.  Tries the
  // outermost preceding `.ds-markdown`, then walks up its ancestors
  // (up to 6 levels, bailing as soon as an ancestor contains the
  // pill — we've walked past the message row).  The first node whose
  // innerText scores > 0.4 against some extracted message wins.  The
  // ancestor walk is a safety net for the case where DeepSeek wraps
  // a fragment (callout, embedded preview) in its own markdown-y
  // container outside the message's own `.ds-markdown`.
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

    const tried = new Set();
    let node = md;
    let chosen = null;
    let chosenAmbiguous = false;

    for (let i = 0; i < 6 && node; i++) {
      if (i > 0 && node.contains(pill)) break; // walked past the row
      const domText = (node.innerText || "").trim();
      if (domText && !tried.has(domText)) {
        tried.add(domText);
        const candidates = [];
        for (let j = 0; j < msgs.length; j++) {
          const m = msgs[j];
          const sc = m.content ? prefixSim(domText, m.content) : 0;
          const st = m.thinking ? prefixSim(domText, m.thinking) : 0;
          const score = Math.max(sc, st);
          if (score > 0.4) {
            candidates.push({
              i: j, role: m.role, score,
              field: sc >= st ? "content" : "thinking",
            });
          }
        }
        if (candidates.length) {
          candidates.sort((a, b) => b.score - a.score);
          const best = candidates[0];
          const tied = candidates.filter(c => best.score - c.score < 0.05);
          chosen = tied.length > 1 ? tied[tied.length - 1] : best;
          chosenAmbiguous = tied.length > 1;
          break;
        }
      }
      node = node.parentElement;
    }

    if (!chosen) {
      return {
        ok: false,
        reason: "no extracted message matched the preceding .ds-markdown",
        domTextHead: (md.innerText || "").trim().slice(0, 100),
        mdClasses: md.className || null,
      };
    }

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
      ambiguous: chosenAmbiguous,
      method: "nearest-preceding-markdown",
      mdClasses: md.className || null,
    };
  };

  const resolveOrFallback = (pill, msgs) => {
    const position = resolvePosition(pill, msgs);
    if (position.ok) return position;
    if (hasMarkdownAfter(pill)) return position;
    const md = nearestPrecedingMarkdown(pill);
    const prevMd = getPreviousMarkdown(md);
    if (prevMd) {
      const prevIdx = resolveText((prevMd.innerText || "").trim(), msgs);
      if (prevIdx >= 0 && msgs.length - prevIdx > 5) return position;
    }
    let lastUserIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") { lastUserIdx = i; break; }
    }
    if (lastUserIdx < 0) return position;
    const totalTurns = _totalTurns(msgs);
    return {
      ok: true,
      index: msgs.length - 1,
      humanIndex: msgs.length,
      turn: totalTurns,
      total: msgs.length,
      totalTurns,
      role: msgs[msgs.length - 1].role,
      score: 0,
      matchedField: null,
      ambiguous: false,
      method: "last-turn-fallback",
      mdClasses: md ? md.className : null,
      originalReason: position.reason,
    };
  };

  // ---- segment extraction -------------------------------------

  const findTurnRange = (msgs, atIndex) => {
    if (atIndex < 0 || atIndex >= msgs.length) return null;
    let start = atIndex;
    while (start > 0 && msgs[start].role !== "user") start--;
    if (msgs[start].role !== "user") {
      return { start: atIndex, end: atIndex, singleMessage: true };
    }
    let end = msgs.length - 1;
    for (let j = atIndex + 1; j < msgs.length; j++) {
      if (msgs[j].role === "user") { end = j - 1; break; }
    }
    return { start, end, singleMessage: false };
  };

  // ---- hijack mode: build filename, POST ack -------------------

  const b64Of = (s) => btoa(unescape(encodeURIComponent(s)));

  const slugify = (s) => (s || "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60);

  const buildHijackFilename = (data, position) => {
    const slug = slugify(data.title);
    const base = `turn_${position.turn}_of_${position.totalTurns}`;
    return slug ? `${slug}_${base}.md` : `${base}.md`;
  };

  const postTurnToHijack = async (data, position, md) => {
    const port = hijackState.port;
    if (!port) throw new Error("hijack mode active but no port configured");

    const filename = buildHijackFilename(data, position);
    const base64 = b64Of(md);

    // 2s timeout: if the CLI is Ctrl+C'd mid-click, we must not
    // hang.  A hung fetch eats the user activation window, and the
    // subsequent clipboard.writeText then fails silently (Firefox
    // is more permissive than Chrome here, but not unconditionally).
    // A short timeout means we fall through to clipboard while the
    // click is still "fresh".
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);

    let resp;
    try {
      resp = await fetch(`http://127.0.0.1:${port}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          __ack: "download",
          filename,
          base64,
          source: "hijack-turn",
          position: {
            turn: position.turn,
            totalTurns: position.totalTurns,
            index: position.humanIndex,
            total: position.total,
            role: position.role,
            method: position.method,
          },
        }),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      // Self-heal: the CLI is gone (or the fetch timed out).  Clear
      // hijack state so the next click skips hijack entirely and
      // goes straight to clipboard.  Without this, a lost CLEAR
      // message (Ctrl+C raced sendMessage, SSE socket closed early,
      // background lost the tab query, etc.) would leave every
      // subsequent click stuck in a dead hijack path.
      hijackState.active = false;
      hijackState.port = null;
      throw new Error(
        `fetch 127.0.0.1:${port} failed: ${err && err.message || err} ` +
        `(hijack state cleared — next click goes to clipboard)`);
    }
    clearTimeout(timer);

    if (!resp.ok) {
      hijackState.active = false;
      hijackState.port = null;
      throw new Error(
        `POST /export failed: HTTP ${resp.status} ` +
        `(hijack state cleared — next click goes to clipboard)`);
    }
    return { filename, bytes: md.length };
  };

  // ---- the pipeline --------------------------------------------

  const runCopyPipeline = async (pill, opts = {}) => {
    if (typeof window.__dsx_extract !== "function") {
      throw new Error(
        "__dsx_extract unavailable — is injected-extract.js loaded?");
    }
    const extractJson = await window.__dsx_extract(
      JSON.stringify({ include_forked: false }));
    const data = JSON.parse(extractJson);
    const msgs = data.messages || [];
    if (!msgs.length) throw new Error("conversation is empty");

    const position = resolveOrFallback(pill, msgs);
    if (!position.ok) {
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

    const useHijack = opts.hijack === true
      || (opts.hijack === undefined && hijackState.active);

    if (useHijack) {
      try {
        const hres = await postTurnToHijack(data, position, md);
        return {
          ok: true,
          chars: md.length,
          segmentMessages: segment.length,
          position,
          range,
          totalMessages: msgs.length,
          mode: "hijack",
          filename: hres.filename,
        };
      } catch (err) {
        // Don't lose the turn because the CLI died — fall back to
        // clipboard with a loud warning, matching the "never copy
        // the wrong thing silently" rule.
        console.warn(
          `[DSX:copy-turn] hijack POST failed: ${err && err.message || err} ` +
          `— falling back to clipboard`);
        // falls through to clipboard write below
      }
    }

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
      mode: "clipboard",
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
        `response containing this message) to clipboard, or write to ` +
        `a file when 'deeppeek hijack_turn' is running`);
    },
    onClick: async (e, el) => {
      const prevPe = el.style.pointerEvents;
      const prevOp = el.style.opacity;
      el.style.pointerEvents = "none";
      el.style.opacity = "0.5";
      try {
        const result = await runCopyPipeline(el);
        const p = result.position;
        const fallback = p.method === "last-turn-fallback";
        const note = fallback
          ? ` [fallback: ${p.originalReason}]`
          : (p.ambiguous ? ", ambiguous" : "");

        if (result.mode === "hijack") {
          console.log(
            `%c[DSX:copy-turn] OK — wrote turn ${p.turn}/${p.totalTurns} ` +
            `(${result.segmentMessages} message(s), ${result.chars} chars) ` +
            `to "${result.filename}" via hijack — resolved from message ` +
            `${p.humanIndex}/${p.total} (${p.role}, ` +
            `match=${p.score.toFixed(2)}, ${p.method}${note})`,
            "color:magenta;font-weight:bold");
        } else {
          console.log(
            `%c[DSX:copy-turn] OK — copied turn ${p.turn}/${p.totalTurns} ` +
            `(${result.segmentMessages} message(s), ${result.chars} chars) ` +
            `to clipboard — resolved from message ${p.humanIndex}/${p.total} ` +
            `(${p.role}, match=${p.score.toFixed(2)}, ${p.method}${note})`,
            fallback
              ? "color:yellow;font-weight:bold"
              : "color:cyan;font-weight:bold");
        }
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

  window.__dsx__.copyTurn.hijack = hijackState;
  window.__dsx__.copyTurn.setHijack = (port) => {
    hijackState.active = !!port;
    hijackState.port = port || null;
    console.log(
      `[DSX:copy-turn] hijack mode manually set to ` +
      `${hijackState.active ? "ON" : "OFF"}` +
      (port ? ` (port ${port})` : ""));
  };
  window.__dsx__.copyTurn.resolvePosition = resolvePosition;
  window.__dsx__.copyTurn.resolveOrFallback = resolveOrFallback;
  window.__dsx__.copyTurn.nearestPrecedingMarkdown = nearestPrecedingMarkdown;
  window.__dsx__.copyTurn.findTurnRange = findTurnRange;

  window.__dsx__.copyTurn.preview = async (pill, opts = {}) => {
    if (typeof window.__dsx_extract !== "function") {
      return { error: "__dsx_extract unavailable" };
    }
    const data = JSON.parse(await window.__dsx_extract(
      JSON.stringify({ include_forked: false })));
    const msgs = data.messages || [];
    const position = resolveOrFallback(pill, msgs);
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
      would_hijack: opts.hijack === true
        || (opts.hijack === undefined && hijackState.active),
      hijack_filename: buildHijackFilename(data, position),
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
      hijack: { active: hijackState.active, port: hijackState.port },
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
