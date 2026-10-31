// =====================================================================
// injected-hijack.js — download-button interception (TIER 3, VOLATILE)
// =====================================================================
//
// Intercepts code-block "Download" button clicks before React's
// handler runs, extracts the block's text content, and POSTs
// {filename, base64} to the CLI's /export endpoint.  The CLI writes
// the file to its cwd instead of the browser's Downloads directory.
//
// Only `.md-code-block` blocks are hijacked.  File-chip downloads
// use a different UI path and are out of scope.
//
// Stability: LOW.  Two independent surfaces here are both volatile:
//
//   · The click interceptor (`_hijackClickHandler`) depends on
//     DeepSeek's DOM, on `composedPath()` behavior, and on the
//     `.ds-button` / `.md-code-block` selectors.
//
//   · The filename derivation chain (four fallback strategies,
//     ported from the recover pipeline's heuristics) is
//     continuously tuned.  Each strategy has its own trade-off
//     between tolerance and false positives.
//
// Depends on: nothing.  Fully self-contained.
//
// Bridge entry points (defined on `window`):
//
//   window.__dsx_hijack_install_json(argsJson)
//   window.__dsx_hijack_uninstall_json(argsJson)
//   window.__dsx_hijack_status_json(argsJson)
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};

  // The local CLI's HTTP server.  Defined here because background.js's
  // DSX_URL is in a different scope; only _postDownload uses it.
  const DSX_URL = "http://127.0.0.1:8765";

  // ---- language label → file extension -------------------------
  const DOWNLOAD_EXT_MAP = {
    js: 'js', javascript: 'js', jsx: 'jsx', mjs: 'mjs', cjs: 'cjs',
    ts: 'ts', typescript: 'ts', tsx: 'tsx',
    py: 'py', python: 'py', pyi: 'pyi',
    sh: 'sh', bash: 'sh', zsh: 'sh', shell: 'sh',
    rb: 'rb', ruby: 'rb', go: 'go', rs: 'rs', rust: 'rs',
    java: 'java', kt: 'kt', kotlin: 'kt', scala: 'scala',
    groovy: 'groovy', c: 'c', cpp: 'cpp', 'c++': 'cpp', cc: 'cc',
    cxx: 'cxx', h: 'h', hpp: 'hpp',
    php: 'php', pl: 'pl', perl: 'pl', lua: 'lua',
    swift: 'swift', dart: 'dart', m: 'm',
    html: 'html', htm: 'html', css: 'css', scss: 'scss',
    sass: 'sass', less: 'less',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    ini: 'ini', cfg: 'cfg', xml: 'xml', svg: 'svg', csv: 'csv',
    md: 'md', markdown: 'md', rst: 'rst', tex: 'tex',
    sql: 'sql', graphql: 'graphql', gql: 'graphql',
    proto: 'proto', vue: 'vue', svelte: 'svelte',
    dockerfile: 'Dockerfile', makefile: 'Makefile',
    diff: 'diff', patch: 'patch',
    txt: 'txt', text: 'txt', plaintext: 'txt', log: 'log',
  };

  // ---- timestamp + shebang sniffing -----------------------------
  function _downloadTimestamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
         + `_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  function _sniffExtFromContent(text, defaultExt) {
    const first = (text.split('\n')[0] || '').trim();
    if (first.startsWith('#!')) {
      if (/bash|sh|zsh/.test(first)) return 'sh';
      if (/python/.test(first))     return 'py';
      if (/node/.test(first))       return 'js';
      if (/ruby/.test(first))       return 'rb';
      if (/perl/.test(first))       return 'pl';
    }
    return defaultExt;
  }

  // ================================================================
  // Filename resolution — ports the recover pipeline's four
  // strategies, chained in the same priority order used by
  // recover.py:
  //
  //   1. Header parse      — recover/extract._parse_file_header
  //   2. Prose single     — extract._SINGLE_FILENAME_RE fallback
  //   3. Prose backtick   — attribute._files_mentioned_in
  //   4. Body comment     — prose_filter.filename_in_orphan_header
  // ================================================================

  const _SOURCE_EXT_PATTERN =
    'py|js|mjs|cjs|jsx|ts|tsx|md|json|yaml|yml|toml|sh|bash|zsh|' +
    'html|htm|css|scss|sass|less|rs|go|rb|php|java|kt|kts|scala|' +
    'c|cc|cpp|cxx|h|hpp|swift|dart|m|sql|graphql|gql|proto|vue|' +
    'svelte|xml|svg|csv|ini|cfg|conf|env|txt|log';

  const _FILENAME_RE = new RegExp(
    '\\b([A-Za-z0-9_./\\-]+\\.(?:' + _SOURCE_EXT_PATTERN + '))\\b',
    'i'
  );
  const _FILENAME_RE_G = new RegExp(
    '\\b([A-Za-z0-9_./\\-]+\\.(?:' + _SOURCE_EXT_PATTERN + '))\\b',
    'gi'
  );

  function _isSourceExt(ext) {
    return new RegExp(`^(${_SOURCE_EXT_PATTERN})$`, 'i')
      .test(ext || '');
  }

  function _parseFileHeaderText(text) {
    // Direct port of recover/extract._parse_file_header.
    let s = (text || '').trim();
    if (!s) return null;

    // Strip markdown-link wrapper: [text](url) -> text
    const linkMatch = s.match(/^\[([^\]]+)\]\([^)]+\)\s*$/);
    if (linkMatch) s = linkMatch[1].trim();
    s = s.replace(/[*`]/g, '');

    // "File: foo.py" / "path: foo.py" / "source: foo.py (mod)"
    const m1 = s.match(
      /^(?:file|path|source)\s*:\s*([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]+)(?:\s*(?:\(([^)]+)\)|—(.+?)|-(.+?)))?\s*$/i
    );
    if (m1) {
      return {
        path: m1[1],
        modifier: (m1[2] || m1[3] || m1[4] || '').trim() || null,
      };
    }

    // "== foo.py =="
    const m2 = s.match(/^==+\s*(\S+)\s*==+$/);
    if (m2) return { path: m2[1], modifier: null };

    // "[foo.py]"
    const m3 = s.match(/^\[([^\]]+)\]$/);
    if (m3 && m3[1].includes('.')) return { path: m3[1], modifier: null };

    // "1. foo.py — modifier" / "foo.py (mod)" / "foo.py — mod"
    const m4 = s.match(
      /^(?:(?:\d+)\s*[.):\-]\s*)?([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,6})\s*(?:(?:—|–|\s-\s|\(|\[)\s*(.+?)\s*(?:\)|\])?)?\s*$/
    );
    if (m4) {
      const ext = m4[1].split('.').pop();
      if (_isSourceExt(ext)) {
        return {
          path: m4[1],
          modifier: (m4[2] || '').trim() || null,
        };
      }
    }

    // Bare filename
    const m5 = s.match(/^([A-Za-z0-9_./\-]+\.[a-zA-Z0-9]{1,5})$/);
    if (m5) {
      const ext = m5[1].split('.').pop();
      if (_isSourceExt(ext) || m5[1].includes('/')) {
        return { path: m5[1], modifier: null };
      }
    }

    // Natural-language fallback: exactly one source-extension
    // filename anywhere in the text.
    const matches = s.match(_FILENAME_RE_G);
    if (matches && matches.length === 1) {
      return { path: matches[0], modifier: null };
    }

    return null;
  }

  function _collectPrecedingProse(block, maxItems) {
    // Reproduces recover/extract.pass1_extract's
    // `current_context_lines` buffering, at DOM granularity: walk
    // the block's previousElementSibling chain up to (and not
    // past) the previous code block, collecting element innerText.
    // Return in document order (earliest first).
    const parts = [];
    let node = block.previousElementSibling;
    let count = 0;
    const limit = maxItems || 4;
    while (node && count < limit) {
      if (node.classList &&
          (node.classList.contains('md-code-block') ||
           node.classList.contains('md-code-block-banner-wrap'))) {
        break;
      }
      const txt = (node.innerText || '').trim();
      if (txt) {
        parts.unshift(txt);
        count++;
      }
      node = node.previousElementSibling;
    }
    return parts;
  }

  function _extractFilenameFromBodyComment(text) {
    // Direct port of recover/prose_filter.filename_in_orphan_header,
    // with the additional "must be a comment line" guard so a
    // statement like `print("foo.py")` doesn't trigger the header.
    const lines = (text || '').split('\n').slice(0, 5);
    const scanned = lines.join('\n');
    const m = scanned.match(_FILENAME_RE);
    if (!m) return null;

    for (const line of lines) {
      if (!line.includes(m[1])) continue;
      const trimmed = line.trim();
      const isComment =
        trimmed.startsWith('//') ||
        trimmed.startsWith('#') ||
        trimmed.startsWith('/*') ||
        trimmed.startsWith('*') ||
        trimmed.startsWith('<!--') ||
        trimmed.startsWith('--');
      if (!isComment) return null;
      break;
    }
    return m[1];
  }

  function _deriveFilenameFromContext(block, bodyText) {
    const preceding = _collectPrecedingProse(block, 4);

    // 1. Header parse, closest-to-block first.
    for (let i = preceding.length - 1; i >= 0; i--) {
      const parsed = _parseFileHeaderText(preceding[i]);
      if (parsed && parsed.path) {
        return { path: parsed.path, source: 'prose-header' };
      }
    }

    // 2. Single source filename anywhere in the prose.
    if (preceding.length) {
      const allProse = preceding.join('\n\n');
      const matches = allProse.match(_FILENAME_RE_G) || [];
      const unique = Array.from(new Set(matches));
      if (unique.length === 1) {
        return { path: unique[0], source: 'prose-single' };
      }

      // 3. Backtick mentions in the CLOSEST prose only.
      const closest = preceding[preceding.length - 1] || '';
      const backtickFiles = [];
      for (const m of closest.matchAll(/`([^`\n]+)`/g)) {
        const content = (m[1] || '').trim();
        if (_FILENAME_RE.test(content)) backtickFiles.push(content);
      }
      if (backtickFiles.length) {
        return {
          path: backtickFiles[backtickFiles.length - 1],
          source: 'prose-backtick',
        };
      }
    }

    // 4. Body comment header.
    const bodyHeader = _extractFilenameFromBodyComment(bodyText);
    if (bodyHeader) return { path: bodyHeader, source: 'body-comment' };

    return null;
  }

  function _deriveDownloadFilename(block, langLabel, text) {
    const declared = _deriveFilenameFromContext(block, text);
    if (declared && declared.path) {
      return declared;
    }
    const rawLang = (langLabel || '').toLowerCase().trim();
    let ext = DOWNLOAD_EXT_MAP[rawLang] || 'txt';
    ext = _sniffExtFromContent(text || '', ext);
    const safeLang = rawLang
      ? (rawLang.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'code')
      : 'code';
    return {
      path: `${safeLang}_${_downloadTimestamp()}.${ext}`,
      source: 'lang-fallback',
    };
  }

  function _extractLangLabel(block) {
    const banner = block.querySelector('.md-code-block-banner');
    if (!banner) return '';
    const first = ((banner.innerText || '').split('\n')[0] || '').trim();
    if (!first) return '';
    if (/^(开头|结尾|Copy|Download)$/i.test(first)) return '';
    if (/^[A-Za-z0-9_+.+#-]+$/.test(first)) return first;
    return '';
  }

  function _extractCodeText(block) {
    // textContent, not innerText — innerText collapses whitespace
    // and would mangle Python/YAML/JavaScript indentation.
    const code = block.querySelector('code') || block.querySelector('pre');
    return code ? (code.textContent || '') : '';
  }

  function _bytesToBase64(bytes) {
    let binary = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  // MODIFIED (2026-11-05): `lang` is now forwarded in the payload
  // so the CLI can use the correct parser when it needs to fall
  // back to structural derivation of the filename.  When the
  // extension's prose-based derivation succeeded, `source` will be
  // one of prose-header / prose-single / prose-backtick /
  // body-comment and the CLI uses the filename as-is.  When the
  // extension fell through to `lang-fallback` (timestamped name),
  // the CLI runs structural analysis on the decoded bytes with the
  // correct language hint and derives the real filename from the
  // module docstring / leading comment.
  async function _postDownload(filename, text, source, lang) {
    const bytes = new TextEncoder().encode(text);
    const base64 = _bytesToBase64(bytes);
    try {
      const resp = await fetch(`${DSX_URL}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          __ack: 'download',
          filename,
          base64,
          source: source || 'code-block',
          lang: lang || '',
          size: bytes.length,
        }),
      });
      return resp.ok;
    } catch (e) {
      console.warn('[deeppeek:hijack] POST failed:', e);
      return false;
    }
  }

  function _isDownloadButton(btn) {
    const span = btn.querySelector('.code-info-button-text');
    const text = ((span ? span.innerText : btn.innerText) || '')
      .trim().toLowerCase();
    return text === 'download' || text === '下载' || text === 'descargar';
  }

  function _hijackClickHandler(ev) {
    const path = ev.composedPath ? ev.composedPath() : [];
    let btn = null;
    for (const el of path) {
      if (el && el.classList && el.classList.contains('ds-button')) {
        btn = el;
        break;
      }
    }
    if (!btn || !_isDownloadButton(btn)) return;

    const block = btn.closest('.md-code-block');
    if (!block) return;
    if (!block.querySelector('.md-code-block-banner')) return;

    // Suppress the default browser download.
    ev.preventDefault();
    ev.stopPropagation();
    ev.stopImmediatePropagation();

    const langLabel = _extractLangLabel(block);
    const text = _extractCodeText(block);
    if (!text) {
      console.warn('[deeppeek:hijack] empty code block, nothing to send');
      return;
    }

    const derived = _deriveDownloadFilename(block, langLabel, text);
    console.log(
      `[deeppeek:hijack] ${derived.path} (${text.length} chars, `
      + `lang=${langLabel || '?'}, source=${derived.source})`);

    // MODIFIED (2026-11-05): pass langLabel through so the CLI can
    // run structural derivation on lang-fallback blocks.
    _postDownload(derived.path, text, derived.source, langLabel).then((ok) => {
      if (!ok) {
        console.warn(
          '[deeppeek:hijack] CLI not reachable — file NOT saved. '
          + 'Is `deeppeek hijack` running?');
      }
    });
  }

  function _installDownloadHijacker() {
    if (window.__dsx_hijack_installed) {
      return { success: true, already_installed: true };
    }
    window.__dsx_hijack_installed = true;
    window.__dsx_hijack_handler = _hijackClickHandler;
    document.addEventListener('click', _hijackClickHandler, true);
    console.log('[deeppeek:hijack] installed');
    return { success: true };
  }

  function _uninstallDownloadHijacker() {
    if (!window.__dsx_hijack_installed) {
      return { success: true, was_installed: false };
    }
    document.removeEventListener(
      'click', window.__dsx_hijack_handler, true);
    window.__dsx_hijack_installed = false;
    window.__dsx_hijack_handler = null;
    console.log('[deeppeek:hijack] uninstalled');
    return { success: true, was_installed: true };
  }

  window.__dsx_hijack_install_json = async function (_argsJson) {
    try {
      const r = _installDownloadHijacker();
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `install threw: ${e.message}` });
    }
  };

  window.__dsx_hijack_uninstall_json = async function (_argsJson) {
    try {
      const r = _uninstallDownloadHijacker();
      return JSON.stringify(r);
    } catch (e) {
      return JSON.stringify({
        success: false, reason: `uninstall threw: ${e.message}` });
    }
  };

  window.__dsx_hijack_status_json = async function (_argsJson) {
    return JSON.stringify({
      success: true,
      installed: !!window.__dsx_hijack_installed,
    });
  };
})();