#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ds_review.py — DeepSeek 侧对等纪律检验（DISCIPLINE.md §DeepSeek 对等纪律）。

校验：
  DS-D1 指令结构化   —— results/ds_reviews/*.md 必须含 YAML frontmatter
                        (review_id/source_url/date/hypotheses) + 三段式正文
                        (理论背景/具体步骤/验收标准)
  DS-D2 假设可证伪   —— 每条 hypothesis 必须有 falsifiable_criterion 与 data_source
  DS-D3 引用可核查   —— 含文献/外部引用的评审须在 frontmatter 登记 sources
  DS-D4 结论对齐数据 —— hypothesis_ledger.yaml 中 refuted 假设须有存在的
                        evidence_file；未承认者列为待办（WARN）
  DS-D5 假设台账状态 —— 台账条目 status 合法；refuted/confirmed 须有 evidence_file；
                        评审提出的假设不得被静默丢弃（须入台账）

被 tools/check_discipline.py 调用，作为 DS-D1..DS-D5 检验并入每轮台账。
也可独立运行：`python3 tools/check_ds_review.py`。

设计原则（审慎）：硬性约束只卡「可机器证明」的部分；
DS「是否承认被推翻假设」设为 WARN 待办而非直接判红——承认需 DS 未来行动，
把 DS 的沉默算作 WB 账面的失败不公平。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
DS_REVIEWS = RESULTS / "ds_reviews"
HLEDGER = RESULTS / "hypothesis_ledger.yaml"

REQUIRED_SECTIONS = ["理论背景", "具体步骤", "验收标准"]
# 实质性引用标记（区别于 frontmatter 的 source_url 或元讨论中的「文献」二字）
LIT_KW = ["研究表明", "据研究", "论文指出", "arXiv", "参考文献", "据论文"]


def _ok(name: str, detail: str) -> Dict[str, Any]:
    return {"check": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str) -> Dict[str, Any]:
    return {"check": name, "status": "FAIL", "detail": detail}


def _warn(name: str, detail: str) -> Dict[str, Any]:
    return {"check": name, "status": "WARN", "detail": detail}


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None


def _load_yaml(p: Path) -> Any:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _review_files() -> List[Path]:
    return sorted(DS_REVIEWS.glob("*.md")) if DS_REVIEWS.exists() else []


# ---------------------------------------------------------------------
# DS-D1 指令结构化
# ---------------------------------------------------------------------
def check_ds_d1() -> Dict[str, Any]:
    files = _review_files()
    if not files:
        return _warn("DS-D1 指令结构化", "尚无 DS 评审落盘（intake 协议待触发）")
    problems: List[str] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        if fm is None:
            problems.append(f"{p.name}: 缺 YAML frontmatter")
            continue
        for fld in ("review_id", "source_url", "date", "hypotheses"):
            if fld not in fm:
                problems.append(f"{p.name}: frontmatter 缺 {fld}")
        for sec in REQUIRED_SECTIONS:
            if not re.search(rf"^#+\s*{sec}", text, re.M):
                problems.append(f"{p.name}: 缺段落『{sec}』")
    detail = "全部 DS 评审符合模板（frontmatter+三段式）" if not problems \
        else "；".join(problems)
    return (_ok if not problems else _fail)("DS-D1 指令结构化", detail)


# ---------------------------------------------------------------------
# DS-D2 假设可证伪 + 数据来源
# ---------------------------------------------------------------------
def check_ds_d2() -> Dict[str, Any]:
    problems: List[str] = []
    for p in _review_files():
        fm = _parse_frontmatter(p.read_text(encoding="utf-8")) or {}
        for h in (fm.get("hypotheses") or []):
            hid = h.get("id", "?")
            if not h.get("falsifiable_criterion"):
                problems.append(f"{p.name}/{hid}: 缺 falsifiable_criterion")
            if not h.get("data_source"):
                problems.append(f"{p.name}/{hid}: 缺 data_source")
    detail = "全部假设带可证伪标准与数据来源" if not problems else "；".join(problems)
    return (_ok if not problems else _fail)("DS-D2 假设可证伪+来源", detail)


# ---------------------------------------------------------------------
# DS-D3 引用可核查
# ---------------------------------------------------------------------
def check_ds_d3() -> Dict[str, Any]:
    problems: List[str] = []
    for p in _review_files():
        text = p.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text) or {}
        mentions_lit = any(kw in text for kw in LIT_KW)
        sources = fm.get("sources") or []
        if mentions_lit and not sources:
            problems.append(
                f"{p.name}: 含文献/外部引用但未在 frontmatter 登记 sources")
    if problems:
        return _warn("DS-D3 引用可核查",
                     "；".join(problems) + "（请补 sources 或确认无虚构引用）")
    return _ok("DS-D3 引用可核查",
               "DS 跨域/文献断言均登记可核查来源（或无外部引用）")


# ---------------------------------------------------------------------
# DS-D4 结论对齐数据（闭环）
# ---------------------------------------------------------------------
def check_ds_d4() -> Dict[str, Any]:
    ledger = _load_yaml(HLEDGER) or {}
    entries = ledger.get("hypotheses", []) if isinstance(ledger, dict) else []
    problems: List[str] = []
    open_items: List[str] = []
    for h in entries:
        if h.get("status") == "refuted":
            ef = h.get("evidence_file", "")
            if not ef or not (ROOT / ef).exists():
                problems.append(f"{h.get('id')}: refuted 但 evidence_file 缺失/不存在")
            elif not h.get("ds_admitted_refutation", False):
                open_items.append(h.get("id"))
    if problems:
        return _fail("DS-D4 结论对齐数据", "；".join(problems))
    if open_items:
        return _warn("DS-D4 结论对齐数据",
                     f"被数据推翻但未显式承认的假设（待 DS 下一轮承认）: {open_items}")
    return _ok("DS-D4 结论对齐数据",
               "refuted 假设均有数据证据；已承认或无需承认")


# ---------------------------------------------------------------------
# DS-D5 假设台账状态
# ---------------------------------------------------------------------
def check_ds_d5() -> Dict[str, Any]:
    ledger = _load_yaml(HLEDGER) or {}
    entries = ledger.get("hypotheses", []) if isinstance(ledger, dict) else []
    if not entries:
        return _warn("DS-D5 假设台账状态", "假设台账为空")
    problems: List[str] = []
    valid = {"pending", "confirmed", "refuted"}
    for h in entries:
        if h.get("status") not in valid:
            problems.append(f"{h.get('id')}: 非法 status={h.get('status')}")
        if h.get("status") in ("confirmed", "refuted") and not h.get("evidence_file"):
            problems.append(f"{h.get('id')}: {h.get('status')} 但缺 evidence_file")
    # 静默丢弃检查：评审中提出的 hypothesis id 必须都在台账
    review_ids = set()
    for p in _review_files():
        fm = _parse_frontmatter(p.read_text(encoding="utf-8")) or {}
        for h in (fm.get("hypotheses") or []):
            review_ids.add(h.get("id"))
    ledger_ids = {h.get("id") for h in entries}
    dropped = review_ids - ledger_ids
    if dropped:
        problems.append(f"DS 评审提出但未入台账的假设: {dropped}")
    detail = f"{len(entries)} 条假设状态合法" if not problems else "；".join(problems)
    return (_ok if not problems else _fail)("DS-D5 假设台账状态", detail)


def run_all() -> List[Dict[str, Any]]:
    return [check_ds_d1(), check_ds_d2(), check_ds_d3(),
            check_ds_d4(), check_ds_d5()]


def main() -> int:
    checks = run_all()
    n_pass = sum(1 for c in checks if c["status"] == "PASS")
    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    n_fail = sum(1 for c in checks if c["status"] == "FAIL")
    print("===== DeepSeek 侧对等纪律检验 =====")
    for c in checks:
        icon = {"PASS": "✔", "WARN": "△", "FAIL": "✘"}[c["status"]]
        print(f" [{icon}] {c['check']}: {c['status']}")
        for line in str(c["detail"]).splitlines():
            print(f"      {line}")
    verdict = "GREEN" if n_fail == 0 and n_warn == 0 else \
        ("GREEN-WITH-WARN" if n_fail == 0 else "RED")
    print(f"===== DS 总结: {n_pass}/{len(checks)} PASS, {n_warn} WARN, "
          f"{n_fail} FAIL -> {verdict} =====")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
