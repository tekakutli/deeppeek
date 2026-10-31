// =====================================================================
// injected-fetch.js — signed-URL byte fetching (STABILITY TIER 1)
// =====================================================================
//
// Every consumer that needs an attachment's bytes goes through this
// file: attachments (browser download), retry (base64 re-attach),
// resume (base64 to CLI).  One fetch implementation, so URL
// resolution, size validation, and error formatting cannot diverge
// between callers.
//
// The signedPath in DeepSeek's file metadata is a RELATIVE URL:
//
//   /file?file_id=<uuid>&state=<signed-token>
//
// Resolves to:
//
//   https://files.deepseeksvc.com/api + signedPath + "&ty=r"
//
// Verified by console probes: `state` is the sole authenticator,
// `file_id` is decorative, `ty=r` is required by CORS, tokens do
// not expire within a conversation's lifetime.  Every fetch is
// checked byte-for-byte against the metadata file_size; mismatch
// means the CDN served the SPA shell instead of the file (the
// silent failure mode that produced 8.5KB HTML files in early POCs).
//
// Exports on `window.__dsx__`:
//
//   _resolveFileUrl(signedPath)                → str | null
//   _guessMimeFromName(name)                   → str
//   _fetchBytes(signedPath, expectedSize)      → {buf, bytes_received, error}
//   _fetchAndDownload(sp, size, listOnly, nm)  → {downloaded, bytes_received, error}
//   _fetchAndEncode(sp, size)                  → {base64, bytes_received, error}
//   _normalizeFileRecord(rawFile, msg, idx, src)→ file-record dict
//   _fragFiles(frag)                           → File[] (only if frag.type=FILE)
//   _hasFiles(msg)                             → bool
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  const DSX_FILE_BASE = "https://files.deepseeksvc.com/api";

  function _resolveFileUrl(signedPath) {
    if (!signedPath) return null;
    let url = signedPath.startsWith("/")
      ? DSX_FILE_BASE + signedPath
      : signedPath;
    if (!url.includes("ty=")) {
      url += (url.includes("?") ? "&" : "?") + "ty=r";
    }
    return url;
  }

  function _guessMimeFromName(name) {
    const lower = (name || "").toLowerCase();
    const map = {
      ".md": "text/markdown", ".txt": "text/plain",
      ".json": "application/json", ".py": "text/x-python",
      ".js": "text/javascript", ".mjs": "text/javascript",
      ".ts": "text/typescript", ".html": "text/html",
      ".css": "text/css", ".yml": "text/yaml",
      ".yaml": "text/yaml", ".toml": "text/toml",
      ".xml": "text/xml", ".csv": "text/csv", ".sh": "text/x-sh",
      ".png": "image/png", ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg", ".gif": "image/gif",
      ".webp": "image/webp", ".svg": "image/svg+xml",
      ".pdf": "application/pdf", ".zip": "application/zip",
    };
    for (const ext in map) if (lower.endsWith(ext)) return map[ext];
    return "application/octet-stream";
  }

  async function _fetchBytes(signedPath, expectedSize) {
    const url = _resolveFileUrl(signedPath);
    if (!url) {
      return { buf: null, bytes_received: null,
               error: "no signedPath on file record" };
    }
    try {
      const resp = await fetch(url, { credentials: "omit" });
      if (!resp.ok) {
        return { buf: null, bytes_received: null,
                 error: `HTTP ${resp.status} ${resp.statusText}` };
      }
      const buf = await resp.arrayBuffer();
      if (expectedSize && buf.byteLength !== expectedSize) {
        const head = new Uint8Array(buf.slice(0, 48));
        const asText = new TextDecoder("utf-8", { fatal: false })
          .decode(head);
        const hex = Array.from(head.slice(0, 8))
          .map(b => b.toString(16).padStart(2, "0")).join(" ");
        return {
          buf: null,
          bytes_received: buf.byteLength,
          error: `size mismatch: expected ${expectedSize}, got ` +
                 `${buf.byteLength} (first bytes: ${hex} | ` +
                 `${JSON.stringify(asText.slice(0, 32))})`,
        };
      }
      return { buf, bytes_received: buf.byteLength, error: null };
    } catch (e) {
      return { buf: null, bytes_received: null,
               error: `fetch threw: ${e.message}` };
    }
  }

  // Consumer: browser download (used by `prompt attachments`).
  async function _fetchAndDownload(signedPath, expectedSize,
                                    listOnly, downloadName) {
    if (listOnly) {
      return { downloaded: false, bytes_received: null, error: null };
    }
    const r = await _fetchBytes(signedPath, expectedSize);
    if (r.error) {
      return { downloaded: false,
               bytes_received: r.bytes_received,
               error: r.error };
    }
    const blob = new Blob([r.buf]);
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = downloadName || "attachment";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      try { a.remove(); } catch (_) {}
      try { URL.revokeObjectURL(objUrl); } catch (_) {}
    }, 1500);
    return { downloaded: true,
             bytes_received: r.bytes_received, error: null };
  }

  // Consumer: base64 (used by `prompt retry` and `prompt resume`).
  async function _fetchAndEncode(signedPath, expectedSize) {
    const r = await _fetchBytes(signedPath, expectedSize);
    if (r.error) {
      return { base64: null, bytes_received: r.bytes_received,
               error: r.error };
    }
    const bytes = new Uint8Array(r.buf);
    let binary = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(i, i + CHUNK));
    }
    return { base64: btoa(binary),
             bytes_received: r.bytes_received, error: null };
  }

  // Normalise one raw file record (snake_case from IndexedDB,
  // camelCase from fiber drafts) to the shape every consumer expects.
  function _normalizeFileRecord(rawFile, msg, msgIdx, source) {
    const name = rawFile.file_name || rawFile.fileName || "(unnamed)";
    const size = rawFile.file_size || rawFile.fileSize || 0;
    const sp = rawFile.signed_path || rawFile.signedPath || null;
    return {
      message_id: (msg && msg.message_id != null)
                    ? msg.message_id : null,
      message_index: msgIdx,
      source,
      file_name: name,
      file_size: size,
      status: rawFile.status || "UNKNOWN",
      signed_path: sp,
      mime: _guessMimeFromName(name),
      base64: null,
      bytes_received: null,
      error: null,
    };
  }

  // A "FILE" fragment carries an array of file records.
  function _fragFiles(frag) {
    if (!frag || String(frag.type).toUpperCase() !== "FILE") return [];
    return Array.isArray(frag.files) ? frag.files : [];
  }

  function _hasFiles(msg) {
    const frags = msg.fragments || msg.content_blocks || [];
    return frags.some(f => _fragFiles(f).length > 0);
  }

  window.__dsx__._resolveFileUrl = _resolveFileUrl;
  window.__dsx__._guessMimeFromName = _guessMimeFromName;
  window.__dsx__._fetchBytes = _fetchBytes;
  window.__dsx__._fetchAndDownload = _fetchAndDownload;
  window.__dsx__._fetchAndEncode = _fetchAndEncode;
  window.__dsx__._normalizeFileRecord = _normalizeFileRecord;
  window.__dsx__._fragFiles = _fragFiles;
  window.__dsx__._hasFiles = _hasFiles;
})();