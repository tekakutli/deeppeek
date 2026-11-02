// =====================================================================
// button-rules/02-continue-btn.js — "Resume" button
// =====================================================================
//
// =====================================================================
// INJECTION POINT
// =====================================================================
//
//   Where:   START of the wrapper <div> around the "Continue" button.
//
//   Which:   any DIV whose direct children include a
//            `.ds-button[role="button"]` with textContent "Continue".
//            This row is DeepSeek's truncation affordance — it only
//            appears when the last assistant reply hit the length
//            limit and the user can ask DeepSeek to keep going.
//
//   Pattern: the row is found via `target` (function) which locates
//            the parent of each "Continue" button, and `filter`
//            asserting "some child has text 'Continue'".  The
//            template is the Continue button itself, with its
//            `.ds-button__content` span rewritten to "Resume" so
//            the two buttons are visually distinct.
//
//   Verify:  __dsx.buttons.debug(true) → teal outline on the row,
//            "continue-btn" label above the clone, legend entry
//            with current count.
//
// =====================================================================
// WHAT IT DOES ON CLICK
// =====================================================================
//
//   Equivalent to running `deeppeek prompt resume` from a shell,
//   minus the disk round-trip:
//
//   Phase 1 (in the CURRENT tab, on click):
//     1. Extract the conversation via `__dsx_extract` — same
//        extraction paths (API → IDB → fiber) as the CLI.
//     2. Format it as markdown — mirrors `formatter.py`'s
//        `format_conversation`, including the unclosed-fence close.
//     3. Extract the "last relevant string" — tail of the last
//        assistant content, or of its thinking, or of the last user
//        prompt (matches `prompts/resume.py`).
//     4. Fetch the first user prompt's attachments via
//        `__dsx_extract_attachments_json({mode:"first",
//        return_base64:true})` — same underlying code path as
//        `deeppeek prompt attachments --first --fetch-to`.
//     5. Bundle {conversation.md, seed_files, resume_text} into a
//        payload and write it to localStorage.
//     6. Open a blank tab SYNCHRONOUSLY (before any await, so the
//        user gesture is still fresh and Firefox won't block the
//        popup), then navigate it to chat.deepseek.com once the
//        async work finishes.
//
//   Phase 2 (in the NEW tab, on page load):
//     7. The resume-worker IIFE at the top of this file checks
//        localStorage for `dsx_resume_pending`.  If found and
//        younger than 30s, it claims it (atomic single-writer),
//        deletes it, waits for the composer to appear, attaches
//        everything, and types the resume text.
//
//   Failure modes are all logged to the console and do NOT leave
//   stray state behind:
//     · attach/type fail → console error, button restores
//     · localStorage quota exceeded → clear error
//     · popup blocked → fall back to navigating current tab
//     · phase 2 times out waiting for composer → clear warning
//
// =====================================================================
// LOADED BY
// =====================================================================
//
//   content.js's injectButtonRules() in the order set by
//   button-rules/_manifest.json.  Runs after injected-buttons.js
//   (which defines window.__dsx__.buttons).
(function () {
  "use strict";
  if (!window.__dsx__ || !window.__dsx__.buttons) {
    console.error(
      "[DSX:button-rules/02-continue-btn] framework not loaded — " +
      "is injected-buttons.js listed before button-rules in content.js?");
    return;
  }

  const B = window.__dsx__.buttons;
  const MARKER_ATTR = "data-dsx-injected";

  // localStorage keys used for the two-phase handoff.
  const PENDING_KEY = "dsx_resume_pending";
  const CLAIM_PREFIX = "dsx_resume_claim_";
  const FRESH_MS = 30000;
  const CHAT_URL = "https://chat.deepseek.com/";

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

  // Mirror of capture/formatter.py::format_conversation().  Produces
  // the same markdown `deeppeek conversation -o` would have written,
  // including the `# [title](url)` header, `## User` / `## Assistant`
  // headings, `### Thought Process` for thinking, and the
  // `\n\n---\n\n` separator between messages.
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

  // Mirror of prompts/resume.py::_extract_last_relevant_string().
  // Order: tail of the last assistant's content → tail of its
  // thinking → tail of the last user prompt.  Flattened to a single
  // line, trimmed to the last `maxWords` words, capped at
  // `maxChars` total, `<UPPER CASE>` placeholders stripped.
  const extractGuidance = (messages, maxWords = 40, maxChars = 500) => {
    const trim = (s) => {
      const cleaned = s.replace(/<[A-Z][A-Z0-9 _-]{2,}>/g, "").trim();
      const words = cleaned.split(/\s+/).filter(Boolean);
      let out = words.slice(-maxWords).join(" ");
      if (out.length > maxChars) out = out.slice(-maxChars).trim();
      return out;
    };
    const tailPara = (text) => {
      if (!text) return "";
      const paras = text.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
      return paras.length ? paras[paras.length - 1] : "";
    };

    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "assistant") continue;
      const content = tailPara(m.content || "");
      if (content) return trim(content);
      const thinking = tailPara(m.thinking || "");
      if (thinking) return trim(thinking);
    }
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "user") continue;
      const content = tailPara(m.content || "");
      if (content) return trim(content);
    }
    return "";
  };

  const buildResumeText = (guidance) =>
    `you were thinking about it, but was interrupted, keep going ` +
    `where you left it: "${guidance}"; ` +
    `dont give me any code yet, simply design it`;

  // ---- Phase 1: gather payload from the current tab -------------

  const prepareResumePayload = async () => {
    if (typeof window.__dsx_extract !== "function") {
      throw new Error("__dsx_extract unavailable — is injected-extract.js loaded?");
    }
    const extractJson = await window.__dsx_extract(
      JSON.stringify({ include_forked: false }));
    const data = JSON.parse(extractJson);
    if (!data.messages || data.messages.length === 0) {
      throw new Error("conversation is empty");
    }

    const conversationMd = formatConversation(data);
    const guidance = extractGuidance(data.messages);
    const resumeText = buildResumeText(guidance);

    // First-prompt attachments, as base64 — same path the CLI uses
    // for `prompt attachments --first --fetch-to <dir>`.
    const seedFiles = [];
    if (typeof window.__dsx_extract_attachments_json === "function") {
      try {
        const attJson = await window.__dsx_extract_attachments_json(
          JSON.stringify({ mode: "first", return_base64: true }));
        const attRes = JSON.parse(attJson);
        if (attRes && Array.isArray(attRes.files)) {
          for (const f of attRes.files) {
            if (f.base64 && f.file_name) {
              seedFiles.push({
                name: f.file_name,
                mime: f.mime || "application/octet-stream",
                base64: f.base64,
              });
            }
          }
        }
      } catch (err) {
        // Attachments are best-effort; a failure here doesn't abort
        // the resume — the conversation still attaches.
        console.warn(
          `[DSX:continue-btn] attachment fetch failed (proceeding ` +
          `without seed files): ${err && err.message || err}`);
      }
    }

    const payload = {
      created_at: Date.now(),
      conversation_filename: "resume_conversation.md",
      conversation_base64: b64Of(conversationMd),
      resume_text: resumeText,
      seed_files: seedFiles,
    };

    return {
      payload,
      stats: {
        messages: data.messages.length,
        conversationChars: conversationMd.length,
        guidanceChars: guidance.length,
        resumeTextChars: resumeText.length,
        seedFiles: seedFiles.length,
      },
    };
  };

  // ---- Phase 2: consume payload on the new tab's page load ------

  const waitForComposer = async (timeoutMs = 15000) => {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        if (window.__dsx__ && typeof window.__dsx__.findComposer === "function") {
          const found = window.__dsx__.findComposer();
          if (found && found.el) return true;
        }
      } catch (_) {}
      await new Promise(r => setTimeout(r, 150));
    }
    return false;
  };

  const runResumePhase2 = async (payload) => {
    const ready = await waitForComposer(15000);
    if (!ready) {
      throw new Error(
        "composer did not appear within 15s — the new chat may not " +
        "have loaded correctly");
    }
    // Let React finish hydrating the composer before we poke it.
    await new Promise(r => setTimeout(r, 500));

    const files = [
      {
        name: payload.conversation_filename || "resume_conversation.md",
        mime: "text/markdown",
        base64: payload.conversation_base64,
      },
      ...(payload.seed_files || []),
    ];

    if (typeof window.__dsx_attach_json !== "function") {
      throw new Error("__dsx_attach_json unavailable");
    }
    const attachJson = await window.__dsx_attach_json(JSON.stringify({
      files,
      opts: { delay_ms: 0 },
    }));
    const attachRes = JSON.parse(attachJson);
    if (!attachRes.success) {
      throw new Error(
        `attach failed: ${attachRes.reason || "(no reason)"}`);
    }

    // Small settle so chips render before typing — matches the
    // ordering `deeppeek prompt resume` uses (attach, then type).
    await new Promise(r => setTimeout(r, 300));

    if (typeof window.__dsx_type_json !== "function") {
      throw new Error("__dsx_type_json unavailable");
    }
    const typeJson = await window.__dsx_type_json(JSON.stringify({
      text: payload.resume_text,
      opts: {},
    }));
    const typeRes = JSON.parse(typeJson);
    if (!typeRes.success) {
      throw new Error(
        `type failed: ${typeRes.reason || "(no reason)"}`);
    }

    return { files: files.length };
  };

  // Resume-worker IIFE — runs once on every page load.  Cheap no-op
  // when there's no pending payload, which is the common case.
  (async () => {
    let pending;
    try { pending = localStorage.getItem(PENDING_KEY); }
    catch (_) { return; }
    if (!pending) return;

    let data;
    try { data = JSON.parse(pending); }
    catch (_) {
      try { localStorage.removeItem(PENDING_KEY); } catch (_) {}
      return;
    }

    // Stale payloads are dropped rather than risking a surprise
    // resume long after the click.
    if (typeof data.created_at !== "number"
        || Date.now() - data.created_at > FRESH_MS) {
      try { localStorage.removeItem(PENDING_KEY); } catch (_) {}
      return;
    }

    // Atomic single-writer claim — prevents a second chat tab that
    // happens to reload within the freshness window from grabbing
    // the same payload.
    const claimKey = CLAIM_PREFIX + data.created_at;
    try {
      if (localStorage.getItem(claimKey)) return;
      localStorage.setItem(claimKey, "1");
    } catch (_) { return; }

    try {
      try { localStorage.removeItem(PENDING_KEY); } catch (_) {}
      console.log(
        `[DSX:resume-worker] picked up pending resume payload ` +
        `(${(data.seed_files || []).length} seed file(s))`);
      const result = await runResumePhase2(data);
      console.log(
        `[DSX:resume-worker] resume complete — ` +
        `${result.files} file(s) attached, resume text typed`);
    } catch (err) {
      console.error(
        `[DSX:resume-worker] failed: ${err && err.message || err}`);
    } finally {
      try { localStorage.removeItem(claimKey); } catch (_) {}
    }
  })();

  // ============================================================
  // Rule: continue-btn — "Resume"
  // ============================================================
  B.rule({
    name: "continue-btn",
    position: "start",
    template: () => {
      for (const b of document.querySelectorAll(".ds-button[role='button']")) {
        if (b.hasAttribute(MARKER_ATTR)) continue;
        if ((b.innerText || "").trim() !== "Continue") continue;
        const base = b.cloneNode(true);
        base.removeAttribute(MARKER_ATTR);
        const content = base.querySelector(".ds-button__content");
        if (content) content.textContent = "Resume";
        return base;
      }
      return null;
    },
    target: () => {
      const rows = new Set();
      for (const b of document.querySelectorAll(".ds-button[role='button']")) {
        if (b.hasAttribute(MARKER_ATTR)) continue;
        if ((b.innerText || "").trim() !== "Continue") continue;
        const p = b.parentElement;
        if (p && p.tagName === "DIV") rows.add(p);
      }
      return [...rows];
    },
    filter: (row) => [...row.children].some(c =>
      !c.hasAttribute(MARKER_ATTR)
      && c.matches(".ds-button[role='button']")
      && (c.innerText || "").trim() === "Continue"),
    onClick: async (e, el) => {
      const prevPe = el.style.pointerEvents;
      const prevOp = el.style.opacity;
      el.style.pointerEvents = "none";
      el.style.opacity = "0.5";

      // Open a blank tab synchronously — before any await — so the
      // user-gesture context is still fresh and Firefox's popup
      // blocker allows it.  We navigate it AFTER the payload is
      // ready, so the new tab loads chat.deepseek.com with the
      // payload already sitting in localStorage.
      let newTab = null;
      try { newTab = window.open("about:blank", "_blank"); }
      catch (_) { newTab = null; }

      try {
        console.log("[DSX:continue-btn] preparing resume payload...");
        const { payload, stats } = await prepareResumePayload();

        try {
          localStorage.setItem(PENDING_KEY, JSON.stringify(payload));
        } catch (err) {
          throw new Error(
            `cannot store resume payload (localStorage quota?): ` +
            `${err && err.message || err}`);
        }

        console.log(
          `[DSX:continue-btn] payload ready — ` +
          `${stats.messages} msg(s), ${stats.conversationChars} chars, ` +
          `${stats.seedFiles} seed file(s), ` +
          `${stats.resumeTextChars} char resume text`);

        if (newTab && !newTab.closed) {
          newTab.location.href = CHAT_URL;
          console.log(
            "[DSX:continue-btn] new tab opening — resume worker " +
            "will attach + type there");
        } else {
          // Popup was blocked; navigate current tab.  Same user
          // outcome, just loses the original tab's state.
          console.log(
            "[DSX:continue-btn] popup blocked — navigating current " +
            "tab to fresh chat");
          window.location.href = CHAT_URL;
        }
      } catch (err) {
        console.error(
          `[DSX:continue-btn] failed: ${err && err.message || err}`);
        if (newTab && !newTab.closed) {
          try { newTab.close(); } catch (_) {}
        }
        el.style.pointerEvents = prevPe;
        el.style.opacity = prevOp;
      }
    },
  });
})();
