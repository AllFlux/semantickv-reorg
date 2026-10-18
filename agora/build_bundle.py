#!/usr/bin/env python3.11
"""
build_bundle.py — Agora 数据构建脚本（阶段一：自动同步）

把「单一事实来源」semantickv-reorg/results/ 下的协作文件
（hypothesis_ledger.yaml / discipline_ledger.yaml / ds_reviews/*.md）
规范化为前端可直接消费的 data/bundle.json。

设计原则：
  - 不修改任何源文件，前端只读 bundle.json
  - 源文件（YAML/MD）始终是 DS/WB 可核查的原始载体（Raw 链接）
  - 每次 WB 跑完实验、更新台账后，重跑本脚本即可刷新看板

用法：
  python3 build_bundle.py [--source ../semantickv-reorg/results] [--out data/bundle.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    sys.exit("缺少 PyYAML，请先执行: sudo pip3 install pyyaml")


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def split_frontmatter(text: str):
    """拆分 DS 评审的 YAML frontmatter 与 markdown 正文。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, m.group(2).strip()


def load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build(source_dir: str, out_path: str) -> dict:
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        sys.exit(f"源目录不存在: {source_dir}")

    bundle: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": source_dir,
        "discipline": None,
        "hypotheses": None,
        "reviews": [],
        "open_items": [],
    }

    # ---- 1. 假设台账 ----
    hyp_path = os.path.join(source_dir, "hypothesis_ledger.yaml")
    if os.path.isfile(hyp_path):
        data = load_yaml(hyp_path)
        bundle["hypotheses"] = {
            "meta": data.get("meta", {}),
            "items": data.get("hypotheses", []),
        }
        # 开放项：被证伪但未显式承认
        for h in data.get("hypotheses", []):
            if h.get("status") == "refuted" and not h.get("ds_admitted_refutation", False):
                bundle["open_items"].append(h.get("id"))

    # ---- 2. 纪律台账 ----
    dis_path = os.path.join(source_dir, "discipline_ledger.yaml")
    if os.path.isfile(dis_path):
        data = load_yaml(dis_path)
        rounds = data.get("rounds", [])
        last = rounds[-1] if rounds else None
        bundle["discipline"] = {
            "meta": {k: v for k, v in data.items() if k != "rounds"},
            "rounds": rounds,
            "last_round": last,
            "last_verdict": (last or {}).get("summary", {}).get("verdict") if last else None,
        }

    # ---- 3. DS 评审画廊 ----
    reviews_dir = os.path.join(source_dir, "ds_reviews")
    if os.path.isdir(reviews_dir):
        files = sorted(
            f for f in os.listdir(reviews_dir) if f.endswith(".md")
        )
        for fn in files:
            with open(os.path.join(reviews_dir, fn), "r", encoding="utf-8") as f:
                text = f.read()
            meta, body = split_frontmatter(text)
            bundle["reviews"].append(
                {
                    "file": fn,
                    "meta": meta,
                    "body": body,
                }
            )

    # 兜底：YAML 会把未加引号的 2026-10-18 解析成 date，需转回字符串
    def _default(o):
        if isinstance(o, (datetime, __import__("datetime").date)):
            return o.isoformat()
        return str(o)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2, default=_default)

    n_hyp = len(bundle["hypotheses"]["items"]) if bundle["hypotheses"] else 0
    n_rev = len(bundle["reviews"])
    print(f"[Agora] 已构建 bundle.json")
    print(f"  - 假设台账: {n_hyp} 条, 开放项(open_items): {bundle['open_items']}")
    print(f"  - 纪律回合: {len(bundle['discipline']['rounds']) if bundle['discipline'] else 0} 轮, 末轮判定: {bundle['discipline']['last_verdict'] if bundle['discipline'] else None}")
    print(f"  - DS 评审: {n_rev} 份")
    print(f"  -> {out_path}")
    return bundle


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    # Agora 作为项目仓库的子目录（<repo>/agora/）时，源数据为 <repo>/results/。
    # 提供多个候选路径以兼容不同布局，取第一个存在的。
    candidates = [
        os.path.join(here, "..", "results"),
        os.path.join(here, "..", "semantickv-reorg", "results"),
        os.path.join(here, "results"),
    ]
    default_source = next((c for c in candidates if os.path.isdir(c)), candidates[0])
    ap.add_argument("--source", default=default_source, help="results/ 目录路径")
    ap.add_argument("--out", default=os.path.join(here, "data", "bundle.json"), help="输出 bundle.json")
    args = ap.parse_args()
    build(args.source, args.out)
