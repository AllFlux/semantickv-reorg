// app.js — Agora 看板主逻辑
(function () {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

  let CONFIG = {};
  let BUNDLE = null;

  const SECTIONS_HTML = `
    <section id="overview">
      <div class="section-head"><h2>概览</h2><span class="hint">单向数据流：results/ 为唯一事实来源 → build_bundle.py → 看板</span></div>
      <div id="inject-top" class="callout" style="display:block;margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
          <b style="font-size:15px">📦 给 DeepSeek 的上下文注入包</b>
          <button class="btn" id="inject-copy-top">一键复制</button>
        </div>
        <textarea id="inject-top-box" readonly
          style="width:100%;min-height:200px;box-sizing:border-box;font-family:var(--mono);font-size:12px;line-height:1.6;padding:10px;border:1px solid var(--border,#ddd);border-radius:6px;background:#fafafa;color:inherit;resize:vertical"></textarea>
        <div class="hint" style="margin-top:6px">纯 Markdown 文本 · 专为 DS 无浏览器环境优化 · 复制后直接粘贴给 DeepSeek 即完成同步</div>
      </div>
      <div id="open-callout" class="callout" style="display:none"></div>
      <div class="cards" id="ov-cards"></div>
    </section>

    <section id="ledger">
      <div class="section-head"><h2>假设台账 · Hypothesis Ledger</h2><span class="hint">DS 提出的每一条预判/处方，带 status 与命运说明</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>ID</th><th>假设陈述</th><th>可证伪标准</th><th>数据源</th><th>状态</th><th>证据文件</th><th>DS 承认</th>
          </tr></thead>
          <tbody id="ledger-body"></tbody>
        </table>
      </div>
    </section>

    <section id="discipline">
      <div class="section-head"><h2>双边纪律检查 · Discipline Ledger</h2><span class="hint">WB 侧 D1–D5 + DS 侧 DS-D1..DS-D5，由 check_discipline.py 逐轮机械检验</span></div>
      <div id="discipline-rounds"></div>
    </section>

    <section id="reviews">
      <div class="section-head"><h2>DS 评审画廊 · Reviews</h2><span class="hint">DeepSeek 按 ds_review_template.md 落盘的评审档案</span></div>
      <div class="reviews" id="reviews"></div>
    </section>

    <section id="files">
      <div class="section-head"><h2>原始文件 · Raw Sources</h2><span class="hint">复制 Raw 链接，发给 DeepSeek 即可让其直接读取纯文本（DS 无浏览器，靠人肉 API Gateway）</span></div>
      <div class="files" id="files"></div>
    </section>

    <section id="inject">
      <div class="section-head"><h2>DS 上下文注入包</h2><span class="hint">一键复制，粘贴给 DeepSeek 完成闭环</span></div>
      <div class="toolbar">
        <button class="btn" id="inject-copy">复制注入包</button>
        <span class="hint">含最新台账 Raw 链接 + 开放项清单 + 格式要求</span>
      </div>
      <pre class="inject-box" id="inject-box"></pre>
    </section>

    <div class="foot">
      Agora · 雅典学院 — 把聊天框里的散漫对话，固化为 <code>有版本控制 / 有审计日志 / 有看板</code> 的现代协作。<br/>
      数据源：<code>semantickv-reorg/results/</code> · 刷新看板：<code>python3 build_bundle.py</code>
    </div>`;

  // ---------- 工具 ----------
  function statusPillClass(status) {
    return ["pending", "confirmed", "refuted"].includes(status) ? status : "pending";
  }

  function verdictClass(v) {
    if (!v) return "red";
    if (v.startsWith("GREEN-WITH")) return "warn";
    if (v === "GREEN") return "green";
    if (v === "RED") return "red";
    return "warn";
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(showToast).catch(fallbackCopy);
    } else {
      fallbackCopy();
    }
    function fallbackCopy() {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); showToast(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  let toastTimer = null;
  function showToast(msg = "已复制到剪贴板") {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 1600);
  }

  function rawUrl(path) {
    const base = (CONFIG.raw_base || "").replace(/\/+$/, "/");
    return base + path.replace(/^\/+/, "");
  }

  // ---------- 渲染 ----------
  function renderOverview() {
    const h = BUNDLE.hypotheses;
    const meta = h ? h.meta : {};
    const hypItems = h ? h.items : [];
    const conf = hypItems.filter((x) => x.status === "confirmed").length;
    const ref = hypItems.filter((x) => x.status === "refuted").length;
    const pend = hypItems.filter((x) => x.status === "pending").length;
    const open = BUNDLE.open_items || [];
    const disc = BUNDLE.discipline;
    const verdict = disc ? disc.last_verdict : "—";

    $("#ov-cards").innerHTML = [
      card("假设总数", hypItems.length, `confirmed ${conf} · refuted ${ref} · pending ${pend}`),
      card("待 DS 承认开放项", open.length, open.length ? open.join(" / ") + " 被证伪未承认" : "无 · DS-D4 闭合", open.length ? "open" : ""),
      card("纪律末轮判定", verdict || "—", `回合 ${disc ? disc.rounds.length : 0} 轮`, verdictClass(verdict)),
      card("DS 评审归档", (BUNDLE.reviews || []).length, "按 ds_review_template.md 落盘"),
    ].join("");

    // 顶部状态灯
    const v = $("#verdict");
    v.className = "verdict " + verdictClass(verdict);
    v.innerHTML = `<span class="dot"></span>${verdict || "—"}`;

    if (open.length) {
      $("#open-callout").style.display = "block";
      $("#open-callout").innerHTML =
        `⚠ <b>开放项（DS-D4 WARN）</b>：以下假设已被真实数据证伪，但 DeepSeek 尚未在评审中显式承认 —— ` +
        open.map((id) => `<code>${id}</code>`).join(" 、") +
        `。请将「DS 上下文注入包」发给 DeepSeek，要求其下一轮评审逐条承认。`;
    } else {
      $("#open-callout").style.display = "none";
    }
  }

  function card(k, v, d, extra = "") {
    return `<div class="card">
      <div class="k">${k}</div>
      <div class="v ${extra}">${v}</div>
      <div class="d">${d}</div>
    </div>`;
  }

  function renderLedger() {
    const h = BUNDLE.hypotheses;
    if (!h) return;
    const rows = h.items
      .map((x) => {
        const isOpen = x.status === "refuted" && !x.ds_admitted_refutation;
        const statusHtml = isOpen
          ? `<span class="pill open">待承认</span>`
          : `<span class="pill ${statusPillClass(x.status)}">${x.status}</span>`;
        const admit = x.status === "refuted"
          ? (x.ds_admitted_refutation ? '<span class="yes">✓ 已承认</span>' : '<span class="no">✗ 未承认</span>')
          : '<span class="muted">—</span>';
        const ev = x.evidence_file
          ? `<a class="evidence" href="${rawUrl(x.evidence_file)}" target="_blank">${x.evidence_file}</a>`
          : '<span class="muted">—</span>';
        const fate = x.fate_note ? `<div class="muted" style="margin-top:4px;font-size:12px">${x.fate_note}</div>` : "";
        return `<tr>
          <td class="id">${x.id}</td>
          <td class="statement">${x.statement || ""}${fate}</td>
          <td class="evidence">${x.falsifiable_criterion || ""}</td>
          <td class="evidence">${x.data_source || ""}</td>
          <td>${statusHtml}</td>
          <td>${ev}</td>
          <td>${admit}</td>
        </tr>`;
      })
      .join("");
    $("#ledger-body").innerHTML = rows;
  }

  function renderDiscipline() {
    const d = BUNDLE.discipline;
    if (!d) return;
    const rounds = (d.rounds || [])
      .slice()
      .reverse()
      .map((r) => {
        const summary = r.summary || {};
        const checks = (r.checks || [])
          .map((c) => {
            const cls = (c.status || "").toLowerCase();
            return `<tr>
              <td>${c.check}</td>
              <td><span class="pill ${cls}">${c.status}</span></td>
              <td class="muted">${c.detail || ""}</td>
            </tr>`;
          })
          .join("");
        const verdictCls = verdictClass(summary.verdict);
        return `<div class="card" style="margin-bottom:16px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
            <span style="font-weight:700">${r.round}</span>
            <span class="muted" style="font-size:12px;font-family:var(--mono)">${r.timestamp || ""}</span>
            <span class="verdict ${verdictCls}" style="margin-left:auto;padding:4px 12px;font-size:12px">
              <span class="dot"></span>${summary.verdict || "—"}
            </span>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>检查项</th><th>结果</th><th>明细</th></tr></thead>
              <tbody>${checks}</tbody>
            </table>
          </div>
          <div class="muted" style="font-size:12px;margin-top:10px">
            PASS ${summary.pass || 0} · WARN ${summary.warn || 0} · FAIL ${summary.fail || 0}
          </div>
        </div>`;
      })
      .join("");
    $("#discipline-rounds").innerHTML = rounds;
  }

  function renderReviews() {
    const reviews = BUNDLE.reviews || [];
    $("#reviews").innerHTML = reviews
      .map((rv, idx) => {
        const m = rv.meta || {};
        const hyps = (m.hypotheses || [])
          .map((hh) => {
            const cls = statusPillClass(hh.status);
            return `<span class="pill ${cls}" title="${hh.id}">${hh.id}:${hh.status}</span>`;
          })
          .join("");
        return `<div class="review" data-idx="${idx}">
          <div class="rv-head">
            <span class="rv-id">${m.review_id || rv.file}</span>
            <span class="rv-focus">${m.focus || ""}</span>
            <span class="rv-date">${m.date || ""} · ${m.reviewer || "DeepSeek"}</span>
          </div>
          ${hyps ? `<div class="rv-hyps">${hyps}</div>` : ""}
          <div class="rv-body md collapsed">${window.MarkdownRenderer.render(rv.body)}</div>
          <div class="rv-toggle">
            <button class="toggle-btn" data-toggle="${idx}">展开全文 ▾</button>
            <button class="btn ghost" data-copyraw="${rawUrl("results/ds_reviews/" + rv.file)}" style="margin-left:8px">复制 Raw 链接</button>
          </div>
        </div>`;
      })
      .join("");

    $$("[data-toggle]").forEach((b) => {
      b.addEventListener("click", () => {
        const body = $(`.review[data-idx="${b.dataset.toggle}"] .rv-body`);
        const collapsed = body.classList.toggle("collapsed");
        b.textContent = collapsed ? "展开全文 ▾" : "收起 ▴";
      });
    });
    $$("[data-copyraw]").forEach((b) => {
      b.addEventListener("click", () => copyText(b.dataset.copyraw));
    });
  }

  function renderFiles() {
    const files = CONFIG.files || [];
    $("#files").innerHTML = files
      .map((f) => {
        const url = rawUrl(f.path);
        return `<div class="file-row">
          <div style="flex:1">
            <div class="fname">${f.label}</div>
            <div class="fpath">${f.path}</div>
          </div>
          <button class="btn" data-raw="${url}">复制 Raw 链接</button>
        </div>`;
      })
      .join("");
    $$("[data-raw]").forEach((b) => {
      b.addEventListener("click", () => copyText(b.dataset.raw));
    });
  }

  function buildInjection() {
    const h = BUNDLE.hypotheses;
    const items = h ? h.items : [];
    const meta = h ? h.meta : {};
    const disc = BUNDLE.discipline;
    const reviews = BUNDLE.reviews || [];
    const latestReview = reviews.length ? reviews[reviews.length - 1] : null;
    const pend = items.filter((x) => x.status === "pending");
    const open = BUNDLE.open_items || [];
    const exp = CONFIG.latest_experiment || {};
    const decisions = (CONFIG.decision_items && CONFIG.decision_items.length)
      ? CONFIG.decision_items
      : open.map((id) => `假设 ${id} 已被数据证伪但未获承认，请在下一轮评审逐条承认并置 ds_admitted_refutation=true`);

    const lines = [];
    lines.push("# SemanticKV-Reorg 项目状态同步");
    lines.push("");
    lines.push("## 📊 当前看板状态");
    lines.push(`- **纪律状态**: ${disc ? disc.last_verdict : "—"}`);
    lines.push(
      `- **活跃假设数(pending)**: ${pend.length}` +
        (pend.length ? `（${pend.map((x) => x.id).join("、")}）` : "")
    );
    lines.push(
      `- **假设总览**: ${items.length} 条（confirmed ${meta.confirmed || 0} / refuted ${meta.refuted || 0} / pending ${meta.pending || 0}）`
    );
    if (exp.name) lines.push(`- **最新实验**: ${exp.name}${exp.status ? `（${exp.status}）` : ""}`);
    if (exp.metrics) lines.push(`- **核心指标**: ${exp.metrics}`);
    lines.push(
      `- **DS 评审归档**: ${reviews.length} 份（最新: ${latestReview ? latestReview.meta.review_id || latestReview.file : "—"}）`
    );
    lines.push("");
    lines.push("## 📁 原始数据 Raw 链接（请按需自行拉取）");
    lines.push(`- 假设台账: ${rawUrl("results/hypothesis_ledger.yaml")}`);
    lines.push(`- 纪律台账: ${rawUrl("results/discipline_ledger.yaml")}`);
    if (latestReview) lines.push(`- 最新评审: ${rawUrl("results/ds_reviews/" + latestReview.file)}`);
    lines.push(`- 看板单文件: ${rawUrl("index.standalone.html")}`);
    lines.push("");
    lines.push("## 🚨 需要 DeepSeek 决策的问题");
    if (decisions.length) {
      decisions.forEach((d, i) => lines.push(`${i + 1}. ${d}`));
    } else {
      lines.push("（无待决项——等待下一轮实验数据）");
    }
    lines.push("");
    lines.push("## 要求");
    lines.push(
      "回复请按 templates/ds_review_template.md（YAML frontmatter + 理论背景/具体步骤/验收标准三段式）；假设须带 falsifiable_criterion 与 data_source。"
    );
    return lines.join("\n");
  }

  function renderInjection() {
    const txt = buildInjection();
    const topBox = $("#inject-top-box");
    if (topBox) topBox.value = txt;
    $("#inject-box").textContent = txt;
  }

  // ---------- nav / scroll ----------
  function initNav() {
    const links = $$(".nav a");
    const sections = links.map((a) => $(a.getAttribute("href")));
    links.forEach((a) =>
      a.addEventListener("click", () => {
        links.forEach((x) => x.classList.remove("active"));
        a.classList.add("active");
      })
    );
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            const id = "#" + e.target.id;
            links.forEach((x) => x.classList.toggle("active", x.getAttribute("href") === id));
          }
        });
      },
      { rootMargin: "-80px 0px -60% 0px" }
    );
    sections.forEach((s) => s && obs.observe(s));
  }

  // ---------- boot ----------
  async function boot() {
    try {
      let cfg, bundle;
      if (window.__AGORA_EMBEDDED__) {
        // 单文件版：数据已内联，无需 fetch（file:// 或离线也能用）
        cfg = window.__AGORA_EMBEDDED__.config;
        bundle = window.__AGORA_EMBEDDED__.bundle;
      } else {
        const [c, b] = await Promise.all([
          fetch("./config.json").then((r) => r.json()),
          fetch("./data/bundle.json").then((r) => r.json()),
        ]);
        cfg = c;
        bundle = b;
      }
      CONFIG = cfg;
      BUNDLE = bundle;
    } catch (e) {
      $("#app").innerHTML =
        `<div class="loading">加载失败：${e.message}<br/>请确认通过 HTTP 服务访问（而非 file://），并已运行 build_bundle.py。</div>`;
      return;
    }

    document.title = CONFIG.site_title || "Agora";
    $("#site-title").textContent = CONFIG.site_title || "Agora";
    $("#site-sub").textContent = CONFIG.subtitle || "";
    $("#app").innerHTML = SECTIONS_HTML;
    if (BUNDLE.generated_at) {
      const t = new Date(BUNDLE.generated_at);
      $("#sync-time").innerHTML = `数据同步: <b>${t.toLocaleString()}</b>`;
    }

    renderOverview();
    renderLedger();
    renderDiscipline();
    renderReviews();
    renderFiles();
    renderInjection();
    initNav();

    $("#inject-copy").addEventListener("click", () =>
      copyText($("#inject-box").textContent)
    );
    const topCopy = $("#inject-copy-top");
    if (topCopy)
      topCopy.addEventListener("click", () =>
        copyText($("#inject-top-box").value)
      );
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
