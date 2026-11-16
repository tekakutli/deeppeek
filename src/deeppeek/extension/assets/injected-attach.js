// =====================================================================
// injected-attach.js — DOM-based file attachment (STABILITY TIER 2)
// =====================================================================
//
// READY-WAIT (2026-11-15, fourth iteration)
// =========================================
// Previous versions waited 3000ms (chip floor) or 30s (broken button
// detection that got stuck on a disabled decoy).  This version uses
// `waitComposerReady` from injected.js, which returns on the FIRST
// of: any send-button enabled, no button found past floor, or
// extended grace elapsed.
//
// Key property: never hangs.  Worst case is bounded by the extended
// grace (floor + 3s ≈ 3.5s) when button detection is dead, or by
// --upload-timeout (30s) only when buttons exist but no chip appears
// (a genuine error).
//
// STRICT MODE (unchanged from previous)
// =====================================
// `opts.strict_composer` uses boundComposer resolution.  Drop target
// is the resolved composer's parentElement.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  function base64ToFile(b64, name, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name,
                    { type: mime || "application/octet-stream" });
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

  function _resolveTextarea(strict) {
    if (!strict) return findTextarea();
    if (typeof window.__dsx__.resolveTargetComposer === "function") {
      const r = window.__dsx__.resolveTargetComposer({
        strict_composer: true,
      });
      return r.el || null;
    }
    return findTextarea();
  }

  function findFileInput() {
    return document.querySelector('input[type="file"]');
  }

  function blobCount() {
    return document.querySelectorAll('img[src^="blob:"]').length;
  }

  async function attachViaDrop(files, strict) {
    const textarea = _resolveTextarea(strict);
    if (!textarea || !textarea.parentElement) {
      return {
        success: false,
        reason: strict
          ? "strict composer not found (no drop target)"
          : "no textarea parent",
      };
    }
    const target = textarea.parentElement;

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    if (dt.files.length !== files.length) {
      return {
        success: false,
        reason: `DataTransfer only holds ` +
                `${dt.files.length}/${files.length}`,
      };
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
      return {
        success: true, method: "drop", dispatched: files.length,
      };
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
      return {
        success: false,
        reason: `input.files assignment threw: ${e.message}`,
      };
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

    return {
      success: true, method: "file-input", dispatched: files.length,
    };
  }

  async function attachOnce(files, skipPreviewWait, strict) {
    const previewsBefore = blobCount();
    let result = await attachViaDrop(files, strict);
    if (!result.success) {
      console.log(
        "[deeppeek:attach] drop failed, trying file-input:", result.reason);
      const fallback = await attachViaFileInput(files);
      if (fallback.success) result = fallback;
      else result = {
        success: false,
        reason: `drop: ${result.reason}; file-input: ${fallback.reason}`,
      };
    }

    if (result.success && !skipPreviewWait) {
      await new Promise((r) => setTimeout(r, 400));
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
    const strict = !!(opts && opts.strict_composer);
    const waitReady = !!(opts && opts.wait_upload);

    let files;
    try {
      files = filesPayload.map((f) =>
        base64ToFile(f.base64, f.name, f.mime)
      );
    } catch (e) {
      return {
        success: false,
        reason: `base64 decode failed: ${e.message}`,
      };
    }

    let result;
    if (delayMs === 0) {
      result = await attachOnce(files, skipPreviewWait, strict);
      result.mode = "batch";
      result.total = files.length;
    } else {
      let successCount = 0;
      let lastMethod = null;
      let lastReason = null;
      const previewsBefore = blobCount();
      for (let i = 0; i < files.length; i++) {
        if (i > 0) await new Promise((r) => setTimeout(r, delayMs));
        const one = await attachOnce([files[i]], false, strict);
        if (one.success) {
          successCount++;
          lastMethod = one.method;
        } else {
          lastReason = `${files[i].name}: ${one.reason}`;
        }
      }
      await new Promise((r) => setTimeout(r, 400));
      const previewsAfter = blobCount();
      result = {
        success: successCount === files.length,
        mode: "serial", method: lastMethod, total: files.length,
        dispatched: successCount,
        previews_before: previewsBefore,
        previews_after: previewsAfter,
        previews_added: previewsAfter - previewsBefore,
        reason: successCount < files.length ? lastReason : undefined,
      };
    }

    // ---- ready-wait ----
    if (result.success && waitReady) {
      const timeoutMs = Number(opts.wait_upload_timeout_ms) || 30000;
      const floorMs = Number(opts.wait_upload_min_ms) || 500;
      const composerEl = _resolveTextarea(strict);

      let wait;
      if (typeof window.__dsx__.waitComposerReady === "function") {
        console.log(
          `[deeppeek:attach] waiting for composer ready ` +
          `(timeout=${timeoutMs}ms, floor=${floorMs}ms)...`);
        wait = await window.__dsx__.waitComposerReady({
          timeout_ms: timeoutMs,
          floor_ms: floorMs,
          extended_grace_ms: 3000,
          poll_ms: 100,
          composer_el: composerEl,
          require_chip: true,
        });
      } else {
        // Very old page-world, use chip-only fallback.
        wait = await window.__dsx__.waitUploadComplete({
          timeout_ms: timeoutMs,
          min_wait_ms: Math.max(floorMs, 1000),
          poll_ms: 200,
        });
        wait.method = "chip";
      }

      result.upload_wait = wait;
      if (!wait.success) {
        result.success = false;
        result.reason = wait.reason || "composer not ready";
        console.warn(
          `[deeppeek:attach] ready-wait failed: ${wait.reason}`);
      } else {
        console.log(
          `[deeppeek:attach] ready after ${wait.waited_ms}ms ` +
          `(method=${wait.method})`);
      }
    }

    return result;
  }

  window.__dsx__.doAttach = doAttach;
  window.__dsx__.attachOnce = attachOnce;
  window.__dsx__.attachViaDrop = attachViaDrop;
  window.__dsx__.attachViaFileInput = attachViaFileInput;
  window.__dsx__.findTextarea = findTextarea;
  window.__dsx__.findFileInput = findFileInput;
  window.__dsx__.blobCount = blobCount;

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
    const opts = (args.opts && typeof args.opts === "object")
      ? args.opts : {};
    const result = await doAttach(files, opts);
    console.log("[deeppeek:attach] result:", result);
    return JSON.stringify(result);
  };
})();