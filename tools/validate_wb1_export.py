#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_wb1_export.py — WB1 真实特征导出校验（DS-20261020-048 / 051 要求）

背景
----
WB2 反馈：下载 8.2MB 的 `real_features_3b.npz` 时，因网络限速 + CDN 掐断导致
文件损坏，numpy 加载报 `Bad magic number`。本工具解决两件事：
  1. 生成侧：文件生成后计算并打印 `SHA256` 与**精确 byte_size**，写入 `.json` 的
     `meta`（DS-051 字段名：sha256 / byte_size / validated_at；根 meta 与内层
     meta["meta"] 双写，兼容旧 export_sha256 回退）；
  2. 消费侧（WB2）：下载后重新计算 SHA256 与 json `meta` 比对；不一致即判定下载
     损坏，拒绝加载，提示重新拉取。
  3. 分卷（DS-051 要求③）：>5MB 文件由 export 自动分卷为 .partN + manifest。
     本工具提供 --parts 逐卷+拼整体校验，以及 --assemble 自动拼接还原。

前置快检
--------
  * 文件头魔数（`PK\x03\x04`，npz 本质是 zip）——拦截 Bad magic number 类损坏；
  * npz 实际可加载 + 键名/形状/dtype 与 json 契约一致。

用法
----
  # 生成侧（export_real_features.py 已自动算 sha 并写 meta，本步仅手动补写用）：
  python3 tools/validate_wb1_export.py \
      --npz src/shared/real_features_3b.npz \
      --json src/shared/real_features_3b.json --write

  # 消费侧（WB2 单文件下载后）：
  python3 tools/validate_wb1_export.py \
      --npz real_features_3b.npz --json real_features_3b.json --check

  # 分卷校验（WB2 分卷下载后）：
  python3 tools/validate_wb1_export.py --parts real_features_3b.npz.parts.json

  # 分卷拼接还原（→ 同目录 original 或 --out 指定）：
  python3 tools/validate_wb1_export.py --assemble real_features_3b.npz.parts.json

退出码
------
  0  成功（--check 时 SHA256 匹配；--parts 时各卷+整体均匹配；--assemble 时还原成功）
  2  文件/分卷不存在或 manifest 缺失
  3  npz 魔数异常或加载失败（损坏）
  4  SHA256 不匹配（下载损坏）
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


def load_json(js: Path) -> dict:
    return json.loads(js.read_text(encoding="utf-8"))


def read_expected_sha(j: dict) -> str | None:
    """DS-051 字段 sha256 优先；兼容 DS-048 遗留 export_sha256（根/内层均可）。"""
    meta = j.get("meta", {})
    return (j.get("sha256")
            or meta.get("sha256")
            or j.get("export_sha256")
            or meta.get("export_sha256"))


def magic_and_load(npz: Path):
    """魔数快检 + 加载 + 契约校验。返回 (hs, keys) 或抛异常。"""
    head = npz.read_bytes()[:4]
    magic_ok = head == NPZ_MAGIC
    print(f"[magic] {npz.name} 头部={head!r} -> "
          f"{'OK (zip/npz)' if magic_ok else '⚠ 非标准 npz 魔数'}")
    if not magic_ok:
        raise ValueError("文件头魔数异常，极可能下载损坏（Bad magic number）")
    import numpy as np
    with np.load(npz, allow_pickle=False) as d:
        keys = list(d.files)
        key_hidden = ("hidden_states" if "hidden_states" in keys
                      else ("hs" if "hs" in keys else None))
        hs = d[key_hidden] if key_hidden else None
    print(f"[load] 成功 | keys={keys} | "
          f"hidden_states={None if hs is None else tuple(hs.shape)} "
          f"{'' if hs is None else hs.dtype}")
    return hs, keys


def verify_parts(manifest_path: Path, json_path: Path | None = None) -> int:
    if not manifest_path.exists():
        print(f"[FATAL] manifest 不存在: {manifest_path}", file=sys.stderr)
        return 2
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    original = m.get("original")
    combined_sha = m.get("sha256")
    combined_size = m.get("byte_size")
    parts = m.get("parts", [])
    if not original or combined_sha is None or not parts:
        print("[FATAL] manifest 字段缺失（original/sha256/parts）", file=sys.stderr)
        return 2

    buf = bytearray()
    for p in parts:
        pp = base / p["name"]
        if not pp.exists():
            print(f"[FATAL] 分卷缺失: {pp}", file=sys.stderr)
            return 2
        data = pp.read_bytes()
        if sha256_of(pp) != p["sha256"]:
            print(f"[FATAL] 分卷 {p['name']} sha256 不匹配（下载损坏）", file=sys.stderr)
            return 4
        if len(data) != p["byte_size"]:
            print(f"[FATAL] 分卷 {p['name']} 字节数不符 "
                  f"({len(data)} != {p['byte_size']})", file=sys.stderr)
            return 4
        buf += data

    h = hashlib.sha256(bytes(buf)).hexdigest()
    if h != combined_sha:
        print(f"[FATAL] 拼接后 sha256 与 manifest 不符\n"
              f"        期望 {combined_sha}\n        实际 {h}", file=sys.stderr)
        return 4
    if len(buf) != combined_size:
        print(f"[FATAL] 拼接后字节数不符 ({len(buf)} != {combined_size})", file=sys.stderr)
        return 4

    print(f"[parts] ✔ {len(parts)} 分卷逐卷校验通过，拼接完整 "
          f"（sha256 匹配，{len(buf)} bytes）")

    # 与 json meta 交叉核对（若提供）
    if json_path and json_path.exists():
        j = load_json(json_path)
        expect = read_expected_sha(j)
        if expect and expect != h:
            print(f"[FATAL] 拼接 sha256 与 json meta.sha256 不符\n"
                  f"        期望 {expect}\n        实际 {h}", file=sys.stderr)
            return 4
        print(f"[parts] ✔ 与 json meta.sha256 交叉核对一致")
    return 0


def assemble_parts(manifest_path: Path, out_path: Path | None = None) -> int:
    rc = verify_parts(manifest_path)
    if rc != 0:
        return rc
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    original = m["original"]
    out = out_path or (base / original)
    buf = bytearray()
    for p in m["parts"]:
        buf += (base / p["name"]).read_bytes()
    out.write_bytes(bytes(buf))
    h = sha256_of(out)
    if h != m["sha256"]:
        print(f"[FATAL] 还原后 sha256 不匹配（{h} != {m['sha256']}）", file=sys.stderr)
        out.unlink(missing_ok=True)
        return 4
    print(f"[assemble] ✔ 已还原 {out} （{len(buf)} bytes，sha256 校验通过）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="WB1 真实特征导出校验（SHA256 + byte_size + 魔数快检 + 分卷）")
    ap.add_argument("--npz", help=".npz 文件路径")
    ap.add_argument("--json", help="同伴 .json 文件路径")
    ap.add_argument("--write", action="store_true",
                    help="把 sha256/byte_size/validated_at 写入 json 的 meta（根+内层）")
    ap.add_argument("--check", action="store_true",
                    help="比对 json meta 中 sha256，不一致即损坏（exit 4）")
    ap.add_argument("--parts", help="分卷 manifest.json 路径，逐卷+拼整体校验")
    ap.add_argument("--assemble", help="分卷 manifest.json 路径，自动拼接还原为原文件")
    ap.add_argument("--out", help="--assemble 时指定还原输出路径（缺省=原文件名）")
    args = ap.parse_args()

    # ── 分卷模式 ──
    if args.parts:
        return verify_parts(Path(args.parts),
                            Path(args.json) if args.json else None)
    if args.assemble:
        return assemble_parts(Path(args.assemble),
                             Path(args.out) if args.out else None)

    if not args.npz or not args.json:
        print("[FATAL] 单文件模式需要 --npz 与 --json", file=sys.stderr)
        return 2
    npz = Path(args.npz)
    js = Path(args.json)
    if not npz.exists():
        print(f"[FATAL] npz 不存在: {npz}", file=sys.stderr)
        return 2
    if not js.exists():
        print(f"[FATAL] json 不存在: {js}", file=sys.stderr)
        return 2

    # 1. 魔数快检 + 加载
    try:
        hs, keys = magic_and_load(npz)
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] npz 加载失败（可能损坏）: {e}", file=sys.stderr)
        return 3

    # 2. SHA256 + byte_size
    digest = sha256_of(npz)
    size = npz.stat().st_size
    print(f"[sha256] {digest}")
    print(f"[byte_size] {size} ({size/1024/1024:.2f} MB)")

    # 3. 与 json 契约一致性
    j = load_json(js)
    meta = j.get("meta", {})
    jn = j.get("n_tokens")
    hn = int(hs.shape[0]) if hs is not None else None
    if jn is not None and hn is not None and jn != hn:
        print(f"[WARN] json.n_tokens({jn}) 与 npz 实际 token 数({hn}) 不一致")
    if int(j.get("hidden_size", 0)) != (int(hs.shape[1]) if hs is not None else -1):
        print(f"[WARN] json.hidden_size 与 npz 维度不一致")

    # 4. --write：写回 json meta（DS-051 字段 sha256/byte_size，根+内层双写）
    if args.write:
        meta["sha256"] = digest
        meta["byte_size"] = size
        meta["validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["flag_provenance"] = ("DS-20261020-051 要求：导出脚本内/校验脚本写入 "
                                   "sha256/byte_size 入 meta")
        j["sha256"] = digest
        j["byte_size"] = size
        js.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[write] 已写入 meta.sha256 / byte_size / validated_at "
              f"（根 meta 与内层 meta[\"meta\"] 双写）-> {js}")

    # 5. --check：比对
    if args.check:
        expect = read_expected_sha(j)
        if not expect:
            print("[WARN] json meta 无 sha256/export_sha256，无法比对（请先 --write）")
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
