# token-research

一套面向代币与公链的**可复现研究工作流**：先搞清业务，再抓链上数据，然后强制祛魅，最后落库留痕。

数据源是 DeFiLlama、CoinGecko、Binance Web3，全部为公开 API，不需要付费订阅。

```
skill/   Claude Code 技能定义 + 抓取脚本 + 方法论参考
ui/      快照库的只读浏览界面（FastAPI + 原生前端）
```

---

## 这个项目解决什么问题

大多数「代币研究」停在两件事上：读官网叙事，抄数据面板。两者都不回答最关键的问题——

> 这个项目**值不值得存在**，它的**代币**有没有真的拿到钱。

所以这套流程把研究拆成六步，每步都有强制的产出物，缺一步就不算完成：

| 步骤 | 做什么 | 强制产出 |
|---|---|---|
| **0 业务定性** | 官网 / 文档 / 代币页 / GitHub 多渠道 | 业务定性卡片（含「信息缺口」） |
| **1 查本地库** | 研究过的不重复拉 | — |
| **2 抓取入库** | 链级或协议级指标，逐源降级 | 快照 + `missing` 清单 |
| **2.5 持有结构** | 谁在拿筹码 | 集中度 + 大户地址身份 |
| **3-4 生态 / 链页** | 协议构成、页面补充指标 | — |
| **5 交叉验证** | 口径清洗 + 数据质量对账 | — |
| **5.5 祛魅** | 四问 + 代币价值捕获 | **必须有结论，不许叙事填空** |
| **6 输出登记** | 四段式报告 + `report add` | 报告路径进库 |

### 三条硬规则

1. **`missing` ≠ `not_applicable`**
   `missing` 只记「本数据源适用于该对象、但请求失败」；本类型根本不用的源记 `not_applicable:<原因>`。
   两者混在一起，回填时就分不清「没抓到」和「不该抓」。

2. **`NULL` ≠ `0`**
   `NULL` 是「数据源明确不覆盖」（如 Bittensor 没有费用），`0` 是「源返回了零」。绝不能合并。

3. **协议赚钱 ≠ 代币赚钱**
   Step 5.5 的 Q5 强制分两行写：`协议层：…` / `代币层：…`。任何捕获路径若只有官方宣称、
   没有链上或第三方可验证证据，记「画饼，待验证」，不计强度。

---

## 快速开始

### 技能（skill/）

把 `skill/` 整个目录复制到你的 skills 目录，即可在 Claude Code 里用 `/token-research` 触发。

```bash
cp -r skill ~/.claude/skills/token-research
```

首次建库并灌入种子数据：

```bash
python skill/scripts/defillama.py init --seed
```

跑一次研究：

```bash
# 公链（链级指标 + 生态 Top15）
python skill/scripts/defillama.py snapshot NEAR --with-protocols

# 协议类代币（走 /protocol/<slug>，自动解析 slug）
python skill/scripts/defillama.py snapshot ENA --kind protocol

# 抓不到市值时加代理（CoinGecko 在国内直连会被重置）
TOKEN_RESEARCH_PROXY=http://127.0.0.1:10808 \
  python skill/scripts/defillama.py snapshot NEAR
```

查库、看历史、登记报告：

```bash
python skill/scripts/defillama.py list --with-reports
python skill/scripts/defillama.py show NEAR
python skill/scripts/defillama.py history NEAR --metric tvl_usd,fees_24h --limit 12
python skill/scripts/defillama.py report add NEAR --path ... --verdict real --one-liner ...
```

### 界面（ui/）

```bash
cd ui && pip install fastapi uvicorn markdown
python server/app.py          # http://127.0.0.1:8020/
```

Windows 下双击 `ui/启动.bat` 会自动挑一个能构造 FastAPI 的解释器再启动。

**界面只读**，不写库。新增快照和登记报告走 CLI。

---

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| `defillama.py` / `store.py` | Python **3.8+** | 主脚本，零第三方依赖 |
| `fetch_binance.py` | Python **3.10+** + `pip install binance-web3-wallet` | 持有结构，独立运行 |
| `ui/server/app.py` | Python 3.10+ + `fastapi` `uvicorn` | 可选 |

`fetch_binance.py` 还需要：

- 环境变量 `BINANCE_WEB3_API_KEY` / `BINANCE_WEB3_API_SECRET`
- 出口 IP **不能是美国**（会返回 `40304 compliance restriction`），香港 / 新加坡可用

抓不到就跳过这一步并在报告里写「未获取持有结构」，**不要编造**。

---

## 已知的坑

这些都是实测踩出来的，写在这里省下一次重踩：

- **DeFiLlama 任何端点都不给市值**。`coins.llama.fi` 只有价格；`/protocol` 的 `mcap` 仅协议类有。
  链和币的市值必须走 CoinGecko `coins/markets`。
- **`/protocol/<slug>` 大小写敏感，且 slug 常与代币符号不一致**（`ENA` → 400，`ethena` → ok）。
  脚本会先直连，失败再退回 `/protocols` 榜单按 symbol 匹配并取 `parentProtocolSlug`。
- **`/protocol/<slug>` 单次响应可达数十秒**，读取阶段的 `socket.timeout` 不是 `URLError` 子类，
  不显式捕获会崩掉整次快照。
- **TVL 的按链分解有重复字段**。`staking` 与 `Ethereum-staking` 完全相等，`pool2` 另有链级变体。
  全部相加会虚高（实测 Pendle 虚高 34%），必须只对原生链字段求和。
- **`fees_1y` 不能直接当年收入**。脚本会自动做分布检验，均值/中位数 ≥20 倍或 30% 以上为 0 日时
  标记 `suspect:*`，此时必须改用中位数或 30 天年化。
- **前十大持币占比高 ≠ 庄家控盘**。必须先核验地址身份：锁仓合约、质押合约、多签、交易所代持
  都要单独剔除，剩下的才是无法解释的持仓。
- **Binance Web3 SDK v10.1.0 的坑**：`proxy` 必须传 dict 不能传 URL 字符串；参数必须用关键字；
  `recv_window` 传 int 会触发 header 类型错误，要传字符串。`get_token_price` /
  `get_token_trading_info` / `list_de_fi_protocols` / `get_de_fi_positions` 因接口设计与封装不匹配而不可用。

更细的口径说明见 `skill/references/defillama口径.md`。

---

## 本地快照库

默认 SQLite，位置按优先级解析：

1. `--db PATH`
2. 环境变量 `TOKEN_RESEARCH_DB`
3. 用户级指针文件 `~/.token_research_db`
4. 沿目录向上找到的已有 `token_research.db`
5. 回退到 skill 同级的 `token_research.db`；**若该位置在 agent 安装目录下**
   （`.claude/skills`、`.user_skills`、`.agents`、Doubao/灵码 的 workspace 等），
   改落用户主目录 `~/token_research.db`，来源标 `default:user_home`

装到 agent 目录时为什么要搬家：那里的库会在升级/卸载技能时被删，而且每个副本各自向上推理
会得到不同路径，**静默分裂成多个库**。落主目录至少保证数据不丢。

**指针自动落盘**：首次建库（`init` 或任意会建表的命令）后，如果指针文件还不存在，
脚本会自动写入当前路径，之后任何副本都解析到同一个库。`--db` 与环境变量属于
「这一次」的显式指定，**不会**改写指针。

```bash
python skill/scripts/defillama.py db path       # 看解析到哪、来源是什么
python skill/scripts/defillama.py db pointer    # 看指针；--db PATH 可改指向
```

**备份**：库在 WAL 模式，`-wal` / `-shm` 必须与主文件一起移动。跨设备请用
`defillama.py db backup --to <file>` 导出单文件，不要分文件复制。

数据库与缓存已在 `.gitignore` 中排除。

---

## 免责声明

本项目的输出是研究流程与数据整理结果，**不构成投资建议**。所有数字都标注了来源与抓取时间，
但第三方数据源本身可能有采集缺陷——报告里的「口径警告」就是为了让这一点可见。

## License

MIT
