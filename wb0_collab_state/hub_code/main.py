#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agora_hub/main.py — WB0 ⇄ WB12 共享枢纽 (MVP)
==========================================
一个单端口 FastAPI 服务，作为 WB0 与 WB1/WB2 的中立数据枢纽：
  - 文件投喂：WB1/WB2 用 curl 把交付物/中间产物推给 WB0（绕过沙箱 GitHub egress 限制）
  - 报告/指令板：WB0 发布评审与指令，WB1/WB2 拉取
  - 机检直连：上传 review YAML 即触发 check_review.py，返回 PASS/FAIL 与缺口
存储：./data/{uploads,reviews,instructions}
机检：调用 /workspace/project_state/check_review.py（--root /workspace/project_state）

启动：uvicorn main:app --host 0.0.0.0 --port 8000
"""
import os, json, time, subprocess, hashlib, uuid, yaml, io, tarfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UP = DATA / "uploads"; RV = DATA / "reviews"; IN = DATA / "instructions"
for p in (UP, RV, IN):
    p.mkdir(parents=True, exist_ok=True)

CHECKER = "/workspace/project_state/check_review.py"
CHECK_ROOT = "/workspace/project_state"
LEDGER = Path(CHECK_ROOT) / "hypothesis_ledger.yaml"
AGORA_BUILDER = Path(CHECK_ROOT) / "build_agora.py"

app = FastAPI(title="Agora Hub · WB0⇄WB12 共享枢纽", version="0.1")

# ---------- 访问日志（观测云端 agent 是否真正在调用 Hub） ----------
import time as _t
from starlette.middleware.base import BaseHTTPMiddleware


# 静默窗口（cron 层已在这些时段禁止触发；此处只做“是否真的静默”的可观测审计）
SILENCE_WINDOWS = [(6 * 60 + 10, 11 * 60 + 10), (15 * 60 + 10, 17 * 60 + 10), (19 * 60 + 10, 21 * 60 + 10)]
SILENCE_LOG = DATA / "manifests" / "silence_writes.json"


def _in_silence(ts=None):
    lt = _t.localtime(ts if ts else _t.time())
    m = lt.tm_hour * 60 + lt.tm_min
    return any(a <= m < b for a, b in SILENCE_WINDOWS)


def _log_silence_write(request):
    """把静默期内的写操作落账，使“静默是否生效”可查而非靠推断。不拦截，只记录。"""
    try:
        SILENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if SILENCE_LOG.exists():
            try:
                arr = json.loads(SILENCE_LOG.read_text(encoding="utf-8"))
            except Exception:
                arr = []
        now = _t.time()
        arr.append({
            "ts": int(now),
            "time": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now)),
            "client": request.client.host if request.client else "?",
            "method": request.method,
            "path": request.url.path,
        })
        SILENCE_LOG.write_text(json.dumps(arr[-300:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class _AccessLog(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        t0 = _t.time()
        resp = await call_next(request)
        try:
            with open("/tmp/hub_access.log", "a") as f:
                f.write(f"{_t.strftime('%H:%M:%S')} {request.client.host if request.client else '?'} "
                        f"{request.method} {request.url.path} -> {resp.status_code} "
                        f"{(_t.time()-t0)*1000:.0f}ms\n")
        except Exception:
            pass
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and _in_silence():
            _log_silence_write(request)
        return resp


app.add_middleware(_AccessLog)

# ---------- 工具 ----------
def _meta(path: Path, extra=None):
    d = {
        "id": path.stem,
        "name": path.name,
        "size": path.stat().st_size,
        "mtime": int(path.stat().st_mtime),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
    }
    if extra:
        d.update(extra)
    return d

def _list(d: Path):
    out = []
    try:
        for name in os.listdir(str(d)):
            p = d / name
            if p.is_file():
                out.append(_meta(p))
    except Exception:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out

def _results_dir():
    return Path(CHECK_ROOT) / "results_v2"

# ---------- manifest 机制：绕开沙箱对目录枚举(readdir)的限制 ----------
MF = DATA / "manifests"
def _mf_path(name):
    return MF / f"{name}_manifest.json"
def _load_manifest(name):
    try:
        return json.loads(_mf_path(name).read_text(encoding="utf-8"))
    except Exception:
        return []
def _append_manifest(name, entry):
    try:
        MF.mkdir(parents=True, exist_ok=True)
        arr = _load_manifest(name)
        key = entry.get("relpath") or entry.get("name") or entry.get("id")
        arr = [e for e in arr if (e.get("relpath") or e.get("name") or e.get("id")) != key]
        arr.append(entry)
        _mf_path(name).write_text(json.dumps(arr, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
def _fallback_scan_files():
    out = []
    try:
        for n in os.listdir(str(UP)):
            p = UP / n
            if p.is_file():
                out.append(_meta(p, {"tree": "uploads", "relpath": ""}))
    except Exception:
        pass
    r2 = _results_dir()
    if r2.exists():
        try:
            for root, _, files in os.walk(str(r2)):
                rp = Path(root)
                for n in files:
                    p = rp / n
                    rel = p.relative_to(r2)
                    out.append(_meta(p, {"tree": "results_v2",
                                         "relpath": f"results_v2/{rel.as_posix()}",
                                         "id": "r2_" + str(rel.with_suffix(""))}))
        except Exception:
            pass
    return out

def _collect_files():
    """统一文件可见层：合并 uploads/ 顶层 与 results_v2/ 交付物树（含子目录）。
    修复缺口1：此前 /api/files 只列 uploads/*，带 relpath 落盘到 project_state/results_v2/ 的交付物
    在索引中“消失”，导致协作方用标准 API 查“实物在不在”得到假阴性、进而误报 open_item。
    修复缺口2（WB2-20261021-ABREV-v1 P2-b/P2-c，2026-10-21 18:05）：r2.glob("*") 非递归，
    results_v2/b_routers/ 等子目录整体不可见；且 id=r2_+stem 致 entity_embeddings.npz/.json
    撞车、下载恒返回 json。现改为递归列举 + 唯一 id（同 stem 冲突时保留首个，其余追加含点后缀），
    下载端点对候选排序保证确定性。"""
    out = _load_manifest("files")
    if not out:
        out = _fallback_scan_files()
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return out

def _run_check(yaml_path: Path):
    """调用机检器，返回 {status, errors}"""
    try:
        r = subprocess.run(
            ["python3", CHECKER, str(yaml_path), "--root", CHECK_ROOT],
            capture_output=True, text=True, timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0
        errors = [l[2:].strip() for l in out.splitlines() if l.strip().startswith("- ")]
        return {"status": "PASS" if ok else "FAIL", "errors": errors, "raw": out.strip()}
    except Exception as e:
        return {"status": "ERROR", "errors": [str(e)], "raw": str(e)}

# ---------- API ----------
@app.get("/api/files")
def list_files(brief: int = 0, tree: str = "", limit: int = 0):
    """文件列表。
    brief=1  → 去掉 sha256/content_type/kind 等，只留 id/name/size/mtime/tree/relpath
    tree=X   → 仅该树（results_v2|uploads）
    limit=N  → 截断前 N 条（已按 mtime 倒序）
    """
    arr = _collect_files()
    if tree:
        arr = [x for x in arr if x.get("tree") == tree]
    if limit:
        arr = arr[:int(limit)]
    if brief:
        arr = [{k: x.get(k) for k in ("id", "name", "size", "mtime", "tree", "relpath")} for x in arr]
    return {"files": arr}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), kind: str = Form("deliverable"),
                 relpath: str = Form("")):
    """WB1/WB2 推交付物/中间产物。
    kind: deliverable|other
    relpath: 若提供（如 results_v2/router_e2e_training.json），则落到 CHECK_ROOT 下，
             使 review 的 data_source 解析到位、机检可由 FAIL 翻 PASS。
    """
    if relpath:
        dest = Path(CHECK_ROOT) / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = UP / file.filename
    data = await file.read()
    dest.write_bytes(data)
    meta = _meta(dest, {"kind": kind, "content_type": file.content_type, "relpath": relpath})
    _append_manifest("files", meta)
    return meta

@app.get("/api/file/{fid:path}")
def get_file(fid: str):
    """单文件下载。修复 2026-10-22：① manifest relpath 曾二次拼接 results_v2 导致恒走兜底；
    ② 裸 id 兜底 UP.glob(fid*)[0] 在无匹配时返回 uploads 首件（静默错值）→ 改为精确匹配，
    无匹配一律 404，绝不返回与请求无关的文件。"""
    fid = (fid or "").strip()
    if not fid:
        raise HTTPException(404, "file id required")

    # 1) manifest 精确匹配（id 或 name），results_v2 用 CHECK_ROOT/rel 正确定位
    for e in (_load_manifest("files") or []):
        if e.get("id") == fid or e.get("name") == fid:
            rel = e.get("relpath") or ""
            if e.get("tree") == "results_v2" and rel:
                p = Path(CHECK_ROOT) / rel
            elif e.get("name"):
                p = UP / e["name"]
            else:
                continue
            if p.is_file():
                return FileResponse(p)

    # 2) r2_ 前缀：按 stem 精确匹配（禁止 glob 退化）
    if fid.startswith("r2_"):
        stem = fid[3:]
        for e in (_load_manifest("files") or []):
            if e.get("tree") != "results_v2":
                continue
            rel = e.get("relpath") or ""
            if not rel:
                continue
            p = Path(CHECK_ROOT) / rel
            if p.is_file() and p.stem == stem:
                return FileResponse(p)
        for p in sorted(_results_dir().glob(f"{stem}*")):
            if p.is_file() and p.stem == stem:
                return FileResponse(p)
        raise HTTPException(404, f"file not found: {fid}")

    # 3) 裸 id（uploads）：精确 name 匹配，命中不到即 404
    p = UP / fid
    if p.is_file():
        return FileResponse(p)
    for e in (_load_manifest("files") or []):
        if e.get("tree") == "uploads" and e.get("name") == fid:
            q = UP / e["name"]
            if q.is_file():
                return FileResponse(q)
    raise HTTPException(404, f"file not found: {fid}")

@app.get("/api/reviews")
def list_reviews():
    out = _load_manifest("reviews")
    if not out:
        out = []
        try:
            for name in os.listdir(str(RV)):
                if name.endswith(".yaml"):
                    p = RV / name
                    out.append(_meta(p, {"check": _run_check(p)}))
        except Exception:
            pass
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"reviews": out}

@app.post("/api/review")
async def post_review(file: UploadFile = File(...)):
    """上传 review YAML → 落盘 + 立即机检。WB1/WB2 也可直接推回执。"""
    name = file.filename if file.filename.endswith(".yaml") else f"{uuid.uuid4().hex[:8]}.yaml"
    dest = RV / name
    dest.write_bytes(await file.read())
    meta = _meta(dest, {"check": _run_check(dest)})
    _append_manifest("reviews", meta)
    return meta

@app.get("/api/instructions")
def list_instructions(brief: int = 0, since: int = 0, target: str = ""):
    """指令列表。
    brief=1  → 不含 body（仅 id/title/target/author/created/body_len），用于轮询筛选
    since=TS → 仅返回 created>=TS 的增量
    target=X → 仅返回 target=X 或 ALL（执行器只取自己的）
    完整正文请用 GET /api/instruction/{id} 单条拉取。
    """
    out = _load_manifest("instructions")
    if not out:
        for p in IN.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    out.sort(key=lambda x: x.get("created", 0), reverse=True)
    if since:
        out = [x for x in out if x.get("created", 0) >= since]
    if target:
        t = target.upper()
        out = [x for x in out if str(x.get("target", "")).upper() in (t, "ALL")]
    if brief:
        out = [{
            "id": x.get("id"),
            "title": x.get("title"),
            "target": x.get("target"),
            "author": x.get("author"),
            "created": x.get("created"),
            "body_len": len(x.get("body") or ""),
        } for x in out]
    return {"instructions": out}

@app.get("/api/instruction/{fid}")
def get_instruction(fid: str):
    for e in (_load_manifest("instructions") or []):
        if e.get("id") == fid:
            p = IN / f"{fid}.json"
            if p.exists():
                return FileResponse(p, media_type="application/json")
    cand = [p for p in IN.glob(f"{fid}*")]
    if not cand:
        raise HTTPException(404, "instruction not found")
    return FileResponse(cand[0], media_type="application/json")

@app.post("/api/instruction")
async def post_instruction(title: str = Form(...), target: str = Form("ALL"),
                           body: str = Form(...), author: str = Form("WB0")):
    """WB0 发布指令/评审给 WB1/WB2。"""
    rid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    payload = {
        "id": rid, "title": title, "target": target,
        "body": body, "author": author, "created": int(time.time()),
    }
    (IN / f"{rid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_manifest("instructions", payload)
    return payload

# ---------- 台账 API（Hub = 唯一事实源；任何沙盒经 HTTP 读写，不再依赖本地路径） ----------
def _load_ledger():
    """优先 ruamel（保留注释），回退 pyyaml。"""
    try:
        from ruamel.yaml import YAML
        y = YAML()
        return y.load(LEDGER.read_text(encoding="utf-8")) or {}
    except ImportError:
        import yaml
        return yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}

def _save_ledger(d):
    """原子写（规范规则1：tmp+rename）；ruamel 保留注释。"""
    try:
        from ruamel.yaml import YAML
        import io
        y = YAML()
        y.width = 4096
        buf = io.StringIO()
        y.dump(d, buf)
        text = buf.getvalue()
    except ImportError:
        import yaml
        text = yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
    tmp = LEDGER.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(LEDGER)

def _bump_version(d):
    v = str(d.get("ledger_version", "0.0"))
    try:
        major, minor = v.split(".")
        d["ledger_version"] = f"{major}.{int(minor)+1}"
    except Exception:
        d["ledger_version"] = "0.1"

def _rebuild_agora():
    """调用 build_agora.py 重生看板。
    注意：build_agora.py 用相对路径读写 hypothesis_ledger.yaml / agora.html，
    故子进程必须在其所在目录（CHECK_ROOT）下运行，否则静默失败。"""
    try:
        r = subprocess.run(["python3", str(AGORA_BUILDER)],
                           cwd=str(CHECK_ROOT), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print("[rebuild] build_agora.py 失败:", r.stderr[:300], file=sys.stderr)
        return r.returncode == 0
    except Exception as e:
        print("[rebuild] 异常:", e, file=sys.stderr)
        return False

@app.get("/api/ledger")
def get_ledger():
    """读台账全文（假设/纪律/open_items）。"""
    if not LEDGER.exists():
        raise HTTPException(404, "ledger not found")
    return _load_ledger()

@app.post("/api/ledger/hypothesis")
def upsert_hypothesis(entry: dict = Body(...)):
    """按 id upsert 一条假设（自动 bump 版本 + 重生看板）。"""
    if not entry.get("id"):
        raise HTTPException(400, "id required")
    d = _load_ledger()
    hyps = d.setdefault("hypotheses", [])
    for i, h in enumerate(hyps):
        if h.get("id") == entry["id"]:
            hyps[i] = {**h, **entry}; break
    else:
        hyps.append(entry)
    d["last_update"] = {"via": "api", "at": int(time.time()),
                        "action": f"upsert {entry['id']}"}
    _bump_version(d); _save_ledger(d); _rebuild_agora()
    return {"ok": True, "ledger_version": str(d["ledger_version"]), "hypothesis": entry["id"]}

@app.post("/api/ledger/open_item")
def upsert_open_item(item: dict = Body(...)):
    """按 id upsert 一条 open_item（阻塞项/待裁定项）。"""
    if not item.get("id"):
        raise HTTPException(400, "id required")
    d = _load_ledger()
    items = d.setdefault("open_items", [])
    for i, o in enumerate(items):
        if o.get("id") == item["id"]:
            items[i] = {**o, **item}; break
    else:
        items.append(item)
    d["last_update"] = {"via": "api", "at": int(time.time()),
                        "action": f"upsert open_item {item['id']}"}
    _bump_version(d); _save_ledger(d); _rebuild_agora()
    return {"ok": True, "ledger_version": str(d["ledger_version"]), "open_item": item["id"]}

@app.get("/api/discipline")
def discipline_check():
    """AI 助手纪律机检（依据 000_AI_HUMAN_COLLAB_DISCIPLINE.md 可机检项）。
    WB0 每轮巡检可调用；FAIL 项应列入裁定。"""
    import re as _re
    PH = _re.compile(r"[（(]\s*(待核|疑似|存疑|待定|TBD|TODO)\s*[）)]", _re.I)
    MO = _re.compile(r"[ãåÃÅ¤¦]")
    issues = []
    for e in (_load_manifest("reviews") or []):
        name = e.get("name") or ""
        p = RV / name
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if PH.search(txt):
            issues.append({"file": name, "rule": "PA-13 占位旗标"})
        if MO.search(txt):
            issues.append({"file": name, "rule": "编码乱码"})
        if "falsifiable_criterion" not in txt:
            issues.append({"file": name, "rule": "DS-D4 缺 falsifiable_criterion"})
        if _re.search(r"^status:\s*refuted\s*$", txt, _re.M) and "ds_admitted_refutation" not in txt:
            issues.append({"file": name, "rule": "DS-D4 refuted 未认错"})
        m = _re.search(r"^data_source:\s*([^\s\n#][^\n]*)$", txt, _re.M)
        if m:
            rel = _re.sub(r"^[-*]\s*", "", m.group(1).strip().strip('"\''))
            if rel and not rel.startswith("http") and rel != name:
                if not (Path(CHECK_ROOT) / rel).exists():
                    issues.append({"file": name, "rule": "D4 data_source 实物不存在", "rel": rel})
            elif rel == name:
                issues.append({"file": name, "rule": "D4 data_source 自引用", "rel": rel})
    for e in (_load_manifest("instructions") or []):
        t, b = e.get("title", ""), e.get("body", "")
        if MO.search(t) or MO.search(b):
            issues.append({"file": e.get("id"), "rule": "编码乱码"})
        if PH.search(b):
            issues.append({"file": e.get("id"), "rule": "PA-13 占位旗标"})
    return {"total": len(issues), "status": "PASS" if not issues else "FAIL", "issues": issues}

@app.get("/api/contract")
def contract_check():
    """BEHAVIOR_CONTRACT 形式机检（只查"有没有回合自检块"，不查内容）。
    基线 2026-10-22 06:50 之前的历史产出豁免。"""
    ITEMS = [f"B-{i:02d}" for i in range(1, 11)]
    BASE = 1792623000

    def _has(t):
        return ("回合自检" in t) or all(x in t for x in ITEMS)

    issues = []
    for e in (_load_manifest("instructions") or []):
        t, b = e.get("title", ""), e.get("body", "")
        if "裁定" not in t:
            continue
        if int(e.get("created", 0) or 0) < BASE:
            continue
        if not _has(b):
            issues.append({"file": e.get("id"), "rule": "缺回合自检块(B-05/B-06)"})
    for e in (_load_manifest("files") or []):
        if e.get("tree") != "results_v2":
            continue
        n = e.get("name", "")
        if not n.endswith(".md") or not n.startswith(("wb1_exec_round_", "wb2_run_report_", "wb0_", "_silence_report_")):
            continue
        rel = e.get("relpath") or ""
        if not rel:
            continue
        p = Path(CHECK_ROOT) / rel
        if not p.is_file() or p.stat().st_mtime < BASE:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not _has(txt):
            issues.append({"file": n, "rule": "缺回合自检块(B-05/B-06)"})
    return {"total": len(issues), "status": "PASS" if not issues else "FAIL", "issues": issues, "baseline_ts": BASE}

@app.get("/api/brief")
def brief():
    """一站式巡检摘要 —— 替代逐个拉 files/reviews/instructions/state/ledger。
    目标 < 3KB，覆盖每轮巡检所需全部计数与增量；正文用 /api/instruction/{id} 单条取。
    设计意图：把"多次全量拉取"压成"一次摘要"，中间数据不进入调用方上下文。"""
    from collections import Counter as _C
    led = _load_ledger() if LEDGER.exists() else {}
    hyps = led.get("hypotheses", []) or []
    items = led.get("open_items", []) or []
    files = _collect_files()
    reviews = _load_manifest("reviews") or []
    instrs = _load_manifest("instructions") or []
    instrs.sort(key=lambda x: x.get("created", 0), reverse=True)
    reviews.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    rc, hc = _C(r.get("check", {}).get("status") for r in reviews), _C(h.get("status") for h in hyps)
    try:
        dchk, cchk = discipline_check(), contract_check()
        chk = {"discipline": dchk.get("status"), "discipline_issues": dchk.get("total"),
               "contract": cchk.get("status"), "contract_issues": cchk.get("total")}
    except Exception as e:
        chk = {"error": str(e)[:80]}
    try:
        sil = silence_status()
        sil_brief = {"in_window": sil["in_silence_window"], "by_window_today": sil["by_window_today"]}
    except Exception:
        sil_brief = {}
    return {
        "now": _now(),
        "ledger_version": str(led.get("ledger_version")),
        "counts": {
            "files": len(files), "reviews": len(reviews),
            "reviews_pass": rc.get("PASS", 0), "reviews_fail": rc.get("FAIL", 0),
            "instructions": len(instrs), "hypotheses": len(hyps),
            "hypotheses_by_status": dict(hc),
            "open_items": len(items),
            "open_items_open": sum(1 for o in items if o.get("status") == "open"),
        },
        "open_items": [{"id": o.get("id"), "status": o.get("status"), "title": (o.get("title") or "")[:80]}
                       for o in items if o.get("status") == "open"],
        "reviews_latest": [{"name": r.get("name"), "mtime": r.get("mtime"), "status": r.get("check", {}).get("status")}
                           for r in reviews[:5]],
        "instructions_latest": [{"id": i.get("id"), "title": (i.get("title") or "")[:70],
                                 "target": i.get("target"), "created": i.get("created")} for i in instrs[:5]],
        "checks": chk,
        "silence": sil_brief,
        "hint": "完整正文：/api/instruction/{id}；机检明细：/api/discipline、/api/contract；文件清单：/api/files?brief=1",
    }


@app.get("/api/audit")
def audit():
    """预聚合视野补偿：brief 化后丢失的"顺带审计"能力，用确定性检查补回。
    只读，不修改任何数据。WB0 每轮巡检调用一次（返回 <1KB）。
    覆盖：brief 自一致 / 索引指向实物 / 空回执 / 非法 target / 重复正文 / 回执索引缺失。"""
    issues = []
    files_all = _collect_files()
    reviews_all = _load_manifest("reviews") or []
    instrs_all = _load_manifest("instructions") or []
    led = _load_ledger() if LEDGER.exists() else {}
    hyps = led.get("hypotheses", []) or []

    # A1 brief 计数与实际是否一致（防"我改 brief 时漏字段"导致全员静默失明）
    try:
        b = brief()
        actual = {"files": len(files_all), "reviews": len(reviews_all),
                  "instructions": len(instrs_all), "hypotheses": len(hyps)}
        for k, v in actual.items():
            bv = (b.get("counts") or {}).get(k)
            if bv != v:
                issues.append({"rule": "A1", "detail": f"brief.{k}={bv} 但实际={v}"})
    except Exception as e:
        issues.append({"rule": "A1", "detail": f"brief 不可用: {str(e)[:100]}"})

    # A2 索引条目指向的实物是否存在（正向校验；反向枚举受沙箱限制）
    miss = []
    for e in (_load_manifest("files") or []):
        rel = e.get("relpath") or ""
        if e.get("tree") == "results_v2" and rel and not (Path(CHECK_ROOT) / rel).exists():
            miss.append(rel)
    if miss:
        issues.append({"rule": "A2", "detail": f"{len(miss)} 个 files 索引指向的文件不存在", "items": miss[:8]})
    miss_rv = [e.get("name") for e in reviews_all if not (RV / str(e.get("name") or "")).exists()]
    if miss_rv:
        issues.append({"rule": "A2", "detail": f"{len(miss_rv)} 个 reviews 索引指向的文件不存在", "items": miss_rv[:8]})

    # A3 空/超小回执、缺 hypotheses 段（顺带审计的核心：全量扫过才看得见）
    for e in reviews_all:
        p = RV / str(e.get("name") or "")
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(txt) < 200:
            issues.append({"rule": "A3", "detail": f"回执过小 {e.get('name')} ({len(txt)}B)"})
        elif "hypotheses:" not in txt:
            issues.append({"rule": "A3", "detail": f"回执缺 hypotheses 段 {e.get('name')}"})

    # A4 非法 target（会导致执行器永远取不到或全取）
    valid = {"WB0", "WB1", "WB2", "ALL"}
    for i in instrs_all:
        t = str(i.get("target") or "")
        if t and t.upper() not in valid:
            issues.append({"rule": "A4", "detail": f"非法 target='{t}' @ {i.get('id')}"})

    # A6 重复正文（乱码重发/同一事实二次发布的先兆）
    from collections import defaultdict as _dd
    seen = _dd(list)
    for i in instrs_all:
        bd = (i.get("body") or "").strip()
        if len(bd) > 100:
            seen[hashlib.sha256(bd.encode()).hexdigest()[:12]].append(i.get("id"))
    for h, ids in seen.items():
        if len(ids) > 1:
            issues.append({"rule": "A6", "detail": f"重复正文 {len(ids)} 条（{h}）", "items": ids[:4]})

    return {"status": "PASS" if not issues else "FAIL", "total": len(issues),
            "counts": {"files": len(files_all), "reviews": len(reviews_all),
                       "instructions": len(instrs_all), "hypotheses": len(hyps)},
            "issues": issues[:30],
            "note": "A1=brief自一致 A2=索引指向实物 A3=空回执/缺段 A4=非法target A6=重复正文"}


@app.get("/api/silence")
def silence_status():
    """静默期可观测性：当前是否在静默窗口 + 今日静默期内的写操作清单。
    用途：判断“静默是否生效”不靠 lastRunAt 推断，直接看本端点。"""
    now = _t.time()
    arr = []
    try:
        if SILENCE_LOG.exists():
            arr = json.loads(SILENCE_LOG.read_text(encoding="utf-8"))
    except Exception:
        arr = []
    today = _t.strftime("%Y-%m-%d", _t.localtime(now))
    todays = [x for x in arr if str(x.get("time", "")).startswith(today)]
    by_path = {}
    for x in todays:
        by_path[x["path"]] = by_path.get(x["path"], 0) + 1
    # 按静默段分组（同日三段互不混淆），供静默期后首轮直接引用
    by_window_today = {}
    for a, b in SILENCE_WINDOWS:
        key = f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
        cnt = 0
        for x in todays:
            hhmm = str(x.get("time", ""))[11:16]
            try:
                hh, mm = int(hhmm[:2]), int(hhmm[3:5])
            except Exception:
                continue
            if a <= hh * 60 + mm < b:
                cnt += 1
        by_window_today[key] = cnt
    return {
        "now": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now)),
        "in_silence_window": _in_silence(now),
        "windows": [{"from": f"{a // 60:02d}:{a % 60:02d}", "to": f"{b // 60:02d}:{b % 60:02d}"} for a, b in SILENCE_WINDOWS],
        "today_silence_writes": len(todays),
        "by_window_today": by_window_today,
        "by_path": by_path,
        "recent": todays[-10:],
        "note": "仅记录不拦截：cron 约束调度器触发，手动会话仍可写入；本端点用于事后审计。",
    }


@app.get("/api/state")
def state_summary():
    """心跳/执行器一站式状态快照：台账摘要 + 回执机检 + 指令计数。"""
    d = _load_ledger() if LEDGER.exists() else {}
    hyps = d.get("hypotheses", []) or []
    reviews = _load_manifest("reviews") or []
    if not reviews:
        reviews = [{"id": p.stem, "check": _run_check(p)} for p in RV.glob("*.yaml")]
    instrs = _load_manifest("instructions") or []
    if not instrs:
        for p in IN.glob("*.json"):
            try: instrs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception: pass
    return {
        "ledger_version": str(d.get("ledger_version")),
        "hypotheses_total": len(hyps),
        "hypotheses_pending": sum(1 for h in hyps if h.get("status") == "pending"),
        "open_items": d.get("open_items", []),
        "reviews": reviews,
        "instructions_count": len(instrs),
        "agora_rebuilt": True,
    }

def _now():
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())

# 假设状态强度序（用于"只升不降"判断；definitive 三者互不覆盖，冲突留给人工）
_STRENGTH = {"pending": 0, "verified_proxy": 1, "verified": 2, "refuted": 3}

def _reconcile_one_review(review_path: Path, ledger: dict, changes: list) -> dict:
    """处理一条回执：仅当机检 PASS 时，把其声明且 definitive 的假设同步进台账。
    纪律：
      - 机检不过不写（DS-D4）；
      - 只升不降（never downgrade）：pending 不会自动升 definitive，definitive 之间冲突不自动覆盖；
      - 不凭空新建假设（回执引用了台账没有的 id → 警告跳过，由 WB0 主会话决定）；
      - 幂等：已 verified/verified_at 的条目不再刷新时间戳。
    """
    chk = _run_check(review_path)
    res = {"review": review_path.stem, "check": chk["status"], "updates": [], "warnings": []}
    if chk["status"] != "PASS":
        res["warnings"].append("机检未 PASS，跳过台账写入：" + "; ".join(chk["errors"]))
        return res
    try:
        rv = yaml.safe_load(review_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        res["warnings"].append(f"回执解析失败: {e}")
        return res
    hyps = rv.get("hypotheses", []) or []
    ledger_hyps = ledger.setdefault("hypotheses", [])
    for rh in hyps:
        rid = rh.get("id")
        if not rid:
            continue
        lh = next((h for h in ledger_hyps if h.get("id") == rid), None)
        if lh is None:
            res["warnings"].append(f"假设 {rid} 不在台账中，跳过（不自动新建）")
            continue
        new_status = rh.get("status", "pending")
        cur_status = lh.get("status", "pending")
        proposed = dict(lh)
        # 同步声明字段（actual_data / 可证伪判据），data_source 仅在回执提供时归一化
        proposed["actual_data"] = rh.get("actual_data", lh.get("actual_data"))
        proposed["falsifiable_criterion"] = rh.get("falsifiable_criterion", lh.get("falsifiable_criterion"))
        proposed["ds_admitted_refutation"] = rh.get("ds_admitted_refutation", lh.get("ds_admitted_refutation", False))
        ds = rh.get("data_source")
        if ds:
            proposed["data_source"] = ds
            if lh.get("data_source_verified") is not True:
                proposed["data_source_verified"] = True
                proposed["data_source_verified_at"] = int(time.time())
        action = "kept"
        if new_status in ("verified", "verified_proxy", "refuted"):
            if cur_status == new_status:
                action = "refreshed"
            elif _STRENGTH.get(cur_status, 0) < _STRENGTH.get(new_status, 0):
                proposed["status"] = new_status
                proposed["verified_at"] = int(time.time())
                proposed["verified_by"] = "hub-reconcile"
                action = "upgraded"
            else:
                res["warnings"].append(f"{rid}: 回执声明 {new_status} 与台账 {cur_status} 冲突，未自动覆盖（留人工）")
                continue
        else:  # pending
            if cur_status in ("verified", "verified_proxy", "refuted"):
                action = "kept_definitive"  # 不降级
        if proposed != dict(lh):
            for k, v in proposed.items():
                lh[k] = v
            changes.append(rid)
        res["updates"].append({"id": rid, "action": action, "from": cur_status, "to": proposed.get("status", cur_status)})
    return res

@app.post("/api/reconcile")
def reconcile():
    """重跑所有回执机检，将机检 PASS 且声明 definitive 的假设同步进台账。
    安全：只升不降、不新建、不伪造、机检不过不写；幂等。
    触发：心跳或 WB2 自动执行器可定时调用，实现无人值守闭环。"""
    if not LEDGER.exists():
        raise HTTPException(404, "ledger not found")
    ledger = _load_ledger()
    v_before = str(ledger.get("ledger_version"))
    changes = []
    results = []
    rv_entries = _load_manifest("reviews") or []
    if rv_entries:
        for e in rv_entries:
            p = RV / f"{e.get('name', '').replace('.yaml', '')}.yaml"
            if p.exists():
                results.append(_reconcile_one_review(p, ledger, changes))
    else:
        for p in RV.glob("*.yaml"):
            results.append(_reconcile_one_review(p, ledger, changes))
    # 更新顶层机检索引
    latest = ledger.get("latest_review")
    if latest:
        rp = RV / f"{latest}.yaml"
        if rp.exists():
            chk = _run_check(rp)
            ledger["review_machine_check"] = (
                f"PASS ({_now()})" if chk["status"] == "PASS"
                else f"FAIL ({_now()}): " + "; ".join(chk["errors"]))
    changed = len(changes) > 0
    if changed:
        ledger["last_update"] = {"via": "reconcile", "at": int(time.time()),
                                 "action": f"sync {len(changes)} hypotheses: {','.join(changes)}"}
        _bump_version(ledger); _save_ledger(ledger); _rebuild_agora()
    return {
        "reviews_checked": len(results),
        "reviews": results,
        "ledger_changed": changed,
        "ledger_version_before": v_before,
        "ledger_version_after": str(ledger.get("ledger_version")),
        "agora_rebuilt": changed,
    }

# ---------- 全量快照（离平台备份用） ----------
@app.get("/api/snapshot")
def snapshot():
    """全量 SSOT 快照（tar.gz）：ledger + reviews + instructions + results_v2 交付物。
    供离平台备份任务（独立沙盒）经公网拉取，与本地文件系统解耦，实现 WB0 侧反脆弱备份。
    仅含协作状态，不含任何密钥（deploy key 在 ~/.ssh，不在 project_state）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if LEDGER.exists():
            tar.add(str(LEDGER), arcname="ledger/hypothesis_ledger.yaml")
        # 修复（2026-10-21 21:xx）：改用 manifest 已知路径清单，避免受限沙箱下
        # rglob 目录枚举失败导致快照残缺（b_routers 18 权重与多数 results_v2 文件丢失）。
        for e in (_load_manifest("reviews") or []):
            p = RV / e.get("name", "")
            if p.is_file():
                tar.add(str(p), arcname=f"reviews/{p.name}")
        for e in (_load_manifest("instructions") or []):
            p = IN / f"{e.get('id', '')}.json"
            if p.is_file():
                tar.add(str(p), arcname=f"instructions/{p.name}")
        _f = _load_manifest("files") or []
        for e in _f:
            if e.get("tree") != "results_v2":
                continue
            rel = e.get("relpath", "")
            if not rel:
                continue
            p = Path(CHECK_ROOT) / rel          # rel 形如 results_v2/b_routers/xxx.pt，保留子目录结构
            if p.is_file():
                tar.add(str(p), arcname=rel)
        if not _f:                               # 兜底：manifest 为空时退回 rglob
            r2 = _results_dir()
            if r2.exists():
                for p in r2.rglob("*"):
                    if p.is_file():
                        tar.add(str(p), arcname=f"results_v2/{p.relative_to(r2).as_posix()}")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="application/gzip",
                   headers={"Content-Disposition": "attachment; filename=agora_snapshot.tgz"})

# ---------- 前端看板 ----------
def _badge(st):
    c = {"PASS": "#1a7f37", "FAIL": "#cf222e", "ERROR": "#bf8700"}.get(st, "#666")
    return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px">{st}</span>'

@app.get("/", response_class=HTMLResponse)
def dashboard():
    files = _collect_files()
    reviews = _load_manifest("reviews") or []
    if not reviews:
        for p in RV.glob("*.yaml"):
            reviews.append(_meta(p, {"check": _run_check(p)}))
    reviews.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    instrs = _load_manifest("instructions") or []
    if not instrs:
        for p in IN.glob("*.json"):
            try:
                instrs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    instrs.sort(key=lambda x: x.get("created", 0), reverse=True)

    file_rows = "".join(
        f"<tr><td><code>{f['name']}</code>{('<span class=src>results_v2</span>' if f.get('tree')=='results_v2' else '')}</td>"
        f"<td>{f['size']}</td><td><code>{f['sha256']}</code></td>"
        f"<td><a href='/api/file/{f['id']}'>下载</a></td></tr>" for f in files) or "<tr><td colspan=4>— 暂无 —</td></tr>"
    rev_rows = "".join(
        f"<tr><td><code>{r['name']}</code></td><td>{_badge(r['check']['status'])}</td>"
        f"<td>{'<br>'.join(r['check']['errors']) or '—'}</td></tr>" for r in reviews) or "<tr><td colspan=3>— 暂无 —</td></tr>"
    instr_rows = "".join(
        f"<tr><td><b>{i.get('title','')}</b><br><span style='color:#8b949e'>→{i.get('target','')} · {i.get('author','')}</span></td>"
        f"<td style='max-width:420px'>{i.get('body','')}</td>"
        f"<td><a href='/api/instruction/{i.get('id','')}'>JSON</a></td></tr>" for i in instrs) or "<tr><td colspan=3>— 暂无 —</td></tr>"

    html = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>Agora Hub · WB0⇄WB12</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0d1117;color:#e6edf3}}
header{{padding:16px 22px;background:#161b22;border-bottom:1px solid #30363d}}
h1{{margin:0;font-size:19px}} .sub{{color:#8b949e;font-size:13px}}
.wrap{{padding:18px 22px;display:grid;gap:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}}
.card h3{{margin:0 0 10px;font-size:15px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border:1px solid #30363d;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#21262d}} code{{color:#79c0ff}} .src{{color:#7ee787;font-size:11px;margin-left:6px}}
form{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}} input,textarea,select{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px}}
button{{background:#238636;color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer}}
.note{{color:#8b949e;font-size:12px;margin-top:8px}}
</style></head><body>
<header><h1>🛰️ Agora Hub · WB0⇄WB12 共享枢纽</h1>
<div class="sub">MVP v0.1 · 文件投喂 + 报告/指令板 + 机检直连（check_review.py）</div></header>
<div class="wrap">

<div class="card"><h3>① 文件投喂（WB1/WB2 → WB0）</h3>
<form action="/api/upload" method="post" enctype="multipart/form-data">
<input type="file" name="file" required>
<select name="kind"><option value="deliverable">deliverable</option><option value="other">other</option></select>
<button type=submit>上传</button></form>
<div class="note">curl 示例： curl -F "file=@router_e2e_training.json" -F "kind=deliverable" http://&lt;URL&gt;/api/upload</div>
<table><tr><th>文件名</th><th>大小</th><th>SHA256(16)</th><th>操作</th></tr>{file_rows}</table></div>

<div class="card"><h3>② 回执/报告机检（上传即 check_review.py）</h3>
<form action="/api/review" method="post" enctype="multipart/form-data">
<input type="file" name="file" accept=".yaml" required>
<button type=submit>提交并机检</button></form>
<table><tr><th>回执</th><th>机检</th><th>缺口</th></tr>{rev_rows}</table></div>

<div class="card"><h3>③ 指令/评审板（WB0 → WB1/WB2）</h3>
<form action="/api/instruction" method="post">
<input name="title" placeholder="标题" style="min-width:200px" required>
<select name="target"><option>ALL</option><option>WB1</option><option>WB2</option></select>
<textarea name="body" placeholder="指令/评审正文…" rows=3 style="min-width:320px" required></textarea>
<button type=submit>发布</button></form>
<table><tr><th>指令</th><th>大小</th><th>操作</th></tr>{instr_rows}</table></div>

<div class="card"><h3>④ 对账 / 自动推进（机检 PASS → 自动入台账）</h3>
<form action="/api/reconcile" method="post"><button type=submit>🔄 运行 /api/reconcile</button></form>
<div class="note">纪律：只升不降、不新建、不伪造、机检不过不写、幂等。可由心跳或 WB2 执行器定时触发，实现无人值守闭环。</div></div>

</div></body></html>"""
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
