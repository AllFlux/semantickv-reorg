#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_ds_brief.py — 一键生成「给 DeepSeek 的投喂包」（纪律 v2.0 · D2-b / D6）

背景（为什么需要这个工具）
------------------------------------------------------------
DS（DeepSeek）**没有浏览器，打不开任何 http/https 链接**，也读不到你发的仓库路径。
它只能读到你**贴在对话里的明文**。此前 4 次「DS 声称 WB2 已推送、远端其实为空」
的事故，根因就是「只给路径 + DS 读不到」→ DS 只能靠摘要措辞猜。

本工具把「报告 + 关键 JSON + 台账状态 + 纪律检验」自动拼成一个**可直接全选复制**
的 Markdown 文件。你把它整段粘给 DS 即可。

用法
------------------------------------------------------------
    # 最小用法：一份报告 + 你要 DS 看的 JSON
    python3 tools/make_ds_brief.py \
        --report results/REAL_MODEL_VALIDATION_V15.md \
        --json results/real_model_v15_topk8.json results/router_bench.json \
        --ledger-ids H-DS10 H-DS24 \
        --title "R18 交付：V1.5 K=8 复核"

    # 只贴当前轮纪律检验
    python3 tools/make_ds_brief.py --report ... --discipline-round R17

    # 输出默认写 results/_ds_brief/<时间戳>_brief.md

  --auto 智能模式（推荐）
------------------------------------------------------------
    # 自动抓「本轮改动过的所有文件」——不用手输路径
    python3 tools/make_ds_brief.py --auto --title "R18 交付"

  原理：用 `git status --porcelain` 找出本轮变更，自动分类：
    * results/*.md（CHANGELOG/REQUEST 除外）→ 报告
    * results/*.json → 内联数据
    * 引用这些 JSON 的假设 → 台账表
    * 纪律台账最新一轮 → 检验表
  若本轮什么都没改，则退回「最近修改 3 份报告 + 3 份 JSON」。

设计约束
------------------------------------------------------------
  * 大 JSON 自动瘦身（列表>12 项折叠、嵌套>3 层截断），避免超长；
  * 每个 JSON 代码块前强制标注 environment 标签（D3 口径自证）；
  * 「文件清单」放最后并明确标注「DS 可忽略」——路径只给 CEO 溯源用；
  * 绝不写入任何 token（扫描 ghp_/github_pat_，命中即报错退出）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
MAX_LIST = 12          # 列表超过这么多项就折叠
MAX_DEPTH = 3          # JSON 嵌套深度上限
MAX_JSON_CHARS = 6000  # 单个 JSON 代码块字符上限


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _trim(obj, depth=0):
    """递归瘦身：深列表折叠、过深层截断。返回 (新对象, 是否被裁剪)。"""
    if depth >= MAX_DEPTH:
        if isinstance(obj, dict):
            return "{…字段已省略}", True
        if isinstance(obj, list):
            return f"[…共{len(obj)}项]", True
        return obj, False
    if isinstance(obj, dict):
        out, cut = {}, False
        for k, v in obj.items():
            nv, c = _trim(v, depth + 1)
            out[k] = nv
            cut = cut or c
        return out, cut
    if isinstance(obj, list):
        cut = len(obj) > MAX_LIST
        shown = obj[:MAX_LIST] if cut else obj
        out = []
        for v in shown:
            nv, c = _trim(v, depth + 1)
            out.append(nv)
            cut = cut or c
        if cut:
            out.append(f"…（共 {len(obj)} 项，仅显示前 {MAX_LIST} 项）")
        return out, cut
    return obj, False


def _fmt_json(path: Path) -> tuple[str, str]:
    """返回 (代码块内容, 环境标签说明)。"""
    raw = path.read_text(encoding="utf-8")
    try:
        d = json.loads(raw)
        env = ""
        if isinstance(d, dict):
            e = d.get("environment") or {}
            cfg = d.get("config") or {}
            env = (e.get("kind") or cfg.get("environment")
                   or cfg.get("device") or "未标注（⚠ 需补 D3 标签）")
        trimmed, cut = _trim(d)
        text = json.dumps(trimmed, ensure_ascii=False, indent=2)
        note = f"（超大已瘦身：列表>12项折叠、嵌套>3层截断）" if cut else ""
    except json.JSONDecodeError as exc:
        text, env, note = f"⚠ JSON 解析失败: {exc}\n\n{raw[:2000]}", "解析失败", ""
    if len(text) > MAX_JSON_CHARS:
        text = text[:MAX_JSON_CHARS] + f"\n…（已截断，原文件 {len(raw)} 字符）"
        note += "（单块超长已截断）"
    return text, f"environment={env} {note}".strip()


def _read_report(path: Path) -> tuple[dict, str]:
    """拆 frontmatter 与正文。"""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        meta = {}
    return meta, m.group(2)


def _ledger_entries(ids: list[str]) -> list[dict]:
    try:
        import yaml
    except ImportError:
        return []
    p = RESULTS / "hypothesis_ledger.yaml"
    if not p.exists():
        return []
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    want = {i.upper() for i in ids}
    return [h for h in d.get("hypotheses", []) if str(h.get("id", "")).upper() in want]


def _discipline_round(round_id: str) -> dict | None:
    try:
        import yaml
    except ImportError:
        return None
    p = RESULTS / "discipline_ledger.yaml"
    if not p.exists():
        return None
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for r in d.get("rounds", []):
        if str(r.get("round")) == round_id:
            return r
    return None


SECRET_RE = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}")

# 「要求 DS 点开」的链接特征（配置值如 hf-mirror 端点不算）
LINK_RE = re.compile(
    r"https?://(?:raw\.githubusercontent\.com|github\.com|gitee\.com|"
    r"chat\.deepseek\.com|huggingface\.co/[^/\s]+/[^/\s]+/(?:blob|raw))",
    re.I)


def self_check(text: str) -> tuple[bool, list[tuple[str, str, str]]]:
    """返回 (是否通过, [(检查项, 结果, 说明)…])。"""
    rows: list[tuple[str, str, str]] = []

    def add(name, ok, detail):
        rows.append((name, "PASS" if ok else "WARN", detail))

    n_fm = 1 if text.startswith("---") else 0
    add("YAML frontmatter", n_fm == 1, "存在" if n_fm else "缺失")

    body = text
    m = re.match(r"^---\s*\n.*?\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        body = m.group(1)
    add("正文字数", len(body) >= 800, f"{len(body)} 字符")

    n_json = len(re.findall(r"^```json", text, re.M))
    add("明文内联数据块", n_json > 0, f"{n_json} 块（含报告正文内的）")

    # 环境标签只对「二、核心数据明文内联」这一节强制要求
    sec = re.search(r"## 二、核心数据明文内联.*?(?=\n## |\Z)", text, re.DOTALL)
    if sec:
        sec_text = sec.group(0)
        n_sec_json = len(re.findall(r"^```json", sec_text, re.M))
        n_env = len(re.findall(r"\*\*环境标签\*\*", sec_text))
        add("环境标签(D3)", n_env >= n_sec_json,
            f"{n_env}/{n_sec_json} 个数据块已标注")
    else:
        add("环境标签(D3)", True, "无内联数据节，跳过")

    has_anom = "反直觉与异常观测" in text
    add("反直觉与异常观测(D3-b)", has_anom, "存在" if has_anom else "缺失")

    links = LINK_RE.findall(text)
    add("仓库/分享链接(D2-b)", not links, f"检出 {len(links)} 个" if links else "0 个")

    secrets = SECRET_RE.findall(text)
    add("密钥泄露", not secrets, f"检出 {len(secrets)} 个" if secrets else "0 个")

    ok = all(r[1] == "PASS" for r in rows)
    return ok, rows


# ----------------------------------------------------------------------
# --auto：按本轮 git 变更自动挑选
# ----------------------------------------------------------------------
def _git_changed() -> list[str]:
    """返回本轮相对 HEAD 变更 + 未跟踪的文件（相对仓库根）。"""
    out: list[str] = []
    try:
        import subprocess
        for cmd in (["git", "diff", "--name-only", "HEAD"],
                    ["git", "ls-files", "--others", "--exclude-standard"]):
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=15)
            if p.returncode == 0:
                out.extend([l.strip() for l in p.stdout.splitlines() if l.strip()])
    except Exception:
        pass
    return sorted(set(out))


def _recent(pattern: str, n: int) -> list[str]:
    files = sorted((ROOT / "results").glob(pattern), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return [f"results/{p.name}" for p in files[:n]]


def autodetect() -> tuple[str | None, list[str], list[str], str | None]:
    """返回 (report, jsons, ledger_ids, discipline_round)。"""
    # 这些是基础设施/说明类文档，不是「本轮实验报告」，默认排除
    INFRA = {"CHANGELOG.md", "REQUEST_TO_DEEPSEEK.md", "CPU_SIM_REPORT.md"}

    changed = _git_changed()
    reports = [f for f in changed
               if f.startswith("results/") and f.endswith(".md")
               and Path(f).name not in INFRA and "_ds_brief" not in f]
    jsons = [f for f in changed
             if f.startswith("results/") and f.endswith(".json") and "_ds_brief" not in f]

    def _mtime(rel: str) -> float:
        p = ROOT / rel
        return p.stat().st_mtime if p.exists() else 0.0

    # 报告优先取「最近修改」的；仍无则退回最近修改的任意 results/*.md
    if not reports:
        cand = [f"results/{p.name}" for p in (ROOT / "results").glob("*.md")
                if p.name not in INFRA]
        reports = sorted(cand, key=_mtime, reverse=True)[:1]
    else:
        reports = sorted(reports, key=_mtime, reverse=True)

    if not jsons:
        jsons = _recent("*.json", 3)

    # 台账：找出 data_source/evidence_file 引用这些 JSON 的假设
    ids: list[str] = []
    try:
        import yaml
        d = yaml.safe_load((RESULTS / "hypothesis_ledger.yaml").read_text(encoding="utf-8")) or {}
        names = {Path(j).name for j in jsons}
        for h in d.get("hypotheses", []):
            blob = f"{h.get('data_source', '')} {h.get('evidence_file', '')}"
            if any(n in blob for n in names):
                ids.append(str(h.get("id")))
    except Exception:
        pass

    # 纪律：最新一轮
    rnd = None
    try:
        import yaml
        dl = yaml.safe_load((RESULTS / "discipline_ledger.yaml").read_text(encoding="utf-8")) or {}
        rounds = dl.get("rounds") or []
        if rounds:
            rnd = str(rounds[-1].get("round"))
    except Exception:
        pass

    return (reports[0] if reports else None), jsons, ids, rnd


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def build(args) -> Path:
    now = datetime.now()
    parts: list[str] = []

    # ---------- 头部 ----------
    rep_path = (ROOT / args.report).resolve() if not Path(args.report).is_absolute() \
        else Path(args.report)
    meta, body = ({}, "")
    if rep_path.exists():
        meta, body = _read_report(rep_path)
    else:
        print(f"[warn] 报告不存在: {args.report}", file=sys.stderr)

    parts.append("---")
    parts.append(f'title: "{args.title}"')
    parts.append(f'generated_at: "{now.isoformat(timespec="seconds")}"')
    if meta.get("experiment_id"):
        parts.append(f'experiment_id: "{meta["experiment_id"]}"')
    if meta.get("environment"):
        parts.append(f'environment: "{meta["environment"]}"')
    if meta.get("verdict"):
        parts.append(f'report_verdict: "{meta["verdict"]}"')
    parts.append(f'inline_json_count: {len(args.json or [])}')
    parts.append("audience: DeepSeek(无浏览器-只能读明文)")
    parts.append("---")
    parts.append("")
    parts.append(f"# {args.title}")
    parts.append("")
    parts.append("> **致 DS**：本文所有关键数据已明文内联，你无需访问任何链接。"
                 "文末文件路径仅供 CEO 溯源，你可忽略。")
    parts.append("")

    # ---------- 0. 报告正文 ----------
    if body.strip():
        parts.append("## 一、实验报告正文（来自 WB1 归档）")
        parts.append("")
        parts.append(body.strip())
        parts.append("")
        parts.append("---")
        parts.append("")

    # ---------- 1. 明文内联数据 ----------
    if args.json:
        parts.append("## 二、核心数据明文内联（D2-b）")
        parts.append("")
        for i, rel in enumerate(args.json, 1):
            p = (ROOT / rel) if not Path(rel).is_absolute() else Path(rel)
            parts.append(f"### 2.{i} `{Path(rel).name}`")
            if not p.exists():
                parts.append("")
                parts.append("```text")
                parts.append(f"⚠ 文件不存在: {rel}")
                parts.append("```")
                parts.append("")
                continue
            text, envnote = _fmt_json(p)
            parts.append("")
            parts.append(f"**环境标签**：`{envnote}`")
            parts.append("")
            parts.append("```json")
            parts.append(text)
            parts.append("```")
            parts.append("")
        parts.append("---")
        parts.append("")

    # ---------- 2. 台账状态 ----------
    if args.ledger_ids:
        entries = _ledger_entries(args.ledger_ids)
        parts.append("## 三、相关假设台账状态")
        parts.append("")
        if not entries:
            parts.append(f"⚠ 未找到假设: {', '.join(args.ledger_ids)}")
        else:
            parts.append("| ID | 状态 | 可证伪标准（摘要） | 数据来源 |")
            parts.append("|---|---|---|---|")
            for h in entries:
                fc = str(h.get("falsifiable_criterion", ""))[:120].replace("|", "／")
                ds = str(h.get("data_source", "")).replace("|", "／")
                parts.append(f"| {h.get('id')} | **{h.get('status')}** | {fc} | {ds} |")
            parts.append("")
            for h in entries:
                parts.append(f"**{h.get('id')}**：{h.get('statement', '')}")
                parts.append("")
                parts.append(f"> {h.get('fate_note', '')}")
                parts.append("")
        parts.append("---")
        parts.append("")

    # ---------- 3. 纪律检验 ----------
    if args.discipline_round:
        r = _discipline_round(args.discipline_round)
        parts.append(f"## 四、纪律检验（{args.discipline_round}）")
        parts.append("")
        if not r:
            parts.append(f"⚠ 台账中无 {args.discipline_round}")
        else:
            s = r.get("summary") or {}
            parts.append(f"**判定：{s.get('verdict')}** "
                         f"（PASS {s.get('pass')} / WARN {s.get('warn')} / FAIL {s.get('fail')}）")
            parts.append("")
            parts.append("| 检查项 | 状态 | 说明 |")
            parts.append("|---|---|---|")
            for c in r.get("checks", []):
                det = str(c.get("detail", "")).replace("\n", " ").replace("|", "／")[:90]
                parts.append(f"| {c.get('check')} | {c.get('status')} | {det} |")
            parts.append("")
        parts.append("---")
        parts.append("")

    # ---------- 4. 悬而未决 ----------
    if args.open_questions:
        parts.append("## 五、悬而未决的问题（请 DS 裁决）")
        parts.append("")
        for q in args.open_questions:
            parts.append(f"{q}")
        parts.append("")
        parts.append("---")
        parts.append("")

    # ---------- 5. 文件清单（仅供 CEO） ----------
    parts.append("## 六、文件清单（仅供 CEO 溯源 · DS 可忽略）")
    parts.append("")
    parts.append("| 文件 | 角色 |")
    parts.append("|---|---|")
    if rep_path.exists():
        parts.append(f"| `{args.report}` | 报告归档 |")
    for rel in (args.json or []):
        parts.append(f"| `{rel}` | 原始数据 |")
    parts.append("| `results/hypothesis_ledger.yaml` | 假设台账 |")
    parts.append("| `results/discipline_ledger.yaml` | 纪律台账 |")
    parts.append("")

    text = "\n".join(parts)

    # ---------- 安全检查 ----------
    hit = SECRET_RE.search(text)
    if hit:
        raise SystemExit("[FATAL] 投喂包含疑似真实 token，已中止。请先清理源文件。")

    out = Path(args.out) if args.out else \
        RESULTS / "_ds_brief" / f"{now.strftime('%Y%m%d_%H%M%S')}_brief.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成给 DeepSeek 的投喂包（无链接·全明文）")
    ap.add_argument("--report", help="报告 .md 路径（可省略，只贴 JSON）")
    ap.add_argument("--json", nargs="*", default=[], help="要内联的 JSON（空格分隔）")
    ap.add_argument("--ledger-ids", nargs="*", default=[], help="要附上的假设 ID")
    ap.add_argument("--discipline-round", default=None, help="附上某轮纪律检验，如 R17")
    ap.add_argument("--auto", action="store_true",
                    help="智能模式：按本轮 git 变更自动挑选全部素材（推荐）")
    ap.add_argument("--open-questions", nargs="*", default=[], help="悬而未决问题（每条一个参数）")
    ap.add_argument("--title", default="WB1 交付简报")
    ap.add_argument("--out", default=None, help="输出路径（默认 results/_ds_brief/<时间戳>_brief.md）")
    args = ap.parse_args()

    if args.auto:
        rep, jsons, ids, rnd = autodetect()
        args.report = args.report or rep
        args.json = args.json or jsons
        args.ledger_ids = args.ledger_ids or ids
        args.discipline_round = args.discipline_round or rnd
        print(f"[auto] 报告={rep} | JSON={len(args.json)} 份 | 假设={args.ledger_ids} "
              f"| 纪律={args.discipline_round}")

    if not args.report and not args.json:
        ap.error("至少给 --report / --json / --auto 之一")

    out = build(args)
    text = out.read_text(encoding="utf-8")
    n = len(text)

    ok, rows = self_check(text)
    print(f"\n[OK] 投喂包已生成：{out}")
    print(f"     体积 {n} 字符 / {n/1024:.1f} KB")
    print("\n----- 发送前自检 -----")
    for name, status, detail in rows:
        icon = "✔" if status == "PASS" else "△"
        print(f" [{icon}] {name}: {status}  {detail}")
    print(" ---------------------")
    if ok:
        print(" ⟹ 全部合规。整段全选复制，直接粘给 DS 即可。")
    else:
        print(" ⟹ 有 WARN 项，请先处理再发送（DS 读不到链接，缺失项会被静默忽略）。")
    print(" 提示：DS 无浏览器，请勿改发链接或路径。")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
