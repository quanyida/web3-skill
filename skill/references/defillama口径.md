# DeFiLlama 数据口径与验证方法

本文档记录 2026-10-15 调研 NEAR / TAO 时验证过的数据口径、踩坑点和交叉验证方法，供后续代币研究复用。

## 1. TVL 统计口径

- **公式**：TVL = Σ(每种代币锁仓数量 × 该代币价格)。价格来自 CoinGecko 等价格源，稳定币按 ~$1 计，原生代币/稳定币/wrapped 代币分开计价。
- **来源三路**：① 协议主动上报（提交 TVL API 端点）；② 索引器读链上合约余额；③ 第三方聚合补齐桥/CEX 等不透明部分。
- **按链拆分**：协议声明自己跑在哪些链上 → TVL 拆分进对应链榜单 → 累加成链 TVL。
- **关键认知**：TVL 衡量「资金沉淀多少」，不衡量「资金进出」，也不等于活跃度。单日大幅涨跌常是代币价格变动而非资金流动。

## 2. 四个已验证的坑（调研 NEAR/TAO 时全部踩到）

| 坑 | 现象 | 处理 |
|---|---|---|
| **CEX 噪声** | Binance / Gate / Bitfinex / Bitget / Bitstamp / KuCoin 等因支持 Near 充提被计入「Near 协议」榜单，TVL 达百亿级 | 按 `category != 'CEX'` 过滤；CEX 协议 category 字段为 `"CEX"` |
| **桥/聚合器双重计算** | Portal 的 $15.7 亿是「所有链」总锁仓，却被计入 NEAR 榜 | 检查协议是否跨多链、category 是否为 Bridge；对 NEAR 原生榜剔除 Portal 这类聚合桥 |
| **锁仓 ≠ 活跃** | dTAO 占 TAO TVL 的 90%（$468.66m / $520.82m），本质是 TAO 币在 AMM 池，无费用、无活跃地址 | 拆 TVL 构成 + 看 Fees + 看 Active Addresses 三件套，不能只看 TVL |
| **价格失真** | 小币种/低流动性代币价格波动放大 TVL 涨跌 | 结合 Inflows 分项判断是价格还是资金流动 |

## 3. 已验证的 API 端点（2026-10-15）

| 端点 | 用途 | 特点 |
|---|---|---|
| `GET https://api.llama.fi/v2/chains` | 链级 TVL + 24h/7d/30d 变化 | 轻量快速，全量链 |
| `GET https://api.llama.fi/protocols` | 全量 ~8700 个协议，含 chains / category / tvl / symbol | **体积大（~9MB）拉取慢**，建议 curl 下载到本地缓存再解析，勿反复在线过滤 |

### ⚠ 链名双体系（重要坑，XRP 调研实测 2026-10-15）

**v2/chains 与 protocols 两个 API 的链名体系不一致**，直接拿 v2/chains 的链名去 protocols 过滤会得到 0 条：

| 链 | v2/chains 链名 | protocols 链名 |
|---|---|---|
| XRP | `XRPL`（另含 `XRPL EVM` 空壳链） | `Ripple` |
| NEAR | `Near` | `Near`（一致） |
| Bittensor | `Bittensor` | `Bittensor`（一致） |

脚本 defillama.py 已内置 `_CHAIN_ALIASES` 映射（xrp/xrpl/ripple → v2:XRPL + protocols:[Ripple,XRPL,XRPL EVM]），`protocols XRP` 可直接使用。

### protocols 数据结构关键字段

```json
{
  "name": "Stader",
  "category": "Liquid Staking",   // 过滤 CEX 的关键字段
  "tvl": 235920000,
  "symbol": "SD",
  "chains": ["Near", "BNB Chain"] // 跨链协议会在多条链出现
}
```

## 4. 交叉验证方法（已实测）

1. **页面 ↔ API 交叉**：浏览器打开 `defillama.com/chain/<name>` 读链页指标（稳定币市值、Chain Fees、App Fees、DEX Volume、Active Addresses、Inflows、价格/市值/FDV 这些 v2/chains 没有），再用 `v2/chains` 核对 TVL 数量级一致。
2. **全量协议过滤**：`protocols` API 按 `chains contains <chain>` 过滤 → 剔除 category=CEX → 按 tvl 降序。
3. **Bittensor 特例**：DeFiLlama 对 Bittensor 有 `Bittensor`（$520.82m）与 `Bittensor EVM`（$0.03m）两条链记录；其页面数据很薄（仅 6 protocols、无 Stablecoins/费用/活跃地址），说明 TAO 的 DeFi 层指标几乎缺失，估值锚在 AI 子网叙事而非 DeFi。

## 5. 链页补充指标（v2/chains 没有，需浏览器读取）

NEAR 链页实测值（2026-10-15，供口径参照）：
- Stablecoins Market Cap: $128.87m（7d -2.41%）
- Chain Fees 24h: $5,587 / Chain Revenue 24h: $3,911
- App Fees 24h: $98,164 / App Revenue 24h: $99,759
- DEXs Volume 24h: $19.23m / Inflows 24h: $47,649
- Active Addresses 24h: 58,615
- Total Raised: $533.7m
- $NEAR $4.62 / MCap $6.049b / FDV $6.049b

Bittensor 链页实测值：
- Token Incentives 24h: $0；$TAO $258.62 / MCap $2.936b / FDV $5.437b
- 无 Stablecoins / Fees / Revenue / Active Addresses 指标（页面缺失）

## 6. 生态结构归类（NEAR 实测，2026-10-15）

NEAR 非 CEX Top 15（剔除 Portal 跨链聚合 $1,566.70m 后）：
Stader(LS $235.92m) / NEAR Intents(Bridge $204.66m) / Rhea Lend(Lending $180.45m) / NEAR Bridge($128.08m) / Meta Pool Near(LS $112.41m) / LiNEAR(LS $103.76m) / KAIO(RWA $39.64m) / RHEA LST(LS $38.17m) / Templar(Lending $31.34m) / Rhea Dex(Dexs $30.89m) / Orderly Bridge($24.14m) / TruStake(LS $2.91m) / Jumbo(Dexs $1.38m) / Rainbow Bridge($1.31m)

→ 结论：NEAR 原生 DeFi 以流动性质押为主力，叠加借贷与桥，生态结构完整，有真实链上经济活动。

## 7. XRPL 链实测口径（2026-10-15，XRP 调研）

链级数据（页面 defillama.com/chain/xrpl + API 交叉验证）：
- TVL $39.32m（24h -4.53%；API 侧 $39.07m 一致）；DEXs Volume 24h $7.36m；Inflows 24h $0
- Stablecoins Market Cap **$1.152b**（7d -12.71%），原生稳定币 RLUSD
- RWA Active AUM $20.92m；Chain Fees 24h $542；App Fees 24h $27,543
- Active Addresses 24h **40,262**；$XRP $1.30 / MCap $82.219b / FDV $130.219b

协议生态（页面 25 个协议 / API 23 个非 CEX，注意双链名）：
- **真正 XRPL 原生单链的只有 3 个**：XRPL DEX(Dexs $34.84m)、Doppler Finance(CeDeFi $117.73m 但为 Ripple+Ethereum 双链合计，页面也显示 2 chains)、XORA(CeDeFi $0.28m)
- 榜单被跨链桥大额污染：Coinbase Bridge $7.83b（5 链合计）、NEAR Intents $204m、FAssets $195m、Axelar $146m、THORChain DEX $63m 等均为多链聚合，非 XRPL 独占
- XRPL EVM 是独立空壳链（5 个协议合计 $0.03m）

关键结论：**XRP 是「大代币、薄 DeFi」**——XRP 市值 $82b 全球前列，但链上 DeFi TVL 仅 $39m；价值载体在稳定币 RLUSD（$1.15b）与支付/结算叙事，而非 DeFi 应用层。活跃地址 40,262 不算低，但链上金融活动稀疏，与市值严重不匹配（TVL/MCap 比 ≈ 0.05%）。
