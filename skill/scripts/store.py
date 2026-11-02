#!/usr/bin/env python3
"""
store.py — token-research 本地快照库（SQLite 持久化层，零网络依赖）。

设计要点
--------
1. 追加式时间序列：快照只追加，不覆盖历史；`--replace-day` 是唯一例外。
2. NULL ≠ 0：NULL 表示「该数据源明确不覆盖此对象」（如 Bittensor 无费用/稳定币），
   0 表示「源返回了零」。`missing` 数组是权威标注。
3. schema 演进纯加法：SQLite 3.32 没有 DROP COLUMN / STRICT / RETURNING，
   迁移只允许 ADD COLUMN，用 PRAGMA user_version 记版本，废弃列保留。
4. 时间语义：captured_at / created_at 用 UTC ISO（带 T/Z）；
   captured_day / report_date 用本地日期，避免早 7 点的刷新被归到前一天。
   所有时间戳由 Python 写入，不用 CURRENT_TIMESTAMP 默认值（格式不一致会导致 ORDER BY 出错）。

表
--
objects            研究对象（链 / 币 / 协议）主表，含 DeFiLlama 双体系翻译字段
aliases            用户输入别名 → 对象（权威解析表）
snapshots          一次抓取一行，宽表指标 + extra_json 逃生口
snapshot_protocols 生态 Top-N 明细（仅 --with-protocols 写入）
reports            Markdown 报告索引 + 一句话结论 + 叙事/实际判定

自测
----
python -c "import store; c = store.connect(':memory:'); store.ensure_schema(c); store.seed(c); print(store.list_objects(c))"
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 种子对象：5 个已研究过的对象。slug 是归一键，aliases 是用户可能的输入。
# defillama_protocols 为 JSON 数组字符串（protocols API 链名可能与 v2/chains 不同）。
# ---------------------------------------------------------------------------
SEED_OBJECTS = [
    {
        "slug": "near",
        "name": "NEAR",
        "kind": "chain",
        "defillama_chain": "Near",
        "defillama_protocols": ["Near"],
        "coingecko_slug": "near",
        "aliases": ["near"],
        "notes": "2026-10-15 首次调研：原生 DeFi 以流动性质押为主力。",
    },
    {
        "slug": "bittensor",
        "name": "Bittensor",
        "kind": "chain",
        "defillama_chain": "Bittensor",
        "defillama_protocols": ["Bittensor"],
        "coingecko_slug": "bittensor",
        "aliases": ["tao", "bittensor"],
        "notes": "DeFi 薄链：DeFiLlama 无费用/稳定币/DEX 数据，估值锚在 AI 子网叙事。",
    },
    {
        "slug": "xrpl",
        "name": "XRPL",
        "kind": "chain",
        "defillama_chain": "XRPL",
        "defillama_protocols": ["Ripple", "XRPL", "XRPL EVM"],
        "coingecko_slug": "ripple",
        "aliases": ["xrp", "xrpl", "ripple"],
        "notes": "大代币、薄 DeFi：市值 $82b 但链上 TVL 仅 $39m，价值载体在 RLUSD 稳定币与支付叙事。",
    },
    {
        "slug": "hyperliquid",
        "name": "Hyperliquid",
        "kind": "chain",
        "defillama_chain": "Hyperliquid L1",
        "defillama_protocols": ["Hyperliquid L1", "Hyperliquid"],
        "coingecko_slug": "hyperliquid",
        "aliases": ["hype", "hyperliquid"],
        "notes": "链上订单簿衍生品交易所，TVL 口径差异大，应看 OI/衍生品量。",
    },
    {
        "slug": "qnt",
        "name": "Quant",
        "kind": "coin",
        "defillama_chain": None,
        "defillama_protocols": None,
        "coingecko_slug": "quant-network",
        "aliases": ["qnt", "quant", "quant-network"],
        "notes": "企业级 B2B 软件订阅（Overledger），DeFiLlama 零收录。",
    },
]

# 快照指标列（与 snapshots 表一一对应，顺序即输出顺序）
METRIC_COLUMNS = [
    "tvl_usd",
    "tvl_change_1d", "tvl_change_7d", "tvl_change_30d",
    "stablecoin_usd", "stablecoin_change_7d",
    "fees_24h", "fees_7d", "fees_30d", "fees_1y", "fees_change_1d",
    "revenue_24h", "revenue_7d", "revenue_30d",
    "dex_volume_24h", "dex_volume_7d", "dex_volume_30d", "dex_change_1d",
    "active_addresses_24h", "inflows_24h",
    "price_usd", "price_change_24h",
    "market_cap_usd", "fdv_usd",
    "protocol_count", "protocol_tvl_usd", "cex_tvl_usd",
]

DDL = """
CREATE TABLE IF NOT EXISTS objects (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  slug                TEXT NOT NULL UNIQUE,
  name                TEXT NOT NULL,
  kind                TEXT NOT NULL CHECK (kind IN ('chain','coin','protocol')),
  defillama_chain     TEXT,
  defillama_protocols TEXT,
  coingecko_slug      TEXT,
  notes               TEXT,
  first_researched_at TEXT NOT NULL,
  last_researched_at  TEXT,
  last_snapshot_at    TEXT,
  last_report_at      TEXT,
  created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aliases (
  alias     TEXT PRIMARY KEY COLLATE NOCASE,
  object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_aliases_obj ON aliases(object_id);

CREATE TABLE IF NOT EXISTS snapshots (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id            INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  captured_at          TEXT NOT NULL,
  captured_day         TEXT NOT NULL,
  origin               TEXT NOT NULL DEFAULT 'api' CHECK (origin IN ('api','page','import')),
  source_status        TEXT NOT NULL DEFAULT '{}',
  missing              TEXT NOT NULL DEFAULT '[]',
  fingerprint          TEXT NOT NULL,
  tvl_usd              REAL,
  tvl_change_1d        REAL, tvl_change_7d REAL, tvl_change_30d REAL,
  stablecoin_usd       REAL, stablecoin_change_7d REAL,
  fees_24h             REAL, fees_7d REAL, fees_30d REAL, fees_1y REAL, fees_change_1d REAL,
  revenue_24h          REAL, revenue_7d REAL, revenue_30d REAL,
  dex_volume_24h       REAL, dex_volume_7d REAL, dex_volume_30d REAL, dex_change_1d REAL,
  active_addresses_24h INTEGER,
  inflows_24h          REAL,
  price_usd            REAL, price_change_24h REAL,
  market_cap_usd       REAL, fdv_usd REAL,
  protocol_count       INTEGER, protocol_tvl_usd REAL, cex_tvl_usd REAL,
  extra_json           TEXT NOT NULL DEFAULT '{}',
  raw_json             TEXT,
  created_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshots_obj_time ON snapshots(object_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_obj_day ON snapshots(object_id, captured_day);

CREATE TABLE IF NOT EXISTS snapshot_protocols (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
  rank         INTEGER NOT NULL,
  name         TEXT NOT NULL,
  category     TEXT,
  symbol       TEXT,
  tvl_usd      REAL,
  chains       TEXT,
  chains_count INTEGER,
  is_cex       INTEGER NOT NULL DEFAULT 0 CHECK (is_cex IN (0,1)),
  is_bridge    INTEGER NOT NULL DEFAULT 0 CHECK (is_bridge IN (0,1)),
  UNIQUE (snapshot_id, name)
);
CREATE INDEX IF NOT EXISTS idx_sp_snap_tvl ON snapshot_protocols(snapshot_id, tvl_usd DESC);

CREATE TABLE IF NOT EXISTS reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id     INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  report_path   TEXT NOT NULL,
  report_date   TEXT NOT NULL,
  title         TEXT,
  one_liner     TEXT NOT NULL,
  business_qual TEXT,
  verdict       TEXT NOT NULL CHECK (verdict IN ('real','narrative','mixed','unknown')),
  verdict_note  TEXT,
  demystify     TEXT,
  snapshot_id   INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL,
  UNIQUE (object_id, report_path)
);
CREATE INDEX IF NOT EXISTS idx_reports_obj_date ON reports(object_id, report_date DESC);

-- 代币持有结构（来源：Binance Web3 API）。链上「谁在拿筹码」的证据，
-- 回答祛魅 Q1「谁真的在用」—— DeFiLlama 完全没有这个维度。
CREATE TABLE IF NOT EXISTS token_holders (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id        INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
  object_id          INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  chain_id           TEXT NOT NULL,
  contract_address   TEXT NOT NULL,
  captured_at        TEXT NOT NULL,
  top10_holding_pct  REAL,
  smart_money_pct    REAL,
  kol_pct            REAL,
  insider_pct        REAL,
  sniper_pct         REAL,
  bundler_pct        REAL,
  dev_pct            REAL,
  pro_pct            REAL,
  fresh_wallet_pct   REAL,
  holders            INTEGER,
  bn_holder_count    INTEGER,
  bn_trader_count_7d INTEGER,
  raw_json           TEXT,
  UNIQUE (snapshot_id, contract_address)
);
CREATE INDEX IF NOT EXISTS idx_holders_obj_time ON token_holders(object_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS holder_entries (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  token_holder_id  INTEGER NOT NULL REFERENCES token_holders(id) ON DELETE CASCADE,
  rank             INTEGER NOT NULL,
  wallet_address   TEXT NOT NULL,
  holding_pct      REAL,
  hold_amount_usd  REAL,
  avg_buy_price    REAL,
  realized_pnl_usd REAL,
  funding_source   TEXT,
  funding_label    TEXT,
  UNIQUE (token_holder_id, wallet_address)
);
CREATE INDEX IF NOT EXISTS idx_entries_th_pct ON holder_entries(token_holder_id, holding_pct DESC);
"""


# ---------------------------------------------------------------------------
# 时间与路径
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """UTC ISO 秒精度，'2026-11-01T09:12:33+00:00'。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_today() -> str:
    """本地日期 'YYYY-MM-DD'（报告文件名/同日去重用本地日期）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _default_db_candidates():
    """候选默认路径（按优先级）。技能可能在多处被复制，DB 只应有一份。

    顺序：<skill根>/../token_research.db（知识库归档布局）
        → 沿父目录向上找已有的 token_research.db（任何副本共享同一份）
    """
    here = os.path.dirname(os.path.abspath(__file__))        # .../skill/scripts
    skill_root = os.path.dirname(here)                       # .../skill
    cands = [os.path.abspath(os.path.join(skill_root, "..", "token_research.db"))]
    cur = skill_root
    for _ in range(6):  # 向上最多 6 层，兜底
        cur = os.path.dirname(cur)
        if not cur or cur == os.path.dirname(cur):
            break
        cands.append(os.path.join(cur, "token_research.db"))
    return cands


def pointer_file() -> str:
    """用户级指针文件：记录 DB 真实位置，让所有技能副本共享同一个库。

    技能可能被复制到多处（知识库归档 / agent skills / .claude skills），
    各自向上推理会得到不同路径、各建一个库。指针文件是跨副本的唯一真相。
    """
    return os.path.join(os.path.expanduser("~"), ".token_research_db")


def read_pointer():
    p = pointer_file()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            val = f.read().strip()
        return val or None
    except OSError:
        return None


def write_pointer(db_path: str) -> bool:
    """把 DB 路径写进用户级指针文件。失败只返回 False，不抛。"""
    try:
        with open(pointer_file(), "w", encoding="utf-8") as f:
            f.write(os.path.abspath(db_path) + "\n")
        return True
    except OSError:
        return False


def resolve_db_path(cli_db=None):
    """DB 路径优先级: --db > TOKEN_RESEARCH_DB > ~/.token_research_db > 候选已存在者 > 首选候选。

    默认落在 skill 的**同级**目录（个人数据区），而不是 skill 内部 —— 技能目录要能干净复制分发。
    返回 (path, source)。source: cli / env:TOKEN_RESEARCH_DB / pointer / existing / default。
    """
    if cli_db:
        return os.path.abspath(cli_db), "cli"
    env = os.environ.get("TOKEN_RESEARCH_DB")
    if env:
        return os.path.abspath(env), "env:TOKEN_RESEARCH_DB"
    ptr = read_pointer()
    if ptr:
        return os.path.abspath(ptr), "pointer"
    cands = _default_db_candidates()
    for c in cands:
        if os.path.isfile(c):
            return c, "existing"
    return cands[0], "default"


def is_skill_install_path(path: str) -> bool:
    """运行版 skill 若没设环境变量，DB 会落到 agent 的 skills 目录 —— 那不是用户数据。"""
    p = path.replace("\\", "/").lower()
    return ".user_skills" in p or "/doubao/" in p or "appdata/local/doubao" in p


def connect(path: str) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA。path 可为 ':memory:'。"""
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            pass  # 只读介质/网络盘上 WAL 可能失败，退化为默认模式
    return conn


def db_meta(path: str) -> dict:
    """db path 命令用：路径、存在性、版本、大小。"""
    exists = os.path.isfile(path)
    out = {"db": path, "exists": exists, "user_version": None, "size_bytes": None}
    if exists:
        out["size_bytes"] = os.path.getsize(path)
        try:
            conn = connect(path)
            out["user_version"] = user_version(conn)
            conn.close()
        except sqlite3.DatabaseError as e:
            out["error"] = "cannot open: {}".format(e)
    return out


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def user_version(conn) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _migrations():
    """版本 n -> 版本 n+1 的函数表。只允许纯加法（ADD COLUMN）。"""
    return {}


def ensure_schema(conn) -> int:
    """建表 + 增量迁移，幂等。返回最终 user_version。"""
    conn.executescript(DDL)
    current = user_version(conn)
    migrations = _migrations()
    while current < SCHEMA_VERSION:
        step = migrations.get(current)
        if step is not None:
            step(conn)
        current += 1
        conn.execute("PRAGMA user_version = {}".format(current))
    conn.commit()
    return current


def seed(conn, force: bool = False) -> dict:
    """灌入 SEED_OBJECTS。已存在的 slug 跳过（force=True 时更新对象字段，不动快照）。"""
    ensure_schema(conn)
    now = utc_now_iso()
    created = updated = skipped = 0
    for spec in SEED_OBJECTS:
        args = (
            spec["name"],
            spec["kind"],
            spec.get("defillama_chain"),
            json.dumps(spec["defillama_protocols"], ensure_ascii=False)
            if spec.get("defillama_protocols") is not None else None,
            spec.get("coingecko_slug"),
            spec.get("notes"),
        )
        row = conn.execute("SELECT id FROM objects WHERE slug = ?", (spec["slug"],)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO objects (slug, name, kind, defillama_chain, defillama_protocols,"
                " coingecko_slug, notes, first_researched_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (spec["slug"],) + args + (now, now),
            )
            object_id = cur.lastrowid
            created += 1
        else:
            object_id = row["id"]
            if force:
                conn.execute(
                    "UPDATE objects SET name=?, kind=?, defillama_chain=?, defillama_protocols=?,"
                    " coingecko_slug=?, notes=? WHERE id=?",
                    args + (object_id,),
                )
                updated += 1
            else:
                skipped += 1
        for alias in spec.get("aliases", []):
            a = alias.strip().lower()
            if not a:
                continue
            owner = conn.execute("SELECT object_id FROM aliases WHERE alias = ?", (a,)).fetchone()
            if owner is not None and owner["object_id"] != object_id:
                continue  # 冲突即保留原绑定，不静默改绑
            if owner is None:
                conn.execute("INSERT INTO aliases (alias, object_id) VALUES (?,?)", (a, object_id))
    conn.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# objects / aliases
# ---------------------------------------------------------------------------
def _object_dict(conn, row) -> dict:
    d = {k: row[k] for k in row.keys()}
    d["defillama_protocols"] = json.loads(d["defillama_protocols"]) if d.get("defillama_protocols") else None
    d["aliases"] = [
        r["alias"] for r in conn.execute(
            "SELECT alias FROM aliases WHERE object_id = ? ORDER BY alias", (row["id"],)
        )
    ]
    return d


def get_object(conn, object_id: int):
    row = conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
    return _object_dict(conn, row) if row else None


def find_object(conn, name: str):
    """按 slug → alias 解析。返回对象 dict 或 None。"""
    if not name:
        return None
    key = name.strip().lower()
    row = conn.execute("SELECT * FROM objects WHERE slug = ?", (key,)).fetchone()
    if row is not None:
        return _object_dict(conn, row)
    row = conn.execute(
        "SELECT o.* FROM aliases a JOIN objects o ON o.id = a.object_id WHERE a.alias = ?", (key,)
    ).fetchone()
    if row is not None:
        return _object_dict(conn, row)
    return None


def suggest_names(conn, name: str, limit: int = 3):
    """未知对象时给出近似名（简单的 difflib 相似度，避免额外依赖）。"""
    import difflib

    key = (name or "").strip().lower()
    candidates = []
    for r in conn.execute("SELECT slug, name FROM objects"):
        candidates.extend([r["slug"], r["name"].lower(), r["name"]])
    for r in conn.execute("SELECT alias FROM aliases"):
        candidates.append(r["alias"])
    seen, uniq = set(), []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return difflib.get_close_matches(key, uniq, n=limit, cutoff=0.5)


def create_object(conn, slug, name, kind, defillama_chain=None, defillama_protocols=None,
                  coingecko_slug=None, aliases=None, notes=None) -> dict:
    slug = (slug or "").strip().lower()
    if not slug:
        raise ValueError("slug 不能为空")
    if kind not in ("chain", "coin", "protocol"):
        raise ValueError("kind 必须是 chain / coin / protocol")
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO objects (slug, name, kind, defillama_chain, defillama_protocols,"
        " coingecko_slug, notes, first_researched_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            slug, name, kind, defillama_chain,
            json.dumps(defillama_protocols, ensure_ascii=False) if defillama_protocols else None,
            coingecko_slug, notes, now, now,
        ),
    )
    object_id = conn.execute("SELECT id FROM objects WHERE slug = ?", (slug,)).fetchone()["id"]
    for alias in (aliases or []) + [slug]:
        a = (alias or "").strip().lower()
        if not a:
            continue
        owner = conn.execute("SELECT object_id FROM aliases WHERE alias = ?", (a,)).fetchone()
        if owner is not None and owner["object_id"] != object_id:
            continue
        if owner is None:
            conn.execute("INSERT INTO aliases (alias, object_id) VALUES (?,?)", (a, object_id))
    conn.commit()
    return get_object(conn, object_id)


def list_objects(conn, kind=None, sort="recent", stale_days=None, with_reports=False):
    """列出对象 + 最新快照摘要 + 报告数。"""
    where, params = "", []
    if kind:
        where = " WHERE kind = ?"
        params.append(kind)
    rows = conn.execute("SELECT * FROM objects" + where, params).fetchall()
    out = []
    now = datetime.now(timezone.utc)
    for row in rows:
        obj = _object_dict(conn, row)
        latest = latest_snapshot(conn, obj["id"])
        reports_n = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE object_id = ?", (obj["id"],)
        ).fetchone()["c"]
        snaps_n = conn.execute(
            "SELECT COUNT(*) c FROM snapshots WHERE object_id = ?", (obj["id"],)
        ).fetchone()["c"]
        item = {
            "slug": obj["slug"],
            "name": obj["name"],
            "kind": obj["kind"],
            "aliases": obj["aliases"],
            "first_researched_at": obj["first_researched_at"],
            "last_researched_at": obj["last_researched_at"],
            "snapshots": snaps_n,
            "reports": reports_n,
            "latest_snapshot": None,
            "latest_report": None,
        }
        if latest:
            days_ago = None
            try:
                ts = datetime.fromisoformat(latest["captured_at"])
                days_ago = round((now - ts).total_seconds() / 86400.0, 2)
            except (ValueError, TypeError):
                pass
            item["latest_snapshot"] = {
                "id": latest["id"],
                "captured_day": latest["captured_day"],
                "days_ago": days_ago,
                "origin": latest["origin"],
                "tvl_usd": latest["metrics"].get("tvl_usd"),
                "tvl_m": _fmt_m(latest["metrics"].get("tvl_usd")),
                "missing": latest["missing"],
            }
        if with_reports:
            rep = conn.execute(
                "SELECT * FROM reports WHERE object_id = ? ORDER BY report_date DESC, id DESC LIMIT 1",
                (obj["id"],),
            ).fetchone()
            if rep:
                item["latest_report"] = _report_dict(rep)
        out.append(item)

    if stale_days is not None:
        out = [
            o for o in out
            if o["latest_snapshot"] is None
            or o["latest_snapshot"]["days_ago"] is None
            or o["latest_snapshot"]["days_ago"] >= stale_days
        ]
    if sort == "name":
        out.sort(key=lambda o: o["slug"])
    elif sort == "tvl":
        out.sort(key=lambda o: -((o["latest_snapshot"] or {}).get("tvl_usd") or 0.0))
    else:  # recent
        out.sort(key=lambda o: -(o["latest_snapshot"]["id"] if o["latest_snapshot"] else -1))
    return out


def touch_object(conn, object_id: int, *, snapshot_at=None, report_at=None):
    obj = get_object(conn, object_id)
    if obj is None:
        return
    last_snap = obj.get("last_snapshot_at")
    last_rep = obj.get("last_report_at")
    if snapshot_at and (last_snap is None or snapshot_at > last_snap):
        last_snap = snapshot_at
    if report_at and (last_rep is None or report_at > last_rep):
        last_rep = report_at
    candidates = [x for x in (last_snap, last_rep) if x]
    last_any = max(candidates) if candidates else obj.get("last_researched_at")
    conn.execute(
        "UPDATE objects SET last_snapshot_at=?, last_report_at=?, last_researched_at=? WHERE id=?",
        (last_snap, last_rep, last_any, object_id),
    )


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------
def _fmt_m(v):
    return round(v / 1e6, 2) if isinstance(v, (int, float)) else None


def _canon(obj) -> str:
    """稳定序列化（键排序、None 显式保留），用于指纹。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(metrics: dict, extra: dict) -> str:
    payload = {
        "metrics": {k: metrics.get(k) for k in METRIC_COLUMNS},
        "extra": extra or {},
    }
    return hashlib.sha1(_canon(payload).encode("utf-8")).hexdigest()


def latest_snapshot(conn, object_id: int):
    row = conn.execute(
        "SELECT * FROM snapshots WHERE object_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (object_id,),
    ).fetchone()
    return _snapshot_dict(conn, row) if row else None


def previous_snapshot(conn, object_id: int, before_id: int):
    row = conn.execute(
        "SELECT * FROM snapshots WHERE object_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (object_id, before_id),
    ).fetchone()
    return _snapshot_dict(conn, row) if row else None


def recent_captured_at(conn, object_id: int, captured_day: str):
    return conn.execute(
        "SELECT * FROM snapshots WHERE object_id = ? AND captured_day = ?", (object_id, captured_day)
    ).fetchall()


def snapshot_by_ref(conn, object_id: int, ref):
    """ref 可为快照 id（int/数字串）或 'YYYY-MM-DD'（取当日最后一条）。"""
    if ref is None:
        return latest_snapshot(conn, object_id)
    s = str(ref).strip()
    if s.isdigit():
        row = conn.execute(
            "SELECT * FROM snapshots WHERE object_id = ? AND id = ?", (object_id, int(s))
        ).fetchone()
        return _snapshot_dict(conn, row) if row else None
    row = conn.execute(
        "SELECT * FROM snapshots WHERE object_id = ? AND captured_day = ?"
        " ORDER BY captured_at DESC, id DESC LIMIT 1",
        (object_id, s),
    ).fetchone()
    return _snapshot_dict(conn, row) if row else None


def _snapshot_dict(conn, row) -> dict:
    if row is None:
        return None
    metrics = {k: row[k] for k in METRIC_COLUMNS}
    metrics_m = {k: _fmt_m(v) for k, v in metrics.items() if k.endswith("_usd")}
    return {
        "id": row["id"],
        "object_id": row["object_id"],
        "captured_at": row["captured_at"],
        "captured_day": row["captured_day"],
        "origin": row["origin"],
        "source_status": json.loads(row["source_status"] or "{}"),
        "missing": json.loads(row["missing"] or "[]"),
        "fingerprint": row["fingerprint"],
        "metrics": metrics,
        "metrics_m": metrics_m,
        "extra": json.loads(row["extra_json"] or "{}"),
        "created_at": row["created_at"],
    }


def insert_snapshot(conn, object_id, metrics, *, origin="api", source_status=None,
                    missing=None, protocols=None, extra=None, raw=None, captured_at=None,
                    holders=None):
    """单事务插入快照（+ 生态明细）并更新 objects 时间戳。

    调用方负责判定是否跳过（同秒 / 指纹相同）；本函数直接插入。
    返回 {"inserted": bool, "snapshot": dict, "reason": str|None}。
    """
    captured_at = captured_at or utc_now_iso()
    captured_day = captured_at[:10]  # 调用方保证 captured_at 为 ISO（UTC）
    extra = extra or {}
    fp = fingerprint(metrics, extra)
    now = utc_now_iso()

    cols = ["object_id", "captured_at", "captured_day", "origin", "source_status",
            "missing", "fingerprint"] + METRIC_COLUMNS + ["extra_json", "raw_json", "created_at"]
    values = [
        object_id, captured_at, captured_day, origin,
        json.dumps(source_status or {}, ensure_ascii=False),
        json.dumps(missing or [], ensure_ascii=False),
        fp,
    ] + [metrics.get(k) for k in METRIC_COLUMNS] + [
        json.dumps(extra, ensure_ascii=False),
        json.dumps(raw, ensure_ascii=False) if raw is not None else None,
        now,
    ]

    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO snapshots ({}) VALUES ({})".format(
                    ", ".join(cols), ", ".join("?" for _ in cols)
                ),
                values,
            )
            snap_id = cur.lastrowid
            if protocols:
                _insert_protocols(conn, snap_id, protocols)
            if holders:
                _insert_holders(conn, snap_id, object_id, holders, captured_at)
            touch_object(conn, object_id, snapshot_at=captured_at)
        reason = None
    except sqlite3.IntegrityError as e:
        if "uq_snapshots_obj_time" in str(e) or "UNIQUE" in str(e).upper():
            existing = conn.execute(
                "SELECT * FROM snapshots WHERE object_id = ? AND captured_at = ?",
                (object_id, captured_at),
            ).fetchone()
            return {"inserted": False, "snapshot": _snapshot_dict(conn, existing),
                    "reason": "same_second"}
        raise

    row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
    return {"inserted": True, "snapshot": _snapshot_dict(conn, row), "reason": reason}


def _insert_protocols(conn, snapshot_id: int, protocols):
    for i, p in enumerate(protocols, start=1):
        chains = p.get("chains") or []
        cat = (p.get("category") or "")
        conn.execute(
            "INSERT OR REPLACE INTO snapshot_protocols"
            " (snapshot_id, rank, name, category, symbol, tvl_usd, chains, chains_count,"
            "  is_cex, is_bridge) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id, p.get("rank", i), p.get("name"), p.get("category"), p.get("symbol"),
                p.get("tvl_usd"),
                json.dumps(chains, ensure_ascii=False) if chains else None,
                len(chains) if chains else None,
                1 if cat == "CEX" else 0,
                1 if cat == "Bridge" else 0,
            ),
        )


def _insert_holders(conn, snapshot_id, object_id, holders, captured_at):
    """holders: {"chain_id","contract_address","distribution":{...},"entries":[{...}], "raw":{}}"""
    dist = holders.get("distribution") or {}
    cur = conn.execute(
        "INSERT OR REPLACE INTO token_holders"
        " (snapshot_id, object_id, chain_id, contract_address, captured_at,"
        "  top10_holding_pct, smart_money_pct, kol_pct, insider_pct, sniper_pct,"
        "  bundler_pct, dev_pct, pro_pct, fresh_wallet_pct, holders,"
        "  bn_holder_count, bn_trader_count_7d, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id, object_id, holders.get("chain_id"), holders.get("contract_address"),
            captured_at,
            dist.get("top10HoldingPercent"), dist.get("smartMoneyHoldingPercent"),
            dist.get("kolHoldingPercent"), dist.get("insiderHoldingPercent"),
            dist.get("sniperHoldingPercent"), dist.get("bundlerHoldingPercent"),
            dist.get("devHoldingPercent"), dist.get("proHoldingPercent"),
            dist.get("freshWalletHoldingPercent"), dist.get("holders"),
            dist.get("bnHolderCount"), dist.get("bnTraderCount7D"),
            json.dumps(holders.get("raw"), ensure_ascii=False) if holders.get("raw") else None,
        ),
    )
    th_id = cur.lastrowid
    for i, e in enumerate(holders.get("entries") or [], start=1):
        lbl = e.get("fundingSourceLabel")
        conn.execute(
            "INSERT OR REPLACE INTO holder_entries"
            " (token_holder_id, rank, wallet_address, holding_pct, hold_amount_usd,"
            "  avg_buy_price, realized_pnl_usd, funding_source, funding_label)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                th_id, e.get("rank", i), e.get("wallet_address"), e.get("holding_pct"),
                e.get("hold_amount_usd"), e.get("avg_buy_price"), e.get("realized_pnl_usd"),
                e.get("funding_source"),
                (lbl.get("tagValue") if isinstance(lbl, dict) else lbl),
            ),
        )


def holders_of(conn, snapshot_id: int):
    """返回该快照下的持币结构（含 Top 大户明细），供 show / history 输出。"""
    row = conn.execute(
        "SELECT * FROM token_holders WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return None
    entries = [
        {
            "rank": e["rank"], "wallet_address": e["wallet_address"],
            "holding_pct": e["holding_pct"], "hold_amount_usd": e["hold_amount_usd"],
            "avg_buy_price": e["avg_buy_price"], "realized_pnl_usd": e["realized_pnl_usd"],
            "funding_source": e["funding_source"], "funding_label": e["funding_label"],
        }
        for e in conn.execute(
            "SELECT * FROM holder_entries WHERE token_holder_id = ? ORDER BY rank",
            (row["id"],))
    ]
    return {
        "chain_id": row["chain_id"],
        "contract_address": row["contract_address"],
        "captured_at": row["captured_at"],
        "distribution": {
            "top10_holding_pct": row["top10_holding_pct"],
            "smart_money_pct": row["smart_money_pct"],
            "kol_pct": row["kol_pct"],
            "insider_pct": row["insider_pct"],
            "sniper_pct": row["sniper_pct"],
            "bundler_pct": row["bundler_pct"],
            "dev_pct": row["dev_pct"],
            "pro_pct": row["pro_pct"],
            "fresh_wallet_pct": row["fresh_wallet_pct"],
            "holders": row["holders"],
            "bn_holder_count": row["bn_holder_count"],
            "bn_trader_count_7d": row["bn_trader_count_7d"],
        },
        "top_holders": entries,
    }


def protocols_of(conn, snapshot_id: int):
    rows = conn.execute(
        "SELECT * FROM snapshot_protocols WHERE snapshot_id = ? ORDER BY rank", (snapshot_id,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "rank": r["rank"], "name": r["name"], "category": r["category"],
            "symbol": r["symbol"], "tvl_usd": r["tvl_usd"], "tvl_m": _fmt_m(r["tvl_usd"]),
            "chains": json.loads(r["chains"]) if r["chains"] else [],
            "chains_count": r["chains_count"],
            "is_cex": bool(r["is_cex"]), "is_bridge": bool(r["is_bridge"]),
        })
    return out


def delete_day(conn, object_id: int, captured_day: str) -> int:
    """--replace-day：删除该对象当天所有快照（级联删明细）。返回删除条数。"""
    with conn:
        cur = conn.execute(
            "DELETE FROM snapshots WHERE object_id = ? AND captured_day = ?",
            (object_id, captured_day),
        )
        return cur.rowcount


def history(conn, object_id: int, metrics=None, limit=20, since=None):
    """返回 {points, series}：points 为逐行明细，series 为按指标成列（可直接喂图表）。"""
    metrics = metrics or ["tvl_usd"]
    metrics = [m for m in metrics if m in METRIC_COLUMNS]
    if not metrics:
        metrics = ["tvl_usd"]
    sql = "SELECT * FROM snapshots WHERE object_id = ?"
    params = [object_id]
    if since:
        sql += " AND captured_day >= ?"
        params.append(since)
    sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    points = []
    for row in reversed(rows):  # 按时间正序输出
        snap = _snapshot_dict(conn, row)
        point = {
            "id": snap["id"],
            "captured_at": snap["captured_at"],
            "captured_day": snap["captured_day"],
            "origin": snap["origin"],
            "missing": snap["missing"],
        }
        for m in metrics:
            point[m] = snap["metrics"].get(m)
        points.append(point)
    series = {m: [[p["captured_day"], p.get(m)] for p in points] for m in metrics}
    return {"metrics": metrics, "count": len(points), "points": points, "series": series}


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def _report_dict(row) -> dict:
    return {
        "id": row["id"],
        "object_id": row["object_id"],
        "report_path": row["report_path"],
        "report_date": row["report_date"],
        "title": row["title"],
        "one_liner": row["one_liner"],
        "business_qual": row["business_qual"],
        "verdict": row["verdict"],
        "verdict_note": row["verdict_note"],
        "demystify": json.loads(row["demystify"]) if row["demystify"] else None,
        "snapshot_id": row["snapshot_id"],
        "created_at": row["created_at"],
    }


def upsert_report(conn, object_id, *, report_path, report_date=None, one_liner,
                  verdict, title=None, business_qual=None, verdict_note=None,
                  demystify=None, snapshot_id=None) -> dict:
    """按 (object_id, report_path) upsert。返回 {"report":…, "upserted": bool}。"""
    if verdict not in ("real", "narrative", "mixed", "unknown"):
        raise ValueError("verdict 必须是 real / narrative / mixed / unknown")
    report_date = report_date or local_today()
    now = utc_now_iso()
    existing = conn.execute(
        "SELECT id FROM reports WHERE object_id = ? AND report_path = ?",
        (object_id, report_path),
    ).fetchone()
    payload = (
        report_date, title, one_liner, business_qual, verdict, verdict_note,
        json.dumps(demystify, ensure_ascii=False) if demystify is not None else None,
        snapshot_id,
    )
    try:
        with conn:
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO reports (object_id, report_path, report_date, title, one_liner,"
                    " business_qual, verdict, verdict_note, demystify, snapshot_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (object_id, report_path) + payload + (now,),
                )
                report_id, upserted = cur.lastrowid, False
            else:
                report_id, upserted = existing["id"], True
                conn.execute(
                    "UPDATE reports SET report_date=?, title=?, one_liner=?, business_qual=?,"
                    " verdict=?, verdict_note=?, demystify=?, snapshot_id=? WHERE id=?",
                    payload + (report_id,),
                )
            touch_object(conn, object_id, report_at=now)
    except sqlite3.IntegrityError as e:
        raise ValueError("报告登记失败：{}".format(e))
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return {"report": _report_dict(row), "upserted": upserted}


def list_reports(conn, object_id=None, limit=20):
    sql = ("SELECT r.*, o.slug AS object_slug, o.name AS object_name"
           " FROM reports r JOIN objects o ON o.id = r.object_id")
    params = []
    if object_id is not None:
        sql += " WHERE r.object_id = ?"
        params.append(object_id)
    sql += " ORDER BY r.report_date DESC, r.id DESC LIMIT ?"
    params.append(int(limit))
    out = []
    for row in conn.execute(sql, params):
        d = _report_dict(row)
        d["object_slug"] = row["object_slug"]
        d["object_name"] = row["object_name"]
        out.append(d)
    return out
