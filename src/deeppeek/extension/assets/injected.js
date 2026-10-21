// Runs in the page world so it can read React fiber state.
// Exposes window.__dsx_extract(), which returns the whole conversation as a JSON string.
(function () {
  "use strict";

  const config = {
    chatContainerSelector: ".ds-virtual-list-visible-items",
    userMessageSelector: "._9663006",
    aiClassPrefix: "_4f9bf79",
    searchHintSelector: "._5255ff8._4d41763",
    thinkingChainSelector: ".ds-think-content",
    finalAnswerSelector: ".ds-message .ds-markdown:last-child",
    titleSelector: ".afa34042.e37a04e4.e0a1edb7",
    answerMarkdownPath: "$0.return.return.return",
    thinkingContentPath: "$0.return.return.return.return",
  };

  function navigateFiberPathFromElement(element, pathString) {
    if (!element || !pathString) return null;
    const fiberKey = Object.keys(element).find((k) => k.startsWith("__reactFiber$"));
    if (!fiberKey) return null;
    let fiber = element[fiberKey];
    const cleaned = pathString.replace(/^\$0\.*/, "");
    if (!cleaned) return fiber;
    for (const step of cleaned.split(".")) {
      if (!step) continue;
      fiber = fiber ? fiber[step] : null;
      if (!fiber) return null;
    }
    return fiber;
  }

  function closeUnclosedFencedCodeBlocks(markdown) {
    if (typeof markdown !== "string" || !markdown.length) return markdown;
    let inFence = false;
    for (const line of markdown.split(/\r?\n/)) {
      const m = line.match(/^ {0,3}(```+)/);
      if (!m) continue;
      if (m[1].length === 3) inFence = !inFence;
    }
    return inFence ? markdown + "\n```" : markdown;
  }

  function findScrollParent(el) {
    let p = el;
    for (let i = 0; i < 30 && p; i++) {
      const st = window.getComputedStyle(p);
      const oy = st.overflowY;
      if (
        (oy === "auto" || oy === "scroll" || oy === "overlay") &&
        p.scrollHeight > p.clientHeight + 2
      ) {
        return p;
      }
      p = p.parentElement;
    }
    return null;
  }

  function tryGetMessageOrdinal(node) {
    if (!node || node.nodeType !== 1) return null;
    const direct = node.getAttribute("data-index");
    if (direct != null && /^\d+$/.test(direct)) return parseInt(direct, 10);
    const nested = node.querySelector("[data-index]");
    if (nested) {
      const v = nested.getAttribute("data-index");
      if (v != null && /^\d+$/.test(v)) return parseInt(v, 10);
    }
    const fiberKey = Object.keys(node).find((k) => k.startsWith("__reactFiber$"));
    if (!fiberKey) return null;
    let fiber = node[fiberKey];
    for (let depth = 0; depth < 80 && fiber; depth++) {
      const mp = fiber.memoizedProps;
      if (mp && typeof mp === "object") {
        for (const k of ["index", "messageIndex", "msgIndex", "order"]) {
          const v = mp[k];
          if (typeof v === "number" && Number.isFinite(v)) return v;
        }
      }
      fiber = fiber.return;
    }
    return null;
  }

  function hashString(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    return String(h);
  }

  function getUserMessage(node) {
    const m = node.matches(config.userMessageSelector)
      ? node
      : node.querySelector(config.userMessageSelector);
    return m ? m.textContent.trim() : null;
  }

  function isAIMessage(node) {
    return node.classList.contains(config.aiClassPrefix);
  }

  function extractSearchOrThinking(node) {
    const hint = node.querySelector(config.searchHintSelector);
    return hint ? hint.textContent.trim() : null;
  }

  function extractThinkingChain(node) {
    const markdownEl = node.querySelector("div.ds-markdown");
    const baseEl = markdownEl || node;
    const nav = navigateFiberPathFromElement(baseEl, config.thinkingContentPath);
    if (!nav || !nav.memoizedProps || !nav.memoizedProps.content) return null;
    return closeUnclosedFencedCodeBlocks(nav.memoizedProps.content);
  }

  function extractFinalAnswer(node) {
    let answerNode = null;
    for (const el of node.querySelectorAll("div.ds-markdown")) {
      if (!el.closest(config.thinkingChainSelector)) {
        answerNode = el;
        break;
      }
    }
    if (!answerNode) answerNode = node.querySelector(config.finalAnswerSelector);
    if (!answerNode) return null;
    const nav = navigateFiberPathFromElement(answerNode, config.answerMarkdownPath);
    if (!nav || !nav.memoizedProps || !nav.memoizedProps.markdown) return null;
    return closeUnclosedFencedCodeBlocks(nav.memoizedProps.markdown);
  }

  function getChatTitle() {
    const el = document.querySelector(config.titleSelector);
    return el ? el.textContent.trim() : null;
  }

  function formatRow(node) {
    const user = getUserMessage(node);
    if (user) {
      return { role: "user", content: user };
    }
    if (!isAIMessage(node)) return null;

    const hint = extractSearchOrThinking(node);
    const thinkingNode = node.querySelector(config.thinkingChainSelector);
    const thinking = thinkingNode ? extractThinkingChain(thinkingNode) : null;
    const content = extractFinalAnswer(node);

    if (!content && !thinking && !hint) return null;
    return {
      role: "assistant",
      content: content || "",
      thinking: thinking || undefined,
      hint: hint || undefined,
    };
  }

  async function sweep(chatContainer, scrollParent) {
    const settleMs = 80;
    const step = scrollParent
      ? Math.max(100, Math.floor(scrollParent.clientHeight * 0.3))
      : 0;
    const best = new Map();

    const consider = (node, pos, i) => {
      const ord = tryGetMessageOrdinal(node);
      if (ord != null && best.has(`o:${ord}`)) return;
      const msg = formatRow(node);
      if (!msg) return;
      const orderKey = ord != null ? ord : pos * 10000 + i;
      const dedupeKey =
        ord != null
          ? `o:${ord}`
          : `h:${hashString(msg.role + "|" + (msg.content || ""))}`;
      const prev = best.get(dedupeKey);
      if (!prev || orderKey < prev.orderKey) best.set(dedupeKey, { orderKey, msg });
    };

    if (!scrollParent || scrollParent.scrollHeight <= scrollParent.clientHeight + 4) {
      let i = 0;
      for (const node of chatContainer.children) {
        consider(node, 0, i);
        i++;
      }
    } else {
      let scrollTop = null;
      for (let pass = 0; pass < 2000; pass++) {
        const maxScroll = Math.max(
          0,
          scrollParent.scrollHeight - scrollParent.clientHeight
        );
        if (scrollTop === null) scrollTop = maxScroll;
        const pos = Math.max(0, Math.min(scrollTop, maxScroll));
        scrollParent.scrollTop = pos;
        await new Promise((r) => requestAnimationFrame(r));
        await new Promise((r) => setTimeout(r, settleMs));
        let i = 0;
        for (const node of chatContainer.children) {
          consider(node, pos, i);
          i++;
        }
        if (pos <= 0) break;
        scrollTop = pos - step;
      }
    }

    return Array.from(best.values())
      .sort((a, b) => a.orderKey - b.orderKey)
      .map((e) => e.msg);
  }

  async function doExtraction() {
    const chatContainer = document.querySelector(config.chatContainerSelector);
    if (!chatContainer) {
      throw new Error("Chat container not found; DeepSeek's page structure may have changed.");
    }
    const scrollParent = findScrollParent(chatContainer);
    const saved = scrollParent ? scrollParent.scrollTop : 0;
    let messages;
    try {
      messages = await sweep(chatContainer, scrollParent);
    } finally {
      if (scrollParent) scrollParent.scrollTop = saved;
    }
    messages = messages.map((m, i) => ({ ...m, index: i }));
    return {
      title: getChatTitle(),
      url: window.location.href,
      messages,
    };
  }

  window.__dsx_extract = async function () {
    return JSON.stringify(await doExtraction());
  };
})();
