# Jobstock

**把招聘信息和投递进度，安安静静放在你自己电脑上的求职管理工具。**

不用注册、不上传简历、不联网也能用。每条岗位是一个可编辑的 JSON 文件，页面上点几下就能记进度；改坏了有历史版本可回退，重要节点一键打包备份。

- 纯 Python 标准库，**零依赖**（不要 npm、不要虚拟环境也行）
- Windows / macOS 通用，双击即可启动
- 数据分层清晰：岗位是客观信息，投递状态只属于你本机
- 可配合 Claude Code / Cursor / MiMo 等 AI Agent 自动搜岗、写岗位

---

## 三分钟上手

### 1. 安装（只需 Python 3.8+）

```bash
git clone https://github.com/Ychris12138/Jobstock.git
cd Jobstock
python install.py          # macOS 若无 python 命令则用 python3 install.py
```

安装时可以一路回车：数据和 CV 默认放在工具目录里。也可以指定到别的磁盘，例如：

```bash
python install.py --data-dir "D:\jobs-data" --cv-dir "~/Documents/my-cv"
```

### 2. 启动

| 系统 | 方式 |
|---|---|
| macOS | 双击 `start.command` |
| Windows | 双击 `start.bat` |
| 通用 | 终端执行 `python server.py` |

浏览器会自动打开（默认 <http://localhost:8770>）。端口被占用会自动换端口；已经开着一个实例时再启动，只会帮你打开页面，不会双开写坏数据。

### 3. 开始用

1. 点 **「＋ 新增岗位」** 录入第一条（公司、岗位必填）
2. 在列表里点行，改 **投递状态**、写个人备注
3. 需要时点 **「⇩ 全量备份」**，把 zip 存到 U 盘或网盘

仓库里自带 2 条**虚构示例岗位**（示例科技 / 虚构云），熟悉界面后可直接删掉。

---

## 你现在能做什么

| 能力 | 说明 |
|---|---|
| 岗位库 | 公司、岗位、职位号、分类、校招/社招/实习、多城市、薪资、链接、截止日期、标签、JD 快照 |
| 投递进度 | 待投递 → 已投递 → 笔试 → 面试 → Offer / 已拒绝 / 已归档；时间线自动记录 |
| 筛选搜索 | 分类、类型、城市、状态、公司、来源、标签（多选 AND）、全文关键词（含 JD） |
| 截止提醒 | 顶部汇总 7 天内截止与已过期；可复制 Markdown，或导出 `.ics` 进系统日历 |
| CV 匹配度 | 把 `cv/` 里的解读关键词与岗位文本比对，列表显示 `6/10`，悬停看命中词 |
| 后悔药 | 岗位每次保存前自动留 3 份历史；个人状态写前留 10 份快照 |
| 去重合并 | 同职位号/同链接自动拦下；重复岗位可一键合并，被合并的还能恢复 |
| 全量备份 | 一个 zip 带走岗位 + 历史 + 投递状态（不含 CV 原文，更安全） |
| AI 协作 | 内置初始化提示词、全网搜岗提示词、CV 解读提示词，复制给 Agent 即可 |

---

## 和 AI Agent 一起用（推荐）

WebUI 岗位页顶部有 **「🚀 用 AI Agent 自动化初始化」**（空库时更显眼）：

1. 用 Claude Code / Cursor / MiMo / Codex **打开本工具目录**
2. 点「复制初始化提示词」，粘贴给 Agent
3. Agent 会：读懂工具约定 → 检查/生成 CV 解读 → **请你确认**方向与关键词  
4. 你同意后，它再按内置搜岗提示词**尽量多**地搜索匹配岗位并写入 `jobs/`
5. 最后自动 `reindex`，你到网页里看匹配度、标投递状态

「CV 与解读」页也可以单独复制：

- **CV 解读提示词**：生成固定格式的 `cv/<名字>.reading.md`
- **全网搜岗提示词**：多渠道、多关键词组合，宁多勿漏

> 搜岗结果只是「客观岗位情报」。你的投递进度、个人备注始终只在本机 `local/`，不会被写进岗位 JSON。

---

## 数据放在哪（很重要）

```
jobs/<id>.json      岗位层：招聘信息（可共享的客观内容）
                    每次覆盖前 → jobs/.history/<id>/ 留最近 3 份
local/status.json   个人层：投递状态 + 个人备注 + 时间线（只在本机）
                    每次写入前 → local/backups/ 留最近 10 份
data/jobs.db        索引层：派生的 sqlite，随时可重建，不是真相源
cv/                 你的简历与解读（最敏感，不进 git）
config.json         本机配置（数据目录、分类、署名等）
```

**所有个人数据默认不进 git。** 换电脑：用「⇩ 全量备份」+ 手动拷 `cv/`。

| | 岗位层 `jobs/*.json` | 个人层 `local/status.json` |
|---|---|---|
| 写什么 | 公司 / 岗位 / 链接 / JD / 截止… | 我投到哪一步、我的备注、时间线 |
| 谁维护 | 你（或 AI 按约定写文件） | 只在你这台机器 |

### 维度别混用

| 维度 | 字段 | 例子 |
|---|---|---|
| 工作地点 | `locations[]` | 北京、上海、远程（多地全列） |
| 招聘类型 | `recruit_type` | 校招 / 社招 / 实习 |
| 岗位分类 | `category` | 可在 `config.json` 的 `categories` 里定制 |
| 主题标签 | `tags[]` | AIGC、2027届…（多选是「同时具备」） |

分类示例：安装时可填 `AI产品,增长,设计`；不配置则用默认通用列表。

### 已下架 ≠ 已归档

- **下架 `closed`**：投递入口关了（客观事实，岗位层）
- **已归档**：你自己选择不投了（个人状态，默认从列表隐藏）

---

## 常用操作

**筛选**  
第一行是高频条件；「更多筛选」里有公司 / 来源 / 截止 ≤ / 匹配 ≥ 等。折叠起来的条件**仍然生效**，按钮上会标数量。  
同一维度多选是 OR，不同维度之间是 AND；多个标签是 AND。

**录入方式**

1. 网页点「＋ 新增岗位」
2. 让 AI 按 `AGENTS.md` 写 `jobs/*.json`，再点「↻ 重建索引」

**尽量填官方职位号 `job_no`**  
这是去重主键。有职位号时，同一个岗录多次会被直接拦下。

**自定义分类**（`config.json`）

```json
{
  "categories": ["AI产品", "算法", "设计", "运营", "其他"],
  "my_name": "你的名字"
}
```

改完重启 server 生效。枚举外的旧数据会**保留并告警**，不会被静默清掉。

---

## API（给进阶用户 / Agent）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs` | 列表 + 筛选 facets + 截止统计 + CV 关键词 |
| GET | `/api/jobs/<id>` | 单条（含时间线、匹配词） |
| POST | `/api/jobs` | 新增 |
| PUT | `/api/jobs/<id>` | 修改（需 `base_rev` 乐观锁） |
| POST | `/api/jobs/<id>/status` | 只改本机投递状态 |
| POST | `/api/reindex` | 重建索引，返回 skipped / warnings / duplicates |
| POST | `/api/dedupe` | 合并强信号重复岗 |
| GET | `/api/backup` | 全量备份 zip |
| GET | `/api/cv` | CV 文件与解读 |
| GET | `/api/prompts` | 初始化 / 搜岗 / CV 解读三套提示词 |

导出 CSV：

```bash
sqlite3 -header -csv data/jobs.db \
  "SELECT company, position, url, deadline FROM jobs" > jobs.csv
```

---

## 自测

```bash
python3 test_server.py
```

全部断言在临时目录跑，不碰你的真实数据。覆盖分层、历史版本、去重合并、筛选语义、截止提醒、CV 匹配、入口安全等。

---

## 给 AI 的约定

完整协议见 [`AGENTS.md`](AGENTS.md)。摘要：

- 只写岗位层字段，**不要**把 `status` / `my_notes` / `history` 写进 `jobs/*.json`
- 改完必须 `python server.py --reindex`，并检查报告
- 城市进 `locations`，招聘类型进 `recruit_type`，不要塞进 `tags`
- 不删除文件；岗位没了用 `closed: true`，自己不投用「已归档」
- `jobs/.history/` 不要手改、不要清空

---

## 版本

当前：**v0.0.1**

隐私默认本地；若你公开分享本仓库，请确认 `jobs/`、`local/`、`cv/`、`config.json` 未被强制加入 git。
