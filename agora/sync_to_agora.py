#!/usr/bin/env python3.11
"""
sync_to_agora.py — Agora 同步脚本（阶段一：自动同步 / GitOps）

把「最新实验结果 → 可视化看板」的闭环自动化：
  1. 重跑 build_bundle.py，从 results/ 重建 data/bundle.json
  2. （可选）git add + commit + push 到仓库，触发 GitHub Pages 更新

设计原则：单一事实来源始终是 results/ 下的 YAML/MD；本脚本只负责「搬运 + 发布」，
不修改任何源数据。DeepSeek 通过 Raw 链接读取，用户作为人肉 API Gateway。

用法：
  python3 sync_to_agora.py                 # 仅本地重建 bundle.json
  python3 sync_to_agora.py --push          # 重建并提交推送到当前 git 仓库
  python3 sync_to_agora.py --push --msg "R2: GREEN-WITH-WARN"
  python3 sync_to_agora.py --repo /path/to/repo   # 指定 git 仓库根目录
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, cwd=None):
    print(f"[sync] $ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit(f"命令失败: {' '.join(cmd)}")
    return r


def find_git_root(start):
    """从 start 向上查找最近的 .git 目录，返回其路径；找不到则返回 start。"""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="git 仓库根目录（默认自动向上查找 .git）")
    ap.add_argument("--push", action="store_true", help="提交并推送到远程")
    ap.add_argument("--msg", default=None, help="提交信息（默认按时间生成）")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo) if args.repo else find_git_root(HERE)

    # 1) 重建 bundle
    run([sys.executable, os.path.join(HERE, "build_bundle.py")], cwd=HERE)

    if not args.push:
        print("\n[sync] 仅本地重建完成。加 --push 可提交并推送到 git 仓库。")
        return

    # 2) git 提交 + 推送
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"[sync] {repo} 不是 git 仓库，无法 push。请先 git init / clone。")

    run(["git", "add", "-A"], cwd=repo)
    # 检查是否有改动
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    if not status.stdout.strip():
        print("[sync] 无改动，跳过提交。")
        return

    from datetime import datetime
    msg = args.msg or f"Agora sync {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    run(["git", "commit", "-m", msg], cwd=repo)
    run(["git", "push"], cwd=repo)
    print(f"\n[sync] 已推送：{msg}")
    print("[sync] 若已配置 GitHub Pages（根目录 /docs /agora），稍候即可在站点看到更新。")


if __name__ == "__main__":
    main()
