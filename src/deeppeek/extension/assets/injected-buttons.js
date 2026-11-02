// =====================================================================
// injected-buttons.js — deeppeek button injector (v4, framework only)
// =====================================================================
//
// AUTO-INJECTION FRAMEWORK.  Contains zero button definitions; the
// actual buttons live in `injected-button-rules.js`, which loads
// after this file and calls `window.__dsx__.buttons.rule({...})` per
// button type.
//
// PUBLIC API (on window.__dsx__.buttons):
//   rule(spec)          register an injection rule
//   amendRule(n, patch) live-patch an existing rule
//   unrule(n)           remove a rule and its clones
//   list()              rule names in registration order
//   status()            per-rule, per-row state
//   rescan()            force a pass now
//   reapply(n)          re-run decorate on all clones of a rule
//   debug(bool)         toggle debug overlay (persists via localStorage)
//   isDebug()           current debug state
//   resetState()        reset every rule's `_state` to its default
//   legend()            console-only colored legend
//
// A rule spec supports:
//   name       unique id
//   position   'start' | 'end' (default 'end')
//   target     selector string OR function returning row elements
//   filter     optional row predicate
//   template   element | selector | function returning element
//   onClick    (e, el, rule) => void       REQUIRED
//   state      { default: bool }           optional — enables rule._state
//   decorate   (clone, rule) => void       optional — runs on every clone
//                                          creation AND every rescan
//   whenActive [{selector, style}]         optional — layout hooks
//   color      '#rrggbb'                   optional — debug overlay color
//
// The framework keeps one shared MutationObserver on <html> and
// re-asserts every rule's clone with rAF debouncing.  Idempotent
// against its own DOM mutations (re-insert is a no-op when ordering
// matches).
//
// DEBUG OVERLAY (`__dsx.buttons.debug(true)`, persists in
// localStorage.dsx_buttons_debug):
//   · per-clone colored outline, colored via rule.color or the
//     default palette below
//   · floating rule-name label above each clone, rAF-repositioned
//   · bottom-right legend listing every rule with its color and
//     current clone count
//
// Hovering any injected clone shows a native tooltip with the rule
// name — always on, needs no debug mode.
//
// ESCAPE HATCH: localStorage.dsx_buttons_enabled='0' disables the
// whole injector until that key is removed and the page is reloaded.
(function () {
  "use strict";
  if (!window.__dsx__) window.__dsx__ = {};
  if (window.__dsx__.buttons) {
    console.log("[DSX:buttons] already loaded — call __dsx.buttons.rescan()");
    return;
  }

  try {
    if (localStorage.getItem("dsx_buttons_enabled") === "0") {
      console.log(
        "%c[DSX:buttons] disabled via localStorage.dsx_buttons_enabled='0'\n" +
        "  re-enable: localStorage.removeItem('dsx_buttons_enabled'); location.reload();",
        "color:orange;font-weight:bold");
      return;
    }
  } catch (_) {}

  const MARKER_ATTR = "data-dsx-injected";
  const DEBUG_KEY = "dsx_buttons_debug";
  const RULES = new Map();
  const TEMPLATE_CACHE = new Map();
  const _layoutApplied = new Map();

  const RULE_COLORS = {
    "default-capsule": "#FF6B6B",
    "continue-btn":    "#4ECDC4",
    "codeblock-copy":  "#FFE66D",
    "message-toolbar": "#A06CD5",
    "thought-scroll":  "#FF9F1C",
    "attach-btn":      "#4D9DE0",
    "toggle-btn":      "#E71D36",
  };
  const FALLBACK_COLOR = "#888888";
  const colorFor = (rule) =>
    rule.color || RULE_COLORS[rule.name] || FALLBACK_COLOR;

  const isMarked = (el) =>
    el && el.nodeType === 1 && el.hasAttribute(MARKER_ATTR);
  const findClone = (row, name) =>
    [...row.querySelectorAll(`[${MARKER_ATTR}]`)]
      .find(n => n.getAttribute(MARKER_ATTR) === name) || null;

  const rowsFor = (rule) => {
    if (typeof rule.target === "function") {
      try { return rule.target() || []; }
      catch (e) { console.error("[DSX:buttons] target threw:", e); return []; }
    }
    return [...document.querySelectorAll(rule.target)];
  };
  const rowPasses = (row, rule) => {
    if (!rule.filter) return true;
    try { return !!rule.filter(row); }
    catch (e) { console.error("[DSX:buttons] filter threw:", e); return false; }
  };
  const snapshotOf = (el) => {
    const s = el.cloneNode(true);
    s.removeAttribute("id");
    s.removeAttribute("data-testid");
    s.querySelectorAll(`[${MARKER_ATTR}]`)
     .forEach(n => n.removeAttribute(MARKER_ATTR));
    s.removeAttribute(MARKER_ATTR);
    return s;
  };
  const resolveTemplate = (rule) => {
    const t = rule.template;
    let live = null;
    if (typeof t === "function") {
      try { live = t(); }
      catch (e) { console.error("[DSX:buttons] template threw:", e); }
    } else if (typeof t === "string") {
      live = document.querySelector(t);
    } else if (t && t.nodeType === 1) {
      live = t;
    }
    if (live && isMarked(live) && live.getAttribute(MARKER_ATTR) === rule.name) {
      live = null;
    }
    if (live) TEMPLATE_CACHE.set(rule.name, snapshotOf(live));
    return TEMPLATE_CACHE.get(rule.name) || null;
  };
  const attachHandler = (el, rule) => {
    if (el.__dsxHandlerAttached) return;
    el.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      try { rule.onClick(e, el, rule); }
      catch (err) {
        console.error(`[DSX:buttons] onClick "${rule.name}" threw:`, err);
      }
    }, true);
    el.__dsxHandlerAttached = true;
  };
  const applyDecorate = (el, rule) => {
    if (typeof rule.decorate !== "function") return;
    try { rule.decorate(el, rule); }
    catch (err) {
      console.error(`[DSX:buttons] decorate "${rule.name}" threw:`, err);
    }
  };
  const ensureRow = (row, rule, tpl) => {
    if (!tpl) return false;
    let clone = findClone(row, rule.name);
    let changed = false;
    if (!clone) {
      clone = tpl.cloneNode(true);
      clone.setAttribute(MARKER_ATTR, rule.name);
      clone.setAttribute("title",
        `[DSX] ${rule.name} (${rule.position || "end"}) — click to fire`);
      changed = true;
    }
    attachHandler(clone, rule);
    applyDecorate(clone, rule);
    const starts = [], ends = [];
    for (const [name, r] of RULES) {
      const el = name === rule.name ? clone : findClone(row, name);
      if (!el) continue;
      ((r.position || "end") === "start" ? starts : ends).push(el);
    }
    const natives = [...row.children].filter(c => !isMarked(c));
    const desired = [...starts, ...natives, ...ends];
    const cur = [...row.children];
    if (!(cur.length === desired.length
          && cur.every((c, i) => c === desired[i]))) {
      for (let i = 0; i < desired.length; i++) {
        if (row.children[i] === desired[i]) continue;
        row.insertBefore(desired[i], row.children[i] || null);
      }
      changed = true;
    }
    return changed;
  };

  const kebab = (s) => s.replace(/[A-Z]/g, m => "-" + m.toLowerCase());
  const reconcileLayout = (rule) => {
    const hooks = rule.whenActive;
    if (!hooks || !hooks.length) return;
    let applied = _layoutApplied.get(rule.name);
    if (!applied) { applied = new Map(); _layoutApplied.set(rule.name, applied); }
    const active = document.querySelectorAll(
      `[${MARKER_ATTR}="${rule.name}"]`).length > 0;
    const desired = new Map();
    if (active) {
      for (const hook of hooks) {
        if (!hook.selector || !hook.style) continue;
        for (const el of document.querySelectorAll(hook.selector)) {
          let m = desired.get(el);
          if (!m) { m = new Map(); desired.set(el, m); }
          for (const [p, v] of Object.entries(hook.style)) m.set(kebab(p), v);
        }
      }
    }
    for (const [el, props] of desired) {
      let origs = applied.get(el);
      if (!origs) { origs = new Map(); applied.set(el, origs); }
      for (const [p, v] of props) {
        if (!origs.has(p)) origs.set(p, el.style.getPropertyValue(p));
        el.style.setProperty(p, v);
      }
    }
    for (const [el, origs] of applied) {
      const keep = desired.get(el);
      for (const [p, orig] of [...origs]) {
        if (keep && keep.has(p)) continue;
        if (orig) el.style.setProperty(p, orig);
        else el.style.removeProperty(p);
        origs.delete(p);
      }
      if (origs.size === 0) applied.delete(el);
    }
  };

  const rescan = () => {
    let changed = 0;
    for (const rule of RULES.values()) {
      const tpl = resolveTemplate(rule);
      if (!tpl) continue;
      for (const row of rowsFor(rule)) {
        if (!rowPasses(row, rule)) continue;
        if (ensureRow(row, rule, tpl)) changed++;
      }
    }
    for (const rule of RULES.values()) {
      if (rule.whenActive && rule.whenActive.length) reconcileLayout(rule);
    }
    if (changed && debugEnabled) debugRebuild();
    return changed;
  };

  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; rescan(); });
  };
  new MutationObserver(schedule).observe(document.documentElement, {
    childList: true, subtree: true,
  });

  // ---- debug overlay --------------------------------------------

  let debugEnabled = false;
  let debugOverlay = null;
  let debugStyleEl = null;
  let debugRafId = null;

  const _readDebug = () => {
    try { return localStorage.getItem(DEBUG_KEY) === "1"; }
    catch (_) { return false; }
  };
  const _writeDebug = (on) => {
    try {
      if (on) localStorage.setItem(DEBUG_KEY, "1");
      else localStorage.removeItem(DEBUG_KEY);
    } catch (_) {}
  };

  const debugStyle = () => {
    const parts = [];
    for (const [name, rule] of RULES) {
      const c = colorFor(rule);
      parts.push(
        `[${MARKER_ATTR}="${name}"]{outline:2px solid ${c} !important;` +
        `outline-offset:2px !important;}`
      );
    }
    return `/* deeppeek buttons debug */\n${parts.join("\n")}\n`;
  };

  const positionLabel = (lbl, clone) => {
    const r = clone.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) {
      lbl.style.display = "none";
      return;
    }
    lbl.style.display = "";
    lbl.style.left = r.left + "px";
    lbl.style.top  = (r.top - 16) + "px";
  };

  const debugRebuild = () => {
    if (!debugEnabled) { debugTeardown(); return; }

    if (!debugStyleEl) {
      debugStyleEl = document.createElement("style");
      debugStyleEl.setAttribute("data-dsx-debug", "style");
      document.head.appendChild(debugStyleEl);
    }
    debugStyleEl.textContent = debugStyle();

    if (!debugOverlay) {
      debugOverlay = document.createElement("div");
      debugOverlay.setAttribute("data-dsx-debug", "overlay");
      debugOverlay.style.cssText = [
        "position:fixed", "inset:0",
        "pointer-events:none", "z-index:2147483647",
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace",
      ].join(";");
      document.body.appendChild(debugOverlay);
    }
    debugOverlay.innerHTML = "";

    for (const [name, rule] of RULES) {
      const color = colorFor(rule);
      const clones = document.querySelectorAll(
        `[${MARKER_ATTR}="${name}"]`);
      clones.forEach((c) => {
        const lbl = document.createElement("div");
        lbl.textContent = name;
        lbl.dataset.dsxFor = name;
        lbl.style.cssText = [
          "position:fixed",
          "background:" + color,
          "color:#000",
          "font-size:10px", "line-height:1",
          "padding:2px 5px", "border-radius:3px",
          "white-space:nowrap",
          "box-shadow:0 0 3px rgba(0,0,0,0.6)",
        ].join(";");
        debugOverlay.appendChild(lbl);
        positionLabel(lbl, c);
      });
    }

    const legend = document.createElement("div");
    legend.setAttribute("data-dsx-debug", "legend");
    legend.style.cssText = [
      "position:fixed", "right:12px", "bottom:12px",
      "background:rgba(0,0,0,0.85)", "color:#fff",
      "font-size:11px", "line-height:1.5",
      "padding:8px 10px", "border-radius:6px",
      "box-shadow:0 0 6px rgba(0,0,0,0.5)",
      "pointer-events:auto", "min-width:200px",
    ].join(";");
    const title = document.createElement("div");
    title.style.cssText =
      "display:flex;justify-content:space-between;align-items:center;" +
      "font-weight:bold;margin-bottom:6px;gap:12px";
    const titleText = document.createElement("span");
    titleText.textContent = "DSX buttons — debug";
    const closeBtn = document.createElement("span");
    closeBtn.textContent = "×";
    closeBtn.style.cssText =
      "cursor:pointer;font-size:14px;line-height:1;padding:0 2px;opacity:0.7";
    closeBtn.title = "close (or: __dsx.buttons.debug(false))";
    closeBtn.addEventListener("click", () => setDebug(false));
    title.appendChild(titleText);
    title.appendChild(closeBtn);
    legend.appendChild(title);

    if (RULES.size === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "opacity:0.6;font-style:italic";
      empty.textContent = "no rules registered";
      legend.appendChild(empty);
    } else {
      for (const [name, rule] of RULES) {
        const color = colorFor(rule);
        const n = document.querySelectorAll(
          `[${MARKER_ATTR}="${name}"]`).length;
        const row = document.createElement("div");
        row.style.cssText =
          "display:flex;align-items:center;gap:6px";
        const sw = document.createElement("span");
        sw.style.cssText = [
          "display:inline-block", "width:10px", "height:10px",
          "border-radius:2px", "background:" + color, "flex:0 0 auto",
        ].join(";");
        const label = document.createElement("span");
        label.textContent = `${name}  (${n})`;
        row.appendChild(sw);
        row.appendChild(label);
        legend.appendChild(row);
      }
    }
    debugOverlay.appendChild(legend);
    debugStartLoop();
  };

  const debugReposition = () => {
    if (!debugOverlay) return false;
    let ok = true;
    for (const [name] of RULES) {
      const clones = document.querySelectorAll(
        `[${MARKER_ATTR}="${name}"]`);
      const labels = debugOverlay.querySelectorAll(
        `[data-dsx-for="${name}"]`);
      if (labels.length !== clones.length) { ok = false; continue; }
      for (let i = 0; i < labels.length; i++) {
        positionLabel(labels[i], clones[i]);
      }
    }
    return ok;
  };

  const debugTick = () => {
    debugRafId = requestAnimationFrame(debugTick);
    if (!debugEnabled) {
      cancelAnimationFrame(debugRafId);
      debugRafId = null;
      return;
    }
    const ok = debugReposition();
    if (!ok) debugRebuild();
  };
  const debugStartLoop = () => {
    if (debugRafId !== null) return;
    debugRafId = requestAnimationFrame(debugTick);
  };
  const debugStopLoop = () => {
    if (debugRafId !== null) {
      cancelAnimationFrame(debugRafId);
      debugRafId = null;
    }
  };
  const debugTeardown = () => {
    debugStopLoop();
    if (debugStyleEl) { debugStyleEl.remove(); debugStyleEl = null; }
    if (debugOverlay) { debugOverlay.remove(); debugOverlay = null; }
  };
  const setDebug = (on) => {
    debugEnabled = !!on;
    _writeDebug(debugEnabled);
    if (debugEnabled) debugRebuild();
    else debugTeardown();
    console.log(`[DSX:buttons] debug ${debugEnabled ? "ON" : "OFF"}`);
  };

  // ---- public API -----------------------------------------------

  const api = {
    rule(spec) {
      if (!spec || !spec.name) throw new Error("rule.name required");
      if (typeof spec.onClick !== "function") throw new Error("rule.onClick required");
      if (spec.target == null) throw new Error("rule.target required");
      if (RULES.has(spec.name)) {
        console.warn("[DSX:buttons] rule exists:", spec.name);
        return;
      }
      if (spec.state) spec._state = spec.state.default ?? false;
      RULES.set(spec.name, spec);
      const t = spec.template;
      if (t && t.nodeType === 1) TEMPLATE_CACHE.set(spec.name, snapshotOf(t));
      console.log("[DSX:buttons] rule registered:", spec.name);
      schedule();
    },
    amendRule(name, patch) {
      const r = RULES.get(name);
      if (!r) { console.warn("[DSX:buttons] rule not found:", name); return; }
      Object.assign(r, patch);
      console.log("[DSX:buttons] rule amended:", name);
      schedule();
    },
    unrule(name) {
      RULES.delete(name);
      TEMPLATE_CACHE.delete(name);
      const applied = _layoutApplied.get(name);
      if (applied) {
        for (const [el, origs] of applied) {
          for (const [p, o] of origs) {
            if (o) el.style.setProperty(p, o);
            else el.style.removeProperty(p);
          }
        }
        _layoutApplied.delete(name);
      }
      document.querySelectorAll(`[${MARKER_ATTR}="${name}"]`)
        .forEach(n => n.remove());
      if (debugEnabled) debugRebuild();
      console.log("[DSX:buttons] rule removed:", name);
    },
    list() { return [...RULES.keys()]; },
    status() {
      const out = {};
      for (const [name, r] of RULES) {
        out[name] = {
          rows: rowsFor(r)
            .filter(row => rowPasses(row, r))
            .map(row => {
              const c = findClone(row, name);
              return {
                childCount: row.children.length,
                hasClone: !!c,
                isFirst: row.firstElementChild === c,
                isLast: row.lastElementChild === c,
              };
            }),
          whenActive: (r.whenActive || []).length,
          state: ("_state" in r) ? r._state : undefined,
        };
      }
      return out;
    },
    reapply(name) {
      const r = RULES.get(name);
      if (!r) return 0;
      const clones = document.querySelectorAll(`[${MARKER_ATTR}="${name}"]`);
      clones.forEach(c => applyDecorate(c, r));
      return clones.length;
    },
    rescan,
    debug(on) { setDebug(on === undefined ? !debugEnabled : on); },
    isDebug() { return debugEnabled; },
    resetState() {
      for (const r of RULES.values()) {
        if ("_state" in r) r._state = r.state?.default ?? false;
        this.reapply(r.name);
      }
    },
    legend() {
      console.log("%c[DSX:buttons] debug legend",
        "color:lime;font-weight:bold");
      if (RULES.size === 0) {
        console.log("  (no rules registered)");
        return;
      }
      for (const [name, rule] of RULES) {
        const color = colorFor(rule);
        const n = document.querySelectorAll(
          `[${MARKER_ATTR}="${name}"]`).length;
        console.log(
          `%c ${name.padEnd(18)}  ${String(n).padStart(3)} clone(s) `,
          `background:${color};color:#000;font-family:monospace;` +
          `padding:2px 4px;border-radius:3px`);
      }
    },
  };

  window.__dsx__.buttons = api;

  window.__dsx_buttons_json = async function (argsJson) {
    let args = {};
    try { args = JSON.parse(argsJson || "{}"); } catch (_) {}
    const cmd = args.cmd;
    try {
      if (cmd === "debug") {
        setDebug(!!args.on);
        return JSON.stringify({ ok: true, debug: debugEnabled });
      }
      if (cmd === "status") return JSON.stringify(api.status());
      if (cmd === "legend") { api.legend(); return JSON.stringify({ ok: true }); }
      if (cmd === "rescan") { api.rescan(); return JSON.stringify({ ok: true }); }
      if (cmd === "reset_state") {
        api.resetState(); return JSON.stringify({ ok: true });
      }
      return JSON.stringify({ ok: false, error: `unknown cmd: ${cmd}` });
    } catch (e) {
      return JSON.stringify({ ok: false, error: String(e && e.message || e) });
    }
  };

  try { rescan(); }
  catch (e) { console.error("[DSX:buttons] initial rescan threw:", e); }

  try { debugEnabled = _readDebug(); } catch (_) {}
  if (debugEnabled) debugRebuild();

  setTimeout(() => {
    const counts = {};
    document.querySelectorAll(`[${MARKER_ATTR}]`).forEach(e => {
      const n = e.getAttribute(MARKER_ATTR);
      counts[n] = (counts[n] || 0) + 1;
    });
    const rulesList = api.list();
    console.log("[DSX:buttons] boot done. Rules:",
      rulesList.length ? rulesList.join(", ") : "(none registered)");
    console.log("[DSX:buttons] clones per rule:", JSON.stringify(counts));
    console.log(
      "%c[DSX:buttons] ready\n" +
      "  toggle debug overlay:  __dsx.buttons.debug(true)\n" +
      "  per-rule state:        __dsx.buttons.status()\n" +
      "  list rules:            __dsx.buttons.list()\n" +
      "  disable injector:      localStorage.dsx_buttons_enabled='0'; location.reload()",
      "color:lime;font-weight:bold;font-family:monospace");
  }, 300);
})();
