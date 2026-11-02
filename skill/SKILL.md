---
name: token-research
version: "3.2"
description: "代币/公链全面研究。调研代币或公链时使用：既包括『背后是什么业务/产品/服务』（官网+官方文档+多渠道业务定性），也包括链上真实使用数据（TVL、稳定币、费用/收入、DEX 交易量、活跃地址、协议生态构成、估值指标）。典型触发：『调研下 XX 链/币』、『XX 是做什么的』、『NEAR 和 TAO 哪个有真数据』、『这个币的 TVL 怎么统计的』、『分析某链生态协议排名』、『对比几条链的实际使用情况』、『XX 解决什么现实问题』、『刷新 XX 快照』、『之前研究过的链现在怎样』、『查一下 XX 的历史数据』。数据源：官网/官方文档 + 新闻 + DeFiLlama 公开 API/链页 + 本地快照库，多渠道交叉验证。"
---

# Token Research（代币/公链全面研究）

两段式研究 + 一次祛魅 + 本地快照库：**Step 0 先搞清「这是什么业务/产品/服务」**；**Step 1 先查本地快照库**（研究过的别重复拉）；然后按需抓链上数据验证「说得对不对、用得实不实」，用祛魅检验拷问「值不值得存在、有没有人付钱」。核心输出是「业务定性 + 真实使用数据 + 祛魅结论 + 最终结论」四段式报告。

## 工作流

按顺序执行。数据用**多渠道 + 页面/API 双路交叉验证**。

### Step 0 — 业务定性（官网探索 + 多渠道，先于一切数据）

**目的：回答「背后是什么业务、什么产品、什么服务」，再谈数据。**

1. 按 `references/业务定性清单.md` 的渠道顺序走：**官网首页 → 官方文档 → 官方博客/新闻 → 代币信息页（CMC/CoinGecko）→ GitHub → 第三方搜索**（每类默认 1 次，失败回退 1 次，不无限重试）。
2. 官网用 browser-use 打开（或 web_fetch），读 hero 定位语与产品线；文档优先读 `docs.<域名>` 或 `<域名>/llms.txt`（如果存在）——里面有产品分类与「谁在用」。
3. 产出「业务定性卡片」：是什么 / 解决谁的问题 / 谁在用 / 怎么赚钱 / 现状里程碑 / 信息来源（标注官方宣称 or 已核实）/ 信息缺口。
4. **硬规则**：官网定位是「自称」，与链上数据冲突时以数据为准并显式标注「叙事 vs 实际」。

### Step 1 — 先查本地快照库（优先于一切联网拉取）

**目的：不重复拉已经拉过的数据；让结论可复现、可追溯。**

```bash
python scripts/defillama.py list                      # 研究过谁、最新快照多久前
python scripts/defillama.py show NEAR                 # 最新快照 + 已登记报告
python scripts/defillama.py history NEAR --limit 12   # 历史时间序列（可 --format csv）
```

判断规则（写死，不许临场发挥）：

| 情况 | 动作 |
|---|---|
| 对象不在库 | 直接 Step 2 的 `snapshot <name> --new` |
| 最近快照 < 24h，且用户没要求「最新」 | **用库内数据**，输出注明「来自快照库 <captured_at>」 |
| 最近快照 ≥ 24h，或用户说「刷新 / 现在怎样」 | `snapshot <name>` 刷新后再分析 |
| 快照里 `missing` 含 fees/stablecoins | 按缺失处理，**不再重试同一数据源** |

### Step 2 — 抓取并入库（替代手工裸拉）

```bash
python scripts/defillama.py snapshot NEAR --with-protocols   # 全套指标 + 生态 Top15，入库并打印与上一条的 diff
python scripts/defillama.py snapshot ENA --kind protocol     # 协议类代币：TVL 走 /protocol/<slug>
python scripts/defillama.py compare NEAR Bittensor           # 临时对比仍可裸拉（不入库）
python scripts/defillama.py refresh --stale-days 1           # 批量刷新全部过期对象
```

- 每个数据源独立降级：任一源 400/404 只写进 `source_status`，不会让整次快照失败。
- **`missing` 只记「该源适用于本对象、但请求失败」**；本类型根本不用的源记为
  `source_status: "not_applicable:<原因>"`。两者不可混——否则回填时无法区分
  「没抓到」和「不该抓」。
- **`diff` 里的 `pct` 是本库两次快照之间的变化，不是 DeFiLlama 页面的 `change_1d`**，两者不可混用。
- `--kind coin` 的币（如 QNT）只取价格，DeFi 指标标 `not_applicable:coin_kind`。
- `--dry-run` 完全不落盘（含 `--new` 新建对象）。

**协议类代币（`--kind protocol`）**：TVL 来自 `api.llama.fi/protocol/<slug>`，
一次拿到完整 TVL 历史序列 + `currentChainTvls` 分解。slug 自动解析：先按对象名直连，
400 时退回 `/protocols` 榜单按 symbol 匹配并取 `parentProtocolSlug`
（**ENA → ethena**，符号与协议名不一致的情况只有榜单里有映射）。
`status_map["protocol_slug"]` 写明解析途径（`direct` / `via_parent_slug` / `unresolved:*`）。

**市值 / FDV（TSR 的分子）**：DeFiLlama 全系端点都不返回市值，链/币类走
CoinGecko `coins/markets`，**本机直连被重置，必须设代理**：

```bash
TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808 python scripts/defillama.py snapshot NEAR
```

抓不到时协议类退回 `/protocol` 自带的 `mcap`（标 `ok:defillama_protocol_fallback`），
其余如实进 `missing`。来源记在 `extra.market_cap_source`，两种口径不要混用。

**数据质量校验（自动）**：费用日序列会做分布检验，均值/中位数 ≥20 倍、或 30% 以上
的日为 0 时自动告警，结果写进 `extra.fees_data_quality` 并出现在 `warnings` 里。

> 实测案例：ENA 近 365 天费用中位数 $841/天、均值 $652,484/天（**776 倍**），
> 前 5% 的日子贡献 43.5% 总量——这是 DeFiLlama adapter 的采集缺陷，
> `total1y` 不能当「年收入」直接引用。

### Step 2.5 — 代币持有结构（可选，但强烈建议做）

**目的：回答祛魅 Q1「谁真的在用」—— 是散户自然分布，还是少数地址控盘。**

DeFiLlama 完全没有这个维度。数据源是 Binance Web3 API：

```bash
# 需 Python>=3.10 + API key + 非美国出口 IP（见下）
TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808 \
  D:/develop/PYTHON3.12/python.exe scripts/fetch_binance.py holders ENA --limit 50 --save
```

**前置条件（缺一不可）**：

1. Python ≥3.10（技能默认 3.8 跑不了，本机用 `D:\develop\PYTHON3.12\python.exe`）
2. `pip install binance-web3-wallet`
3. 环境变量 `BINANCE_WEB3_API_KEY` / `BINANCE_WEB3_API_SECRET`
4. **出口 IP 必须是香港/新加坡/日本等**。美国 IP 一律 `40304 compliance restriction`
5. 需要代理时设 `TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808`

**怎么读这些数字**：

| 指标 | 含义 | 判读 |
|---|---|---|
| `top10_holding_pct` | 前10地址占比 | >50% = 高度集中；>80% = 极端控盘 |
| `smart_money_pct` | 聪明钱持仓 | 长期 <0.01% = 机构/专业资金不参与 |
| `fresh_wallet_pct` | 新钱包持仓 | 高 = 空投猎人在刷；低 = 无新增散户 |
| `bn_trader_count_7d` | 币安钱包 7 日交易者 | 与 holders 对比看换手活跃度 |
| `funding_label` | 资金来源标签 | 出现 `Binance`/`Upbit` = 交易所冷钱包代持，非个人控盘 |

**硬规则**：

- **大户 `avg_buy_price` 全为 0 时，不能解读为「零成本获利」** —— 多数是转入而非买入，脚本会自动 warning 标注。
- 前 10 占比高但带交易所标签（Binance/Upbit/OKX）的，是**代持**不是控盘，要分开算。
- 无标签的大额地址（各占 5-10%）通常是协议金库、做市商、机构托管 —— 需结合 Step 0 业务定性判断，不要直接写「庄家」。
- 该数据源失败（IP 被限/无 key）时**跳过本步**并在报告注明「未获取持有结构」，不得编造。

### Step 3 — 拉协议生态构成

```bash
python scripts/defillama.py snapshot NEAR --with-protocols --limit 15   # 入库版
python scripts/defillama.py protocols NEAR --limit 15                   # 只看看不入库
python scripts/defillama.py protocols Bittensor --all                   # --all 保留 CEX 查看噪声
```

- 默认 `category != 'CEX'` 过滤，避免 Binance/Gate 等充提支持污染榜单。
- 注意跨链聚合桥（如 Portal）TVL 为全链总锁仓，需从原生榜剔除或标注。
- `snapshot_protocols` 表存的 `chains_count` 字段就是「多链聚合」的直接判据。

### Step 4 — 链页补充指标（浏览器）

API 拿不到的指标（**Active Addresses / Inflows / Total Raised** 等）需打开 `https://defillama.com/chain/<name>` 读取。

用 browser-use 或 curl 读页面文本；TAO 这类 DeFi 薄链页面会缺失多项指标，缺失即如实报告「无数据」。页面值可用 `import` 以 `origin: "page"` 补录进库。

### Step 5 — 交叉验证与口径清洗

1. 页面 TVL ↔ `v2/chains` TVL 数量级核对一致（库内数据与页面实时值差 > 10% 要显式说明原因）。
2. 协议榜：剔除 category=CEX；跨链桥标注聚合口径。
3. 判断「锁仓 vs 活跃」：高 TVL 但无费用/活跃地址 → 定性为叙事锚定而非真实使用。
4. **业务 vs 数据对账**：Step 0 的「官方宣称」与 Step 2-4 的真实数据逐条对账——宣称「跨境结算」的链，DEX 数据稀薄并不矛盾，关键看结算量/稳定币/活跃地址；宣称「DeFi 生态」的链则看 TVL/费用/协议数。
5. **数据质量对账（必做）**：查 `snapshot.extra.fees_data_quality`。`verdict` 为
   `suspect:spiky` / `suspect:sparse` / `suspect:median_zero` 时，**不得直接引用
   `fees_1y` / `total1y` 当「年收入」**，必须在报告里写明中位数与均值的差距，
   必要时改用中位数×365 作保守口径，或直接标注「该指标不可用」。
   同时做一条内部一致性检查：`30d` 应与 `24h×30`、`7d×4` 同量级，差一个数量级就要回头查源。

   > 反面教材：ENA 的费用 adapter 返回的 365 天均值被少数尖峰日主导（776 倍），
   > 若不校验，年化收入会被高估三个数量级。

### Step 5.5 — 祛魅检验（Reality Check，必做，不可跳过）

**目的：验证「这项目值不值得存在、有没有人愿意付钱」，剥离一切叙事。**

按 `references/祛魅检验清单.md` 强制回答 4 问（每问必须有证据，找不到证据就写「无公开证据」）：

1. **谁真的在用？** 日活量级、用户画像、高频刚需还是叙事爱好者（举例：自称服务经济 Agent 的平台，若活跃事件全在体育/地缘、核心赛道区零活跃 → 叙事 vs 实际）。
2. **不用它用什么？** 2-3 个成熟替代品，成本/可靠性/易用性一句话对比。
3. **钱从哪来？** 谁付钱 → 付多少 → 收入模式是否闭环；无闭环 → 靠代币/融资/卖数据活着。
4. **消失测试**：明天消失谁受损？真问题还是发明的问题？叙事定价还是产品定价。

#### Q5 — 代币价值捕获（必做，见 `references/代币价值捕获路径.md`）

**协议赚钱 ≠ 代币赚钱。** 这是代币研究最容易漏、也最致命的一问。

对照六条路径判定「钱到底进没进持币者口袋」：

| 路径 | 强度 | 画饼信号 |
|---|---|---|
| 回购 / 销毁 | 中等偏弱 | 只在官网写「将回购」，链上无动作 |
| **费用分享 / 分红** | **较强** | 需要真实可持续收入；收益率恒定=靠增发 |
| **强制使用（Utility）** | **较强** | 做成「可选折扣券」=假 Utility |
| 质押 / 锁仓 | 中等 | 收益来自增发 → 本质稀释 |
| 治理权 | 弱到中等 | 参与率 <5%、前 10 占 80% → 摆设 |
| 纯叙事 / 投机 | 短期强长期弱 | 无基本面支撑 |

**输出必须分两行写**：

```
协议层：<real / unknown / 无业务>
代币层：<有捕获路径+强度 / 零捕获 / 纯叙事>
```

**硬规则**：

- 协议成功与代币成功**分开判定**，不许合并。
- 任何路径若只有官方宣称、无链上/合约/第三方可验证证据 → 记「画饼，待验证」，不计强度。
- **官方自己承认的空档是最强负面证据**（如「储备基金分配 0%」「Fee Switch 未激活」）。
- 不用「未来会开」加分成。判断当下，不判断路线图。

**硬规则**：祛魅结论与业务定性、链上数据同权输出；4 问中任何一问无证据，如实标注「无公开证据」，不得用叙事代替。

### Step 6 — 输出结构化结论（四段式）+ 登记报告

1. **业务定性**（Step 0 卡片，含信息缺口）。
2. **数据表格**：链级指标（TVL / 稳定币 / 费用 / DEX / 活跃地址 / 市值），生态构成（原生 vs 桥污染），必要时 ECharts 呈现（按 doubao-visualization 规范）。
3. **祛魅结论**（Step 5.5 四问判定：需求真实性 / 替代对比 / 付费闭环 / 消失测试）。
4. **最终结论**：业务定位 + 数据验证后的定性（叙事 vs 实际）+ 祛魅判定（真问题 or 发明的问题）+ 对研究/产品决策的含义（如：适不适合做链上监控产品、值不值得单独建采集器、值不值得花时间深入）。
   每条数据注明来源、抓取时间、口径警告（CEX/桥/价格失真）。

写完 Markdown 后**必须登记报告**，否则下次调研查不到结论：

```bash
python scripts/defillama.py report add NEAR \
  --path "币圈/区块链/区块链工具/token-research代币研究方法论/near-token-research-2026-11-01.md" \
  --date 2026-11-01 --verdict real \
  --one-liner "原生 DeFi 以流动性质押为主力，生态结构完整，有真实链上经济活动。" \
  --business-qual "分片公链，主打链抽象与意图结算。" \
  --verdict-note "..." \
  --demystify '{"q1_who_uses":"...","q2_alternatives":"...","q3_revenue":"...","q4_disappear":"..."}'
```

`--verdict` 取值：`real`（真需求有闭环）/ `narrative`（叙事定价）/ `mixed`（部分真实）/ `unknown`（证据不足）。

## 本地快照库（token_research.db）

**位置解析**（按优先级）：

1. `--db PATH`
2. 环境变量 `TOKEN_RESEARCH_DB`
3. **用户级指针文件** `~/.token_research_db`（首次建库后自动写入一行 DB 绝对路径）
4. 沿目录向上找到的已有 `token_research.db`
5. 回退：skill 同级的 `token_research.db`；**若该路径在 agent 安装目录下**
   （`.claude/skills`、`.user_skills`、`.agents`、Doubao/灵码 workspace 等），
   改落 `~/token_research.db`，来源标 `default:user_home`

技能会被复制到多处（知识库归档 / agent skills / .claude skills），**指针文件让所有副本共享同一个库**，不会各建一个。

> **为什么装在 agent 目录时要搬家**：那里的库在升级/卸载技能时会被删，而且每个副本
> 各自向上推理会得到不同路径，**静默分裂成多个库**（实测：`.claude/skills` 与
> `.doubao/.user_skills` 两副本各建一个，互不可见）。落主目录至少保证数据不丢。
> 已有库不受影响——只要文件存在就沿用，不搬家。
>
> **指针落盘时机**：首次建库（`init` 或任何会建表的命令）后指针仍为空时自动写入。
> `--db` 与环境变量属于「这一次」的显式指定，**不改写**指针。

```bash
python scripts/defillama.py db path       # 当前解析到哪个库、来源是什么
python scripts/defillama.py db pointer    # 查看指针文件；加 --db PATH 可改指向
```

**语义（关键）**：

- 快照**只追加**，不覆盖历史；同日多次刷新会产生多条（盘中数据变化是合法事实）。
- `NULL` 表示「该数据源明确不覆盖此对象」（如 Bittensor 无费用/稳定币）；`0` 表示「源返回了零」。**两者绝不能混**。
- `missing` 数组是权威标注；`source_status` 记录每类源的 `ok` / `missing:http400` 等。
- `origin` 区分 `api`（接口抓取）/ `page`（链页抄录）/ `import`（批量回填）。

**命令速查**：

```bash
python scripts/defillama.py init --seed              # 建库 + 灌入 NEAR/Bittensor/XRPL/Hyperliquid/QNT
python scripts/defillama.py db path                  # 确认 DB 落在哪
python scripts/defillama.py list --with-reports      # 对象 + 最新快照 + 最近报告
python scripts/defillama.py show <name> --with-protocols   # 快照详情（含 holder_structure）
python scripts/defillama.py history <name> --metric tvl_usd,fees_24h --since 2026-10-01
python scripts/defillama.py import <file.json> --dry-run
python scripts/defillama.py db backup --to <file>    # VACUUM INTO 热备份
```

**备份纪律**：DB 在 WAL 模式，`-wal`/`-shm` 必须与主文件一起移动，同步工具分文件复制会损坏库。跨设备请用 `db backup` 导出单文件，不要把三件套单独同步；`.gitignore` 已忽略 DB 与缓存。

## 口径与坑（重要，务必读）

详见 `references/defillama口径.md`，核心四点：

1. **CEX 噪声**：Binance/Gate 等因支持充提被计入链协议榜 → 按 `category != 'CEX'` 过滤。
2. **桥双重计算**：Portal 等聚合桥 TVL 是全链数据 → 从原生榜剔除或标注。
3. **锁仓 ≠ 活跃**：dTAO 占 TAO TVL 90% 但无费用无活跃 → 拆「TVL 构成 + Fees + Active Addresses」三件套。
4. **价格失真**：单日 ±10% 常是币价变动 → 结合 Inflows 判断。

**API 现状提醒（2026-11 实测）**：

- `v2/chains` **已不再返回** `change_1d/7d/30d`，涨跌幅由脚本用 `v2/historicalChainTvl` 自算，不要直接读 API 字段。
- `Bittensor` 已从 `v2/chains` 下架（只剩 `Bittensor EVM` $0.03M），脚本会标 `missing:not_in_v2_chains` 并如实报无 TVL，**不推算、不用协议聚合值替代**。
- 稳定币/费用/DEX 三个端点的链名用 `v2/chains` 体系（Near / XRPL / Hyperliquid L1），不是 `Ripple`。
- `/protocol/<slug>` **大小写敏感**（`ENA`→400，`ethena`→ok），且 slug 常与代币符号不一致，必须走自动解析。
- `/protocol/<slug>` 单次响应可达数十秒；读取阶段超时不是 `URLError` 子类，脚本已显式兜住（否则会崩掉整次快照）。
- **DeFiLlama 任何端点都不给市值**：`coins.llama.fi` 只有价格，`/protocol` 的 `mcap` 仅协议类有。
- 协议 slug 的 dexs / stablecoins 端点不适用（400/500），脚本标 `not_applicable:protocol_level_source`，不要当成抓取失败去重试。

## 参考文件

- `references/业务定性清单.md` — Step 0 必读：渠道清单、频率纪律、业务定性卡片模板。
- `references/defillama口径.md` — Step 2-5 必读：链名双体系、CEX/桥/锁仓口径坑。
- `references/祛魅检验清单.md` — Step 5.5 必读：4 问模板、替代品对比表、付费闭环表、消失测试判定标准。
- `references/代币价值捕获路径.md` — Step 5.5 Q5 必读：六条捕获路径对照表、每条路径的真/画饼判据、历史案例（ENA/PEPE/BEAM/QNT/NEAR）。

## 脚本说明

- `scripts/defillama.py`：网络层 + CLI。子命令 `chain / protocols / top / compare / init / db / snapshot / refresh / list / show / history / report / import`。
  数据源：`api.llama.fi`（链/协议/TVL/费用）+ `coins.llama.fi`（价格）+ `api.coingecko.com`（市值/FDV，需代理）。
- **解释器分工**：主脚本 `defillama.py` / `store.py` 跑 Python 3.8；`fetch_binance.py` 需
  ≥3.10。本机 `D:\develop\PYTHON3.12` 的 fastapi 环境坏了（不影响本技能，但别拿它建 venv 跑 Web 服务）。
- `scripts/store.py`：SQLite 持久化层（DDL 在模块 docstring），零网络依赖，可用 `:memory:` 离线自测。
- `scripts/fetch_binance.py`：Binance Web3 持有结构抓取（`holders` / `search`）。**需 Python≥3.10，独立于主脚本运行**；抓到的数据落入 `token_holders` / `holder_entries` 表，`show` 命令会带出 `holder_structure` 字段。

### Binance Web3 SDK 已知问题（v10.1.0）

以下 5 个 POST 接口内部硬编码 `request_body = {}`，无法传代币参数，调用必返回 `40001`：
`get_token_basic_info`、`get_token_price`、`get_token_trading_info`、`list_de_fi_protocols`、`get_de_fi_positions`。
**不要用它们**；价格用 DeFiLlama/CoinGecko，协议 TVL 用 DeFiLlama。

可用的 10 个 GET 接口：`get_supported_chains`、`search_token`、`get_token_advanced_info`、`get_holders_ranking`、`get_token_dev_info`、`get_rwa_token_list`、`get_rwa_token_price`、`get_hot_token_list`、`get_top_liquidity_pools`、`get_candles`、`get_top_traders`。
- `protocols` API 全量 ~8700 条、约 9MB —— 已内置本地缓存（默认 6 小时，`--no-cache` 绕过 / `--refresh-cache` 强制重拉），无需手工 curl。
- 所有命令输出 JSON 到 stdout；stdout/stderr 已统一为 UTF-8，中文结论不会乱码。

## 硬规则

- 每个数字必须有来源（官网/文档/页面快照或 API 返回），并在输出注明抓取时间。
- 官网定位语是「官方宣称」，链上数据是「已核实事实」，两者冲突必须显式标注，不得混写。
- 缺失指标如实报「无数据」，不得编造或按零值处理。
- 登录墙/验证码渠道：停止并如实报告，不绕过。
- 多渠道每类默认 1 次（失败回退 1 次），不无限重试。
- 图表数据必须与正文表格同源，口径一致（单位、时间范围、是否含 CEX）。
- **祛魅必做**：所有调研必须完成 Step 5.5 四问；任一问无证据如实标「无公开证据」，禁止用官网叙事填空；祛魅结论不得被项目宣传话术覆盖。
- **库优先**：报告中的链上数字优先取自快照库（可复现、可比）；只有库内缺失时才用实时页面，并注明「实时页面，未入库」。
- **调研必登记**：每次调研结束必须 `report add` 登记报告路径 + 一句话结论 + 业务定性 + `verdict`。
