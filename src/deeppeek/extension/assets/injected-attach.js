// =====================================================================
// injected-attach.js — DOM-based file attachment (STABILITY TIER 2)
// =====================================================================
//
// Places File objects into the DeepSeek composer:
//
//   primary  — dragover + drop on textarea.parentElement
//   fallback — input.files = DataTransfer, dispatch change+input
//
// Confirmed against DeepSeek's current DOM.  Synthetic paste is
// blocked by Firefox's Xray wall and is not used.
//
// Stability: MODERATE.  The drop target has been stable for months.
// The file-input fallback is more fragile.  The overall contract
// ("given N File objects, place them in the composer") is unlikely
// to change.
//
// Exports on `window.__dsx__`:
//
//   doAttach(filesPayload, opts) → {success, method, dispatched, ...}
//   attachOnce / attachViaDrop / attachViaFileInput (retry caller)
//   findTextarea / findFileInput / blobCount
//
// Bridge entry points (defined on `window`):
//
//   window.__dsx_attach(...)           — debug only (object args)
//   window.__dsx_attach_json(argsJson) — content-script bridge
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  function base64ToFile(b64, name, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: mime || "application/octet-stream" });
  }

  function findTextarea() {
    const active = document.activeElement;
    if (active && active.tagName === "TEXTAREA") {
      const r = active.getBoundingClientRect();
      if (r.width > 100 && r.height > 20) return active;
    }
    return [...document.querySelectorAll("textarea")].find((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 100 && r.height > 20;
    }) || null;
  }

  function findFileInput() {
    return document.querySelector('input[type="file"]');
  }

  function blobCount() {
    return document.querySelectorAll('img[src^="blob:"]').length;
  }

  async function attachViaDrop(files) {
    const textarea = findTextarea();
    if (!textarea || !textarea.parentElement) {
      return { success: false, reason: "no textarea parent" };
    }
    const target = textarea.parentElement;

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    if (dt.files.length !== files.length) {
      return { success: false, reason: `DataTransfer only holds ${dt.files.length}/${files.length}` };
    }

    try {
      const over = new DragEvent("dragover", {
        dataTransfer: dt, bubbles: true, cancelable: true,
      });
      target.dispatchEvent(over);
      if (!over.defaultPrevented) {
        return { success: false, reason: "dragover not accepted by target" };
      }
    } catch (e) {
      return { success: false, reason: `dragover threw: ${e.message}` };
    }

    try {
      const drop = new DragEvent("drop", {
        dataTransfer: dt, bubbles: true, cancelable: true,
      });
      target.dispatchEvent(drop);
      if (!drop.defaultPrevented) {
        return { success: false, reason: "drop not accepted by target" };
      }
      return { success: true, method: "drop", dispatched: files.length };
    } catch (e) {
      return { success: false, reason: `drop threw: ${e.message}` };
    }
  }

  async function attachViaFileInput(files) {
    const input = findFileInput();
    if (!input) return { success: false, reason: "no file input on page" };

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);

    try {
      input.files = dt.files;
    } catch (e) {
      return { success: false, reason: `input.files assignment threw: ${e.message}` };
    }
    if (input.files.length !== files.length) {
      return {
        success: false,
        reason: `input.files=${input.files.length} but files=${files.length}`,
      };
    }

    try {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (e) {
      return { success: false, reason: `dispatch threw: ${e.message}` };
    }

    return { success: true, method: "file-input", dispatched: files.length };
  }

  async function attachOnce(files, skipPreviewWait) {
    const previewsBefore = blobCount();
    let result = await attachViaDrop(files);
    if (!result.success) {
      console.log("[deeppeek:attach] drop failed, trying file-input:", result.reason);
      const fallback = await attachViaFileInput(files);
      if (fallback.success) result = fallback;
      else result = {
        success: false,
        reason: `drop: ${result.reason}; file-input: ${fallback.reason}`,
      };
    }

    if (result.success && !skipPreviewWait) {
      await new Promise((r) => setTimeout(r, 800));
      const previewsAfter = blobCount();
      result.previews_before = previewsBefore;
      result.previews_after = previewsAfter;
      result.previews_added = previewsAfter - previewsBefore;
    } else if (result.success) {
      result.previews_before = previewsBefore;
      result.previews_after = previewsBefore;
      result.previews_added = 0;
      result.skipped_preview_wait = true;
    }

    return result;
  }

  async function doAttach(filesPayload, opts) {
    const delayMs = Math.max(0, Number(opts && opts.delay_ms) || 0);
    const skipPreviewWait = !!(opts && opts.skip_preview_wait);

    let files;
    try {
      files = filesPayload.map((f) =>
        base64ToFile(f.base64, f.name, f.mime)
      );
    } catch (e) {
      return { success: false, reason: `base64 decode failed: ${e.message}` };
    }

    if (delayMs === 0) {
      const result = await attachOnce(files, skipPreviewWait);
      return { ...result, mode: "batch", total: files.length };
    }

    let successCount = 0;
    let lastMethod = null;
    let lastReason = null;
    const previewsBefore = blobCount();
    for (let i = 0; i < files.length; i++) {
      if (i > 0) await new Promise((r) => setTimeout(r, delayMs));
      const one = await attachOnce([files[i]]);
      if (one.success) {
        successCount++;
        lastMethod = one.method;
      } else {
        lastReason = `${files[i].name}: ${one.reason}`;
      }
    }
    await new Promise((r) => setTimeout(r, 800));
    const previewsAfter = blobCount();
    return {
      success: successCount === files.length,
      mode: "serial",
      method: lastMethod,
      total: files.length,
      dispatched: successCount,
      previews_before: previewsBefore,
      previews_after: previewsAfter,
      previews_added: previewsAfter - previewsBefore,
      reason: successCount < files.length ? lastReason : undefined,
    };
  }

  window.__dsx__.doAttach = doAttach;
  window.__dsx__.attachOnce = attachOnce;
  window.__dsx__.attachViaDrop = attachViaDrop;
  window.__dsx__.attachViaFileInput = attachViaFileInput;
  window.__dsx__.findTextarea = findTextarea;
  window.__dsx__.findFileInput = findFileInput;
  window.__dsx__.blobCount = blobCount;

  // Bridge entry points (defined on `window` for content.js Xray).
  window.__dsx_attach = async function (filesPayload, opts) {
    const result = await doAttach(filesPayload, opts || {});
    console.log("[deeppeek:attach] result:", result);
    return JSON.stringify(result);
  };

  window.__dsx_attach_json = async function (argsJson) {
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (e) {
      return JSON.stringify({
        success: false,
        reason: `arg parse failed: ${e.message}`,
      });
    }
    const files = Array.isArray(args.files) ? args.files : [];
    const opts = (args.opts && typeof args.opts === "object") ? args.opts : {};
    const result = await doAttach(files, opts);
    console.log("[deeppeek:attach] result:", result);
    return JSON.stringify(result);
  };
})();