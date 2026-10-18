#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_sync.py — Agora 一键同步（DS-20261018-007 工作流验收项）

用法（一次性配置后，日常只需一条命令）：

  # 1) 一次性配置（两种方式任选其一）
  #    a. 环境变量方式（推荐，不落盘）：
  export GITHUB_TOKEN=github_pat_xxxxx        # Fine-grained PAT，仅授权本仓库 Contents: Read and write
  #    b. 或写入 git 凭据：git remote set-url origin https://<TOKEN>@github.com/AllFlux/semantickv-reorg.git
  #       （注意：b 会把 token 明文写进 .git/config）

  # 2) 日常同步（在仓库任意位置）：
  python3 auto_sync.py                # 同步 results/ + 看板 + 注入包，并打印 Raw 链接
  python3 auto_sync.py --check        # 只查看有什么待同步，不推送

安全提示：
  - Fine-grained PAT 建议只勾选 semantickv-reorg 一个仓库 + Contents 读写 + 7 天有效期；
  - 不要把 PAT 粘贴到对话/截图/提交信息里；
  - 泄露时立即到 GitHub Settings → Developer settings 撤销。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "AllFlux/semantickv-reorg"
BRANCH = "main"

# 需要同步到 GitHub 的看板关键文件（Raw 链接给 DS 用）
SYNC_PATHS = [
    "index.html",                                   # 仓库根：看板单文件（Pages 首页）
    "results/hypothesis_ledger.yaml",
    "results/discipline_ledger.yaml",
    "results/real_model_v15_hybrid_protection.json",
    "results/real_model_v15_hybrid_global.json",
    "results/real_model_v15_topk8_win128_baseline.json",
    "results/REQUEST_TO_DEEPSEEK.md",
    "results/REAL_MODEL_VALIDATION_V15.md",
]
# 评审与目录类文件整体提交（git add 按目录）
SYNC_DIRS = ["results/ds_reviews"]


def sh(cmd, cwd=ROOT):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[git] {' '.join(cmd)} 失败:\n{r.stderr.strip()}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只查看待同步项，不提交推送")
    ap.add_argument("--msg", default=None, help="提交信息")
    args = ap.parse_args()

    # 0) 前置：重建看板数据与单文件版（results/ 为唯一事实来源）
    print("[1/4] 重建看板数据 bundle.json …")
    sh([sys.executable, "agora/build_bundle.py"])
    print("[1/4] 重建单文件看板 index.html …")
    sh([sys.executable, "agora/build_standalone.py"])
    sh(["cp", "agora/index.standalone.html", "index.html"])

    # 1) 待同步清单
    pending = [p for p in SYNC_PATHS if (ROOT / p).exists()]
    for d in SYNC_DIRS:
        if (ROOT / d).exists():
            pending += [str(p.relative_to(ROOT)) for p in sorted((ROOT / d).glob("*.md"))]
    print(f"[2/4] 待同步 {len(pending)} 个文件")
    for p in pending:
        print("   -", p)
    if args.check:
        return 0

    # 2) 确认远程与 token
    remotes = sh(["git", "remote", "-v"]).stdout.strip()
    if not remotes:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("[!] 未配置远程且无 GITHUB_TOKEN。请先：export GITHUB_TOKEN=github_pat_xxx")
            return 1
        url = f"https://{token}@github.com/{REPO}.git"
        sh(["git", "remote", "add", "origin", url])
        print(f"[git] 已添加远程 origin → github.com/{REPO}（token 不回显）")
    else:
        print("[git] 远程已配置：")
        print("   " + remotes.splitlines()[0])

    # 3) 提交推送
    msg = args.msg or f"auto-sync: 看板+台账+实验结果 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    sh(["git", "add"] + pending)
    r = sh(["git", "commit", "-m", msg])
    if "nothing to commit" in (r.stdout + r.stderr):
        print("[3/4] 无新改动，跳过提交（仍会尝试 push 确保同步）")
    else:
        print("[3/4] 已提交:", msg)
    r = sh(["git", "push", "origin", BRANCH])
    if r.returncode != 0:
        print("[4/4] 推送失败——检查 token 权限（需 Contents: Read and write）或网络")
        return 1
    print("[4/4] 推送成功 ✓  Pages 将在 1~2 分钟内自动重建")

    # 4) 回报 DS 可用的 Raw 链接
    base = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
    print("\n===== 给 DS 的 Raw 链接（最新）=====")
    for p in ["results/hypothesis_ledger.yaml", "results/discipline_ledger.yaml",
              "index.html"]:
        print(f"  {base}/{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
