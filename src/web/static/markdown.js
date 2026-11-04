// Minimal, XSS-safe Markdown renderer: everything is HTML-escaped first, then a small
// subset (fences, inline code, headings, lists, quotes, bold/italic, links) is formatted.
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function inline(text) {
    // text is already escaped. Protect inline code spans first.
    const codes = [];
    text = text.replace(/`([^`\n]+)`/g, function (_, c) {
      codes.push("<code>" + c + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = text
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return text.replace(/\u0000(\d+)\u0000/g, function (_, i) { return codes[+i]; });
  }

  function render(src) {
    const lines = escapeHtml(src || "").split("\n");
    const out = [];
    let para = [];
    let list = null; // "ul" | "ol"

    function flushPara() {
      if (para.length) { out.push("<p>" + inline(para.join("<br>")) + "</p>"); para = []; }
    }
    function closeList() {
      if (list) { out.push("</" + list + ">"); list = null; }
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
      if (fence) {
        flushPara(); closeList();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { code.push(lines[i]); i++; }
        const lang = fence[1] ? ' data-lang="' + fence[1] + '"' : "";
        out.push("<pre" + lang + "><code>" + code.join("\n") + "</code></pre>");
        continue;
      }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushPara(); closeList();
        const level = Math.min(m[1].length + 2, 6);
        out.push("<h" + level + ">" + inline(m[2]) + "</h" + level + ">");
      } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        const kind = /^\s*\d/.test(line) ? "ol" : "ul";
        if (list !== kind) { closeList(); out.push("<" + kind + ">"); list = kind; }
        out.push("<li>" + inline(m[1]) + "</li>");
      } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
        flushPara(); closeList();
        out.push("<blockquote>" + inline(m[1]) + "</blockquote>");
      } else if (/^\s*$/.test(line)) {
        flushPara(); closeList();
      } else if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) {
        flushPara(); closeList();
        out.push("<hr>");
      } else {
        closeList();
        para.push(line);
      }
    }
    flushPara(); closeList();
    return out.join("");
  }

  global.NexusMarkdown = { render: render, escapeHtml: escapeHtml };
})(typeof window !== "undefined" ? window : globalThis);
