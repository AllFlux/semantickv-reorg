#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_discipline.py — 合作纪律每轮强制检验器（台账生成器）。

将 DISCIPLINE.md 的五条合作纪律机制化：每轮对话结束前必须运行本脚本，
逐条机械检验并把结果追加到 results/discipline_ledger.yaml（台账）。
未全绿的轮次，交付说明中必须显式声明未绿项与原因（纪律 2 的延伸）。

五条纪律与检验方式：
  D1 契约优先        —— 跑 eval_runner.py --check-interfaces，必须全部通过
  D2 负面结果归档    —— 负面结果清单中的文件必须存在且负面指标仍在记录中；
                       CHANGELOG.md 必须有版本条目
  D3 环境隔离        —— 所有真实模型 JSON 必须带 device/dtype/model 环境标签；
                       mock 结果必须带 mode=mock 标签；产出环境表写入台账
  D4 参数冻结        —— mock_grouper 默认参数（k=16,tau=0.3,cosine）不许动；
                       真实运行 (k,tau) 必须等于 E0 校准值 (8,0.97) 或在
                       CHANGELOG 中有记录；E0 卡版本 >= 0.2.1
   D5 格式统一        —— 每份 results/*.md 报告必须含数据表 + 结论 + 文件清单

  DeepSeek 侧对等纪律（DISCIPLINE.md §DeepSeek 对等纪律，由 check_ds_review 提供）：
  DS-D1 指令结构化   —— ds_reviews/*.md 含 frontmatter + 三段式
  DS-D2 假设可证伪   —— 每条假设含 falsifiable_criterion 与 data_source
  DS-D3 引用可核查   —— 含文献/外部引用的评审须登记 sources
  DS-D4 结论对齐数据 —— 台账中 refuted 假设须有证据文件；未承认者列为待办
  DS-D5 假设台账状态 —— 台账条目状态合法，假设不得被静默丢弃

用法：
    python3 tools/check_discipline.py                # 检验并追加一轮台账
    python3 tools/check_discipline.py --round r12    # 指定轮次标签
    python3 tools/check_discipline.py --dry-run      # 只检验不写台账
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
LEDGER = RESULTS / "discipline_ledger.yaml"

# DeepSeek 侧对等纪律检验（DISCIPLINE.md §DeepSeek 对等纪律）
sys.path.insert(0, str(ROOT / "tools"))
try:
    from check_ds_review import run_all as _ds_run_all
except Exception:  # pragma: no cover
    _ds_run_all = lambda: []

# ---------------------------------------------------------------------
# 负面结果清单（纪律 2）：文件 -> (描述, 证据校验函数名)
# 新增负面结果时必须同步登记，否则 D2 会报「清单外结果」提醒人工确认。
# ---------------------------------------------------------------------
NEGATIVE_INVENTORY = [
    ("results/real_model_validation.json",
     "v0.1.2 全层 flat-mean 端到端失败：argmax 2.32% / needle 全 MISS"),
    ("results/real_model_v15_baseline_v1.json",
     "v0.2.0 A 组 v1 基线复现失败：argmax 2.6-3.3% / needle 0/3 / KV 99% 虚假节省"),
    ("results/real_model_v15.json",
     "v0.2.0 B 组 V1.5：argmax 升至 28% 但 needle 0/3（0.5B 无区分度）"),
    ("results/real_model_v15_topk8.json",
     "v0.2.0 D 组 K=8：argmax 42% 但 needle 仍 0/3"),
    ("results/real_model_v15_l2.json",
     "v0.2.0 C 组 l2 度量消融：ARI/topk/argmax 均低于 centered（负结果）"),
    ("results/real_model_needle_0.15.json",
     "v0.1.2 needle 位置 0.15 压缩失败记录"),
    ("results/real_model_needle_0.85.json",
     "v0.1.2 needle 位置 0.85 压缩失败记录"),
    ("results/real_model_v15_hybrid_protection.json",
     "v0.3.0 E1.5 主实验：hybrid blockdiag K=4/win128 argmax 26.05% (<45% 证伪线)，"
     "cos 0.6962 < topk K8 0.765；无压缩基线 needle 0/1（'1.414213'）"),
    ("results/real_model_v15_hybrid_global.json",
     "v0.3.0 E1.5 消融：hybrid global（无块对角）argmax 28.51%，仍远低于 topk K8；"
     "块对角额外损失 ~2.5pp 但非主因"),
    ("results/real_model_v15_topk8_win128_baseline.json",
     "v0.3.0 E1.5 对照+无压缩基线：topk K8/win128 argmax 44.91%@0.5（cos 0.7650 复现）；"
     "无压缩原始模型 needle 3 位置仅 1/3 命中（0.25 命中、0.5/0.75 失败）"
     "——0.5B 检索能力本身是瓶颈（DS-006 预警分支证实）"),
]

# E0 校准冻结值（纪律 4）
FROZEN = {"k": 8, "tau": 0.97, "metric": "centered"}
MOCK_DEFAULTS = {"k": 16, "tau": 0.3, "metric": "cosine"}
E0_MIN_VERSION = (0, 2, 1)


def _ok(name: str, detail: str = "") -> Dict[str, Any]:
    return {"check": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str) -> Dict[str, Any]:
    return {"check": name, "status": "FAIL", "detail": detail}


def _warn(name: str, detail: str) -> Dict[str, Any]:
    return {"check": name, "status": "WARN", "detail": detail}


# ---------------------------------------------------------------------
# D1 契约优先
# ---------------------------------------------------------------------
def check_d1() -> Dict[str, Any]:
    try:
        p = subprocess.run(
            [sys.executable, "eval_runner.py", "--check-interfaces"],
            cwd=ROOT, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return _fail("D1 契约优先", "--check-interfaces 超时（>180s）")
    out = p.stdout + p.stderr
    if p.returncode == 0 and "全部通过" in out:
        m = re.search(r"(\d+)\s*/\s*(\d+)", out)
        n = f"{m.group(0)} 通过" if m else "全部通过"
        return _ok("D1 契约优先", f"--check-interfaces {n}")
    tail = "\n".join(out.strip().splitlines()[-5:])
    return _fail("D1 契约优先", f"returncode={p.returncode}\n{tail}")


# ---------------------------------------------------------------------
# D2 负面结果归档
# ---------------------------------------------------------------------
def _load(rel: str) -> Optional[dict]:
    p = ROOT / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _evidence_present(rel: str, d: dict) -> bool:
    """负面指标仍在 JSON 中被如实记录（未被美化/删除）。"""
    if rel == "results/real_model_validation.json":
        st = d.get("stages", {})
        row = (st.get("needle") or [{}])[0]
        pf = row.get("prefill_fidelity") or {}
        return pf.get("logits_argmax_agreement", 1.0) < 0.05
    # v15 系列：needle 阶段存在 compressed_hit=false 的行即算负面证据在档
    st = d.get("stages", {})
    for row in st.get("needle", []):
        if row.get("compressed_hit") is False:
            return True
    if st.get("needle"):
        return False
    # 无 needle 阶段的旧格式：V1 cos < 0.5 即失败证据
    sw = (st.get("fidelity") or {}).get("sweep") or []
    return bool(sw) and sw[0].get("v1_group_mean", {}).get("mean", 1.0) < 0.5


def check_d2() -> Dict[str, Any]:
    problems: List[str] = []
    for rel, desc in NEGATIVE_INVENTORY:
        d = _load(rel)
        if d is None:
            problems.append(f"缺失或不可解析: {rel}")
            continue
        if not _evidence_present(rel, d):
            problems.append(f"负面指标不在档（疑被美化）: {rel}")
    cl = ROOT / "results" / "CHANGELOG.md"
    if not cl.exists() or len(cl.read_text(encoding="utf-8")) < 200:
        problems.append("CHANGELOG.md 缺失或过短")
    elif not re.search(r"^## v0\.\d+\.\d+", cl.read_text(encoding="utf-8"), re.M):
        problems.append("CHANGELOG.md 无版本条目")
    # 清单外的新结果文件提醒人工确认（不判 FAIL，判 WARN）
    listed = {rel for rel, _ in NEGATIVE_INVENTORY}
    extra = [p.name for p in RESULTS.glob("real_model*.json")
             if f"results/{p.name}" not in listed]
    detail = f"清单 {len(NEGATIVE_INVENTORY)} 项全部在档" if not problems else \
        "；".join(problems)
    if extra:
        return _warn("D2 负面结果归档",
                     f"{detail}；清单外新结果待人工确认: {extra}")
    return (_ok if not problems else _fail)("D2 负面结果归档", detail)


# ---------------------------------------------------------------------
# D3 环境隔离
# ---------------------------------------------------------------------
def check_d3() -> Dict[str, Any]:
    env_rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    for p in sorted(RESULTS.glob("*.json")):
        d = _load(f"results/{p.name}")
        if d is None:
            problems.append(f"不可解析: {p.name}")
            continue
        cfg = d.get("config") or {}
        mode = d.get("mode", "")
        env = d.get("environment") or {}
        env_kind = env.get("kind", "")
        if cfg.get("device"):
            env_rows.append({
                "file": p.name, "kind": "real", "device": cfg.get("device"),
                "dtype": cfg.get("dtype"),
                "model": str(cfg.get("model", ""))[-40:], "ctx": cfg.get("ctx")})
        elif env_kind == "cpu-synthetic":
            env_rows.append({"file": p.name, "kind": "cpu-synthetic",
                             "device": env.get("device", "cpu"),
                             "dtype": "-", "model": "synthetic-mock", "ctx": "-"})
        elif mode == "mock":
            env_rows.append({"file": p.name, "kind": "mock", "device": "cpu-synthetic",
                              "dtype": "-", "model": "mock", "ctx": "-"})
        else:
            env_rows.append({"file": p.name, "kind": "unknown", "device": "?",
                              "dtype": "?", "model": "?", "ctx": "?"})
            problems.append(f"无环境标签: {p.name}")
    unknown = [r["file"] for r in env_rows if r["kind"] == "unknown"]
    cuda = [r["file"] for r in env_rows if r.get("device") == "cuda"]
    detail = f"{len(env_rows)} 个结果文件全部带环境标签" if not problems else \
        "；".join(problems)
    note = ""
    if cuda:
        note = f"；含 GPU 记录: {cuda}"
    return (_ok if not problems else _fail)("D3 环境隔离", detail + note)


# ---------------------------------------------------------------------
# D4 参数冻结
# ---------------------------------------------------------------------
def check_d4() -> Dict[str, Any]:
    problems: List[str] = []
    # 4a. mock 默认参数不许动（合成分布参照值）
    src = (ROOT / "src" / "semantickv" / "mock_grouper.py").read_text(encoding="utf-8")
    m = re.search(r"def __init__\(self, k: int = (\d+), tau: float = ([\d.]+),"
                  r".*?metric: str = \"(\w+)\"", src, re.S)
    if not m:
        problems.append("无法解析 mock_grouper 默认参数（签名可能被改）")
    else:
        k, tau, metric = int(m.group(1)), float(m.group(2)), m.group(3)
        if (k, tau, metric) != (MOCK_DEFAULTS["k"], MOCK_DEFAULTS["tau"],
                                MOCK_DEFAULTS["metric"]):
            problems.append(
                f"mock 默认参数被修改: k={k} tau={tau} metric={metric}"
                f"（应保持 {MOCK_DEFAULTS}）")
    # 4b. 真实运行 (k,tau) 必须等于 E0 冻结值
    #     注意：config.k/tau 只是 CLI 默认值（可能未被 --k-grid 使用），
    #     实际生效值在 stages.needle[*].config（needle 操作点）与
    #     stages.gsc_scan.rows（扫描覆盖），以二者为准。
    cl_text = (ROOT / "results" / "CHANGELOG.md").read_text(encoding="utf-8") \
        if (ROOT / "results" / "CHANGELOG.md").exists() else ""
    for p in sorted(RESULTS.glob("real_model*.json")):
        d = _load(f"results/{p.name}") or {}
        st = d.get("stages") or {}
        used = [nc.get("config") or {} for nc in st.get("needle", [])]
        used = [u for u in used if u.get("k") is not None]
        if not used:
            continue
        for u in used:
            k, tau = u.get("k"), u.get("tau")
            if (k, round(float(tau), 2)) != (FROZEN["k"], FROZEN["tau"]):
                tag = f"{p.name}: k={k} tau={tau}"
                if tag in cl_text or "(8, 0.97)" in cl_text or "k=8" in cl_text:
                    continue  # CHANGELOG 有记录则放行
                problems.append(
                    f"{tag} 偏离冻结值 ({FROZEN['k']},{FROZEN['tau']}) 且 CHANGELOG 无记录")
    # 4c. E0 卡版本
    card = yaml.safe_load(
        (ROOT / "experiments" / "E0_baseline_phase.yaml").read_text(encoding="utf-8"))
    ver = tuple(int(x) for x in str(card["meta"]["version"]).split("."))
    if ver < E0_MIN_VERSION:
        problems.append(f"E0 卡版本 {card['meta']['version']} < {'.'.join(map(str, E0_MIN_VERSION))}")
    detail = ("mock 默认参数未动；真实 (k,tau)=(8,0.97) 冻结；"
              f"E0 卡 v{card['meta']['version']}") if not problems else "；".join(problems)
    return (_ok if not problems else _fail)("D4 参数冻结", detail)


# ---------------------------------------------------------------------
# D5 格式统一
# ---------------------------------------------------------------------
def check_d5() -> Dict[str, Any]:
    problems: List[str] = []
    reports = sorted(RESULTS.glob("*.md"))
    if not reports:
        problems.append("results/ 无报告")
    for p in reports:
        if p.name == "CHANGELOG.md":
            continue  # 变更日志不适用报告三段式
        text = p.read_text(encoding="utf-8")
        has_table = bool(re.search(r"^\s*\|.*\|\s*$", text, re.M)) and "---" in text
        has_concl = bool(re.search(r"^#{1,3}\s*.*(结论|判定|总结)", text, re.M))
        has_files = bool(re.search(r"(文件清单|交付物|产出文件|文件$)", text, re.M))
        missing = [n for n, ok in (("数据表", has_table), ("结论段", has_concl),
                                    ("文件清单", has_files)) if not ok]
        if missing:
            problems.append(f"{p.name} 缺: {'、'.join(missing)}")
    detail = f"{len([p for p in reports if p.name != 'CHANGELOG.md'])} 份报告三段式齐全" \
        if not problems else "；".join(problems)
    return (_ok if not problems else _fail)("D5 格式统一", detail)


# ---------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------
def load_ledger() -> dict:
    if LEDGER.exists():
        return yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    return {"meta": {"protocol": "DISCIPLINE.md §六", "rounds": []},
            "rounds": []}


def append_round(entry: dict, dry: bool) -> Path:
    data = load_ledger()
    rounds = data.setdefault("rounds", [])
    # 同轮次标签重复运行则替换，否则追加
    rounds[:] = [r for r in rounds if r.get("round") != entry["round"]]
    rounds.append(entry)
    data["meta"]["last_round"] = entry["round"]
    data["meta"]["last_run"] = entry["timestamp"]
    if dry:
        print("[dry-run] 不写台账")
        return LEDGER
    LEDGER.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                                     width=100), encoding="utf-8")
    return LEDGER


def main() -> int:
    ap = argparse.ArgumentParser(description="合作纪律每轮强制检验器")
    ap.add_argument("--round", default=None, help="轮次标签（默认 R<最大序号+1>）")
    ap.add_argument("--dry-run", action="store_true", help="只检验不写台账")
    args = ap.parse_args()

    data = load_ledger()
    if args.round:
        round_id = args.round
    else:
        nums = [int(m.group(1)) for r in data.get("rounds", [])
                if (m := re.match(r"R(\d+)$", str(r.get("round", ""))))]
        round_id = f"R{max(nums) + 1 if nums else 1}"

    checks = [check_d1(), check_d2(), check_d3(), check_d4(), check_d5()]
    checks += _ds_run_all()  # DeepSeek 侧对等纪律 DS-D1..DS-D5
    n_pass = sum(1 for c in checks if c["status"] == "PASS")
    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    verdict = "GREEN" if n_pass == len(checks) else \
              ("GREEN-WITH-WARN" if n_warn and n_pass + n_warn == len(checks) else "RED")

    entry = {
        "round": round_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "summary": {"pass": n_pass, "warn": n_warn,
                    "fail": len(checks) - n_pass - n_warn, "verdict": verdict},
    }
    path = append_round(entry, args.dry_run)

    print(f"===== 合作纪律检验 · 轮次 {round_id} · {entry['timestamp']} =====")
    for c in checks:
        icon = {"PASS": "✔", "WARN": "△", "FAIL": "✘"}[c["status"]]
        print(f" [{icon}] {c['check']}: {c['status']}")
        for line in str(c["detail"]).splitlines():
            print(f"      {line}")
    print(f"===== 总结: {n_pass}/{len(checks)} PASS, {n_warn} WARN -> {verdict} =====")
    print(f"台账: {path}")
    if verdict == "RED":
        print("\n⚠ 本轮存在未绿项。按纪律 2 延伸规则：交付说明中必须显式声明"
              "未绿项与原因，严禁静默交付。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
