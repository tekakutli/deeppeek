// =====================================================================
// button-rules/02-continue-btn.js — "Resume" button
// =====================================================================
//
// INJECTION POINT
// ---------------
//   Where:   START of the wrapper <div> around the "Continue" button.
//   Which:   any DIV whose direct children include a
//            `.ds-button[role="button"]` with textContent "Continue".
//   Verify:  __dsx.buttons.debug(true) → teal outline on the row,
//            "continue-btn" label above the clone.
//
// WHAT IT DOES ON CLICK
// ---------------------
//   Phase 1 (current tab):
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
//     · the resume-worker IIFE at the top of this file claims the
//       pending payload (extension storage first, localStorage
//       fallback), waits for the composer, attaches conversation +
//       seed files, types the resume text
//
// CONTAINER SELECTION — TWO MODES
// -------------------------------
//   Plain click   → opens in the SAME container the current tab is
//                   in.  Detected at click time via
//                   callBridge(DSX_CURRENT_CONTAINER), which uses
//                   browser.tabs.getCurrent() inline in the content
//                   script (no background hop, no query, no hang).
//                   This is the "just resume where I am" default.
//
//   Shift-click   → opens a container picker.  Two sub-modes:
//                     · list available → shows every container from
//                       browser.contextualIdentities.query
//                     · list unavailable → manual input for a
//                       container name ("Work") or raw
//                       cookieStoreId ("firefox-container-3")
//                   Picks are one-shot: no persistence.  Next plain
//                   click resumes in the current container again.
//
//   No preference is stored between clicks.  The user's original
//   complaint — "plain click opens in the last picked container" —
//   is fixed by never reading a stored preference on the plain path.
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
//   Content script (see content.js's installPostMessageBridge)
//   listens for `{dsx_req, msg}` and replies with
//   `{dsx_resp, payload: <json string>}`.  Both directions are
//   strings/plain-objects; no Xray involvement.
//
// LOADED BY
// ---------
//   content.js's injectButtonRules() via button-rules/_manifest.json.
//   Runs after injected-buttons.js.
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

  const CHAT_URL = "https://chat.deepseek.com/";

  // Firefox/Zen cookieStoreIds look like `firefox-container-N` (or
  // `<prefix>-container-N`).  Anything matching this shape is
  // forwarded to the background as a raw `container_id` (no name
  // resolution, no query call); anything else is treated as a
  // display name and resolved by the background.
  const CONTAINER_ID_RE = /^[a-z0-9]+-container-\d+$/i;

  // ---- cross-realm bridge via postMessage -----------------------
  //
  // Sends `{dsx_req, msg}` on `window`; content.js's listener
  // replies with `{dsx_resp: <same reqId>, payload: <json string>}`.
  // Resolves with the parsed payload object, or throws on timeout.
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

  // ---- current-container detection ------------------------------
  //
  // Detects the container the current tab is in, so plain-click
  // can open the new tab in the same container.  Returns:
  //   null                                       → default container
  //   { cookieStoreId: "firefox-container-N", name? } → specific
  //
  // Fails safe: on any bridge error, returns null (default
  // container).  The cross-container payload channel (extension
  // storage) still carries the resume payload, so the resume lands
  // — just in the default container rather than the current one.
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
        `[DSX:continue-btn] could not detect current container: ` +
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
  //     container name ("Work") or a raw cookieStoreId
  //     ("firefox-container-3"), so the button still works when the
  //     container API is broken
  //   · "Cancel"
  //
  // `currentCid` is used ONLY to highlight the row the user is
  // currently in — not to persist a default.  Callers pass the
  // detected current-tab container's cookieStoreId (or null for
  // default).
  //
  // Resolves with:
  //   { action: "pick", cookieStoreId?, name? }  → one-shot pick
  //   null                                        → cancel, abort
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
          `[DSX:continue-btn] attachment fetch failed (proceeding ` +
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

  const runResumePhase2 = async (payload) => {
    const ready = await waitForComposer(15000);
    if (!ready) {
      throw new Error(
        "composer did not appear within 15s — the new chat may not " +
        "have loaded correctly");
    }
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

  // Resume worker — runs on every page load.  Claims a pending
  // payload from extension storage first (cross-container), then
  // falls back to localStorage (same-container).
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

  // ---- open the new tab ----------------------------------------
  //
  // Argument shapes:
  //   null                → browser default (no container args)
  //   { cookieStoreId }   → raw ID, forwarded as container_id
  //   { name }            → forwarded as container, resolved by bg
  //
  // Callers always pass an explicit pref (never undefined) — no
  // localStorage read on this path.  Plain click passes the
  // detected current-tab container; shift-click passes the picker
  // result.  Both are one-shot.
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
    decorate: (clone) => {
      clone.setAttribute(
        "title",
        `[DSX] continue-btn — Resume in the current container ` +
        `(shift-click to pick a different container)`);
    },
    onClick: async (e, el) => {
      const prevPe = el.style.pointerEvents;
      const prevOp = el.style.opacity;
      const setBusy = (on) => {
        el.style.pointerEvents = on ? "none" : prevPe;
        el.style.opacity = on ? "0.5" : prevOp;
      };
      setBusy(true);

      try {
        // ---- pick container: shift-click → user chooses;
        //      plain click → current container ----
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
            console.warn(
              `[DSX:continue-btn] container list unavailable ` +
              `(${listReason}) — falling back to manual entry`);
          }

          // Highlight the current tab's container in the picker so
          // the user sees "here's where I am" as a visual reference.
          const current = await detectCurrentContainer();
          const currentCid = current ? current.cookieStoreId : null;

          const picked = await showContainerPicker(
            containers,
            currentCid,
            { allowManual: !listAvailable },
          );
          if (!picked) {
            setBusy(false);
            return;
          }

          pickedCid = (picked.cookieStoreId || picked.name)
            ? { cookieStoreId: picked.cookieStoreId || null,
                name: picked.name || null }
            : null;
        } else {
          // Plain click — same container as the current tab.
          pickedCid = await detectCurrentContainer();
        }

        // ---- prepare payload ----
        console.log("[DSX:continue-btn] preparing resume payload...");
        const { payload, stats } = await prepareResumePayload();

        // ---- persist to localStorage (same-container channel) ----
        try {
          localStorage.setItem("dsx_resume_pending", JSON.stringify(payload));
        } catch (err) {
          console.warn(
            `[DSX:continue-btn] localStorage save failed: ` +
            `${err && err.message || err}`);
        }

        // ---- persist to extension storage (cross-container channel) ----
        let extStorageOk = false;
        try {
          const saveRes = await callBridge(
            { type: "DSX_RESUME_SAVE", payload }, 10000);
          if (saveRes && saveRes.success) {
            extStorageOk = true;
          } else {
            console.warn(
              `[DSX:continue-btn] extension-storage save failed: ` +
              `${(saveRes && saveRes.reason) || "unknown"}`);
          }
        } catch (err) {
          console.warn(
            `[DSX:continue-btn] extension-storage save threw: ` +
            `${err && err.message || err} — continuing with ` +
            `localStorage only (cross-container resume will not work)`);
        }

        console.log(
          `[DSX:continue-btn] payload saved ` +
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
        console.log(
          `[DSX:continue-btn] new tab opening via ${openRes.method}` +
          (openRes.cookie_store_id
            ? ` (container: ${openRes.cookie_store_id})`
            : "") +
          " — resume worker will attach + type there");
      } catch (err) {
        console.error(
          `[DSX:continue-btn] failed: ${err && err.message || err}`);
      } finally {
        setBusy(false);
      }
    },
  });
})();
