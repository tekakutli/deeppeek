// =====================================================================
// button-rules/_resume.js — shared resume pipeline
// =====================================================================
//
// Loaded FIRST (via button-rules/_manifest.json) so `__dsx__.resume`
// exists before 02-continue-btn.js, 03-rate-limit-retry.js, and
// 04-length-limit-reached.js run.  Files prefixed with `_` are shared
// helpers — they define namespaces on `window.__dsx__` but do NOT
// register rules.
//
// THE PIPELINE (what `fullPipeline` does)
// ---------------------------------------
//   Phase 1 (current tab, on click):
//     · detect which container to open the new tab in
//         · shift-click → picker (list + manual fallback for name
//           or cookieStoreId); current tab's container highlighted
//         · plain click → same container the current tab is in
//     · extract conversation via __dsx_extract
//     · format markdown (mirrors formatter.py)
//     · extract last-thought guidance (mirrors prompts/resume.py)
//     · fetch first prompt's attachments as base64
//     · write payload to localStorage (same-container fallback)
//       AND to extension storage via the in-memory background
//       channel (cross-container channel)
//     · open a new tab via callBridge(DSX_OPEN_TAB)
//
//   Phase 2 (new tab, on page load):
//     · the resume-worker IIFE at the bottom of this file claims the
//       pending payload (extension storage first, localStorage
//       fallback), waits for the composer, attaches conversation +
//       seed files, types the resume text
//     · single-writer claim means only one tab ever gets the payload
//
// CHAINED RESUMES (2026-11-08)
// ----------------------------
//   A second-or-later GUI resume finds the previous resume file
//   (or a merge file from an even earlier resume) as one of the
//   seed attachments — because the first resume attached its
//   conversation to the new tab's composer, and that attachment
//   becomes the first user message of the next conversation.  Left
//   alone, the AI would see the same history twice — once as
//   `resume_conversation.md`, once as `merged_resume_<ts>.md` — with
//   no signal that one precedes the other.
//
//   Fix: before building the attach list, classify seed files into
//   prior-resumes and other-seeds, merge the prior resumes with the
//   current conversation into ONE flat file, and attach only that.
//   Non-resume seeds (original attachments like images) stay in the
//   list unchanged — they were the seed of the first resume, are
//   re-attached by each subsequent one, and the AI still needs them.
//
//   This mirrors prompts/resume.py's `_stage_merge_history` exactly:
//     · detection: content has `MERGED FILE 1/` in the first 2 KB,
//       OR filename starts with `resume_` / `merged_resume_`
//     · extraction: recursively unwraps nested merges, so a
//       GUI→GUI→CLI chain stays flat (A+B+C, not (A+B)+C)
//     · emission: `merge_md.py`'s `+` separator / backtick fence /
//       `MERGED FILE i/N` header, byte-for-byte compatible with
//       the CLI's output — so a GUI merge is detected by a later
//       CLI resume, and vice versa
//
// CROSS-REALM BRIDGE (postMessage, not exportFunction)
// ---------------------------------------------------
//   Page world cannot `await` a Promise returned by `exportFunction`
//   — Firefox's Xray barrier rejects `.then` access with
//   "Permission denied to access property 'then'".  `Promise.race`
//   and manual `.then()` hit the same wall.
//
//   Fix: `callBridge()` uses `window.postMessage`, which crosses
//   the content-script ↔ page-world boundary via structured clone.
//   content.js's installPostMessageBridge forwards `msg` to the
//   background and posts `{dsx_resp, payload: <json string>}` back.
//   Both directions are strings/plain-objects; no Xray involvement.
//
// CONTAINER SELECTION — TWO MODES
// -------------------------------
//   Plain click   → opens in the SAME container the current tab is
//                   in.  Detected via callBridge(DSX_CURRENT_CONTAINER),
//                   which uses sender.tab.cookieStoreId in the
//                   background (content scripts see a reduced
//                   tabs.Tab without container info).
//
//   Shift-click   → opens a container picker.  Two sub-modes:
//                     · list available → every container from
//                       browser.contextualIdentities.query
//                     · list unavailable → manual input for a
//                       name ("Work") or raw cookieStoreId
//                       ("firefox-container-3")
//                   Picks are one-shot: no persistence.  Next plain
//                   click resumes in the current container again.
//
// EXPORTED API (window.__dsx__.resume)
// ------------------------------------
//   fullPipeline(e, el, label)
//       Complete click handler: pick container, prepare payload,
//       save, open tab.  `label` is used in console logs to
//       identify which rule triggered the pipeline.
//       Used by 02-continue-btn.js, 03-rate-limit-retry.js, and
//       04-length-limit-reached.js.
//
//   callBridge(msg, timeoutMs)
//       Raw postMessage bridge — for rules that need to talk to
//       the background directly.
//
//   detectCurrentContainer()
//       Returns {cookieStoreId, name} or null.
//
//   showContainerPicker(containers, currentCid, opts)
//       Modal <dialog> picker.
//
//   prepareResumePayload()
//       Extract + format + return {payload, stats}.
//
//   openNewTab(pref)
//       Open a tab with an optional container.
//
//   isMergeContent(text)
//   isResumeOrMergeAttachment(file)
//   extractFromMerge(text)
//   emitMergedBlocks(parts)
//   buildAttachList(payload)
//       Merge helpers (2026-11-08) — exposed for console debugging.
//
// Nothing here registers a rule.  Rules live in numbered files.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};
  if (window.__dsx__.resume) return;   // idempotent

  const CHAT_URL = "https://chat.deepseek.com/";
  const CONTAINER_ID_RE = /^[a-z0-9]+-container-\d+$/i;

  // ---- cross-realm bridge via postMessage -----------------------
  //
  // Sends `{dsx_req, msg}` on `window`; content.js's listener
  // replies with `{dsx_resp: <same reqId>, payload: <json string>}`.
  // Resolves with the parsed payload object, or rejects on timeout.
  const callBridge = (msg, timeoutMs = 10000) =>
    new Promise((resolve, reject) => {
      const reqId =
        "dsx_" + Math.random().toString(36).slice(2) + "_" + Date.now();
      const timer = setTimeout(() => {
        window.removeEventListener("message", handler);
        reject(new Error(
          `bridge ${msg.type} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      const handler = (ev) => {
        if (ev.source !== window) return;
        const d = ev.data;
        if (!d || typeof d !== "object") return;
        if (d.dsx_resp !== reqId) return;
        window.removeEventListener("message", handler);
        clearTimeout(timer);
        try {
          resolve(JSON.parse(d.payload));
        } catch (e) {
          resolve({
            success: false,
            reason: `bridge response parse failed: ${e.message}`,
          });
        }
      };
      window.addEventListener("message", handler);
      window.postMessage({ dsx_req: reqId, msg }, "*");
    });

  // ---- helpers --------------------------------------------------

  const b64Of = (s) => btoa(unescape(encodeURIComponent(s)));
  const b64Decode = (s) => decodeURIComponent(escape(atob(s)));

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
      const c = tailPara(m.content || "");
      if (c) return trim(c);
      const t = tailPara(m.thinking || "");
      if (t) return trim(t);
    }
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "user") continue;
      const c = tailPara(m.content || "");
      if (c) return trim(c);
    }
    return "";
  };

  const buildResumeText = (guidance) =>
    `you were thinking about it, but was interrupted, keep going ` +
    `where you left it: "${guidance}"; ` +
    `dont give me any code yet, simply design it`;

  // ---- merge helpers (2026-11-08) -------------------------------
  //
  // Ported from prompts/resume.py and scripts/merge_md.py so the
  // GUI resume button handles chained resumes the same way the CLI
  // command does.  See the module docstring ("CHAINED RESUMES")
  // for the why, and prompts/resume.py's docstring for the
  // original rationale.
  //
  // Compatibility contract:
  //   · A merge produced here MUST be detectable by
  //     prompts/resume.py::_is_resume_or_merge_attachment — which
  //     checks for `MERGED FILE 1/` in the first 2 KB.
  //   · The block format MUST match merge_md.py's so that a
  //     subsequent CLI resume can flatten it via
  //     _extract_from_merge, and vice versa.
  // Both hold by construction: same regex, same emit function.

  const _MERGE_SEP_CHAR = "+";
  const _MERGE_MIN_SEPARATOR_WIDTH = 70;
  const _MERGE_MIN_FENCE_WIDTH = 3;
  const _MERGE_FENCE_CHAR = "`";

  // Port of merge_md.py's _MERGE_HEADER_RE.  /m for line-anchored
  // `^`, /g for matchAll.  The `\1` backreference ensures opener
  // and closer use the same `+` width, so a `+` run inside the
  // fenced content cannot be mistaken for a boundary.
  const _MERGE_HEADER_RE =
    /^(\+{70,})\nMERGED FILE (\d+)\/(\d+): (.+?)\n\1\n/gm;

  // Port of merge_md.py's _max_run: longest consecutive run of `ch`
  // in `text`.
  function _maxRun(text, ch) {
    if (!text) return 0;
    let longest = 0, current = 0;
    for (let i = 0; i < text.length; i++) {
      if (text[i] === ch) {
        current++;
        if (current > longest) longest = current;
      } else {
        current = 0;
      }
    }
    return longest;
  }

  // Separator width strictly exceeds any `+` run in `content`, so
  // the boundary cannot be faked by content.
  function _separatorFor(content) {
    const width = Math.max(
      _MERGE_MIN_SEPARATOR_WIDTH,
      _maxRun(content, _MERGE_SEP_CHAR) + 1,
    );
    return _MERGE_SEP_CHAR.repeat(width);
  }

  // Fence width strictly exceeds any backtick run in `content`,
  // for the same reason.
  function _fenceFor(content) {
    const width = Math.max(
      _MERGE_MIN_FENCE_WIDTH,
      _maxRun(content, _MERGE_FENCE_CHAR) + 1,
    );
    return _MERGE_FENCE_CHAR.repeat(width);
  }

  // True iff `text` looks like merge_md.py output.  Only the head
  // is checked (2048 chars, matching the Python helper) — a
  // `MERGED FILE 1/` appearing that early is decisive.
  function _isMergeContent(text) {
    if (typeof text !== "string") return false;
    return text.slice(0, 2048).indexOf("MERGED FILE 1/") !== -1;
  }

  // Recursively unwrap a merge_md.py file into a flat list of
  // {name, content} pairs, in original order.  Ports
  // prompts/resume.py::_extract_from_merge (which itself is a port
  // of dumps_evolve.py's parser).  A nested merge contributes its
  // leaves in place, so a chain A→B→C produces [A, B, C], not
  // [[A,B], C] or any other shape.
  function _extractFromMerge(text, depth) {
    depth = depth || 0;
    if (depth > 20) {
      console.warn(
        "[DSX:resume] merge nesting too deep; stopping recursion");
      return [{ name: "unknown.md", content: text }];
    }

    const headers = [...text.matchAll(_MERGE_HEADER_RE)];
    if (headers.length === 0) {
      return [{ name: "unknown.md", content: text }];
    }

    const result = [];
    for (let i = 0; i < headers.length; i++) {
      const m = headers[i];
      const name = m[4];
      const contentStart = m.index + m[0].length;
      const contentEnd = (i + 1 < headers.length)
        ? headers[i + 1].index
        : text.length;
      const chunk = text.slice(contentStart, contentEnd);

      // Chunk shape (see _emitMergedBlocks):
      //   <fence>\n<content>\n<fence>\n\n
      // The fence width matches the opener, so locate the closer
      // by exact-match of "\n" + fence + "\n".
      const opener = chunk.match(/^(`{3,})[^\n]*\n/);
      let inner;
      if (!opener) {
        inner = chunk.replace(/\n+$/, "");
      } else {
        const n = opener[1].length;
        const fence = "`".repeat(n);
        const closerPat = "\n" + fence + "\n";
        const last = chunk.lastIndexOf(closerPat);
        if (last < 0) {
          // Defensive: closer without trailing newline.
          const fallback = "\n" + fence;
          const lastFallback = chunk.lastIndexOf(fallback);
          inner = (lastFallback < 0)
            ? chunk.slice(opener[0].length).replace(/\n+$/, "")
            : chunk.slice(opener[0].length, lastFallback);
        } else {
          inner = chunk.slice(opener[0].length, last);
        }
      }

      if (_isMergeContent(inner)) {
        result.push(..._extractFromMerge(inner, depth + 1));
      } else {
        result.push({ name, content: inner });
      }
    }
    return result;
  }

  // Emit `parts` (list of {name, content}) as a merge_md.py-
  // compatible merged file.  Same format as merge_md.py's
  // _emit_block loop: per-block `+` separator, MERGED FILE i/N
  // header, backtick fence, content.  No preamble — the CLI's
  // preamble is verbose (for human readers); for a chat attachment
  // the block headers alone communicate structure.  Kept minimal
  // on purpose to reduce attention dilution.
  function _emitMergedBlocks(parts) {
    const total = parts.length;
    let out = "";
    for (let i = 0; i < total; i++) {
      const { name, content } = parts[i];
      const body = content == null ? "" : content;
      const sep = _separatorFor(body);
      const fence = _fenceFor(body);
      const idx = i + 1;
      out += sep + "\n";
      out += `MERGED FILE ${idx}/${total}: ${name}\n`;
      out += sep + "\n";
      out += fence + "\n";
      out += body;
      if (body && !body.endsWith("\n")) out += "\n";
      out += fence + "\n\n";
    }
    return out;
  }

  // True iff `file` (a {name, mime, base64} record from the attach
  // payload) looks like a prior resume.  Port of
  // prompts/resume.py::_is_resume_or_merge_attachment:
  //
  //   · filename starts with `resume_` / `merged_resume_` — fast
  //     path, catches the JS pipeline's own output and the CLI's
  //     `resume_<slug>_<ts>.md`
  //   · OR content has `MERGED FILE 1/` in the first 2 KB —
  //     catches renamed merges (e.g. a CLI merge whose stem doesn't
  //     start with `resume_`)
  //
  // The content-sniff is gated on text-ish MIME/extension so a
  // binary seed (PNG, PDF) is never fed to b64Decode — decoding
  // non-UTF-8 through decodeURIComponent would throw or produce
  // garbage.
  function _isResumeOrMergeAttachment(file) {
    if (!file || typeof file !== "object") return false;
    const name = file.name || "";
    if (name.startsWith("resume_") || name.startsWith("merged_resume_")) {
      return true;
    }
    const mime = file.mime || "";
    const isTexty = mime.startsWith("text/")
      || mime === "application/json"
      || mime === "application/octet-stream"
      || name.endsWith(".md")
      || name.endsWith(".txt");
    if (!isTexty || !file.base64) return false;
    try {
      const head = b64Decode(file.base64).slice(0, 2048);
      return _isMergeContent(head);
    } catch (_e) {
      return false;
    }
  }

  // YYYYMMDD_HHMMSS, matching the convention used by
  // prompts/resume.py's _timestamp (and injected-hijack.js's
  // _downloadTimestamp).
  function _timestamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
         + `_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  // ---- container detection --------------------------------------
  //
  // Detects the container the current tab is in, so plain-click
  // can open the new tab in the same container.  Routes through the
  // background because content scripts get a reduced tabs.Tab object
  // (tab.cookieStoreId is undefined there); the background sees the
  // full Tab via `sender.tab.cookieStoreId`.
  //
  // Returns:
  //   null                                       → default container
  //   { cookieStoreId: "firefox-container-N", name? } → specific
  const detectCurrentContainer = async () => {
    try {
      const res = await callBridge(
        { type: "DSX_CURRENT_CONTAINER" }, 5000);
      if (res && res.success && res.cookieStoreId) {
        return {
          cookieStoreId: res.cookieStoreId,
          name: res.name || null,
        };
      }
    } catch (err) {
      console.warn(
        `[DSX:resume] could not detect current container: ` +
        `${err && err.message || err} — falling back to browser default`);
    }
    return null;
  };

  // ---- container picker (modal <dialog>) -----------------------
  //
  // Renders a modal picker with:
  //   · "Default (no container)"
  //   · one entry per available container (name + cookieStoreId)
  //   · (when allowManual) a free-text input that accepts either a
  //     container name or a raw cookieStoreId, so the button still
  //     works when the container API is broken
  //   · "Cancel"
  //
  // `currentCid` is used ONLY to highlight the row the user is
  // currently in — not to persist a default.
  //
  // Resolves with { action:"pick", cookieStoreId?, name? } or null.
  const showContainerPicker = (containers, currentCid, opts = {}) => {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.setAttribute("data-dsx-resume-picker", "1");
      dlg.style.cssText = `
        padding: 16px;
        border: 1px solid #3a3a3a;
        border-radius: 8px;
        background: #1f1f1f;
        color: #e0e0e0;
        min-width: 300px;
        max-width: 480px;
        max-height: 70vh;
        overflow-y: auto;
        font-family: ui-monospace,SFMono-Regular,Menlo,monospace;
        font-size: 13px;
        z-index: 2147483647;
      `;

      const title = document.createElement("div");
      title.textContent = "Resume into container:";
      title.style.cssText =
        "font-weight: bold; margin-bottom: 12px; font-size: 14px;";
      dlg.appendChild(title);

      const makeBtn = (label, cid, action, btnOpts = {}) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.style.cssText = `
          display: block;
          width: 100%;
          padding: 8px 12px;
          margin: 4px 0;
          text-align: left;
          background: ${cid === currentCid ? "#3a3a3a" : "transparent"};
          color: inherit;
          border: 1px solid #3a3a3a;
          border-radius: 4px;
          cursor: pointer;
          font-family: inherit;
          font-size: inherit;
        `;
        b.addEventListener("mouseover", () => {
          b.style.background = "#2f2f2f";
        });
        b.addEventListener("mouseout", () => {
          b.style.background = cid === currentCid ? "#3a3a3a" : "transparent";
        });
        b.addEventListener("click", () => {
          dlg.close();
          resolve({ action, cookieStoreId: cid, name: btnOpts.name ?? null });
        });
        return b;
      };

      dlg.appendChild(makeBtn("Default (no container)", null, "pick"));

      if (containers.length > 0) {
        for (const c of containers) {
          const label = `${c.name} (${c.cookieStoreId})`;
          dlg.appendChild(makeBtn(label, c.cookieStoreId, "pick",
                                  { name: c.name }));
        }
      }

      if (opts.allowManual) {
        const label = document.createElement("div");
        label.textContent = containers.length > 0
          ? "Or type a container name, or paste a cookieStoreId:"
          : "Container list unavailable — type a container name, " +
            "or paste a cookieStoreId:";
        label.style.cssText =
          "margin-top: 12px; margin-bottom: 6px; font-size: 12px; opacity: 0.85;";
        dlg.appendChild(label);

        const hint = document.createElement("div");
        hint.textContent =
          "(name like \"Work\"; ID like firefox-container-3 from " +
          "about:preferences#containers)";
        hint.style.cssText =
          "margin-bottom: 6px; font-size: 11px; opacity: 0.6;";
        dlg.appendChild(hint);

        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Work   or   firefox-container-3";
        input.style.cssText = [
          "width: 100%",
          "padding: 6px 8px",
          "box-sizing: border-box",
          "background: #111",
          "color: #e0e0e0",
          "border: 1px solid #3a3a3a",
          "border-radius: 4px",
          "font-family: inherit",
          "font-size: 12px",
        ].join(";");
        dlg.appendChild(input);

        const useBtn = document.createElement("button");
        useBtn.textContent = "Use this";
        useBtn.style.cssText = [
          "display: block",
          "width: 100%",
          "padding: 8px 12px",
          "margin: 8px 0 4px 0",
          "text-align: center",
          "background: #2a4a2a",
          "color: #d0f0d0",
          "border: 1px solid #3a5a3a",
          "border-radius: 4px",
          "cursor: pointer",
          "font-family: inherit",
          "font-size: inherit",
        ].join(";");

        const submitManual = () => {
          const v = input.value.trim();
          if (!v) { input.focus(); return; }
          const isId = CONTAINER_ID_RE.test(v);
          dlg.close();
          if (isId) {
            resolve({ action: "pick", cookieStoreId: v, name: null });
          } else {
            resolve({ action: "pick", cookieStoreId: null, name: v });
          }
        };
        useBtn.addEventListener("click", submitManual);
        input.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter") { ev.preventDefault(); submitManual(); }
        });
        dlg.appendChild(useBtn);
      }

      const cancel = document.createElement("button");
      cancel.textContent = "Cancel";
      cancel.style.cssText = `
        margin-top: 8px;
        padding: 6px 12px;
        background: transparent;
        color: #888;
        border: 1px solid #2a2a2a;
        border-radius: 4px;
        cursor: pointer;
        font-family: inherit;
        font-size: 12px;
      `;
      cancel.addEventListener("click", () => { dlg.close(); resolve(null); });
      dlg.appendChild(cancel);

      dlg.addEventListener("cancel", () => resolve(null));
      dlg.addEventListener("close", () => {
        resolve(null);
        setTimeout(() => dlg.remove(), 0);
      });

      document.body.appendChild(dlg);
      dlg.showModal();
    });
  };

  // ---- Phase 1: prepare payload --------------------------------

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
        console.warn(
          `[DSX:resume] attachment fetch failed (proceeding ` +
          `without seed files): ${err && err.message || err}`);
      }
    }

    return {
      payload: {
        created_at: Date.now(),
        conversation_filename: "resume_conversation.md",
        conversation_base64: b64Of(conversationMd),
        resume_text: resumeText,
        seed_files: seedFiles,
      },
      stats: {
        messages: data.messages.length,
        conversationChars: conversationMd.length,
        guidanceChars: guidance.length,
        resumeTextChars: resumeText.length,
        seedFiles: seedFiles.length,
      },
    };
  };

  // ---- Phase 2: consume payload -------------------------------

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

  // Build the attach list for a resume payload.  Returns
  // [{name, mime, base64}, ...] ready for __dsx_attach_json.
  //
  // Cases:
  //   1. No prior resumes in seed_files
  //        → [current_conversation, ...all_seeds]
  //        (unchanged from before 2026-11-08 — a first-time GUI
  //        resume behaves exactly as it did)
  //   2. Prior resumes present
  //        → [merged_resume_<ts>.md, ...non_resume_seeds]
  //        The merged file combines (prior_resume_1_content,
  //        prior_resume_2_content, …, current_conversation) in
  //        that order, so it reads oldest → newest.  Prior
  //        merges are flattened recursively so a chain
  //        GUI→GUI→CLI stays flat.
  //
  // Non-resume seeds (images, docs) are NEVER merged into the
  // conversation; they were the seed of the first resume, the AI
  // still needs them, and folding them into the merged text would
  // corrupt binary content.
  function _buildAttachList(payload) {
    const seedFiles = Array.isArray(payload.seed_files)
      ? payload.seed_files
      : [];
    const currentFile = {
      name: payload.conversation_filename || "resume_conversation.md",
      mime: "text/markdown",
      base64: payload.conversation_base64,
    };

    const resumeSeeds = [];
    const otherSeeds = [];
    for (const f of seedFiles) {
      if (_isResumeOrMergeAttachment(f)) {
        resumeSeeds.push(f);
      } else {
        otherSeeds.push(f);
      }
    }

    if (resumeSeeds.length === 0) {
      console.log(
        `[DSX:resume] no prior resumes in ${seedFiles.length} ` +
        `seed file(s); attaching current conversation as-is`);
      return [currentFile, ...otherSeeds];
    }

    // Flatten each prior resume.  A prior merge contributes its
    // leaves; a plain conversation contributes itself.  Any seed
    // that fails to decode is preserved as a separate attachment
    // rather than dropped — better to give the AI an unmerged
    // extra chip than to silently lose a file.
    const parts = [];
    for (const r of resumeSeeds) {
      let text;
      try {
        text = b64Decode(r.base64);
      } catch (e) {
        console.warn(
          `[DSX:resume] cannot decode ${r.name} ` +
          `(${e && e.message || e}); attaching it as a separate file`);
        otherSeeds.push(r);
        continue;
      }
      if (_isMergeContent(text)) {
        parts.push(..._extractFromMerge(text));
      } else {
        parts.push({ name: r.name, content: text });
      }
    }

    // Current conversation goes LAST: the merged file reads
    // oldest → newest, with the just-captured one at the tail.
    let currentText;
    try {
      currentText = b64Decode(payload.conversation_base64);
    } catch (e) {
      console.warn(
        `[DSX:resume] cannot decode current conversation ` +
        `(${e && e.message || e}); not merging`);
      return [currentFile, ...resumeSeeds, ...otherSeeds];
    }
    parts.push({ name: currentFile.name, content: currentText });

    const mergedBody = _emitMergedBlocks(parts);
    const mergedName = `merged_resume_${_timestamp()}.md`;

    console.log(
      `[DSX:resume] merged ${resumeSeeds.length} prior resume(s) + ` +
      `current → ${mergedName} ` +
      `(${parts.length} conversation(s), ${mergedBody.length} chars)`);

    return [
      {
        name: mergedName,
        mime: "text/markdown",
        base64: b64Of(mergedBody),
      },
      ...otherSeeds,
    ];
  }

  const runResumePhase2 = async (payload) => {
    const ready = await waitForComposer(15000);
    if (!ready) {
      throw new Error(
        "composer did not appear within 15s — the new chat may not " +
        "have loaded correctly");
    }
    await new Promise(r => setTimeout(r, 500));

    const files = _buildAttachList(payload);

    if (typeof window.__dsx_attach_json !== "function") {
      throw new Error("__dsx_attach_json unavailable");
    }
    const attachJson = await window.__dsx_attach_json(JSON.stringify({
      files,
      opts: { delay_ms: 0 },
    }));
    const attachRes = JSON.parse(attachJson);
    if (!attachRes.success) {
      throw new Error(`attach failed: ${attachRes.reason || "(no reason)"}`);
    }

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
      throw new Error(`type failed: ${typeRes.reason || "(no reason)"}`);
    }

    return { files: files.length };
  };

  // ---- open the new tab ----------------------------------------
  //
  // Argument shapes:
  //   null                → browser default (no container args)
  //   { cookieStoreId }   → raw ID, forwarded as container_id
  //   { name }            → forwarded as container, resolved by bg
  const openNewTab = async (pref) => {
    try {
      const args = { url: CHAT_URL };
      if (pref) {
        if (pref.cookieStoreId) {
          args.container_id = pref.cookieStoreId;
        } else if (pref.name) {
          args.container = pref.name;
        }
      }
      const res = await callBridge({ type: "DSX_OPEN_TAB", ...args }, 10000);
      if (res && res.success) {
        return { ok: true, method: "bridge",
                 cookie_store_id: res.cookie_store_id };
      }
      const reason = res && res.reason ? res.reason : "unknown";
      return { ok: false, reason, method: "bridge" };
    } catch (err) {
      return { ok: false,
               reason: String((err && err.message) || err),
               method: "bridge" };
    }
  };

  // ---- the full click pipeline --------------------------------
  //
  // `label` identifies the rule that triggered this — used in
  // console logs so debugging shows which button started the
  // pipeline.  Rules pass their own `rule.name`.
  //
  // `e` is the click event (must have been triggered by a real
  // click, so `shiftKey` is populated).  `el` is the injected clone
  // (used for the busy state).
  const fullPipeline = async (e, el, label = "resume") => {
    const log = (msg) => console.log(`[DSX:${label}] ${msg}`);
    const warn = (msg) => console.warn(`[DSX:${label}] ${msg}`);
    const error = (msg) => console.error(`[DSX:${label}] ${msg}`);

    const prevPe = el.style.pointerEvents;
    const prevOp = el.style.opacity;
    const setBusy = (on) => {
      el.style.pointerEvents = on ? "none" : prevPe;
      el.style.opacity = on ? "0.5" : prevOp;
    };
    setBusy(true);

    try {
      // ---- pick container ----
      let pickedCid;

      if (e.shiftKey) {
        let containers = [];
        let listAvailable = false;
        let listReason = "unavailable";

        try {
          const listRes = await callBridge(
            { type: "DSX_LIST_CONTAINERS" }, 10000);
          if (listRes && listRes.success && listRes.available) {
            containers = listRes.containers || [];
            listAvailable = true;
          } else {
            listReason = (listRes && listRes.reason) || "unknown";
          }
        } catch (err) {
          listReason = String((err && err.message) || err);
        }

        if (!listAvailable) {
          warn(
            `container list unavailable ` +
            `(${listReason}) — falling back to manual entry`);
        }

        const current = await detectCurrentContainer();
        const currentCid = current ? current.cookieStoreId : null;

        const picked = await showContainerPicker(
          containers, currentCid, { allowManual: !listAvailable });
        if (!picked) return;   // cancelled — finally restores busy

        pickedCid = (picked.cookieStoreId || picked.name)
          ? { cookieStoreId: picked.cookieStoreId || null,
              name: picked.name || null }
          : null;
      } else {
        pickedCid = await detectCurrentContainer();
      }

      // ---- prepare payload ----
      log("preparing resume payload...");
      const { payload, stats } = await prepareResumePayload();

      // ---- persist to localStorage (same-container channel) ----
      try {
        localStorage.setItem("dsx_resume_pending", JSON.stringify(payload));
      } catch (err) {
        warn(
          `localStorage save failed: ${err && err.message || err}`);
      }

      // ---- persist to extension storage (cross-container channel) ----
      let extStorageOk = false;
      try {
        const saveRes = await callBridge(
          { type: "DSX_RESUME_SAVE", payload }, 10000);
        if (saveRes && saveRes.success) {
          extStorageOk = true;
        } else {
          warn(
            `extension-storage save failed: ` +
            `${(saveRes && saveRes.reason) || "unknown"}`);
        }
      } catch (err) {
        warn(
          `extension-storage save threw: ${err && err.message || err} ` +
          `— continuing with localStorage only ` +
          `(cross-container resume will not work)`);
      }

      log(
        `payload saved ` +
        `(localStorage${extStorageOk ? " + extension-storage" : ""}) — ` +
        `${stats.messages} msg(s), ${stats.conversationChars} chars, ` +
        `${stats.seedFiles} seed file(s), ` +
        `${stats.resumeTextChars} char resume text`);

      // ---- open the tab ----
      const openRes = await openNewTab(pickedCid);
      if (!openRes.ok) {
        try { localStorage.removeItem("dsx_resume_pending"); } catch (_) {}
        try {
          await callBridge(
            { type: "DSX_RESUME_SAVE", payload: null }, 5000);
        } catch (_) {}
        throw new Error(`open tab failed: ${openRes.reason}`);
      }
      log(
        `new tab opening via ${openRes.method}` +
        (openRes.cookie_store_id
          ? ` (container: ${openRes.cookie_store_id})`
          : "") +
        " — resume worker will attach + type there");
    } catch (err) {
      error(`failed: ${err && err.message || err}`);
    } finally {
      setBusy(false);
    }
  };

  // ---- resume-worker (runs once on every page load) ------------
  //
  // Claims a pending payload from extension storage first
  // (cross-container), then falls back to localStorage
  // (same-container).  Runs regardless of which rule registered
  // (or whether any rule registered at all) — the worker only
  // needs the pipeline, not a button.
  (async () => {
    console.log("[DSX:resume-worker] page load — checking for pending payload");

    let payload = null;
    let payloadSource = null;

    try {
      const claim = await callBridge({ type: "DSX_RESUME_CLAIM" }, 10000);
      if (claim && claim.success && claim.payload) {
        payload = claim.payload;
        payloadSource = "extension-storage";
      }
    } catch (err) {
      console.warn(
        `[DSX:resume-worker] extension-storage claim failed: ` +
        `${err && err.message || err} — trying localStorage`);
    }

    if (!payload) {
      try {
        const raw = localStorage.getItem("dsx_resume_pending");
        if (raw) {
          const data = JSON.parse(raw);
          if (typeof data.created_at === "number"
              && Date.now() - data.created_at < 30000) {
            payload = data;
            payloadSource = "localStorage";
          }
          localStorage.removeItem("dsx_resume_pending");
        }
      } catch (_) {}
    }

    if (!payload) {
      console.log(
        "[DSX:resume-worker] no pending payload — nothing to resume");
      return;
    }

    try {
      console.log(
        `[DSX:resume-worker] claimed payload from ${payloadSource} ` +
        `(${(payload.seed_files || []).length} seed file(s))`);
      const result = await runResumePhase2(payload);
      console.log(
        `[DSX:resume-worker] resume complete — ` +
        `${result.files} file(s) attached, resume text typed`);
    } catch (err) {
      console.error(
        `[DSX:resume-worker] failed: ${err && err.message || err}`);
    }
  })();

  window.__dsx__.resume = {
    fullPipeline,
    callBridge,
    detectCurrentContainer,
    showContainerPicker,
    prepareResumePayload,
    runResumePhase2,
    openNewTab,
    // Merge helpers (2026-11-08) — exposed for console debugging.
    // Use __dsx__.resume.extractFromMerge("<merged text>") to see
    // the flat parts; __dsx__.resume.buildAttachList(payload) to
    // see the attach list a payload would produce.
    isMergeContent: _isMergeContent,
    isResumeOrMergeAttachment: _isResumeOrMergeAttachment,
    extractFromMerge: _extractFromMerge,
    emitMergedBlocks: _emitMergedBlocks,
    buildAttachList: _buildAttachList,
  };
})();
