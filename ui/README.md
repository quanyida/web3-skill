# token-research-ui

token-research 快照库的只读浏览界面。后端是一个 FastAPI 只读 API，前端是 `web/` 下的静态页
（原生 HTML/CSS/JS，无构建步骤）。

**它不写库。** 要新增快照或登记报告，用 token-research 技能自带的 CLI。

---

## 快速开始

双击 **`启动.bat`**。

脚本会依次检查 Python、`fastapi`/`uvicorn`（缺则安装）、`markdown`（可选），
然后启动服务并打印地址。默认 `http://127.0.0.1:8020/`。

**不要直接双击 `web/index.html`** —— 页面上的每个数字都是通过 `fetch` 取回来的，
`file://` 协议下接口调不通，页面只会显示「连不上接口」。

### 手动启动

```bash
pip install fastapi uvicorn markdown
python server/app.py                              # 默认 127.0.0.1:8020
python server/app.py --port 8021 --host 0.0.0.0   # 换端口 / 对外
python server/app.py --db path/to/token_research.db
```

---

## 目录结构

```
token-research-ui/
├── 启动.bat            Windows 一键启动
├── server/
│   └── app.py          只读 JSON API + 静态托管 web/
└── web/
    ├── index.html      页面骨架（icon rail + 概览 + 代币表 + 报告 + 方法）
    ├── app.css         data-terminal 基础样式表
    ├── ui.css          本项目的配色与组件层（覆盖 app.css 的变量）
    ├── charts.js       SVG 图表（TSR 排名条形图、迷你走势）
    ├── api.js          唯一的网络层，其余脚本只调 window.API
    └── ui.js           渲染逻辑：KPI、表、详情面板、报告阅读器
```

---

## 依赖：技能里的 `store.py`

`server/app.py` 不复制任何 SQLite 代码，直接 `import store`，与技能的 CLI 共用同一份
持久化层和同一套 DB 路径解析规则。所以 **`web/` 和 `server/` 必须留在
`token-research-ui/` 下**，否则 `app.py` 找不到技能目录会直接退出。

查找顺序（命中即停）：

1. `<ui>/../../.claude/skills/token-research/scripts/store.py`
2. `<ui>/../../币圈/区块链/区块链工具/token-research代币研究方法论/skill/scripts/store.py`

都找不到时，用 `PYTHONPATH` 指向技能的 `scripts/` 目录。

---

## DB 定位

解析顺序（与 `defillama.py` 一致）：

```
--db PATH > $TOKEN_RESEARCH_DB > ~/.token_research_db 指针文件 > 向上查找 > 技能同级目录
```

当前机器解析到指针文件：

```
~/.token_research_db  →  F:\个人知识库\币圈\区块链\区块链工具\token-research代币研究方法论\token_research.db
```

`/api/health` 会回显实际使用的路径（`db`）和来源（`db_source`），排查时先看它。
如果 DB 落在 agent skills 目录下，启动日志会打一条 `[warn]` 建议改用 `TOKEN_RESEARCH_DB`。

---

## API

全部 `GET`，无写接口。交互文档在 `/docs`。

| 路径 | 说明 |
|---|---|
| `/api/health` | 服务与库状态：DB 路径、来源、`user_version`、各表行数 |
| `/api/stats` | 首页 KPI：总数 / 已判定 / 叙事 / 混合 / 真实 / 有持有结构 / TVL 合计 / 最近抓取日 |
| `/api/objects` | 对象列表。`?kind=chain\|coin\|protocol` `?stale_days=N` `?with_reports=1` `?sort=recent\|tvl\|name` `?limit=N`。每条附带 `tvl_usd` / `market_cap_usd` / `tsr` / `symbol` / `holders` / `top10_pct` |
| `/api/objects/{name}` | 单对象全貌：快照 + 生态明细 + 持有结构 + 报告 + 时间序列。`?at=<id\|YYYY-MM-DD>` `?history=N` `?with_protocols=0` |
| `/api/objects/{name}/history` | 时间序列。`?metric=tvl_usd,fees_24h` `?limit=N` `?since=YYYY-MM-DD` |
| `/api/reports` | 已登记报告。`?name=<对象>` `?limit=N` |
| `/api/reports/{id}` | 单份报告，含渲染后的 HTML 正文（markdown 缺失时 `html` 为 `null`，前端降级为原文） |
| `/api/reports/{id}/raw` | 报告原文，`text/markdown` |

### 两个口径约定

- **TSR = TVL ÷ 市值**，由后端算好放进 `tsr` 字段。前端不自己算，避免两处口径漂移。
- **代码不猜**：`symbol` 只在快照 `extra.price_symbol` 里记了才返回，否则 `null`，
  页面渲染成灰色破折号而不是空白。

---

## 报告正文的路径解析

DB 里 `reports.report_path` 是**相对知识库根**的路径。知识库根按下列顺序确定：

```
$TOKEN_RESEARCH_ROOT > 向上找到 .git 或 CLAUDE.md 的目录
```

解析时会做 `realpath` 越界检查，越界或文件不存在时 `path_ok=false`、`html=null`，
不会泄露服务器真实路径。

---

## 常见问题

**页面显示「连不上接口」**
服务没跑，或你直接双击了 `index.html`。先用 `启动.bat` 或 `python server/app.py` 起服务。

**启动即退出，提示找不到 `store.py`**
`server/` 被移出了 `token-research-ui/`，或技能目录不在上面两个候选位置。
设 `PYTHONPATH` 指向技能的 `scripts/`。

**报告打开是纯文本**
没装 `python-markdown`。`pip install markdown` 后重启。

**改了 `web/` 下的文件但页面没变**
服务把 `web/` 静态托管在 `/`，改完直接刷新即可；如果浏览器缓存了，Ctrl+F5。
`--reload` 只对 Python 代码生效。

**配色不对（整页发黑、边框发灰）**
`app.css` 在 `:root[data-theme="dark"]`（特异性 0,1,1）上声明了它自己的深色调色板，
会压过 `ui.css` 的普通 `:root`（0,1,0）。`ui.css` 里为此把选择器写成了
`:root, :root[data-theme="dark"]`。改配色时别把这个选择器退化回单个 `:root`。

---

## 相关

- 技能本体（SOP + CLI）：`.claude/skills/token-research/`
- 数据库：`币圈/区块链/区块链工具/token-research代币研究方法论/token_research.db`
