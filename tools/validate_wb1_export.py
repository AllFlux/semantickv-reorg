#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_wb1_export.py — WB1 真实特征导出校验（DS-20261020-048-WB1 要求）

背景
----
WB2 反馈：下载 8.2MB 的 `real_features_3b.npz` 时，因网络限速导致断点续传文件
损坏，numpy 加载报 `Bad magic number`。本工具解决两个问题：
  1. 生成侧：文件生成后计算并打印 `SHA256` 与**精确 byte_size**，并把该记录
     写入 `.json` 的 `meta`（`export_sha256` / `export_byte_size` / `validated_at`）；
  2. 消费侧（WB2）：下载后重新计算 SHA256，与 json `meta` 中记录比对；
     不一致即判定下载损坏，拒绝加载，提示重新拉取。

此外做两道前置快检：
  * 文件头魔数（`PK\x03\x04`，npz 本质是 zip）——拦截 Bad magic number 类损坏；
  * npz 实际可加载 + 键名/形状/dtype 与 json 契约一致。

用法
----
  # 生成侧（export_real_features.py 已自动调用此步）：
  python3 tools/validate_wb1_export.py \
      --npz src/shared/real_features_3b.npz \
      --json src/shared/real_features_3b.json --write

  # 消费侧（WB2 下载后）：
  python3 tools/validate_wb1_export.py \
      --npz real_features_3b.npz \
      --json real_features_3b.json --check

退出码
------
  0  成功（--check 时表示 SHA256 匹配）
  2  文件不存在
  3  npz 魔数异常或加载失败（损坏）
  4  --check 时 SHA256 不匹配（下载损坏）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

# npz 本质是 zip 归档，文件头魔数为 "PK\x03\x04"
NPZ_MAGIC = b"PK\x03\x04"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="WB1 真实特征导出校验（SHA256 + byte_size + 魔数快检）")
    ap.add_argument("--npz", required=True, help=".npz 文件路径")
    ap.add_argument("--json", required=True, help="同伴 .json 文件路径")
    ap.add_argument("--write", action="store_true",
                    help="把 sha256/byte_size/validated_at 写回 json 的 meta")
    ap.add_argument("--check", action="store_true",
                    help="比对 json meta 中已有 sha256，不一致即损坏（exit 4）")
    args = ap.parse_args()

    npz = Path(args.npz)
    js = Path(args.json)
    if not npz.exists():
        print(f"[FATAL] npz 不存在: {npz}", file=sys.stderr)
        return 2
    if not js.exists():
        print(f"[FATAL] json 不存在: {js}", file=sys.stderr)
        return 2

    # 1. 魔数快检：拦截 Bad magic number 类损坏
    head = npz.read_bytes()[:4]
    magic_ok = head == NPZ_MAGIC
    print(f"[magic] {npz.name} 头部={head!r} -> "
          f"{'OK (zip/npz)' if magic_ok else '⚠ 非标准 npz 魔数'}")
    if not magic_ok:
        print("[FATAL] 文件头魔数异常，极可能下载损坏（Bad magic number）。",
              file=sys.stderr)
        return 3

    # 2. 加载 + 契约校验
    try:
        import numpy as np
        with np.load(npz, allow_pickle=False) as d:
            keys = list(d.files)
            key_hidden = "hidden_states" if "hidden_states" in keys else (
                "hs" if "hs" in keys else None)
            hs = d[key_hidden] if key_hidden else None
        print(f"[load] 成功 | keys={keys} | "
              f"hidden_states={None if hs is None else tuple(hs.shape)} "
              f"{'' if hs is None else hs.dtype}")
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] npz 加载失败（可能损坏）: {e}", file=sys.stderr)
        return 3

    # 3. SHA256 + byte_size
    digest = sha256_of(npz)
    size = npz.stat().st_size
    print(f"[sha256] {digest}")
    print(f"[byte_size] {size} ({size/1024/1024:.2f} MB)")

    # 4. 与 json 契约一致性
    j = json.loads(js.read_text(encoding="utf-8"))
    meta = j.get("meta", {})
    jn = j.get("n_tokens")
    hn = int(hs.shape[0]) if hs is not None else None
    if jn is not None and hn is not None and jn != hn:
        print(f"[WARN] json.n_tokens({jn}) 与 npz 实际 token 数({hn}) 不一致")
    if int(j.get("hidden_size", 0)) != (int(hs.shape[1]) if hs is not None else -1):
        print(f"[WARN] json.hidden_size 与 npz 维度不一致")

    # 5. --write：写回 json meta（DS-048 要求落在 meta 对象内）
    if args.write:
        meta["export_sha256"] = digest
        meta["export_byte_size"] = size
        meta["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["flag_provenance"] = ("DS-20261020-048-WB1 要求：导出后计算 SHA256 + "
                                   "byte_size 写入 meta，供 WB2 下载后比对防损坏")
        j["meta"] = meta
        js.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[write] 已写入 meta.export_sha256 / export_byte_size / "
              f"validated_at -> {js}")

    # 6. --check：比对
    if args.check:
        expect = meta.get("export_sha256")
        if not expect:
            print("[WARN] json meta 无 export_sha256，无法比对（请先 --write）")
        elif expect == digest:
            print("[check] ✔ SHA256 匹配，文件完整，可安全加载。")
        else:
            print(f"[check] ✘ SHA256 不匹配！期望 {expect}\n"
                  f"        实际 {digest}\n"
                  f"        → 下载损坏，请重新拉取后重试。", file=sys.stderr)
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
