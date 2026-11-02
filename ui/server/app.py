#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — token-research 快照库的只读 HTTP API。

前后端分离：本文件只提供 JSON 接口，前端是 token-research-ui/web/ 下的静态页。
直接复用技能里的 store.py（同一进程，无跨进程复制），DB 路径解析规则与 CLI 完全一致。

启动
----
    python server/app.py                      # 默认 127.0.0.1:8020
    python server/app.py --port 8020 --host 0.0.0.0

DB 定位（与 defillama.py 一致）
    --db PATH > $TOKEN_RESEARCH_DB > ~/.token_research_db > 向上查找 > skill 同级

接口
----
    GET  /api/health                 服务与库状态（DB 路径、表行数、user_version）
    GET  /api/objects                对象列表（含最新快照摘要）
         ?kind=chain|coin|protocol   ?stale_days=N   ?with_reports=1
    GET  /api/objects/{name}         单个对象全貌：快照 + 持有结构 + 报告 + 历史
         ?at=<id|YYYY-MM-DD>         指定快照（默认最新）
         ?history=12                 附带最近 N 条时间序列
    GET  /api/objects/{name}/history 时间序列
         ?metric=tvl_usd,fees_24h    ?limit=N   ?since=YYYY-MM-DD
    GET  /api/reports                已登记报告
         ?name=<对象>   ?limit=N
    GET  /api/stats                  概览统计（供首页 KPI）

只读：没有任何写接口。要写库请用技能自带的 CLI（snapshot / report add / import）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# ---- 让 store.py 可导入：技能可能在多处，按候选顺序找 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_CANDIDATES = [
    os.path.join(_HERE, "..", "..", ".claude", "skills", "token-research", "scripts"),
    os.path.join(_HERE, "..", "..", "币圈", "区块链", "区块链工具",
                 "token-research代币研究方法论", "skill", "scripts"),
]
for _p in _SKILL_CANDIDATES:
    _p = os.path.abspath(_p)
    if os.path.isfile(os.path.join(_p, "store.py")):
        sys.path.insert(0, _p)
        break
else:
    sys.exit("[error] 找不到 store.py。请把 server/ 放在 token-research-ui/ 下，"
             "或用 PYTHONPATH 指向技能的 scripts/ 目录。")

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    sys.exit("[error] 需要 fastapi：pip install fastapi uvicorn")

import store  # noqa: E402

app = FastAPI(
    title="token-research 快照库 API",
    version="1.0.0",
    description="只读接口。写库请用技能 CLI（snapshot / report add / import）。",
)

# 前端独立部署时（如 vite dev server）需要放行跨域；同源部署时可以去掉。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173",
                   "http://127.0.0.1:8020", "http://localhost:8020"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 请求级连接：每次请求开一个只读连接，避免 sqlite 跨线程复用
# ---------------------------------------------------------------------------
_DB_PATH = {"value": None}


def db_path() -> str:
    if _DB_PATH["value"] is None:
        _DB_PATH["value"] = store.resolve_db_path()[0]
    return _DB_PATH["value"]


def get_conn():
    conn = store.connect(db_path())
    store.ensure_schema(conn)   # 幂等，空库也能起
    return conn


def _obj_or_404(conn, name: str):
    obj = store.find_object(conn, name)
    if obj is None:
        hint = store.suggest_names(conn, name)
        msg = "对象 '{}' 不在库中".format(name)
        if hint:
            msg += "，是不是: {}？".format(" / ".join(hint))
        raise HTTPException(status_code=404, detail=msg)
    return obj

def _latest_holders(conn, object_id: int):
    """该对象最近一次持有结构（可能挂在旧快照上，不一定是 latest snapshot）。"""
    row = conn.execute(
        "SELECT th.holders, th.top10_holding_pct, th.captured_at,"
        " (SELECT s.captured_day FROM snapshots s WHERE s.id = th.snapshot_id) AS captured_day"
        " FROM token_holders th WHERE th.object_id = ?"
        " ORDER BY th.captured_at DESC, th.id DESC LIMIT 1",
        (object_id,),
    ).fetchone()
    if row is None:
        return None
    return {"holders": row["holders"], "top10_holding_pct": row["top10_holding_pct"],
            "captured_at": row["captured_at"], "captured_day": row["captured_day"]}


def _obj_public(obj: dict) -> dict:
    keys = ("id", "slug", "name", "kind", "defillama_chain", "defillama_protocols",
            "coingecko_slug", "notes", "aliases", "first_researched_at",
            "last_researched_at", "last_snapshot_at", "last_report_at")
    return {k: obj.get(k) for k in keys}


def _snap_public(snap: dict) -> dict:
    return {
        "id": snap["id"],
        "captured_at": snap["captured_at"],
        "captured_day": snap["captured_day"],
        "origin": snap["origin"],
        "metrics": snap["metrics"],
        "metrics_m": snap["metrics_m"],
        "extra": snap["extra"],
        "missing": snap["missing"],
        "source_status": snap["source_status"],
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/api/health", summary="服务与库状态")
def health():
    path = db_path()
    meta = store.db_meta(path)
    out = {"service": "token-research-api", "db": path, "db_exists": meta["exists"],
           "db_source": store.resolve_db_path()[1], "user_version": meta["user_version"],
           "size_bytes": meta["size_bytes"], "counts": {}}
    if meta["exists"]:
        conn = get_conn()
        try:
            for t in ("objects", "snapshots", "snapshot_protocols",
                      "token_holders", "holder_entries", "reports"):
                out["counts"][t] = conn.execute(
                    "SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
        finally:
            conn.close()
    return out


@app.get("/api/stats", summary="概览统计")
def stats():
    """首页 KPI：总数 / 已判定 / 叙事 / 有持有结构 / TVL 合计 / 最近抓取日。"""
    conn = get_conn()
    try:
        objects = store.list_objects(conn, with_reports=True)
        total = len(objects)
        judged = 0
        narrative = 0
        mixed = 0
        verify = 0
        total_tvl = 0.0
        days = []
        for o in objects:
            obj = store.find_object(conn, o["slug"])
            if obj is None:
                continue
            snap = store.latest_snapshot(conn, obj["id"])
            rep = conn.execute(
                "SELECT verdict FROM reports WHERE object_id = ? ORDER BY report_date DESC, id DESC LIMIT 1",
                (obj["id"],)).fetchone()
            if rep:
                judged += 1
                if rep["verdict"] == "narrative":
                    narrative += 1
                elif rep["verdict"] == "mixed":
                    mixed += 1
                elif rep["verdict"] == "real":
                    verify += 1
            if snap:
                total_tvl += snap["metrics"].get("tvl_usd") or 0.0
                days.append(snap["captured_day"])
        return {
            "total": total, "judged": judged, "narrative": narrative,
            "mixed": mixed, "real": verify,
            "with_holders": conn.execute(
                "SELECT COUNT(DISTINCT object_id) FROM token_holders").fetchone()[0],
            "total_tvl_usd": total_tvl,
            "total_tvl_m": round(total_tvl / 1e6, 2),
            "latest_captured_day": max(days) if days else None,
            "db": db_path(),
        }
    finally:
        conn.close()


@app.get("/api/objects", summary="对象列表")
def list_objects(
    kind: str = Query(None, description="chain / coin / protocol"),
    stale_days: float = Query(None, description="只返回超过 N 天未更新的对象"),
    with_reports: int = Query(0, description="1 = 附带最近报告"),
    sort: str = Query("recent", description="recent / tvl / name"),
    limit: int = Query(None),
):
    conn = get_conn()
    try:
        rows = store.list_objects(conn, kind=kind, sort=sort,
                                  stale_days=stale_days, with_reports=bool(with_reports))
        if limit:
            rows = rows[:limit]
        # 给每个对象补上 TSR（TVL / 市值）、代码、持有结构。TSR 由两部分算出，
        # 前端不自己算，免得两处口径不一致。
        for r in rows:
            obj = conn.execute("SELECT id FROM objects WHERE slug = ?", (r["slug"],)).fetchone()
            if obj is None:
                continue
            snap = store.latest_snapshot(conn, obj["id"])
            m = (snap or {}).get("metrics") or {}
            extra = (snap or {}).get("extra") or {}
            tvl = m.get("tvl_usd")
            mcap = m.get("market_cap_usd")
            r["tvl_usd"] = tvl
            r["market_cap_usd"] = mcap
            r["fdv_usd"] = m.get("fdv_usd")
            r["tsr"] = (tvl / mcap) if (tvl and mcap) else None
            # 代码不猜：只有快照 extra 里记了 price_symbol 才给，否则 null。
            r["symbol"] = extra.get("price_symbol")
            h = _latest_holders(conn, obj["id"])
            r["holders"] = h["holders"] if h else None
            r["top10_pct"] = h["top10_holding_pct"] if h else None
            r["holders_captured_day"] = h["captured_day"] if h else None
        return {"db": db_path(), "count": len(rows), "objects": rows}
    finally:
        conn.close()


@app.get("/api/objects/{name}", summary="单个对象全貌")
def get_object(
    name: str,
    at: str = Query(None, description="快照 id 或 YYYY-MM-DD，默认最新"),
    history: int = Query(12, description="附带最近 N 条时间序列，0 表示不带"),
    with_protocols: int = Query(1, description="1 = 附带生态明细（若有）"),
):
    conn = get_conn()
    try:
        obj = _obj_or_404(conn, name)
        snap = store.snapshot_by_ref(conn, obj["id"], at)
        if snap is None:
            raise HTTPException(
                status_code=404,
                detail="对象 '{}' 没有匹配的快照（--at {}）".format(name, at))
        out = {
            "object": _obj_public(obj),
            "snapshot": _snap_public(snap),
            "protocols": store.protocols_of(conn, snap["id"]) if with_protocols else None,
            "holder_structure": store.holders_of(conn, snap["id"]),
            "reports": store.list_reports(conn, obj["id"], limit=20),
            "db": db_path(),
        }
        if history:
            out["history"] = store.history(conn, obj["id"], limit=history)
        return out
    finally:
        conn.close()


@app.get("/api/objects/{name}/history", summary="时间序列")
def get_history(
    name: str,
    metric: str = Query("tvl_usd", description="逗号分隔的指标名"),
    limit: int = Query(30),
    since: str = Query(None, description="YYYY-MM-DD"),
):
    conn = get_conn()
    try:
        obj = _obj_or_404(conn, name)
        metrics = [m.strip() for m in (metric or "tvl_usd").split(",") if m.strip()]
        data = store.history(conn, obj["id"], metrics, limit=limit, since=since)
        return {"object": {"slug": obj["slug"], "name": obj["name"]}, **data,
                "db": db_path()}
    finally:
        conn.close()


@app.get("/api/reports", summary="已登记报告")
def get_reports(
    name: str = Query(None, description="只看某个对象"),
    limit: int = Query(20),
):
    conn = get_conn()
    try:
        object_id = None
        if name:
            object_id = _obj_or_404(conn, name)["id"]
        return {"object": name, "reports": store.list_reports(conn, object_id, limit=limit),
                "db": db_path()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 报告正文：DB 里的 report_path 是相对「知识库根」的路径，读盘后渲染成 HTML
# ---------------------------------------------------------------------------
_ROOT = {"value": None}

def repo_root() -> str:
    """知识库根：$TOKEN_RESEARCH_ROOT > 向上找到 .git / CLAUDE.md 的目录。"""
    if _ROOT["value"] is None:
        env = os.environ.get("TOKEN_RESEARCH_ROOT")
        if env and os.path.isdir(env):
            _ROOT["value"] = os.path.abspath(env)
        else:
            cur = _HERE
            found = None
            for _ in range(6):
                cur = os.path.dirname(cur)
                if (os.path.isdir(os.path.join(cur, ".git"))
                        or os.path.isfile(os.path.join(cur, "CLAUDE.md"))):
                    found = cur
                    break
            _ROOT["value"] = found or os.path.abspath(os.path.join(_HERE, "..", ".."))
    return _ROOT["value"]

def _report_or_404(conn, report_id: int):
    row = conn.execute(
        "SELECT r.*, o.slug AS object_slug, o.name AS object_name"
        " FROM reports r JOIN objects o ON o.id = r.object_id WHERE r.id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="报告 id={} 不在库中".format(report_id))
    return row

def _resolve_report_file(rel_path: str):
    """相对知识库根解析，越界直接返回 None（不泄露服务器路径）。"""
    if not rel_path:
        return None
    root = os.path.realpath(repo_root())
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full

def _render_markdown(text: str):
    """python-markdown 存在就渲染，否则 None（前端降级成 <pre> 原文）。"""
    try:
        import markdown
    except ImportError:
        return None
    try:
        return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        return None

@app.get("/api/reports/{report_id}", summary="单份报告（含渲染后的正文）")
def get_report_detail(report_id: int):
    conn = get_conn()
    try:
        row = _report_or_404(conn, report_id)
        d = {
            "id": row["id"], "object_id": row["object_id"],
            "object_slug": row["object_slug"], "object_name": row["object_name"],
            "report_path": row["report_path"], "report_date": row["report_date"],
            "title": row["title"], "one_liner": row["one_liner"],
            "business_qual": row["business_qual"], "verdict": row["verdict"],
            "verdict_note": row["verdict_note"],
            "demystify": json.loads(row["demystify"]) if row["demystify"] else None,
            "snapshot_id": row["snapshot_id"], "created_at": row["created_at"],
        }
        full = _resolve_report_file(d["report_path"])
        raw = None
        if full:
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError):
                raw = None
        return {
            "report": d,
            "path_ok": full is not None,
            "raw": raw,
            "html": _render_markdown(raw) if raw else None,
            "root": repo_root(),
            "db": db_path(),
        }
    finally:
        conn.close()

@app.get("/api/reports/{report_id}/raw", summary="报告原文（text/markdown）")
def get_report_raw(report_id: int):
    from fastapi.responses import PlainTextResponse
    conn = get_conn()
    try:
        row = _report_or_404(conn, report_id)
        full = _resolve_report_file(row["report_path"])
        if full is None:
            raise HTTPException(status_code=404, detail="报告文件不在知识库根下：{}".format(
                row["report_path"]))
        try:
            with open(full, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=500, detail="读取失败：{}".format(e))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 静态前端（同源部署）：web/ 与 server/ 同级
# ---------------------------------------------------------------------------
_WEB = os.path.abspath(os.path.join(_HERE, "..", "web"))
if os.path.isdir(_WEB):
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")


def main(argv=None):
    p = argparse.ArgumentParser(description="token-research 快照库 API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8020)
    p.add_argument("--db", help="覆盖 DB 路径（默认按技能规则解析）")
    p.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = p.parse_args(argv)

    if args.db:
        os.environ["TOKEN_RESEARCH_DB"] = os.path.abspath(args.db)
    _DB_PATH["value"] = store.resolve_db_path()[0]

    path = _DB_PATH["value"]
    print("[token-research-api] db = {}".format(path))
    print("[token-research-api] exists = {}".format(os.path.isfile(path)))
    if store.is_skill_install_path(path):
        print("[warn] DB 落在 agent skills 目录，建议设 TOKEN_RESEARCH_DB 指向个人知识库")
    print("[token-research-api] http://{}:{}  (docs: /docs)".format(args.host, args.port))

    import uvicorn
    uvicorn.run("app:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
