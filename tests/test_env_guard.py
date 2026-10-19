# -*- coding: utf-8 -*-
"""test_env_guard.py — 环境口径守卫单测（DS-20261019-029 / 035 验收项）

验证 `fixed_route_assault.validate_env_label` 的行为：
  * 权威标签（kaggle-7b / autodl-7b）必须四条断言全绿，否则拒绝；
  * 代理标签一律放行（它本来就自证是代理，不可能冒称 7B）；
  * 任何一条断言失败都必须被点名，便于定位。

运行：python3 tests/test_env_guard.py   （退出码 0 = 全过）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from fixed_route_assault import compute_env_claims, validate_env_label  # noqa: E402

GOOD_7B = dict(model="Qwen/Qwen2.5-7B-Instruct", load_in_4bit=True,
               ctx=8192, device="cuda", cuda_available=True)
GOOD_3B = dict(model="Qwen/Qwen2.5-3B-Instruct", load_in_4bit=True,
               ctx=8192, device="cuda", cuda_available=True)

CASES = [
    # (说明, 环境标签, 参数, 期望 ok, 期望失败项)
    ("7B 4bit cuda 8K → kaggle-7b", "kaggle-7b", GOOD_7B, True, []),
    ("7B 4bit cuda 8K → autodl-7b", "autodl-7b", GOOD_7B, True, []),
    ("3B 4bit cuda 8K → autodl-3b（DS-040 口径）", "autodl-3b", GOOD_3B, True, []),
    ("3B 4bit cuda 8K → kaggle-3b", "kaggle-3b", GOOD_3B, True, []),
    ("3B 但 ctx=2048 冒充 autodl-3b", "autodl-3b",
     dict(model="Qwen/Qwen2.5-3B-Instruct", load_in_4bit=True, ctx=2048,
          device="cuda", cuda_available=True),
     False, ["ctx_ge_4096"]),
    ("0.5B CPU 冒充 kaggle-7b", "kaggle-7b",
     dict(model="Qwen/Qwen2.5-0.5B", load_in_4bit=False, ctx=512,
          device="cpu", cuda_available=False),
     False, ["model_size_in_3b_7b", "load_in_4bit", "ctx_ge_4096", "device_uses_cuda"]),
    ("7B 但跑 CPU（无 4bit）冒充 autodl-7b", "autodl-7b",
     dict(model="Qwen/Qwen2.5-7B-Instruct", load_in_4bit=False, ctx=2048,
          device="cpu", cuda_available=False),
     False, ["load_in_4bit", "ctx_ge_4096", "device_uses_cuda"]),
    ("7B 4bit 但 ctx=2048 冒充 autodl-7b", "autodl-7b",
     dict(model="Qwen/Qwen2.5-7B-Instruct", load_in_4bit=True, ctx=2048,
          device="cuda", cuda_available=True),
     False, ["ctx_ge_4096"]),
    ("代理标签永远放行（CPU 0.5B）", "local-sandbox-cpu-proxy-0.5B",
     dict(model="Qwen/Qwen2.5-0.5B", load_in_4bit=False, ctx=512,
          device="cpu", cuda_available=False),
     True, []),
    ("未知标签不拦截（非权威）", "some-lab-box",
     dict(model="Qwen/Qwen2.5-0.5B", load_in_4bit=False, ctx=512,
          device="cpu", cuda_available=False),
     True, []),
]


def main() -> int:
    fails = 0
    print("=" * 78)
    print("环境口径守卫单测（DS-029/035）")
    print("=" * 78)
    for desc, env, kw, exp_ok, exp_failed in CASES:
        claims = compute_env_claims(kw["model"], kw["load_in_4bit"], kw["ctx"],
                                    kw["device"], kw["cuda_available"])
        ok, failed = validate_env_label(env, claims)
        passed = (ok == exp_ok) and (sorted(failed) == sorted(exp_failed))
        icon = "✔" if passed else "✘"
        print(f" [{icon}] {desc}")
        print(f"      env={env}  ok={ok}  failed={failed}")
        if not passed:
            print(f"      ← 期望 ok={exp_ok} failed={sorted(exp_failed)}")
            fails += 1
    print("-" * 78)
    verdict = "PASS" if fails == 0 else f"FAIL ({fails} 项)"
    print(f"⟹ {verdict}  ({len(CASES) - fails}/{len(CASES)})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
