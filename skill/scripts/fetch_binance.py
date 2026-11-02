#!/usr/bin/env python3
"""
fetch_binance.py — Binance Web3 API 持有结构抓取（token-research 的可选增强数据源）。

补的是 DeFiLlama 完全没有的维度：**谁在拿筹码**。
- 集中度画像：前10地址占比、聪明钱/KOL/内幕/狙击手/捆绑者持仓
- 大户明细：持仓比例、成本、已实现盈亏、**资金来源溯源**（判断是否同一批钱包）

用途：为祛魅检验 Q1「谁真的在用」提供链上证据。

前置条件（缺一不可）
--------------------
1. Python >= 3.10（SDK 要求）。本文件**不能**在技能默认的 3.8 下运行。
2. `pip install binance-web3-wallet`
3. 环境变量 BINANCE_WEB3_API_KEY / BINANCE_WEB3_API_SECRET
4. **出口 IP 必须是香港/新加坡/日本等支持地区**。美国 IP 返回
   `40304 Service not available due to compliance restriction`。
5. 需要代理时设 TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808

已知 SDK bug（v10.1.0）
-----------------------
`get_token_basic_info` / `get_token_price` / `get_token_trading_info` /
`list_de_fi_protocols` 内部硬编码 `request_body = {}`，无法传代币参数，
调用必返回 40001。本脚本只用不受影响的 GET 类接口。

用法
----
python fetch_binance.py holders ENA --chain 1 \
       --contract 0x57e114b691db790c35207b2e685d4a43181e6061 [--limit 50]

输出 JSON 到 stdout；加 --save 则同时写入本地快照库（token_holders / holder_entries）。
找不到代币地址时可用 --search ENA 先查。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_PROXY = os.environ.get("TOKEN_RESEARCH_PROXY")

# 常见代币的链 ID + 合约地址（Binance chain id：1=Ethereum, 56=BSC, CT_501=Solana）
KNOWN_COINS = {
    "pepe":  ("1", "0x6982508145454ce325ddbe47a25d4ec3d2311933"),
    "shib":  ("1", "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"),
    "ena":   ("1", "0x57e114b691db790c35207b2e685d4a43181e6061"),
    "usde":  ("1", "0x4c9edd5852cd905f086c759e8383e09bff1e68b3"),
    "uni":   ("1", "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"),
    "aave":  ("1", "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"),
    "crv":   ("1", "0xd533a949740bb3306d119cc777fa900ba034cd52"),
    "ldo":   ("1", "0x5a98fcbea516cf06857215779fd812ca3bef1b32"),
}


# ---------------------------------------------------------------------------
# 凭据（支持从 Windows 注册表兜底读取，因为新开的 shell 常常没继承环境变量）
# ---------------------------------------------------------------------------
def _get_credential(name):
    val = os.environ.get(name)
    if val:
        return val, "env"
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.check_output(
                ["reg", "query", r"HKCU\Environment", "/v", name],
                stderr=subprocess.DEVNULL,
            ).decode("gbk", "replace")
            for line in out.splitlines():
                if name in line:
                    for tag in ("REG_EXPAND_SZ", "REG_SZ"):
                        if tag in line:
                            return line.split(tag)[-1].strip(), "registry"
        except (OSError, subprocess.SubprocessError):
            pass
    return None, None


def build_client():
    try:
        from binance_web3_common.constants import WEB3_WALLET_REST_API_PROD_URL
        from binance_sdk_web3_wallet.rest_api.rest_api import ConfigurationRestAPI
        from binance_sdk_web3_wallet.web3_wallet import Web3Wallet
    except ImportError as e:
        raise SystemExit(
            "[error] 需要 Python>=3.10 且已安装 binance-web3-wallet：{}\n"
            "        （技能默认 Python 是 3.8，请用 D:\\develop\\PYTHON3.12\\python.exe 运行）".format(e)
        )

    key, ksrc = _get_credential("BINANCE_WEB3_API_KEY")
    secret, _ = _get_credential("BINANCE_WEB3_API_SECRET")
    if not key or not secret:
        raise SystemExit("[error] 缺少 BINANCE_WEB3_API_KEY / BINANCE_WEB3_API_SECRET")

    kwargs = dict(api_key=key, api_secret=secret,
                  base_path=WEB3_WALLET_REST_API_PROD_URL, timeout=30000)
    if DEFAULT_PROXY:
        from urllib.parse import urlparse
        u = urlparse(DEFAULT_PROXY)
        kwargs["proxy"] = {
            "host": u.hostname, "port": u.port or 10808,
            "protocol": u.scheme or "http",
        }
    client = Web3Wallet(config_rest_api=ConfigurationRestAPI(**kwargs))
    return client.rest_api, ksrc


def _unwrap(resp):
    """把 SDK 的响应对象统一成 (code, msg, data)。"""
    d = resp.data()
    if hasattr(d, "to_dict"):
        d = d.to_dict()
    elif hasattr(d, "__dict__") and not isinstance(d, dict):
        d = dict(d.__dict__)
    if isinstance(d, dict):
        return d.get("code"), d.get("msg"), d.get("data")
    return None, None, d


def _first(x):
    if isinstance(x, list):
        return x[0] if x else {}
    return x or {}


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_search(api, args):
    # 注意：search_token 传 recv_window=int 会触发 SDK 的 header 类型 bug，不要传
    code, msg, data = _unwrap(api.search_token(chains=args.chain, search=args.keyword))
    if code != 0:
        raise SystemExit("[error] search_token code={} msg={}".format(code, msg))
    rows = []
    for t in (data or []):
        rows.append({
            "token_symbol": t.get("tokenSymbol"), "token_name": t.get("tokenName"),
            "chain_id": t.get("binanceChainId"), "contract": t.get("tokenContractAddress"),
            "price": t.get("price"), "market_cap": t.get("marketCap"),
            "holders": t.get("holders"), "liquidity": t.get("liquidity"),
        })
    print(json.dumps({"keyword": args.keyword, "count": len(rows), "results": rows},
                     ensure_ascii=False, indent=2))


def cmd_holders(api, args):
    chain_id = args.chain
    contract = args.contract
    if not contract:
        hit = KNOWN_COINS.get(args.name.lower())
        if hit:
            chain_id, contract = hit
        else:
            raise SystemExit(
                "[error] 不知道 '{}' 的合约地址，请用 --chain/--contract 指定，"
                "或先跑 `search <关键词>` 查。".format(args.name))

    warnings = []

    # 市值 + 总量：命令行优先，其次从本地快照库自动取
    #
    # 关键口径（2026-11-01 实测 ZAMA 时发现）：
    #   币安 API 的 holdingPercent 基数是「代币总量 total_supply」，不是流通量。
    #   实测 ZAMA #1 持 7.665e9 枚 = 67.58%，而 7.665e9 / 11.341e9(总量) = 67.58%。
    #   若按流通市值换算美元，会把 542M 错算成 122M。所以两个都要取，分别算。
    mcap = args.mcap
    total_supply = args.total_supply
    price = args.price
    snap_row = None
    if mcap is None or total_supply is None or price is None:
        try:
            import store as _store
            conn = _store.connect(_store.resolve_db_path(args.db)[0])
            obj = _store.find_object(conn, args.name)
            if obj is not None:
                snap_row = _store.latest_snapshot(conn, obj["id"])
                if snap_row:
                    if mcap is None:
                        mcap = snap_row["metrics"].get("market_cap_usd")
                    if price is None:
                        price = snap_row["metrics"].get("price_usd")
                    if total_supply is None:
                        total_supply = (snap_row.get("extra") or {}).get("total_supply")
            conn.close()
        except Exception:
            pass
    args._mcap = mcap
    args._total_supply = total_supply
    args._price = price

    code, msg, dist = _unwrap(api.get_token_advanced_info(chain_id, contract))
    if code != 0:
        raise SystemExit("[error] get_token_advanced_info code={} msg={}".format(code, msg))
    dist = _first(dist)

    entries = []
    code2, msg2, rows = _unwrap(api.get_holders_ranking(chain_id, contract))
    # 注意：holdAmount 是**代币数量（枚）**，不是美元金额。实测 PEPE 时发现，
    # 11.92% × 420.69 万亿枚 = 5.01 万亿枚，与 API 返回值吻合。
    # 这里按「占比 × 市值」换算成美元，估值缺失时留 None，绝不把枚数当美元。
    mcap = args._mcap
    total_supply = getattr(args, "_total_supply", None)
    if code2 == 0:
        for i, r in enumerate((rows or [])[: args.limit], start=1):
            pct = _num(r.get("holdingPercent"))
            amount_tokens = _num(r.get("holdAmount"))
            # 优先用「枚数 × 价格」直接算，因为它不依赖 pct 的基数假设；
            # 拿不到枚数时才退回 pct 换算，且明确标注基数。
            usd = None
            usd_basis = None
            price = getattr(args, "_price", None)
            if amount_tokens is not None and price:
                usd, usd_basis = round(amount_tokens * price, 2), "tokens_x_price"
            elif mcap and pct is not None and total_supply:
                # pct 基数是总量 → 枚数 = pct × total_supply，再乘隐式单价 mcap/流通量
                usd, usd_basis = round(mcap * pct / 100, 2), "pct_x_marketcap(基数疑为非流通)"
            entries.append({
                "rank": i,
                "wallet_address": r.get("holderWalletAddress"),
                "holding_pct": pct,
                "hold_amount_tokens": amount_tokens,
                "hold_amount_usd": usd,
                "usd_basis": usd_basis,
                "avg_buy_price": _num(r.get("avgBuyPrice")),
                "realized_pnl_usd": _num(r.get("realizedPnlUsd")),
                "funding_source": r.get("fundingSource"),
                "fundingSourceLabel": r.get("fundingSourceLabel"),
            })
    else:
        warnings.append("get_holders_ranking 失败 code={} msg={}".format(code2, msg2))
    if mcap is None:
        warnings.append("未提供 --mcap，hold_amount_usd 为空（API 的 holdAmount 是代币枚数）")
    if total_supply:
        warnings.append(
            "注意：holdingPercent 基数为代币总量({:.4g} 枚)，非流通量。"
            "按占比×市值换算会低估美元值（实测 ZAMA 差 4.4 倍），"
            "故优先用「枚数×价格」。".format(total_supply))

    # 大户成本全为 0 通常意味着「转入而非买入」，如实标注，避免误读为零成本获利
    zero_cost = sum(1 for e in entries if not e.get("avg_buy_price"))
    if entries and zero_cost == len(entries):
        warnings.append("全部大户 avg_buy_price=0：多为转入而非买入，不能解读为零成本获利")

    out = {
        "object": {"name": args.name, "chain_id": chain_id, "contract": contract},
        "distribution": {
            "top10_holding_pct": _num(dist.get("top10HoldingPercent")),
            "smart_money_pct": _num(dist.get("smartMoneyHoldingPercent")),
            "kol_pct": _num(dist.get("kolHoldingPercent")),
            "insider_pct": _num(dist.get("insiderHoldingPercent")),
            "sniper_pct": _num(dist.get("sniperHoldingPercent")),
            "bundler_pct": _num(dist.get("bundlerHoldingPercent")),
            "dev_pct": _num(dist.get("devHoldingPercent")),
            "pro_pct": _num(dist.get("proHoldingPercent")),
            "fresh_wallet_pct": _num(dist.get("freshWalletPct") or dist.get("freshWalletHoldingPercent")),
            "holders": _num(dist.get("holders")),
            "bn_holder_count": _num(dist.get("bnHolderCount")),
            "bn_trader_count_7d": _num(dist.get("bnTraderCount7D")),
        },
        "top_holders": entries,
        "warnings": warnings,
        "source": "Binance Web3 API (api/v1/dex/market/token/*)",
    }

    if args.save:
        import store
        conn = store.connect(store.resolve_db_path(args.db)[0])
        store.ensure_schema(conn)
        obj = store.find_object(conn, args.name)
        if obj is None:
            raise SystemExit("[error] 对象 '{}' 不在库中，先运行 "
                             "defillama.py snapshot {} --kind coin --new".format(args.name, args.name))
        latest = store.latest_snapshot(conn, obj["id"])
        if latest is None:
            raise SystemExit("[error] 对象 '{}' 还没有快照，先跑 defillama.py snapshot".format(args.name))
        holders_payload = {
            "chain_id": chain_id,
            "contract_address": contract,
            "distribution": {k: (str(v) if isinstance(v, str) else v)
                             for k, v in dist.items()},
            "entries": entries,
            "raw": dist,
        }
        with conn:
            store._insert_holders(conn, latest["id"], obj["id"], holders_payload,
                                  latest["captured_at"])
        out["saved"] = {"db": store.resolve_db_path(args.db)[0],
                        "snapshot_id": latest["id"]}
        conn.close()

    _emit(out)


def _num(v):
    """API 里数值多是字符串，统一转 float；空值/非法保持 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s and s.lower() not in ("null", "n/a", "-") else None
    except ValueError:
        return None


def _emit(obj):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="fetch_binance.py",
        description="Binance Web3 API 持有结构抓取（需 Python>=3.10 + API key + 非美国 IP）")
    p.add_argument("--db", help="SQLite 路径（--save 时用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("holders", help="抓取代币持有结构与大户明细")
    s.add_argument("name")
    s.add_argument("--chain", default=None, help="Binance 链 ID（1=Ethereum, 56=BSC）")
    s.add_argument("--contract", default=None, help="代币合约地址")
    s.add_argument("--limit", type=int, default=50, help="大户条数，默认 50")
    s.add_argument("--mcap", type=float, default=None,
                   help="代币市值（USD）。用于把 holdAmount（枚数）换算成美元；"
                        "不给则 hold_amount_usd 为空。可用 --save 时从快照库自动取")
    s.add_argument("--price", type=float, default=None,
                   help="代币单价（USD）。优先用「枚数×价格」算美元，避免 pct 基数问题；自动从快照库取")
    s.add_argument("--total-supply", type=float, default=None,
                   help="代币总量（枚）。holdingPercent 的基数是总量而非流通量；自动从快照库 extra.total_supply 取")
    s.add_argument("--save", action="store_true", help="写入本地快照库")

    s = sub.add_parser("search", help="按关键词搜索代币，拿链 ID + 合约地址")
    s.add_argument("keyword")
    s.add_argument("--chain", default="1")

    args = p.parse_args(argv)
    api, ksrc = build_client()
    if args.cmd == "search":
        cmd_search(api, args)
    else:
        cmd_holders(api, args)


if __name__ == "__main__":
    main()
