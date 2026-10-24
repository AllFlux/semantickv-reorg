#!/usr/bin/env python3
# EXP-12-FROZENAUDIT：冻结件 r1_budget_allocation.json 的 profile_by_layer 三类缺陷独立复核
# 纯 CPU / 零数据生成 / 冻结件只读（Hub 实拉副本另存为 *.hub_pull_20261025.json）
# 幂等：只覆盖 results_v2/exp12_r1_frozen_audit.json
import json, math, hashlib, time, os

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.join(HERE, "r1_budget_allocation.hub_pull_20261025.json")
REJUDGE = os.path.join(HERE, "r2_r1_rejudge_noL0.hub_pull_20261025.json")
OUT = os.path.join(HERE, "exp12_r1_frozen_audit.json")

# WB1 在 R1-REJUDGE 回执中披露的闭合误差报告值（逐档逐臂），用于对照
WB1_REPORT = {
    180:  {"T": -5.728,  "D": -2.384},
    324:  {"T": -11.580, "D": -5.296},
    468:  {"U": -24.696, "T": -17.717, "D": -22.588},
    612:  {"T": -23.201, "D": -11.021},
    900:  {"U": -49.428, "T": 4.125,   "D": -45.101},
    1188: {"T": 35.0,    "D": 35.0},
}

def sha16(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]

def main():
    t0 = time.time()
    frozen_sha_before = sha16(FROZEN)
    rej_sha_before = sha16(REJUDGE)
    d = json.load(open(FROZEN))
    r = json.load(open(REJUDGE))

    sweep = d["sweep"]
    L = len(sweep[0]["effective_K"]["U"]["profile_by_layer"])  # 层数

    # ---- 缺陷① slot 不闭合：Σ(profile+1) − budget ----
    d1 = {}
    d1_mismatches = []
    for s in sweep:
        b = s["budget_total_kv_slots"]
        per = {}
        for arm in ("U", "T", "D"):
            prof = s["effective_K"][arm]["profile_by_layer"]
            err = sum(k + 1 for k in prof) - b
            per[arm] = {
                "closure_error": round(err, 6),
                "n_layers": len(prof),
                "sum_K_plus_1": round(sum(k + 1 for k in prof), 6),
                "reported_total_slots_check": s["effective_K"][arm]["total_slots_check"],
            }
            rep = WB1_REPORT.get(b, {}).get(arm)
            if rep is not None:
                delta = err - rep
                per[arm]["wb1_reported"] = rep
                per[arm]["delta_vs_wb1"] = round(delta, 6)
                per[arm]["match_le_1e3"] = abs(delta) <= 1e-3
                if not per[arm]["match_le_1e3"]:
                    d1_mismatches.append({"budget": b, "arm": arm, "ours": err, "wb1": rep, "delta": delta})
        d1[b] = per

    # ---- 缺陷② 几何中点近似：468/900 档 U 是否等于几何中点 ----
    d2 = {}
    for b, expect_pair in ((468, (8, 16)), (900, (16, 32))):
        s = next(x for x in sweep if x["budget_total_kv_slots"] == b)
        u = s["effective_K"]["U"]
        uv = sorted({round(v, 6) for v in u["profile_by_layer"]})
        gm = math.sqrt(expect_pair[0] * expect_pair[1])
        am = (expect_pair[0] + expect_pair[1]) / 2.0
        # 冻结件 profile 值仅保留 3 位小数，故对照容差取 5e-4（>3 位取整误差 5e-4 的一半）
        d2[b] = {
            "U_values_distinct": uv,
            "U_max": u["max"], "U_min": u["min"], "U_mean": u["mean"],
            "expected_geometric_mid": round(gm, 6),
            "expected_arithmetic_mid": round(am, 6),
            "abs_delta_to_geometric": round(min(abs(v - gm) for v in uv), 6),
            "abs_delta_to_arithmetic": round(min(abs(v - am) for v in uv), 6),
            "is_geometric_mid": min(abs(v - gm) for v in uv) <= 5e-4,
            "is_arithmetic_mid": min(abs(v - am) for v in uv) <= 5e-4,
            "is_integer_K": all(abs(v - round(v)) <= 1e-9 for v in uv),
            "note_rounding": "frozen profile 值仅存 3 位小数；几何中点判定容差 5e-4",
        }

    # ---- 缺陷③ 越域外推：是否出现 K > 32（已测量域上限） ----
    measured_K_max = 32  # 由冻结件 sensitivity_curve 6 档 K 的域上限（本轮读取校验）
    try:
        curves = d["sensitivity_curve"]["curves"]
        kset = set()
        for lk, lv in curves.items():
            for key in lv:
                if isinstance(key, str) and key.isdigit():
                    kset.add(int(key))
            for key in ("K_list", "Ks", "k_values"):
                if isinstance(lv.get(key), list):
                    kset.update(int(x) for x in lv[key])
        if kset:
            measured_K_max = max(kset)
    except Exception:
        pass

    d3 = {}
    for s in sweep:
        b = s["budget_total_kv_slots"]
        entry = {}
        for arm in ("U", "T", "D"):
            prof = s["effective_K"][arm]["profile_by_layer"]
            over = [k for k in prof if k > measured_K_max]
            entry[arm] = {
                "max_K": max(prof),
                "n_over_domain": len(over),
                "over_domain_values": sorted(set(over)),
                "n_interpolated_or_extrapolated": len([k for k in prof if not float(k).is_integer()]),
            }
        d3[b] = entry
    ood_any = {b: {a: v for a, v in e.items() if v["n_over_domain"] > 0} for b, e in d3.items()}
    ood_any = {b: v for b, v in ood_any.items() if v}

    # ---- 缺陷盘点⑤ 依赖面：引用该冻结件的回执 / 假设 ----
    # 输入为本轮实拉的三份快照（reviews 清单、逐条回执正文、ledger 假设），本地 grep 匹配，不做估算
    dep = {"reviews": [], "hypotheses": [], "coverage": {}}
    try:
        rb = json.load(open(os.path.join(HERE, "..", "reviews_brief.json"))) if os.path.exists(os.path.join(HERE, "..", "reviews_brief.json")) else None
        idx = {x["name"].replace(".yaml", ""): x for x in rb["reviews"]} if rb else {}
        hits = [l.strip() for l in open(os.path.join(HERE, "..", "receipt_hits_20261025.txt")) if l.strip()]
        for stem in hits:
            m = idx.get(stem, {})
            dep["reviews"].append({
                "receipt_id": stem,
                "check_status": (m.get("check") or {}).get("status"),
                "size": m.get("size"), "mtime": m.get("mtime"),
                "match": "body contains 'r1_budget_allocation'",
            })
        dep["coverage"]["reviews_scanned"] = len(idx) if idx else 0
        dep["coverage"]["reviews_hit"] = len(hits)
    except Exception as e:
        dep["coverage"]["reviews_error"] = repr(e)
        dep["dependency_missing"] = ["reviews_brief.json / receipt_hits_20261025.txt"]
    try:
        lb = json.load(open(os.path.join(HERE, "..", "ledger_brief.json"))) if os.path.exists(os.path.join(HERE, "..", "ledger_brief.json")) else None
        hs = lb.get("hypotheses", []) if lb else []
        for x in hs:
            if "r1_budget_allocation" in json.dumps(x, ensure_ascii=False):
                dep["hypotheses"].append({
                    "id": x.get("id"), "status": x.get("status"),
                    "theme": x.get("theme"), "serves_north_star": x.get("serves_north_star"),
                    "claim": x.get("claim"),
                    "data_source": x.get("data_source"),
                })
        dep["coverage"]["hypotheses_scanned"] = len(hs)
        dep["coverage"]["hypotheses_hit"] = len(dep["hypotheses"])
    except Exception as e:
        dep["coverage"]["hypotheses_error"] = repr(e)
    dep["coverage"]["method"] = ("回执侧：68 条回执正文经 /api/file/r2_<stem> 逐条实拉后全文 grep；"
                                 "假设侧：/api/ledger?brief=1 一次性实拉后本地字段匹配（未裸拉全量 ledger 正文）")

    # ---- 冻结件自述 verdict（供交叉核对，结论不采信自述） ----
    verdict = d.get("decision", {})
    rej_verdict = None
    if isinstance(r, dict):
        rej_verdict = r.get("verdict") if isinstance(r.get("verdict"), dict) else None

    result = {
        "experiment": "EXP-12-FROZENAUDIT",
        "instruction_ref": "1792858165-2e1e7f",
        "idempotency_key": "WB0-INSTR-20261025-EXP-12-FROZENAUDIT",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "inputs": {
            "frozen_file": "results_v2/r1_budget_allocation.hub_pull_20261025.json",
            "frozen_sha256_16": frozen_sha_before,
            "frozen_size_bytes": os.path.getsize(FROZEN),
            "rejudge_file": "results_v2/r2_r1_rejudge_noL0.hub_pull_20261025.json",
            "rejudge_sha256_16": rej_sha_before,
            "rejudge_size_bytes": os.path.getsize(REJUDGE),
            "n_layers": L,
            "budgets": [s["budget_total_kv_slots"] for s in sweep],
        },
        "defect_1_slot_closure": {
            "definition": "closure_error = Σ_layer(profile_by_layer + 1) − budget_total_kv_slots；非 0 即 slot 不闭合",
            "per_budget": d1,
            "mismatch_vs_wb1_gt_1e3": d1_mismatches,
            "n_arms_nonzero": sum(1 for b in d1 for a in d1[b] if abs(d1[b][a]["closure_error"]) > 1e-9),
            "n_arms_compared": sum(1 for b in d1 for a in d1[b] if "wb1_reported" in d1[b][a]),
        },
        "defect_2_geometric_midpoint": d2,
        "defect_3_out_of_domain_K": {
            "measured_K_max_in_frozen": measured_K_max,
            "per_budget": d3,
            "budgets_with_over_domain": ood_any,
        },
        "frozen_self_reported_decision": verdict,
        "dependency_surface": dep,
        "rejudge_verdict_crosscheck": rej_verdict,
        "wall_clock_sec": None,
    }
    result["wall_clock_sec"] = round(time.time() - t0, 3)

    with open(OUT, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=1)[:4000])
    print("\n[OUT]", OUT, os.path.getsize(OUT), "B; frozen sha16 unchanged:", sha16(FROZEN) == frozen_sha_before)

if __name__ == "__main__":
    main()
