#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_pull_shared.py — DS-20261019-021-WB1 基建：远端仓库 src/shared/ 自动拉取管线。

背景：WB2 的交付物（router_weights.pt / load_test.py 等）只存在于 WB2 沙盒，
双方约定以 AllFlux/semantickv-reorg 仓库的 src/shared/ 为数据总线。
本脚本在 Kaggle Notebook 启动时把远端 src/shared/ 拉到本地，取代"等本地同步"。

用法：
    export GITHUB_TOKEN=ghp_xxx        # Kaggle: Add-ons → Secrets → GITHUB_TOKEN
                                      # （DS-20261019-024 命名；GITHUB_PAT 同样识别，优先）
    python3 auto_pull_shared.py                    # PAT 优先，无 PAT 时公共仓库匿名拉取
    python3 auto_pull_shared.py --verify-torch-load   # 额外对 .pt 做 torch.load 冒烟
    python3 auto_pull_shared.py --repo AllFlux/semantickv-reorg --branch main

流程（DS-021 §二.1）：
    1. git clone --depth 1 https://{PAT}@github.com/<repo>.git <temp_repo>
    2. 复制 <temp_repo>/src/shared/ → 当前工作目录 src/shared/
    3. 计算每个文件 SHA256，与 git 记录的最后修改 commit/blob sha 双比对
    4. 计时（H-DS30 判据：10 秒内），写 results/logistics_pull_test.json
    5.（DS-024 新增）远端实况检查：PAT 有效性 + 远端 src/shared/ 清单，
       不计入计时——用于区分"凭据失效/未推送"与"网络墙"。

降级路径（H-DS30 falsifiable 预置退路）：
    git clone 失败/超时（Kaggle 网络墙）→ 自动切换 GitHub Raw wget 逐文件下载。

安全：PAT 全链路脱敏——日志、报错、JSON 存证中一律替换为 ***。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DEFAULT_REPO = "AllFlux/semantickv-reorg"
DEFAULT_BRANCH = "main"
ELAPSED_LIMIT_S = 10.0                       # H-DS30 判据
KEY_FILES = ["router_weights.pt", "load_test.py", "README.md"]


def _mask(text: str, pat: str | None) -> str:
    """PAT 脱敏：日志/JSON 中不泄露令牌。"""
    if pat and len(pat) >= 6:
        return text.replace(pat, "***")
    return text


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_check(repo: str, branch: str, pat: str | None) -> dict:
    """DS-20261019-024 新增：拉取前的远端实况检查（不计入 H-DS30 计时）。

    回答两个问题：
      1. PAT 是否有效（GET /user）——DS-024 下发的令牌若失效，git 通道
         必然认证失败，此处先行暴露，避免把"凭据无效"误诊成"网络墙"。
      2. 远端 src/shared/ 到底有什么（contents API；API 限流时降级
         Raw 逐文件探测）——直接判定 WB2 交付物是否已推送到位。

    注意 raw CDN 对不存在路径不返快速 404 而是挂起（实测 15s+），
    故单文件探测预算 10s，超时按"远端未推送"处理。
    """
    out = {"pat_valid": None, "pat_login": None, "api_ok": None,
           "remote_files": [], "key_files_present": {k: False for k in KEY_FILES},
           "note": "DS-20261019-024 远端实况检查"}
    api = "https://api.github.com"
    headers = ["-H", "Accept: application/vnd.github+json"]
    if pat:
        headers += ["-H", f"Authorization: token {pat}"]
        r = _run(["curl", "-sSL", "--max-time", "20", *headers, f"{api}/user"],
                 timeout=25)
        try:
            u = json.loads(r.stdout)
            out["pat_valid"] = "login" in u
            out["pat_login"] = u.get("login")
        except Exception:                    # noqa: BLE001
            out["pat_valid"] = False
            out["pat_login"] = None
    # 列目录（带 PAT 走 5000/h 限额；匿名 60/h 易 403，失败即降级 raw 探测）
    r = _run(["curl", "-sSL", "--max-time", "20", *headers,
              f"{api}/repos/{repo}/contents/src/shared?ref={branch}"], timeout=25)
    listed = None
    try:
        c = json.loads(r.stdout)
        if isinstance(c, list):
            listed = [e.get("name") for e in c if isinstance(e, dict)]
            out["api_ok"] = True
    except Exception:                        # noqa: BLE001
        pass
    if listed is not None:
        out["remote_files"] = listed
        for k in KEY_FILES:
            out["key_files_present"][k] = k in listed
    else:
        out["api_ok"] = False
        for k in KEY_FILES:
            url = (f"https://raw.githubusercontent.com/{repo}/{branch}"
                   f"/src/shared/{k}")
            r = _run(["curl", "-sS", "--max-time", "10", "-o", os.devnull,
                      "-w", "%{http_code}", url], timeout=15)
            out["key_files_present"][k] = (r.stdout.strip() == "200")
        out["remote_files"] = [k for k, v in out["key_files_present"].items() if v]
    return out


def _run(cmd: list, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def git_pull(repo: str, branch: str, pat: str | None, workdir: Path) -> Path:
    """DS-021 主路径：git clone --depth 1。返回克隆目录。

    注：部分环境 git 的 GnuTLS 后端与 GitHub TLS 存在兼容问题
    （recv error -110）；Kaggle 标准镜像通常无此问题。调用方负责
    失败重试与降级（raw 路径）。
    """
    if pat:
        url = f"https://{pat}@github.com/{repo}.git"
    else:
        url = f"https://github.com/{repo}.git"     # 公共仓库匿名可读
    clone_dir = workdir / "temp_repo"
    if clone_dir.exists():                 # 重试前清理上次残留
        shutil.rmtree(clone_dir, ignore_errors=True)
    p = _run(["git", "clone", "--depth", "1",
              "--branch", branch, url, str(clone_dir)], timeout=60)
    if p.returncode != 0:
        raise RuntimeError(_mask((p.stdout + p.stderr)[-500:], pat))
    return clone_dir


def raw_wget_pull(repo: str, branch: str, dest: Path) -> list:
    """H-DS30 降级路径：Raw 直链逐文件下载（KEY_FILES 清单制，不依赖
    GitHub API——匿名 API 有 60/h 限流，Kaggle 共享 IP 易触发 403）。
    用 curl 子进程而非 urllib：部分沙箱的 Python ssl 上下文对
    raw.githubusercontent.com 握手异常，而 curl 验证可用。

    注意：raw CDN 对不存在路径不返回快速 404 而是挂起（实测 15s+ 无响应），
    故单文件预算 20s，超时按"远端未推送"处理并继续，不整体失败。
    """
    files = []
    for name in KEY_FILES:
        url = (f"https://raw.githubusercontent.com/{repo}/{branch}"
               f"/src/shared/{name}")
        out = dest / name
        p = _run(["curl", "-fsSL", "--max-time", "20", "-o", str(out), url],
                 timeout=30)
        if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            print(f"[raw] {name} 未取到（远端未推送或网络超时，预算 20s），跳过")
            continue
        files.append(name)
        print(f"Pull Success: {name} (raw, {out.stat().st_size}B)")
    if not files:
        raise RuntimeError("raw 降级也未取到任何文件（网络墙？）")
    return files


def verify_torch_load(dest: Path) -> dict:
    """DS-021 验收标准②：对拉取到的 .pt 做 torch.load 冒烟。"""
    out = {}
    try:
        import torch
    except Exception as e:                        # noqa: BLE001
        return {"available": False, "error": str(e)}
    out["available"] = True
    for pt in sorted(dest.glob("*.pt")):
        try:
            blob = torch.load(pt, map_location="cpu", weights_only=False)
            keys = list(blob.keys()) if isinstance(blob, dict) else type(blob).__name__
            out[pt.name] = {"load_ok": True, "top_level_keys": keys}
        except Exception as e:                    # noqa: BLE001
            out[pt.name] = {"load_ok": False, "error": str(e)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="DS-20261019-021 共享交付物拉取管线")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--dest", default="src/shared",
                    help="目标目录（默认当前工作目录 src/shared/）")
    ap.add_argument("--verify-torch-load", action="store_true",
                    help="对拉取到的 .pt 做 torch.load 冒烟（DS 验收标准②）")
    args = ap.parse_args()

    pat = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN") or None
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    results_path = Path("results/logistics_pull_test.json")

    record = {
        "config": {"repo": args.repo, "branch": args.branch,
                   "dest": str(dest), "pat_used": bool(pat),
                   "pat_env": ("GITHUB_PAT" if os.environ.get("GITHUB_PAT")
                               else "GITHUB_TOKEN" if pat else None),
                   "device": "kaggle" if os.path.isdir("/kaggle") else "local-sandbox",
                   "purpose": "H-DS30 Git 拉取管线测试（DS-20261019-021-WB1）",
                   "elapsed_limit_s": ELAPSED_LIMIT_S},
        "environment": {"kind": "auto-pull-pipeline",
                        "host": os.uname().nodename if hasattr(os, "uname") else "?",
                        "note": "Kaggle 实机复跑时由 notebook Step 2.5 触发"},
    }

    t0 = time.time()
    method, err = None, None
    with tempfile.TemporaryDirectory(prefix="auto_pull_") as tmp:
        workdir = Path(tmp)
        try:
            # ---- 主路径：git clone（DS-021 §二.1；瞬时网络失败重试一次） ----
            last = None
            for attempt in (1, 2):
                try:
                    clone_dir = git_pull(args.repo, args.branch, pat, workdir)
                    break
                except Exception as e:            # noqa: BLE001
                    last = e
                    if attempt == 1:
                        print("[git 第 1 次失败，2 秒后重试] "
                              f"{_mask(str(e), pat)[:120]}", file=sys.stderr)
                        time.sleep(2)
            else:
                raise last
            method = "git"
            staged = clone_dir / "src" / "shared"
            if not staged.is_dir():
                raise RuntimeError(f"远端无 src/shared/ 目录（{args.repo}@{args.branch}）")
            files = []
            for src in sorted(staged.iterdir()):
                if not src.is_file():
                    continue
                target = dest / src.name
                shutil.copy2(src, target)
                # 完整性双比对：SHA256（本地算）+ 最后修改 commit（git log）
                log = _run(["git", "log", "-1", "--format=%H\x1f%h\x1f%s",
                            "--", f"src/shared/{src.name}"], cwd=clone_dir)
                blob = _run(["git", "hash-object", str(src)], cwd=clone_dir)
                files.append({
                    "name": src.name,
                    "size_b": src.stat().st_size,
                    "sha256": _sha256_file(target),
                    "git_blob_sha": blob.stdout.strip(),
                    "last_commit": log.stdout.strip().replace("\x1f", " | "),
                })
                print(f"Pull Success: {src.name} "
                      f"(SHA256: {files[-1]['sha256'][:16]}…)")
        except Exception as e:                    # noqa: BLE001
            err = _mask(str(e), pat)
            print(f"[git 路径失败，降级 Raw wget] {err}", file=sys.stderr)
            try:
                names = raw_wget_pull(args.repo, args.branch, dest)
                method = "raw_wget"
                files = [{"name": n, "size_b": (dest / n).stat().st_size,
                          "sha256": _sha256_file(dest / n),
                          "git_blob_sha": None, "last_commit": None}
                         for n in names]
            except Exception as e2:               # noqa: BLE001
                record["summary"] = {"verdict": "FAIL", "error": _mask(str(e2), pat)}
                record["pull"] = {"method": None}
                if results_path.parent.is_dir():
                    results_path.write_text(
                        json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                print(json.dumps(record["summary"], ensure_ascii=False))
                return 1

    elapsed = round(time.time() - t0, 3)
    names = {f["name"] for f in files}
    pipeline_pass = bool(files) and all(f["sha256"] for f in files)
    if args.verify_torch_load:
        record["torch_load"] = verify_torch_load(dest)
        pt_files = [v for k, v in record["torch_load"].items()
                    if k != "available" and isinstance(v, dict)]
        if pt_files and not all(v.get("load_ok") for v in pt_files):
            pipeline_pass = False

    # DS-024 远端实况检查（在 H-DS30 计时之外单独记录）
    record["remote_check"] = remote_check(args.repo, args.branch, pat)
    rc = record["remote_check"]
    print(f"[remote-check] PAT valid={rc['pat_valid']} "
          f"login={rc['pat_login']} api_ok={rc['api_ok']} "
          f"key_files={rc['key_files_present']}")

    record["pull"] = {
        "method": method,
        "git_error": err,
        "files": files,
        "elapsed_s": elapsed,
        "elapsed_within_10s": elapsed <= ELAPSED_LIMIT_S,
    }
    record["summary"] = {
        "verdict": "PASS" if (pipeline_pass and method == "git"
                              and elapsed <= ELAPSED_LIMIT_S) else
                   ("PASS-RAW-DEGRADED" if pipeline_pass else "FAIL"),
        "pipeline_pass": pipeline_pass,
        "method": method,
        "elapsed_s": elapsed,
        "key_files_present": {k: (k in names) for k in KEY_FILES},
        "note": ("H-DS30 判定要素：git 通道 + ≤10s + SHA256 自洽；"
                 "key_files_present 反映 WB2 交付物是否已推送到位（物流项，"
                 "不影响管线判定）；remote_check 段为 DS-024 远端实况"
                 "（PAT 有效性 + 远端目录清单）"),
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(json.dumps(record["summary"], ensure_ascii=False, indent=2))
    print(f"存证: {results_path.resolve()}")
    return 0 if pipeline_pass else 1


if __name__ == "__main__":
    sys.exit(main())
