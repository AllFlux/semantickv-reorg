// markdown.js — 轻量 Markdown -> HTML 渲染器
// 覆盖 DS 评审用到的语法：标题 / 粗斜体 / 行内码 / 代码块 / 有序无序列表 /
// 引用块 / 链接 / 分割线 / 段落。不做完整 CommonMark，只保证看板可读。
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // 行内：先转义，再做 **粗** *斜* `码` [链接](url)
  function inline(text) {
    let s = escapeHtml(text);
    // 行内代码（优先，避免内部被其它规则破坏）
    s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    // 粗体
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // 斜体
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    // 链接 [text](url)
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, u) => {
      const safe = /^(https?:|#|\/)/.test(u) ? u : "#";
      return `<a href="${safe}" target="_blank" rel="noopener">${t}</a>`;
    });
    return s;
  }

  function render(md) {
    if (!md) return "";
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let html = "";
    let i = 0;
    let listType = null; // 'ul' | 'ol' | null

    function closeList() {
      if (listType) {
        html += `</${listType}>`;
        listType = null;
      }
    }

    while (i < lines.length) {
      let line = lines[i];

      // 代码块
      if (/^```/.test(line)) {
        closeList();
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) {
          buf.push(escapeHtml(lines[i]));
          i++;
        }
        i++; // 跳过结束 ```
        html += `<pre><code>${buf.join("\n")}</code></pre>`;
        continue;
      }

      // 分割线
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        closeList();
        html += "<hr/>";
        i++;
        continue;
      }

      // 标题
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        const lvl = h[1].length;
        html += `<h${lvl}>${inline(h[2])}</h${lvl}>`;
        i++;
        continue;
      }

      // 引用块
      if (/^>\s?/.test(line)) {
        closeList();
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          buf.push(inline(lines[i].replace(/^>\s?/, "")));
          i++;
        }
        html += `<blockquote>${buf.join("<br/>")}</blockquote>`;
        continue;
      }

      // 无序列表
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      if (ul) {
        if (listType !== "ul") {
          closeList();
          html += "<ul>";
          listType = "ul";
        }
        html += `<li>${inline(ul[1])}</li>`;
        i++;
        continue;
      }

      // 有序列表
      const ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ol) {
        if (listType !== "ol") {
          closeList();
          html += "<ol>";
          listType = "ol";
        }
        html += `<li>${inline(ol[1])}</li>`;
        i++;
        continue;
      }

      // 空行
      if (/^\s*$/.test(line)) {
        closeList();
        i++;
        continue;
      }

      // 普通段落（合并连续非空行）
      closeList();
      const buf = [line];
      i++;
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^(#{1,6}\s|>\s?|\s*[-*]\s|\s*\d+\.\s|```)/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i++;
      }
      html += `<p>${inline(buf.join(" "))}</p>`;
    }
    closeList();
    return html;
  }

  global.MarkdownRenderer = { render };
})(window);
