#!/usr/bin/env python3
"""
defillama.py — DeFiLlama 链上数据拉取 + 本地快照库。

子命令
------
实时拉取（不入库）
  chain <name>        链级 TVL + 24h/7d/30d 变化
  protocols <chain>   该链协议排名（按 TVL 降序），默认剔除 CEX 噪声
  top <chain>         同 protocols
  compare <c1> <c2>   两条链核心指标对比

快照库
  init [--seed]                       建表 + 灌入 NEAR/Bittensor/XRPL/Hyperliquid/QNT
  db path                             打印 DB 路径解析结果与库信息
  snapshot <name>                     抓全套指标入库，并打印与上一条的 diff
  list                                已研究过的对象 + 最新快照摘要
  show <name> [--at ref]              某对象的快照详情（默认最新）
  history <name> [--metric a,b]       时间序列（points + 可直接喂图表的 series）

报告索引
  report add <name> ...               登记 Markdown 报告与一句话结论
  report list [<name>]                列出已登记报告

回填
  import <file.json> [--dry-run]      从 JSON 批量回填对象/快照/报告（幂等）

所有命令输出 JSON 到 stdout；链名大小写不敏感（near / NEAR / tao / xrp / hype）。

数据源: api.llama.fi / stablecoins.llama.fi / coins.llama.fi（公开，无需鉴权）

口径警告:
  桥/聚合器（如 Portal）TVL 为全链聚合，会污染单链榜单；
  CEX（Binance/Gate/Bitfinex 等）因支持该链充提也会被计入，
  默认按 category != 'CEX' 过滤。判断生态厚度务必看排除 CEX 后的榜。
  NULL 表示该源不覆盖此对象（如 Bittensor 无费用/稳定币数据），0 表示源返回了零 —— 两者不可混。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

API = "https://api.llama.fi"
STABLE_API = "https://stablecoins.llama.fi"
COINS_API = "https://coins.llama.fi"
COINGECKO_API = "https://api.coingecko.com"
USER_AGENT = "Mozilla/5.0 (token-research)"

CACHE_TTL_HOURS = float(os.environ.get("TOKEN_RESEARCH_CACHE_TTL_HOURS", "6"))


# Windows 控制台默认 GBK，中文与 emoji 会变成乱码或抛 UnicodeEncodeError。
# 进程内统一成 UTF-8（3.7+ 才有 reconfigure），失败就退回原有 _emit 降级逻辑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# 输出（GBK 控制台安全）
# ---------------------------------------------------------------------------
def _emit(obj):
    """打印 JSON。GBK 控制台遇中文时先试 UTF-8 字节流，再降级 ASCII 转义，不崩。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    try:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return
    except Exception:
        print(json.dumps(obj, ensure_ascii=True, indent=2))


def _emit_err(msg):
    line = "[error] {}\n".format(msg)
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except UnicodeEncodeError:
        try:
            sys.stderr.buffer.write(line.encode("utf-8"))
            sys.stderr.buffer.flush()
        except Exception:
            sys.stderr.write("[error] {}\n".format(
                msg.encode("ascii", "backslashreplace").decode()))


def _die(msg, code=1):
    _emit_err(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 网络层
# ---------------------------------------------------------------------------
# Windows 开了系统代理（v2rayN/clash 等）时，urllib 会从注册表读取 ProxyServer
# 并自动套用，而且会把 https 代理写成 'https://127.0.0.1:port'，导致境外站点
# TLS 握手直接 SSLEOFError。
# 本脚本默认显式禁用系统代理（DeFiLlama 各端点直连可用，更快也更稳）。
# 需要访问境外源时显式设环境变量，例如：
#   TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808   (v2rayN 的 HTTP 代理口)
# 注意端口方向：10808 通常才是可用的 HTTP 代理，10809 实测对境外不通。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener():
    proxy = os.environ.get("TOKEN_RESEARCH_PROXY")
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return _NO_PROXY_OPENER


def _try_get(url, timeout=60):
    """返回 (data, status)。任何失败都返回 (None, status)，不抛。

    读取阶段的 socket.timeout 不是 URLError 的子类（连接阶段超时才是），
    不显式捕获会直接崩掉整次快照 —— 协议级 /protocol 端点响应常达数十秒，
    这条路径必须兜住。
    """
    if not url.startswith("http"):
        url = API + url
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw), "ok"
    except urllib.error.HTTPError as e:
        return None, "missing:http{}".format(e.code)
    except urllib.error.URLError as e:
        return None, "error:{}".format(getattr(e, "reason", e))
    except (socket.timeout, TimeoutError) as e:
        return None, "error:timeout:{}".format(e)
    except (ValueError, UnicodeDecodeError) as e:
        return None, "error:badjson:{}".format(e)
    except (OSError, http.client.HTTPException) as e:
        return None, "error:{}:{}".format(type(e).__name__, e)


def _get(path, timeout=60):
    """旧的严格拉取（老命令用）。失败即抛，保持原有行为。"""
    data, status = _try_get(path, timeout)
    if data is None:
        raise RuntimeError("fetch failed: {} ({})".format(path, status))
    return data


def _num(v):
    """脏数据清洗：null / false / '' / '-' / NaN → None；不做 float() 硬转。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if v != v else v  # NaN
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s or s in ("-", "null", "N/A", "n/a"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# v2/chains 链名 → protocols API 链名 的别名映射（两套 API 命名不一致）
_CHAIN_ALIASES = {
    "xrp": ("XRPL", ["Ripple", "XRPL", "XRPL EVM"]),
    "xrpl": ("XRPL", ["Ripple", "XRPL", "XRPL EVM"]),
    "ripple": ("XRPL", ["Ripple", "XRPL", "XRPL EVM"]),
    "tao": ("Bittensor", ["Bittensor"]),
    "hype": ("Hyperliquid L1", ["Hyperliquid L1", "Hyperliquid"]),
    "hyperliquid": ("Hyperliquid L1", ["Hyperliquid L1", "Hyperliquid"]),
}


def _norm(name):
    """返回 (v2/chains 标准链名, protocols 匹配链名列表)。"""
    n = name.strip().lower()
    if n in _CHAIN_ALIASES:
        return _CHAIN_ALIASES[n]
    return name.strip(), [name.strip()]


def _fmt_m(v):
    return round(v / 1e6, 2) if isinstance(v, (int, float)) else None


# 需要额外输出 *_m（百万美元）口径的指标。注意：price_usd 是单价，不能按百万折算。
MILLION_METRICS = ("tvl_usd", "stablecoin_usd", "market_cap_usd", "fdv_usd",
                   "protocol_tvl_usd", "cex_tvl_usd")


def _metrics_m(metrics):
    return {k: _fmt_m(metrics.get(k)) for k in MILLION_METRICS if k in metrics}


# ---------------------------------------------------------------------------
# protocols 本地缓存（~9MB，别反复拉）
# ---------------------------------------------------------------------------
def _cache_dir(db_path):
    if os.environ.get("TOKEN_RESEARCH_CACHE_DIR"):
        return os.environ["TOKEN_RESEARCH_CACHE_DIR"]
    if db_path in (None, ":memory:"):
        return None
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), ".cache")


def fetch_protocols_data(db_path=None, no_cache=False, refresh_cache=False,
                         accept_stale=False):
    """返回 (list, cache_info)。缓存失败不影响主流程。

    accept_stale=True 时过期缓存也照用（不过期才回源）。symbol→slug 映射这类
    「元数据查询」用它：榜单细节会变，但协议 slug 极少变，为了确认一个 slug
    重拉 9MB 不划算。
    """
    cdir = _cache_dir(db_path)
    cache_file = os.path.join(cdir, "protocols.json") if cdir else None
    now = time.time()

    if cache_file and not no_cache and not refresh_cache and os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                payload = json.load(f)
            age = now - float(payload.get("fetched_at", 0))
            fresh = age < CACHE_TTL_HOURS * 3600
            if (fresh or accept_stale) and payload.get("data"):
                return payload["data"], {"hit": True, "age_s": round(age),
                                         "ttl_h": CACHE_TTL_HOURS, "stale": not fresh}
        except (OSError, ValueError):
            pass  # 缓存损坏 → 重拉

    data, status = _try_get("/protocols", timeout=180)
    if data is None:
        _die("protocols API 拉取失败 ({})".format(status))

    info = {"hit": False, "age_s": 0, "ttl_h": CACHE_TTL_HOURS}
    if cache_file:
        try:
            os.makedirs(cdir, exist_ok=True)
            tmp = cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now, "count": len(data),
                           "source": "api.llama.fi/protocols", "data": data},
                          f, ensure_ascii=False)
            os.replace(tmp, cache_file)  # 原子写
        except OSError as e:
            _emit_err("protocols 缓存写入失败（不影响本次结果）: {}".format(e))
    return data, info


def pull_protocols(chain_name, proto_names=None, keep_cex=False, limit=15,
                   db_path=None, no_cache=False, refresh_cache=False):
    """协议排名。返回 (out_dict, protocols_rows_for_db)。"""
    prots, cache_info = fetch_protocols_data(db_path, no_cache, refresh_cache)
    names = [c.lower() for c in (proto_names or [chain_name])]
    all_rows = [p for p in prots
                if any(n in [c.lower() for c in (p.get("chains") or [])] for n in names)]
    rows = all_rows if keep_cex else [p for p in all_rows if (p.get("category") or "") != "CEX"]
    rows.sort(key=lambda p: _num(p.get("tvl")) or 0.0, reverse=True)
    top = rows[:limit]

    out = {
        "chain": chain_name,
        "matched_protocol_names": names,
        "total_protocols": len(rows),
        "cex_excluded": not keep_cex,
        "protocols": [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "tvl_m": _fmt_m(_num(p.get("tvl"))),
                "tvl_usd": _num(p.get("tvl")),
                "symbol": p.get("symbol"),
            }
            for p in top
        ],
        "source": "api.llama.fi/protocols",
        "cache": cache_info,
        "fetched_at": store.utc_now_iso(),
    }
    db_rows = [
        {
            "rank": i,
            "name": p.get("name"),
            "category": p.get("category"),
            "symbol": p.get("symbol"),
            "tvl_usd": _num(p.get("tvl")),
            "chains": p.get("chains") or [],
        }
        for i, p in enumerate(top, start=1)
    ]
    agg = {
        "protocol_count": len(rows),
        "protocol_tvl_usd": sum((_num(p.get("tvl")) or 0.0) for p in rows) or None,
        "cex_tvl_usd": sum((_num(p.get("tvl")) or 0.0) for p in all_rows
                           if (p.get("category") or "") == "CEX") or None,
    }
    return out, db_rows, agg


# ---------------------------------------------------------------------------
# 指标抓取（每个源独立降级）
# ---------------------------------------------------------------------------
def _pct_change(series, current):
    """series: [(date_ts:int, value:float)] 升序。按天数回溯算涨跌幅。"""
    if current is None or not series:
        return None
    out = {}
    for days in (1, 7, 30):
        if len(series) > days:
            prev = series[-1 - days][1]
            out[days] = round((current - prev) / prev * 100, 2) if prev else None
        else:
            out[days] = None
    return out


def grab_chain_metrics(chain_name):
    """链级 TVL + 变化。

    注意：v2/chains 已不再返回 change_1d/7d/30d（只剩 tvl），
    涨跌幅改由 v2/historicalChainTvl 自算，否则全为 null。
    """
    chains, status = _try_get("/v2/chains")
    if chains is None:
        return {}, status
    hit = next((c for c in chains if (c.get("name") or "").lower() == chain_name.lower()), None)
    if hit is None:
        return {}, "missing:not_in_v2_chains"
    tvl = _num(hit.get("tvl"))

    metrics = {"tvl_usd": tvl}
    hist, hstatus = _try_get(
        "/v2/historicalChainTvl/{}".format(urllib.request.quote(str(chain_name), safe="")))
    if isinstance(hist, list) and hist:
        series = [(_num(p.get("date")), _num(p.get("tvl"))) for p in hist if _num(p.get("tvl")) is not None]
        chg = _pct_change(series, tvl) or {}
        metrics["tvl_change_1d"] = chg.get(1)
        metrics["tvl_change_7d"] = chg.get(7)
        metrics["tvl_change_30d"] = chg.get(30)
    else:
        metrics["tvl_change_1d"] = None
        metrics["tvl_change_7d"] = None
        metrics["tvl_change_30d"] = None
    return metrics, "ok" if hstatus == "ok" or tvl is not None else hstatus


def check_series_quality(values, label=""):
    """日序列分布检验，返回 (quality_dict, warn_list)。

    DeFiLlama 的 adapter 偶有采集缺陷：Ethena 费用近 365 天中位数 $841/天、
    均值 $652,484/天，差 776 倍 —— 均值被少数尖峰日主导，直接拿 total1y 当
    「年收入」会把结论放大三个数量级。这里只做判断，不改写上游数字。
    """
    vals = [v for v in values if isinstance(v, (int, float))]
    n = len(vals)
    if n < 30:
        return None, []

    s = sorted(vals)
    total = sum(vals)
    mean = total / n
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    zeros = sum(1 for v in vals if v == 0)
    q = {"n_days": n, "sum": total, "mean": mean, "median": median}
    warns = []

    if median == 0 and mean > 0:
        q["mean_over_median"] = None
        q["verdict"] = "suspect:median_zero"
        warns.append("{} 近{}天中位数为 0 但均值 {:.0f}，序列极可能是尖峰/间断采集".format(
            label, n, mean))
        return q, warns

    if median > 0:
        ratio = mean / median
        q["mean_over_median"] = round(ratio, 1)
        if ratio >= 20:
            q["verdict"] = "suspect:spiky"
            # 前 5% 的日子贡献了多少总量 —— 判断是否「靠几天撑起来」
            top5 = sum(s[int(n * 0.95):])
            share = round(top5 / total * 100, 1) if total else None
            q["top5pct_day_share_of_total"] = share
            warns.append(
                "{} 数据质量存疑：近{}天均值 {:.0f} / 中位数 {:.0f}（{:.0f} 倍）{}，"
                "勿直接用累计值下结论".format(
                    label, n, mean, median, ratio,
                    "，前5%的日子贡献 {:.1f}% 总量".format(share) if share is not None else ""))
        else:
            q["verdict"] = "ok"

    zero_share = round(zeros / n * 100, 1)
    q["zero_days"] = zeros
    q["zero_share_pct"] = zero_share
    if zero_share >= 30:
        q.setdefault("verdict", "suspect:sparse")
        warns.append("{} 近{}天有 {:.1f}% 的日子为 0，adapter 覆盖可能不全".format(
            label, n, zero_share))

    return q, warns


def grab_fees(chain_name, data_type=None, with_series=False):
    """链费用（默认）或收入（dataType=dailyRevenue）。

    with_series=True 时额外返回 totalDataChart 的日值列表，供质量检验用。
    """
    path = "/summary/fees/{}".format(urllib.request.quote(str(chain_name), safe=""))
    if data_type:
        path += "?dataType={}".format(data_type)
    d, status = _try_get(path, timeout=60)
    if d is None:
        return {}, status
    out = {
        "24h": _num(d.get("total24h")),
        "7d": _num(d.get("total7d")),
        "30d": _num(d.get("total30d")),
        "1y": _num(d.get("total1y")),
        "change_1d": _num(d.get("change_1d")),
    }
    if with_series:
        # totalDataChart: [[unix_ts, value], ...]，只保留近 365 天
        chart = d.get("totalDataChart") or []
        out["_series"] = [v for _, v in chart[-365:] if isinstance(v, (int, float)) or v is None]
    return out, "ok"


def grab_dexs(chain_name):
    path = ("/overview/dexs/{}?excludeTotalDataChart=true"
            "&excludeTotalDataChartBreakdown=true").format(
        urllib.request.quote(str(chain_name), safe=""))
    d, status = _try_get(path, timeout=60)
    if d is None:
        return {}, status
    return {
        "24h": _num(d.get("total24h")),
        "7d": _num(d.get("total7d")),
        "30d": _num(d.get("total30d")),
        "change_1d": _num(d.get("change_1d")),
    }, "ok"


def grab_stablecoins(chain_name):
    path = "{}/stablecoincharts/{}".format(
        STABLE_API, urllib.request.quote(str(chain_name), safe=""))
    arr, status = _try_get(path, timeout=60)
    if not isinstance(arr, list) or not arr:
        return {}, status if status != "ok" else "missing:empty"
    last = arr[-1]
    cur = _num(((last.get("totalCirculatingUSD") or {}).get("peggedUSD")))
    chg7 = None
    if len(arr) >= 8:
        prev = _num(((arr[-8].get("totalCirculatingUSD") or {}).get("peggedUSD")))
        if prev:
            chg7 = round((cur - prev) / prev * 100, 2) if cur is not None else None
    if cur is None:
        return {}, "missing:no_peggedUSD"
    return {"usd": cur, "change_7d": chg7}, "ok"


def grab_price(cg_slug):
    if not cg_slug:
        return {}, "missing:no_coingecko_slug"
    path = "{}/prices/current/coingecko:{}".format(
        COINS_API, urllib.request.quote(str(cg_slug), safe=""))
    d, status = _try_get(path, timeout=30)
    if d is None:
        return {}, status
    coin = (d.get("coins") or {}).get("coingecko:{}".format(cg_slug))
    if not coin:
        return {}, "missing:not_priced"
    return {
        "price_usd": _num(coin.get("price")),
        "confidence": _num(coin.get("confidence")),
        "symbol": coin.get("symbol"),
    }, "ok"


def grab_market_data(cg_slug):
    """市值 / FDV / 24h 涨跌（CoinGecko markets 端点）。

    DeFiLlama 全系端点都不返回市值 —— coins.llama.fi 只有价格，/protocol/<slug>
    只对协议有 mcap。所以链/币类对象的市值只能走这里。

    本机实测 CoinGecko 直连会被重置（WinError 10054），必须设
    TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808。抓不到时如实返回 error 状态，
    由调用方决定是否用 DeFiLlama 的 mcap 兜底。
    """
    if not cg_slug:
        return {}, "missing:no_coingecko_slug"
    url = "{}/api/v3/coins/markets?vs_currency=usd&ids={}".format(
        COINGECKO_API, urllib.request.quote(str(cg_slug), safe=""))
    data, status = _try_get(url, timeout=30)
    if not isinstance(data, list) or not data:
        return {}, status if status != "ok" else "missing:not_in_coingecko_markets"
    c = data[0]
    mcap = _num(c.get("market_cap"))
    fdv = _num(c.get("fully_diluted_valuation"))
    return {
        "market_cap_usd": mcap,
        "fdv_usd": fdv,
        "price_change_24h": _num(c.get("price_change_percentage_24h")),
        # 价格以 coins.llama.fi 为准（与 TVL 同源），这里只在主价格源缺失时补位
        "price_usd": _num(c.get("current_price")),
        "circulating_supply": _num(c.get("circulating_supply")),
        "total_supply": _num(c.get("total_supply")),
    }, ("ok" if mcap is not None else "missing:no_market_cap")


def grab_protocol_metrics(slug):
    """协议级 TVL + 变化 + 代币元信息（api.llama.fi/protocol/<slug>）。

    与链级端点不同，这里一次返回完整 TVL 历史序列和 currentChainTvls 分解，
    涨跌幅自算（同 grab_chain_metrics，不读 API 的 change_* 字段）。
    """
    data, status = _try_get(
        "/protocol/{}".format(urllib.request.quote(str(slug), safe="")), timeout=45)
    if not isinstance(data, dict):
        return {}, {}, status

    series = [(_num(p.get("date")), _num(p.get("totalLiquidityUSD")))
              for p in (data.get("tvl") or [])]
    series = [(d, v) for d, v in series if d is not None and v is not None]
    tvl = series[-1][1] if series else None
    chg = _pct_change(series, tvl) or {}

    metrics = {
        "tvl_usd": tvl,
        "tvl_change_1d": chg.get(1),
        "tvl_change_7d": chg.get(7),
        "tvl_change_30d": chg.get(30),
    }
    extra_meta = {
        "protocol_slug": slug,
        "protocol_name": data.get("name"),
        "token_symbol": data.get("symbol"),
        "gecko_id": data.get("gecko_id"),
        "category": data.get("category"),
        "chains": data.get("chains") or [],
        "is_parent_protocol": data.get("isParentProtocol"),
    }
    # TVL 按链分解。currentChainTvls 含 "staking"/"pool2" 等口径后缀键，
    # 与真实链名混在一起时容易误读，原样保留并在 SKILL 里提示拆口径。
    ctv = data.get("currentChainTvls")
    if isinstance(ctv, dict) and ctv:
        extra_meta["tvl_by_chain"] = ctv
    if data.get("mcap") is not None:
        extra_meta["mcap_from_defillama"] = _num(data.get("mcap"))
    return metrics, extra_meta, "ok"


def resolve_protocol_slug(name, db_path=None, no_cache=False, refresh_cache=False):
    """把对象名/代币符号解析成 /protocol/<slug> 可用的 slug。

    返回 (slug, how)。先把输入原样当 slug 试（最直接、最新）；400/404 时退回
    /protocols 全量榜单按 symbol / name 匹配，并优先取 parentProtocolSlug ——
    ENA→ethena 这种「符号与协议名不一致」的情况只有榜单里有完整映射。
    都失败时返回 (None, 'unresolved')。
    """
    direct = str(name).strip().lower()
    if direct:
        data, st = _try_get(
            "/protocol/{}".format(urllib.request.quote(direct, safe="")), timeout=45)
        if isinstance(data, dict):
            return direct, "direct"

    try:
        # 容忍过期缓存：为解析一个 slug 重拉 9MB 不划算
        prots, _ = fetch_protocols_data(db_path, no_cache, refresh_cache,
                                        accept_stale=True)
    except SystemExit:
        # fetch_protocols_data 拉取失败会 _die；解析阶段不该中断整体流程
        return None, "unresolved:list_unavailable"

    want = str(name).strip().upper()
    exact_sym = [p for p in prots if (p.get("symbol") or "").upper() == want]
    exact_name = [p for p in prots if (p.get("name") or "").strip().lower() == str(name).strip().lower()]
    pool = exact_sym or exact_name
    if not pool:
        return None, "unresolved:not_found"

    # 子协议（Ethena USDe / USDtb）共享 symbol，父协议才是研究对象
    parents = [p.get("parentProtocolSlug") for p in pool if p.get("parentProtocolSlug")]
    if parents:
        uniq = sorted(set(parents))
        return uniq[0], "via_parent_slug{}".format("" if len(uniq) == 1 else ":ambiguous({})".format(",".join(uniq)))
    slugs = sorted({p.get("slug") for p in pool if p.get("slug")})
    if len(slugs) == 1:
        return slugs[0], "via_symbol"
    return None, "unresolved:ambiguous({})".format(",".join(slugs[:3]))


def collect_metrics(obj, with_protocols=False, limit=15, db_path=None,
                    no_cache=False, refresh_cache=False, coin_slug=None):
    """按对象类型抓全套指标。返回 (metrics, extra, missing, source_status, protocols, raw)。

    missing 只记「该源适用于本对象、但请求失败」的指标；「本对象类型根本不用这个源」
    一律写进 source_status 的 not_applicable:*，绝不塞进 missing —— 否则上游
    `import` 回填时无法区分「没抓到」和「不该抓」。
    """
    metrics, extra, missing, status_map, protocols, raw = {}, {}, [], {}, None, {}
    dq_warnings = []

    def napp(*keys, reason="kind"):
        """标记某几类源对本对象不适用（不进 missing）。"""
        for k in keys:
            status_map.setdefault(k, "not_applicable:{}".format(reason))

    if obj["kind"] == "protocol":
        # /protocol/<slug> 大小写敏感（"ENA"→400，"ethena"→ok），且符号常与协议名
        # 不一致（ENA→ethena），必须走解析器而不能直接拿对象名拼 URL。
        # 对象 slug 优先，失败再用 defillama_chain 里可能存的符号/名称。
        slug, how = resolve_protocol_slug(
            obj["slug"], db_path, no_cache, refresh_cache)
        if slug is None and obj.get("defillama_chain"):
            slug, how = resolve_protocol_slug(
                obj["defillama_chain"], db_path, no_cache, refresh_cache)
        status_map["protocol_slug"] = how
        if slug is None:
            missing.append("tvl")
            napp("fees", "revenue", reason="protocol_slug_unresolved")
            slug = None
        else:
            m, meta, st = grab_protocol_metrics(slug)
            status_map["protocol_tvl"] = st
            metrics.update(m)
            extra.update(meta)
            if st != "ok":
                missing.append("tvl")

            # 协议 slug 适用的费用/收入端点与链名同一套
            f, st = grab_fees(slug, with_series=True)
            status_map["fees"] = st

            if st == "ok":
                for k, v in f.items():
                    if not k.startswith("_"):
                        metrics["fees_" + k] = v
                q, w = check_series_quality(f.get("_series"), "fees")
                if q:
                    extra["fees_data_quality"] = q
                dq_warnings.extend(w)
            else:
                missing.append("fees")

            r, st = grab_fees(slug, data_type="dailyRevenue")
            status_map["revenue"] = st
            if st == "ok":
                for k, v in r.items():
                    if k in ("24h", "7d", "30d"):
                        metrics["revenue_" + k] = v
            else:
                missing.append("revenue")

        napp("dexs", "stablecoins", "protocols", reason="protocol_level_source")

        # 协议自带 gecko_id → 自动补上价格源，省掉手工 --coin-slug
        if not coin_slug and not obj.get("coingecko_slug") and extra.get("gecko_id"):
            coin_slug = extra["gecko_id"]

    elif obj["kind"] == "chain":
        chain = obj.get("defillama_chain") or obj["name"]

        m, st = grab_chain_metrics(chain)
        status_map["chains"] = st
        metrics.update(m)
        if st != "ok":
            missing.append("tvl")
            if st == "missing:not_in_v2_chains":
                # 链已从 v2/chains 下架（如 Bittensor 只剩 Bittensor EVM），
                # 不推算 TVL；协议聚合值单独放进 extra 并显式标注为 proxy。
                status_map["tvl_proxy"] = "not_computed"

        f, st = grab_fees(chain, with_series=True)
        status_map["fees"] = st
        if st == "ok":
            for k, v in f.items():
                if not k.startswith("_"):
                    metrics["fees_" + k] = v
            q, w = check_series_quality(f.get("_series"), "fees")
            if q:
                extra["fees_data_quality"] = q
            dq_warnings.extend(w)
        else:
            missing.append("fees")

        r, st = grab_fees(chain, data_type="dailyRevenue")
        status_map["revenue"] = st
        if st == "ok":
            for k, v in r.items():
                if k in ("24h", "7d", "30d"):
                    metrics["revenue_" + k] = v
        else:
            missing.append("revenue")

        d, st = grab_dexs(chain)
        status_map["dexs"] = st
        if st == "ok":
            for k, v in d.items():
                metrics["dex_volume_" + k if k != "change_1d" else "dex_change_1d"] = v
        else:
            missing.append("dex")

        s, st = grab_stablecoins(chain)
        status_map["stablecoins"] = st
        if st == "ok":
            metrics["stablecoin_usd"] = s.get("usd")
            metrics["stablecoin_change_7d"] = s.get("change_7d")
        else:
            missing.append("stablecoins")

        if with_protocols:
            pout, db_rows, agg = pull_protocols(
                chain, obj.get("defillama_protocols"), keep_cex=False, limit=limit,
                db_path=db_path, no_cache=no_cache, refresh_cache=refresh_cache)
            protocols = db_rows
            metrics.update(agg)
            raw["protocols"] = {"total_protocols": pout["total_protocols"],
                                "cache": pout.get("cache")}

    price_slug = coin_slug or obj.get("coingecko_slug")
    p, st = grab_price(price_slug)
    status_map["prices"] = st
    if st == "ok":
        metrics["price_usd"] = p.get("price_usd")
        extra["price_confidence"] = p.get("confidence")
        extra["price_symbol"] = p.get("symbol")
        if price_slug:
            extra["coingecko_slug"] = price_slug
    else:
        missing.append("price")

    # 市值/FDV：TSR（TVL÷市值）是核心口径，但这几个字段 DeFiLlama 全系端点都不给。
    mk, st = grab_market_data(price_slug)
    status_map["market_cap"] = st
    if st.startswith("ok"):
        for k in ("market_cap_usd", "fdv_usd", "price_change_24h"):
            metrics[k] = mk.get(k)
        for k in ("circulating_supply", "total_supply"):
            if mk.get(k) is not None:
                extra[k] = mk[k]
        extra["market_cap_source"] = "coingecko:markets"
        if metrics.get("price_usd") is None and mk.get("price_usd") is not None:
            metrics["price_usd"] = mk["price_usd"]
            extra.setdefault("price_symbol", p.get("symbol"))
            note = "price_usd 来自 CoinGecko markets（coins.llama.fi 缺失）"
            extra["price_source"] = note
    else:
        # 协议端点自带 mcap，可作为兜底（口径为 DeFiLlama 自算，可能与 CoinGecko 有差异）
        fb = extra.get("mcap_from_defillama")
        if fb is not None:
            metrics["market_cap_usd"] = fb
            status_map["market_cap"] = "ok:defillama_protocol_fallback"
            extra["market_cap_source"] = "api.llama.fi/protocol (fallback)"
        else:
            missing.append("market_cap")

    if obj["kind"] == "coin":
        # 纯代币（如 QNT）只取价格：链级/协议级指标都不适用，且不是「抓取失败」。
        napp("chains", "fees", "revenue", "dexs", "stablecoins", "protocols",
             reason="coin_kind")

    if dq_warnings:
        extra["data_quality_warnings"] = dq_warnings

    return metrics, extra, missing, status_map, protocols, (raw or None)


# ---------------------------------------------------------------------------
# 老命令（输出结构保持不变）
# ---------------------------------------------------------------------------
def cmd_chain(chain_name):
    """链级指标。涨跌幅由 historicalChainTvl 自算（v2/chains 已不返回 change_*）。"""
    metrics, status = grab_chain_metrics(chain_name)
    if "tvl_usd" not in metrics:
        _die("chain '{}' not found in v2/chains ({})".format(chain_name, status))
    _emit({
        "name": chain_name,
        "tvl_usd": metrics.get("tvl_usd"),
        "tvl_m": _fmt_m(metrics.get("tvl_usd")),
        "change_1d": metrics.get("tvl_change_1d"),
        "change_7d": metrics.get("tvl_change_7d"),
        "change_30d": metrics.get("tvl_change_30d"),
        "source": "api.llama.fi/v2/chains + v2/historicalChainTvl",
        "fetched_at": store.utc_now_iso(),
    })


def cmd_protocols(chain_name, keep_cex=False, limit=15, db_path=None,
                  no_cache=False, refresh_cache=False):
    out, _, _ = pull_protocols(chain_name, [chain_name], keep_cex=keep_cex, limit=limit,
                               db_path=db_path, no_cache=no_cache, refresh_cache=refresh_cache)
    _emit(out)


def cmd_compare(a_chain, b_chain):
    ma, sa = grab_chain_metrics(a_chain)
    mb, sb = grab_chain_metrics(b_chain)
    for nm, m, st in ((a_chain, ma, sa), (b_chain, mb, sb)):
        if "tvl_usd" not in m:
            _die("chain '{}' not found ({})".format(nm, st))
    _emit({
        "compare": [a_chain, b_chain],
        "metrics": {
            "tvl_m": [_fmt_m(ma.get("tvl_usd")), _fmt_m(mb.get("tvl_usd"))],
            "change_1d": [ma.get("tvl_change_1d"), mb.get("tvl_change_1d")],
            "change_7d": [ma.get("tvl_change_7d"), mb.get("tvl_change_7d")],
            "change_30d": [ma.get("tvl_change_30d"), mb.get("tvl_change_30d")],
        },
        "source": "api.llama.fi/v2/chains + v2/historicalChainTvl",
        "fetched_at": store.utc_now_iso(),
    })


# ---------------------------------------------------------------------------
# 库命令
# ---------------------------------------------------------------------------
def open_db(args, require_schema=True):
    path, source = store.resolve_db_path(getattr(args, "db", None))
    if store.is_skill_install_path(path):
        _emit_err("DB 落在 agent 的安装目录里，不是用户数据区（升级/卸载可能被删）。"
                  "当前: {}。建议设 TOKEN_RESEARCH_DB 或用 --db 显式指定。".format(path))
    conn = store.connect(path)
    if require_schema:
        store.ensure_schema(conn)
        # 首次建库后落指针，后续任意副本都解析到同一个库，不会各自分裂
        store.maybe_claim_pointer(path, source)
    return conn, path, source


def _object_or_die(conn, name, create=False, kind=None, dry_run=False):
    """解析对象；不存在且 create=True 时新建。

    dry_run=True 时**不落盘**：返回一个 id=None 的伪对象供后续抓取，避免
    `snapshot <unknown> --new --dry-run` 在库里留下无数据的 objects 行
    （create_object 自带 commit，早于 dry_run 判断就执行了）。
    """
    obj = store.find_object(conn, name)
    if obj is not None:
        return obj
    suggestions = store.suggest_names(conn, name)
    if not create:
        msg = "对象 '{}' 不在库中".format(name)
        if suggestions:
            msg += "，是不是: {}？".format(" / ".join(suggestions))
        msg += "（确认要新建请加 --new）"
        _die(msg, 2)
    chain_name, proto_names = _norm(name)
    if dry_run:
        return {
            "id": None, "slug": name.strip().lower(), "name": name.strip(),
            "kind": kind or "chain", "defillama_chain": chain_name,
            "defillama_protocols": proto_names, "coingecko_slug": None,
            "aliases": [name.strip().lower()], "notes": None,
        }
    obj = store.create_object(
        conn,
        slug=name.strip().lower(),
        name=name.strip(),
        kind=kind or "chain",
        defillama_chain=chain_name,
        defillama_protocols=proto_names,
        coingecko_slug=None,
        aliases=[name.strip().lower()],
        notes="自动创建于 {}".format(store.utc_now_iso()),
    )
    return obj


def cmd_init(args):
    conn, path, source = open_db(args)
    seeded = store.seed(conn, force=args.force) if args.seed else None
    uver = store.user_version(conn)
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]
    # 落一个用户级指针，避免技能的其他副本各建一个库。
    # 显式 --db / 环境变量不改指针（那是用户在指定「这一次」，不是「以后都用它」）。
    pointer_written, pointer_reason = store.maybe_claim_pointer(path, source)
    _emit({"db": path, "db_source": source, "user_version": uver,
           "tables": tables, "seeded": seeded,
           "pointer": store.pointer_file(), "pointer_written": pointer_written,
           "pointer_reason": pointer_reason})


def cmd_db_path(args):
    path, source = store.resolve_db_path(args.db)
    info = store.db_meta(path)
    info["db_source"] = source
    ptr = store.read_pointer()
    info["pointer"] = store.pointer_file()
    info["pointer_target"] = ptr
    if source == "default:user_home":
        info["note"] = ("技能装在 agent 安装目录里，DB 已改落用户主目录：{}。"
                        "想指定别处请设 TOKEN_RESEARCH_DB 或用 --db。".format(path))
    elif store.is_skill_install_path(path):
        info["warning"] = ("该路径在 agent 安装目录下，升级/卸载可能被删；"
                           "建议设 TOKEN_RESEARCH_DB 指向个人知识库")
    _emit(info)


def cmd_db_pointer(args):
    ptr = store.read_pointer()
    out = {"pointer": store.pointer_file(), "target": ptr,
           "exists": bool(ptr) and os.path.isfile(ptr)}
    if args.db:
        out["written"] = store.write_pointer(args.db)
        out["target"] = os.path.abspath(args.db)
    _emit(out)


def cmd_snapshot(args):
    conn, path, _ = open_db(args)
    obj = _object_or_die(conn, args.name, create=args.new, kind=args.kind,
                         dry_run=args.dry_run)

    metrics, extra, missing, status_map, protocols, raw = collect_metrics(
        obj,
        with_protocols=args.with_protocols,
        limit=args.limit,
        db_path=path,
        no_cache=args.no_cache,
        refresh_cache=args.refresh_cache,
        coin_slug=args.coin_slug,
    )
    fp = store.fingerprint(metrics, extra)
    today = store.local_today()
    # dry-run 新建的伪对象没有 id，查历史快照无意义
    latest = store.latest_snapshot(conn, obj["id"]) if obj["id"] else None
    warnings = ["{}: {} -> NULL".format(k, v) for k, v in status_map.items()
                if v.startswith("missing") or v.startswith("error")]
    # 数据质量告警单独列出：它们不是「取不到」，而是「取到了但不可信」
    warnings.extend(extra.get("data_quality_warnings") or [])

    skipped = None
    # --replace-day 语义就是「用这次的精确数据替换当天旧值」，必须绕过相同指纹的跳过判定
    if latest and not args.force and not args.replace_day:
        if latest["captured_day"] == today and latest["fingerprint"] == fp:
            skipped = "identical_to_latest"

    previous = latest
    inserted = None
    if skipped:
        snapshot = latest
    elif args.dry_run:
        snapshot = {
            "id": None, "captured_at": store.utc_now_iso(), "captured_day": today,
            "origin": "api", "source_status": status_map, "missing": missing,
            "fingerprint": fp, "metrics": {k: metrics.get(k) for k in store.METRIC_COLUMNS},
            "extra": extra,
        }
    else:
        if args.replace_day:
            removed = store.delete_day(conn, obj["id"], today)
            if removed:
                warnings.append("--replace-day 删除了当天 {} 条快照".format(removed))
        res = store.insert_snapshot(
            conn, obj["id"], metrics, origin="api", source_status=status_map,
            missing=missing, protocols=protocols, extra=extra,
            raw=raw if args.raw else None,
        )
        inserted = res["inserted"]
        snapshot = res["snapshot"]
        if not inserted:
            skipped = res["reason"]
        previous = store.previous_snapshot(conn, obj["id"], snapshot["id"]) or None

    # 跳过（重复指纹/同秒）时不自比自，diff 无意义 → null，并在 warnings 里说明
    diff = None if skipped else _diff(previous, snapshot)

    out = {
        "object": {
            "id": obj["id"], "slug": obj["slug"], "name": obj["name"], "kind": obj["kind"],
            "defillama_chain": obj.get("defillama_chain"), "aliases": obj["aliases"],
            "matched_input": args.name,
        },
        "snapshot": {
            "id": snapshot["id"],
            "captured_at": snapshot["captured_at"],
            "captured_day": snapshot["captured_day"],
            "origin": snapshot["origin"],
            "metrics": {k: snapshot["metrics"].get(k) for k in store.METRIC_COLUMNS},
            "metrics_m": _metrics_m(snapshot["metrics"]),
            "extra": snapshot.get("extra") or extra,
            "missing": snapshot["missing"],
            "source_status": snapshot["source_status"],
        },
        "protocols": (store.protocols_of(conn, snapshot["id"])
                      if protocols and snapshot.get("id") else None),
        "previous": ({"id": previous["id"], "captured_at": previous["captured_at"]}
                     if previous else None),
        "diff": diff,
        "skipped": skipped,
        "inserted": inserted,
        "dry_run": bool(args.dry_run),
        "warnings": warnings,
        "db": path,
    }
    _emit(out)


DIFF_METRICS = [
    "tvl_usd", "stablecoin_usd", "fees_24h", "revenue_24h", "dex_volume_24h",
    "price_usd", "market_cap_usd", "protocol_count", "protocol_tvl_usd",
]


def _diff(prev, cur):
    if not prev or not cur:
        return None
    out = {}
    for k in DIFF_METRICS:
        p = prev["metrics"].get(k)
        c = cur["metrics"].get(k)
        if p is None and c is None:
            out[k] = {"prev": None, "now": None, "abs": None, "pct": None, "note": "missing in both"}
            continue
        if p is None:
            out[k] = {"prev": None, "now": c, "abs": None, "pct": None,
                      "note": "became_available"}
            continue
        if c is None:
            out[k] = {"prev": p, "now": None, "abs": None, "pct": None, "note": "now_missing"}
            continue
        abs_ = c - p
        pct = round(abs_ / abs(p) * 100, 2) if p else None
        out[k] = {"prev": p, "now": c, "abs": abs_, "pct": pct}
    return {
        "from": {"id": prev["id"], "captured_at": prev["captured_at"]},
        "to": {"id": cur.get("id"), "captured_at": cur["captured_at"]},
        "metrics": out,
    }


def cmd_refresh(args):
    """批量刷新库内过期对象（按 list 的最新快照时间）。"""
    conn, path, _ = open_db(args)
    stale = store.list_objects(conn, stale_days=args.stale_days)
    results = []
    for item in stale:
        obj = store.find_object(conn, item["slug"])
        if obj is None:
            continue
        ns = argparse.Namespace(
            name=obj["slug"], kind=obj["kind"], new=False,
            with_protocols=args.with_protocols, limit=args.limit,
            coin_slug=None, force=False, replace_day=False, raw=False,
            dry_run=args.dry_run, no_cache=False, refresh_cache=False,
            db=args.db,
        )
        try:
            metrics, extra, missing, status_map, protocols, raw = collect_metrics(
                obj, with_protocols=ns.with_protocols, limit=ns.limit,
                db_path=path, coin_slug=None,
                no_cache=ns.no_cache, refresh_cache=ns.refresh_cache)
            fp = store.fingerprint(metrics, extra)
            latest = store.latest_snapshot(conn, obj["id"])
            if latest and latest["captured_day"] == store.local_today() \
                    and latest["fingerprint"] == fp:
                results.append({"slug": obj["slug"], "skipped": "identical_to_latest"})
                continue
            if args.dry_run:
                results.append({"slug": obj["slug"], "action": "would_insert"})
                continue
            res = store.insert_snapshot(conn, obj["id"], metrics, origin="api",
                                        source_status=status_map, missing=missing,
                                        protocols=protocols, extra=extra)
            results.append({
                "slug": obj["slug"],
                "snapshot_id": res["snapshot"]["id"],
                "action": "inserted" if res["inserted"] else "skipped:{}".format(res["reason"]),
                "diff": _diff(store.previous_snapshot(conn, obj["id"], res["snapshot"]["id"])
                              if res["inserted"] else None,
                              res["snapshot"]) if res["inserted"] else None,
                "missing": missing,
            })
        except Exception as e:  # 单个对象失败不影响批量
            results.append({"slug": obj["slug"], "error": str(e)})
    _emit({"db": path, "stale_days": args.stale_days, "count": len(results),
           "results": results})


def cmd_db_backup(args):
    conn, path, _ = open_db(args)
    target = os.path.abspath(args.to)
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(target):
        _die("目标已存在，拒绝覆盖: {}".format(target), 2)
    conn.execute("VACUUM INTO ?", (target,))
    _emit({"db": path, "backup": target, "size_bytes": os.path.getsize(target)})


def cmd_list(args):
    conn, path, _ = open_db(args)
    objects = store.list_objects(conn, kind=args.kind, sort=args.sort,
                                 stale_days=args.stale_days,
                                 with_reports=args.with_reports)
    if args.limit:
        objects = objects[:args.limit]
    _emit({"db": path, "count": len(objects), "objects": objects})


def cmd_show(args):
    conn, path, _ = open_db(args)
    obj = _object_or_die(conn, args.name)
    snap = store.snapshot_by_ref(conn, obj["id"], args.at)
    if snap is None:
        _die("没有匹配的快照 (--at {})".format(args.at), 2)
    out = {
        "object": {k: obj[k] for k in
                   ("id", "slug", "name", "kind", "defillama_chain",
                    "defillama_protocols", "coingecko_slug", "notes",
                    "aliases", "first_researched_at", "last_researched_at",
                    "last_snapshot_at", "last_report_at")},
        "snapshot": {
            "id": snap["id"], "captured_at": snap["captured_at"],
            "captured_day": snap["captured_day"], "origin": snap["origin"],
            "metrics": snap["metrics"],
            "metrics_m": _metrics_m(snap["metrics"]),
            "extra": snap["extra"], "missing": snap["missing"],
            "source_status": snap["source_status"],
        },
        "protocols": store.protocols_of(conn, snap["id"]) if args.with_protocols else None,
        "holder_structure": store.holders_of(conn, snap["id"]),
        "reports": store.list_reports(conn, obj["id"], limit=10),
        "db": path,
    }
    _emit(out)


def cmd_history(args):
    conn, path, _ = open_db(args)
    obj = _object_or_die(conn, args.name)
    metrics = [m.strip() for m in (args.metric or "tvl_usd").split(",") if m.strip()]
    data = store.history(conn, obj["id"], metrics, limit=args.limit, since=args.since)
    if args.format == "csv":
        cols = ["captured_at", "origin"] + data["metrics"]
        lines = [",".join(cols)]
        for p in data["points"]:
            lines.append(",".join([str(p["captured_at"]), str(p["origin"])]
                                  + ["" if p.get(m) is None else str(p[m]) for m in data["metrics"]]))
        print("\n".join(lines))
        return
    _emit({"object": {"slug": obj["slug"], "name": obj["name"]},
           "metrics": data["metrics"], "count": data["count"],
           "points": data["points"], "series": data["series"], "db": path})


def cmd_report_add(args):
    conn, path, _ = open_db(args)
    obj = _object_or_die(conn, args.name)
    snapshot_id = args.snapshot_id
    if snapshot_id is None and args.snapshot_at:
        snap = store.snapshot_by_ref(conn, obj["id"], args.snapshot_at)
        snapshot_id = snap["id"] if snap else None
    demystify = None
    if args.demystify:
        try:
            demystify = json.loads(args.demystify)
        except ValueError as e:
            _die("--demystify 不是合法 JSON: {}".format(e), 2)
    res = store.upsert_report(
        conn, obj["id"], report_path=args.path, report_date=args.date,
        one_liner=args.one_liner, verdict=args.verdict, title=args.title,
        business_qual=args.business_qual, verdict_note=args.verdict_note,
        demystify=demystify, snapshot_id=snapshot_id,
    )
    _emit({"report": res["report"], "upserted": res["upserted"],
           "object": {"slug": obj["slug"], "name": obj["name"]}, "db": path})


def cmd_report_list(args):
    conn, path, _ = open_db(args)
    object_id = None
    name = None
    if args.name:
        obj = _object_or_die(conn, args.name)
        object_id, name = obj["id"], obj["slug"]
    _emit({"object": name, "reports": store.list_reports(conn, object_id, limit=args.limit),
           "db": path})


def cmd_import(args):
    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    conn, path, _ = open_db(args)
    report = {"db": path, "file": args.file, "dry_run": bool(args.dry_run),
              "objects": [], "conflicts": []}
    for spec in payload.get("objects", []):
        slug = (spec.get("slug") or "").strip().lower()
        obj = store.find_object(conn, slug)
        action = "exists"
        if obj is None:
            if args.dry_run:
                action = "would_create"
                obj = {"id": None, "slug": slug, "name": spec.get("name") or slug,
                       "kind": spec.get("kind", "chain")}
            else:
                obj = store.create_object(
                    conn, slug=slug, name=spec.get("name") or slug,
                    kind=spec.get("kind", "chain"),
                    defillama_chain=spec.get("defillama_chain"),
                    defillama_protocols=spec.get("defillama_protocols"),
                    coingecko_slug=spec.get("coingecko_slug"),
                    aliases=spec.get("aliases") or [slug],
                    notes=spec.get("notes"),
                )
                action = "created"

        obj_out = {"slug": obj["slug"], "action": action, "snapshots": [], "reports": []}

        for snap in spec.get("snapshots", []):
            captured_at = snap.get("captured_at") or store.utc_now_iso()
            exists = None
            if obj["id"] is not None:
                exists = conn.execute(
                    "SELECT id FROM snapshots WHERE object_id=? AND captured_at=?",
                    (obj["id"], captured_at)).fetchone()
            if exists is not None:
                obj_out["snapshots"].append({"captured_at": captured_at, "action": "skipped"})
                continue
            if args.dry_run:
                obj_out["snapshots"].append({"captured_at": captured_at, "action": "would_insert"})
                continue
            res = store.insert_snapshot(
                conn, obj["id"], snap.get("metrics") or {},
                origin=snap.get("origin", "import"),
                source_status=snap.get("source_status") or {},
                missing=snap.get("missing") or [],
                protocols=snap.get("protocols"),
                extra=snap.get("extra") or {},
                captured_at=captured_at,
            )
            obj_out["snapshots"].append(
                {"captured_at": captured_at, "id": res["snapshot"]["id"],
                 "action": "inserted" if res["inserted"] else "skipped:{}".format(res["reason"])})

        for rep in spec.get("reports", []):
            if args.dry_run:
                obj_out["reports"].append({"report_path": rep.get("report_path"),
                                           "action": "would_upsert"})
                continue
            snapshot_id = None
            if rep.get("snapshot_captured_at") and obj["id"] is not None:
                row = conn.execute(
                    "SELECT id FROM snapshots WHERE object_id=? AND captured_at=?",
                    (obj["id"], rep["snapshot_captured_at"])).fetchone()
                snapshot_id = row["id"] if row else None
            res = store.upsert_report(
                conn, obj["id"],
                report_path=rep["report_path"],
                report_date=rep.get("report_date"),
                one_liner=rep.get("one_liner") or "(未填写)",
                verdict=rep.get("verdict", "unknown"),
                title=rep.get("title"),
                business_qual=rep.get("business_qual"),
                verdict_note=rep.get("verdict_note"),
                demystify=rep.get("demystify"),
                snapshot_id=snapshot_id,
            )
            obj_out["reports"].append({"report_path": rep["report_path"],
                                       "action": "updated" if res["upserted"] else "inserted"})
        report["objects"].append(obj_out)
    _emit(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="defillama.py", description="DeFiLlama 数据拉取 + token-research 本地快照库",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", help="SQLite 路径（默认 env TOKEN_RESEARCH_DB 或 scripts/../token_research.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("chain", help="链级 TVL（不入库）")
    sp.add_argument("name")

    sp = sub.add_parser("protocols", help="协议排名（不入库）")
    sp.add_argument("name")
    sp.add_argument("--all", action="store_true", help="保留 CEX")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--refresh-cache", action="store_true")

    sp = sub.add_parser("top", help="同 protocols")
    sp.add_argument("name")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--refresh-cache", action="store_true")

    sp = sub.add_parser("compare", help="两链对比（不入库）")
    sp.add_argument("a")
    sp.add_argument("b")

    sp = sub.add_parser("init", help="建表 + 灌种子")
    sp.add_argument("--seed", action="store_true")
    sp.add_argument("--force", action="store_true")

    spdb = sub.add_parser("db", help="库信息")
    sub_db = spdb.add_subparsers(dest="db_cmd", required=True)
    sub_db.add_parser("path", help="打印 DB 路径解析结果")
    sub_db.add_parser("pointer", help="打印/校验用户级指针文件 ~/.token_research_db")
    sb = sub_db.add_parser("backup", help="热备份（VACUUM INTO）")
    sb.add_argument("--to", required=True)

    sp = sub.add_parser("refresh", help="批量刷新库内过期对象")
    sp.add_argument("--stale-days", type=float, default=1.0)
    sp.add_argument("--with-protocols", action="store_true")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("snapshot", help="抓全套指标入库并打印 diff")
    sp.add_argument("name")
    sp.add_argument("--kind", choices=["chain", "coin", "protocol"])
    sp.add_argument("--new", action="store_true", help="允许新建未知对象")
    sp.add_argument("--with-protocols", action="store_true", help="同时抓生态 Top-N（拉 9MB）")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--coin-slug", help="CoinGecko slug（覆盖对象配置）")
    sp.add_argument("--force", action="store_true", help="指纹相同也强行追加")
    sp.add_argument("--replace-day", action="store_true", help="删除当天旧快照后重插")
    sp.add_argument("--raw", action="store_true", help="保存上游原始响应")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--refresh-cache", action="store_true")

    sp = sub.add_parser("list", help="已研究的对象")
    sp.add_argument("--kind", choices=["chain", "coin", "protocol"])
    sp.add_argument("--stale-days", type=float)
    sp.add_argument("--sort", choices=["recent", "tvl", "name"], default="recent")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--with-reports", action="store_true")

    sp = sub.add_parser("show", help="快照详情")
    sp.add_argument("name")
    sp.add_argument("--at", help="快照 id 或 YYYY-MM-DD，默认最新")
    sp.add_argument("--with-protocols", action="store_true")

    sp = sub.add_parser("history", help="时间序列")
    sp.add_argument("name")
    sp.add_argument("--metric", help="逗号分隔，如 tvl_usd,fees_24h")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--since", help="YYYY-MM-DD")
    sp.add_argument("--format", choices=["json", "csv"], default="json")

    spr = sub.add_parser("report", help="报告索引")
    sub_rep = spr.add_subparsers(dest="report_cmd", required=True)
    s = sub_rep.add_parser("add")
    s.add_argument("name")
    s.add_argument("--path", required=True, help="vault 相对路径")
    s.add_argument("--date")
    s.add_argument("--one-liner", required=True)
    s.add_argument("--verdict", required=True,
                   choices=["real", "narrative", "mixed", "unknown"])
    s.add_argument("--title")
    s.add_argument("--business-qual")
    s.add_argument("--verdict-note")
    s.add_argument("--demystify", help="祛魅四问 JSON")
    s.add_argument("--snapshot-id", type=int)
    s.add_argument("--snapshot-at", help="快照 id 或日期")
    s = sub_rep.add_parser("list")
    s.add_argument("name", nargs="?")
    s.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("import", help="从 JSON 回填")
    sp.add_argument("file")
    sp.add_argument("--dry-run", action="store_true")

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    cmd = args.cmd
    try:
        if cmd == "chain":
            cname, _ = _norm(args.name)
            cmd_chain(cname)
        elif cmd in ("protocols", "top"):
            cname, _ = _norm(args.name)
            cmd_protocols(cname, keep_cex=args.all, limit=args.limit,
                          db_path=store.resolve_db_path(args.db)[0],
                          no_cache=getattr(args, "no_cache", False),
                          refresh_cache=getattr(args, "refresh_cache", False))
        elif cmd == "compare":
            ca, _ = _norm(args.a)
            cb, _ = _norm(args.b)
            cmd_compare(ca, cb)
        elif cmd == "init":
            cmd_init(args)
        elif cmd == "db":
            if args.db_cmd == "backup":
                cmd_db_backup(args)
            elif args.db_cmd == "pointer":
                cmd_db_pointer(args)
            else:
                cmd_db_path(args)
        elif cmd == "refresh":
            cmd_refresh(args)
        elif cmd == "snapshot":
            cmd_snapshot(args)
        elif cmd == "list":
            cmd_list(args)
        elif cmd == "show":
            cmd_show(args)
        elif cmd == "history":
            cmd_history(args)
        elif cmd == "report":
            if args.report_cmd == "add":
                cmd_report_add(args)
            else:
                cmd_report_list(args)
        elif cmd == "import":
            cmd_import(args)
        else:
            _die("unknown command: {}".format(cmd))
    except sqlite3.DatabaseError as e:
        _die("数据库错误: {}".format(e))
    except (ValueError, RuntimeError) as e:
        _die(str(e))


if __name__ == "__main__":
    main()
