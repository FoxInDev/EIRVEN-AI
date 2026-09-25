/*
 * EIRVEN AI — 2.4.0
 * Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
 * Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
 * Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
 * EIRVEN-LICENSE-HEADER
 */
(() => {
  "use strict";

  const ALLOWED_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeHref(raw) {
    try {
      const url = new URL(String(raw || "").trim(), "https://appassets.eirven/");
      return ALLOWED_PROTOCOLS.has(url.protocol) ? escapeHtml(url.href) : "";
    } catch {
      return "";
    }
  }

  function normalizeTex(value) {
    return String(value ?? "")
      .replace(/\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}/g, "$1")
      .replace(/\\url\s*\{[^{}]*\}/g, "\\text{link}")
      .replace(/\\(?:includegraphics|htmlClass|htmlId|htmlStyle|htmlData)\b/g, "")
      .replace(/\\\s*\n/g, " ")
      .replace(/\\_/g, "_")
      .replace(/\s+/g, " ")
      .trim();
  }

  function renderMath(value, displayMode) {
    const tex = normalizeTex(value);
    if (!tex) return "";
    try {
      if (!window.katex?.renderToString) throw new Error("KaTeX unavailable");
      const html = window.katex.renderToString(tex, {
        displayMode: !!displayMode,
        throwOnError: true,
        strict: "ignore",
        trust: false,
        output: "htmlAndMathml",
        maxExpand: 1000,
        maxSize: 30,
      });
      return displayMode ? `<div class="math-display">${html}</div>` : `<span class="math-inline">${html}</span>`;
    } catch {
      const tag = displayMode ? "div" : "span";
      return `<${tag} class="math-fallback">${escapeHtml(tex)}</${tag}>`;
    }
  }

  function inline(value) {
    let raw = String(value ?? "");
    const code = [];
    const math = [];
    raw = raw.replace(/`([^`\n]+)`/g, (_, body) => {
      const key = `\u0000CODE${code.length}\u0000`;
      code.push(`<code>${escapeHtml(body)}</code>`);
      return key;
    });
    raw = raw.replace(/\\{1,2}\((.+?)\\{1,2}\)/g, (_, body) => {
      const key = `\u0000INLINEMATH${math.length}\u0000`;
      math.push(renderMath(body, false));
      return key;
    });
    raw = raw.replace(/(^|[^$\\])\$([^$\n]+?)\$(?!\$)/g, (_, prefix, body) => {
      const key = `\u0000INLINEMATH${math.length}\u0000`;
      math.push(renderMath(body, false));
      return `${prefix}${key}`;
    });

    let text = escapeHtml(raw);
    text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
      const safe = safeHref(href);
      return safe ? `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
    text = text
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    code.forEach((item, index) => { text = text.replace(`\u0000CODE${index}\u0000`, item); });
    math.forEach((item, index) => { text = text.replace(`\u0000INLINEMATH${index}\u0000`, item); });
    return text;
  }

  function protectBlockMath(source) {
    const blocks = [];
    const text = String(source ?? "").replace(
      /```[\s\S]*?```|\\{1,2}\[([\s\S]*?)\\{1,2}\]|\$\$([\s\S]*?)\$\$/g,
      (whole, bracketed, dollars) => {
        if (whole.startsWith("```")) return whole;
        const key = `\u0000BLOCKMATH${blocks.length}\u0000`;
        const body = String(bracketed ?? dollars ?? "")
          // Some models emit `\[\` on one line and a stray `\` before `\]`.
          // Strip only those standalone boundary slashes; never eat commands such
          // as `\Delta`, `\rho` or `\left` at the start of a valid formula.
          .replace(/^\s*\\\s*\n/, "")
          .replace(/\n\s*\\\s*$/, "");
        blocks.push(renderMath(body, true));
        return `\n${key}\n`;
      },
    );
    return { text, blocks };
  }

  function render(source) {
    const protectedMath = protectBlockMath(String(source ?? "").replace(/\r\n?/g, "\n"));
    const lines = protectedMath.text.split("\n");
    const output = [];
    let paragraph = [];
    let list = "";
    let fence = null;
    let fenceLines = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      output.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    };
    const closeList = () => {
      if (!list) return;
      output.push(`</${list}>`);
      list = "";
    };
    const openList = (kind) => {
      flushParagraph();
      if (list === kind) return;
      closeList();
      output.push(`<${kind}>`);
      list = kind;
    };
    const closeFence = () => {
      output.push(`<pre><code${fence ? ` class="language-${escapeHtml(fence.replace(/[^a-z0-9_+-]/gi, ""))}"` : ""}>${escapeHtml(fenceLines.join("\n"))}</code></pre>`);
      fence = null;
      fenceLines = [];
    };

    for (const line of lines) {
      const marker = line.match(/^```\s*([\w+-]*)\s*$/);
      if (marker) {
        if (fence !== null) closeFence();
        else {
          flushParagraph();
          closeList();
          fence = marker[1] || "";
        }
        continue;
      }
      if (fence !== null) {
        fenceLines.push(line);
        continue;
      }
      const block = line.match(/^\u0000BLOCKMATH(\d+)\u0000$/);
      if (block) {
        flushParagraph();
        closeList();
        output.push(protectedMath.blocks[Number(block[1])] || "");
        continue;
      }
      const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
      if (unordered) {
        openList("ul");
        output.push(`<li>${inline(unordered[1])}</li>`);
        continue;
      }
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (ordered) {
        openList("ol");
        output.push(`<li>${inline(ordered[1])}</li>`);
        continue;
      }
      if (!line.trim()) {
        flushParagraph();
        closeList();
        continue;
      }
      closeList();
      paragraph.push(line);
    }
    flushParagraph();
    closeList();
    if (fence !== null) closeFence();
    return output.join("");
  }

  function renderInto(node, source) {
    if (!node) return;
    node.innerHTML = render(source);
  }

  window.EirvenMarkdown = Object.freeze({ escapeHtml, render, renderInto, renderMath });
})();
