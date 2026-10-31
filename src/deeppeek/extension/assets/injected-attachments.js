// =====================================================================
// injected-attachments.js — attachment extraction modes (TIER 3)
// =====================================================================
//
// Every mode that `deeppeek prompt attachments` can select:
//
//   focus        — composer chips (draft or edit-mode message N)
//   first        — literal first user message on active path
//   prompt-regex — most recent matching prompt WITH files
//   message-id   — one specific message
//   last-N       — last N messages that had files
//   all          — every attachment in the conversation
//   filename     — attachments whose basename contains PATTERN
//
// This is the mode-selection surface, so it grows steadily.  It
// depends on the shared read/fetch primitives (idb, fetch) which
// are TIER 1 — mode additions here cannot break the read path.
//
// The `return_base64` flag on the opts toggles between two
// downstream consumers: browser-download (used by the interactive
// `prompt attachments` command) and base64-to-caller (used by
// `prompt retry` and `prompt resume`).
//
// Exports on `window.__dsx__`:
//
//   _fragFiles(frag)               → File[]
//   _hasFiles(msg)                 → bool
//   doExtractAttachments(args)     → result dict
//
// Bridge entry point (defined on `window`):
//
//   window.__dsx_extract_attachments_json(argsJson)
//
// Cross-file deps (via `window.__dsx__`):
//   _collectIndexedDBMessages, extractMessageBody, normalizeRoleFromIDB,
//   _fetchAndDownload, _fetchAndEncode, _normalizeFileRecord,
//   getChatTitle
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // FILE fragments live under `fragments` or `content_blocks`.
  // Both shapes appear in different DeepSeek message versions.
  function _fragFiles(frag) {
    if (!frag || String(frag.type).toUpperCase() !== "FILE") return [];
    return Array.isArray(frag.files) ? frag.files : [];
  }

  function _hasFiles(msg) {
    const frags = msg.fragments || msg.content_blocks || [];
    return frags.some(f => _fragFiles(f).length > 0);
  }

  window.__dsx__._fragFiles = _fragFiles;
  window.__dsx__._hasFiles = _hasFiles;

  // ---------- mode: focus -----------------------------------------
  //
  // Attachments currently in the composer, whether a new draft or
  // an edit of a previous message.  Detects edit-mode via the
  // textarea's fiber chain: composer messageId -2 = new draft,
  // +N = editing message N.
  async function _extractFocus() {
    const chip = document.querySelector(".ds-animated-size-item");
    if (!chip) {
      return { success: true, mode: "focus", files: [],
               reason: "no attachment chip in composer" };
    }
    let fiber = chip[Object.keys(chip)
      .find(k => k.startsWith("__reactFiber"))];
    let fileList = null;
    while (fiber && !fileList) {
      if (fiber.memoizedProps && fiber.memoizedProps.fileList) {
        fileList = fiber.memoizedProps.fileList;
        break;
      }
      fiber = fiber.return;
    }

    let composerMessageId = null;
    const ta = document.querySelector("textarea");
    if (ta) {
      let tf = ta[Object.keys(ta)
        .find(k => k.startsWith("__reactFiber"))];
      let d = 0;
      while (tf && d < 30) {
        if (tf.memoizedProps &&
            tf.memoizedProps.messageId !== undefined) {
          composerMessageId = tf.memoizedProps.messageId;
          break;
        }
        tf = tf.return; d++;
      }
    }

    const files = (fileList || []).map(f =>
      window.__dsx__._normalizeFileRecord(
        f, { message_id: composerMessageId }, -1, "composer"));
    return {
      success: true, mode: "focus",
      composer_message_id: composerMessageId,
      files,
    };
  }

  // ---------- mode: prompt-regex ----------------------------------
  //
  // Backward scan from the conversation end; first matching user
  // prompt WITH files wins.  Mirrors `capture --last-regex`.
  async function _extractByPromptRegex(pattern, flags, includeForked,
                                        allMatches) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    let re;
    try { re = new RegExp(pattern, flags); }
    catch (e) {
      return { success: false, ok: false,
               reason: `invalid regex: ${e.message}` };
    }

    const matches = [];
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (window.__dsx__.normalizeRoleFromIDB(m.role) !== "user") continue;
      const promptText = window.__dsx__.extractMessageBody(m).content || "";
      if (!re.test(promptText)) continue;
      if (!_hasFiles(m)) continue;
      matches.push({ msg: m, idx: i });
      if (!allMatches) break;
    }

    if (matches.length === 0) {
      return { success: true, mode: "prompt-regex",
               files: [], matched_message_ids: [],
               reason: "no user prompt matching regex had attachments" };
    }

    const files = [];
    for (const { msg, idx } of matches) {
      const frags = msg.fragments || msg.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(window.__dsx__._normalizeFileRecord(
            f, msg, idx, "prompt-regex"));
        }
      }
    }
    return {
      success: true, mode: "prompt-regex",
      matched_message_ids: matches.map(x => x.msg.message_id),
      files,
    };
  }

  // ---------- mode: message-id ------------------------------------
  async function _extractByMessageId(id, includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const files = [];
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      if (m.message_id !== id) continue;
      const frags = m.fragments || m.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(window.__dsx__._normalizeFileRecord(
            f, m, i, "message-id"));
        }
      }
    }
    return { success: true, mode: "message-id",
             matched_message_ids: [id], files };
  }

  // ---------- mode: last-N ----------------------------------------
  async function _extractLastN(n, includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const picks = [];
    for (let i = messages.length - 1; i >= 0 && picks.length < n; i--) {
      if (_hasFiles(messages[i])) {
        picks.push({ msg: messages[i], idx: i });
      }
    }
    picks.reverse();  // oldest first in the batch
    const files = [];
    for (const { msg, idx } of picks) {
      const frags = msg.fragments || msg.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(window.__dsx__._normalizeFileRecord(
            f, msg, idx, "last-N"));
        }
      }
    }
    return {
      success: true, mode: "last-N",
      matched_message_ids: picks.map(p => p.msg.message_id),
      files,
    };
  }

  // ---------- mode: first -----------------------------------------
  //
  // Literal first user message on the active path.  Returns its
  // files — empty when the seed prompt had none.  That is not an
  // error: "the very first prompt" is the anchor, not "the first
  // prompt that has files".  The caller sees an empty `files` and
  // proceeds with just the conversation markdown.
  async function _extractFirst(includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      if (window.__dsx__.normalizeRoleFromIDB(m.role) !== "user") continue;
      const files = [];
      const frags = m.fragments || m.content_blocks || [];
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(window.__dsx__._normalizeFileRecord(
            f, m, i, "first"));
        }
      }
      return {
        success: true, mode: "first",
        matched_message_ids: [m.message_id],
        files,
      };
    }
    return {
      success: true, mode: "first",
      matched_message_ids: [], files: [],
      reason: "no user message in conversation",
    };
  }

  // ---------- mode: all -------------------------------------------
  async function _extractAll(includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const files = [];
    const ids = new Set();
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      const frags = m.fragments || m.content_blocks || [];
      let any = false;
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          files.push(window.__dsx__._normalizeFileRecord(
            f, m, i, "all"));
          any = true;
        }
      }
      if (any) ids.add(m.message_id);
    }
    return {
      success: true, mode: "all",
      matched_message_ids: Array.from(ids),
      files,
    };
  }

  // ---------- mode: filename --------------------------------------
  async function _extractByFilename(pattern, caseSensitive,
                                     includeForked) {
    const messages = await window.__dsx__._collectIndexedDBMessages(
      includeForked);
    if (!messages) {
      return { success: false, ok: false,
               reason: "session not found in IndexedDB" };
    }
    const needle = caseSensitive ? pattern : pattern.toLowerCase();
    const files = [];
    const ids = new Set();
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      const frags = m.fragments || m.content_blocks || [];
      let any = false;
      for (const frag of frags) {
        for (const f of _fragFiles(frag)) {
          const name = f.file_name || f.fileName || "";
          const hay = caseSensitive ? name : name.toLowerCase();
          if (!hay.includes(needle)) continue;
          files.push(window.__dsx__._normalizeFileRecord(
            f, m, i, "filename"));
          any = true;
        }
      }
      if (any) ids.add(m.message_id);
    }
    return {
      success: true, mode: "filename",
      matched_message_ids: Array.from(ids),
      files,
    };
  }

  // ---------- dispatcher ------------------------------------------
  async function doExtractAttachments(args) {
    const mode = args.mode || "focus";
    const listOnly = !!args.list_only;
    const includeForked = !!args.include_forked;

    let result;
    if (mode === "focus") {
      result = await _extractFocus();
    } else if (mode === "prompt-regex") {
      result = await _extractByPromptRegex(
        args.pattern, args.flags || "i", includeForked,
        !!args.all_matches);
    } else if (mode === "message-id") {
      result = await _extractByMessageId(
        Number(args.message_id), includeForked);
    } else if (mode === "last-N") {
      result = await _extractLastN(
        Number(args.last), includeForked);
    } else if (mode === "first") {
      result = await _extractFirst(includeForked);
    } else if (mode === "all") {
      result = await _extractAll(includeForked);
    } else if (mode === "filename") {
      result = await _extractByFilename(
        args.pattern, !!args.case_sensitive, includeForked);
    } else {
      return { success: false, ok: false,
               reason: `unknown mode: ${mode}` };
    }

    if (!result.success) return result;

    // Fetch bytes — two paths:
    //   · return_base64  → encode and hand back to the caller
    //                      (used by `deeppeek prompt resume` and
    //                      `deeppeek prompt retry`)
    //   · default        → trigger a browser download per file
    //                      (used by interactive `deeppeek prompt
    //                      attachments`)
    // Sequential in both cases — parallel risks throttling and
    // makes per-file diagnostics confusing.
    const returnBase64 = !!args.return_base64;
    for (const f of result.files) {
      if (!f.signed_path) {
        f.error = "no signed_path on file record";
        continue;
      }
      if (returnBase64) {
        const fetched = await window.__dsx__._fetchAndEncode(
          f.signed_path, f.file_size);
        if (fetched.error) {
          f.error = fetched.error;
        } else {
          f.base64 = fetched.base64;
          f.bytes_received = fetched.bytes_received;
        }
      } else {
        const fetched = await window.__dsx__._fetchAndDownload(
          f.signed_path, f.file_size, listOnly, f.file_name);
        f.downloaded = fetched.downloaded;
        f.bytes_received = fetched.bytes_received;
        if (fetched.error) f.error = fetched.error;
        // Give Firefox a beat between downloads so it
        // registers each one individually instead of
        // coalescing them into a "Save As" prompt storm.
        if (!listOnly && fetched.downloaded) {
          await new Promise(r => setTimeout(r, 200));
        }
      }
    }

    result.ok = true;
    result.title = window.__dsx__.getChatTitle();
    result.url = window.location.href;
    return result;
  }

  window.__dsx__.doExtractAttachments = doExtractAttachments;

  // ---------- bridge entry point ----------------------------------
  window.__dsx_extract_attachments_json = async function (argsJson) {
    let args = {};
    try {
      if (typeof argsJson === "string" && argsJson.length > 0) {
        args = JSON.parse(argsJson);
      }
    } catch (e) {
      return JSON.stringify({
        success: false, ok: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    try {
      const result = await doExtractAttachments(args);
      console.log(
        `[deeppeek:attachments] mode=${args.mode || "focus"} ` +
        `${(result.files || []).length} file(s) ` +
        `(${(result.files || []).filter(f => !f.error).length} ok)`);
      return JSON.stringify(result);
    } catch (e) {
      console.error("[deeppeek:attachments] threw:", e);
      return JSON.stringify({
        success: false, ok: false,
        reason: `extract threw: ${e.message}`,
      });
    }
  };
})();