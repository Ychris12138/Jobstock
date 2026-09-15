# Jobstock

**一个为中文求职场景设计的本地求职 Dashboard。**

把岗位、截止日期、投递进度、面试状态和自己的备注放在一个页面里管理。**不注册账号、不上传简历、不依赖云服务**，数据默认只保存在你的电脑上。

> 适合：校招 / 实习 / 社招，同时投很多公司、需要长期整理岗位的人。

- 🖥️ **本地优先**：岗位、投递状态、CV 都留在本机
- ⚡ **零依赖**：Python 标准库 + 原生 HTML/JS，不需要 npm / pip install
- 🍎🪟 **Windows / macOS 可用**：安装后可双击启动
- 📋 **一页管理求职流程**：待投递 → 已投递 → 笔试 → 面试 → Offer
- ⏰ **截止日期提醒**：支持 Markdown 复制和 `.ics` 日历导出
- 🤖 **Agent 友好**：Claude Code / Codex / Cursor 等可直接帮你初始化、搜岗和写入岗位
- 🧯 **有后悔药**：岗位历史版本、个人状态快照、全量备份

---

## 60 秒开始

你只需要 **Python 3.8+**。

### 普通用户

```bash
git clone https://github.com/Ychris12138/Jobstock.git
cd Jobstock
python install.py
```

macOS 如果没有 `python` 命令：

```bash
python3 install.py
```

安装时不知道怎么选，**一路回车即可**。

安装完成后：

| 系统 | 启动方式 |
|---|---|
| Windows | 双击 `start.bat` |
| macOS | 双击 `start.command` |
| 通用 | `python server.py` / `python3 server.py` |

浏览器会自动打开本地页面，默认地址：<http://localhost:8770>。

---

## 🤖 让 Agent 帮你安装（推荐）

如果你已经在用 Claude Code、Codex、Cursor 或其他 Coding Agent，**不用自己研究安装步骤**。

把下面这段直接发给 Agent：

```text
请帮我安装并初始化 Jobstock：
https://github.com/Ychris12138/Jobstock

要求：
1. clone 到合适的本地目录；
2. 先阅读 README.md 和 AGENTS.md；
3. 使用默认配置完成安装；
4. 运行 test_server.py，确认测试通过；
5. 启动本地 WebUI；
6. 不上传、提交或同步我的 CV、岗位数据、投递状态和 config.json；
7. 完成后告诉我本地目录和启动方式。
```

### Agent 快捷命令

macOS / Linux：

```bash
git clone https://github.com/Ychris12138/Jobstock.git && cd Jobstock && python3 install.py --yes && python3 test_server.py
```

Windows PowerShell / CMD（有 `python` 时）：

```bash
git clone https://github.com/Ychris12138/Jobstock.git && cd Jobstock && python install.py --yes && python test_server.py
```

安装完后运行：

```bash
python server.py
```

> Agent 进入仓库后应先读 [`AGENTS.md`](AGENTS.md)。那里写清楚了哪些数据可以改、哪些个人数据绝对不要碰。

---

## 第一次打开后做什么？

最简单的用法只有三步：

1. 点 **「＋ 新增岗位」**，录入公司和岗位；
2. 投递后更新 **投递状态**，写自己的备注；
3. 每隔一段时间点 **「⇩ 全量备份」**。

如果你已经有 CV，可以把文件放进 `cv/`，然后在页面的 **「CV 与解读」** 中使用内置提示词，让 Agent 帮你生成关键词解读和搜岗计划。

---

## Jobstock 能做什么？

| 功能 | 你实际会用到的东西 |
|---|---|
| 岗位库 | 公司、岗位、职位号、城市、薪资、链接、截止日期、JD、标签 |
| 投递管理 | 待投递 / 已投递 / 笔试 / 面试 / Offer / 已拒绝 / 已归档 |
| 搜索筛选 | 公司、分类、城市、招聘类型、状态、来源、标签、全文关键词 |
| 截止提醒 | 7 天内截止、已过期、复制 Markdown、导出 `.ics` 日历 |
| CV 匹配 | 根据 `cv/` 中的关键词解读显示简单匹配度 |
| 去重 | 同职位号 / 同链接拦截，重复岗位可合并 |
| 历史版本 | 岗位保留最近 3 份历史；个人状态保留最近 10 份快照 |
| 本地备份 | 一键导出 zip，方便换电脑或重要操作前备份 |
| Agent 协作 | 内置初始化、CV 解读、全网搜岗提示词 |

---

## 为什么它适合和 AI Agent 一起用？

很多求职工具把 AI 做成一个聊天框；Jobstock 更希望 **Agent 直接维护你的求职资料库**。

你可以让 Agent：

- 根据你的 CV 和求职方向批量搜索岗位；
- 按统一字段整理公司、岗位、JD、城市、截止日期；
- 自动去重；
- 更新 `jobs/*.json` 后重建索引；
- 帮你找出快截止但还没投的岗位。

同时，**岗位信息和个人投递状态是分开的**：Agent 可以整理岗位情报，但不会因为批量改岗位而覆盖你的个人备注和投递时间线。

WebUI 中已经内置三套可复制提示词：

- **初始化提示词**：让 Agent 读懂项目并准备你的求职工作区
- **CV 解读提示词**：把简历整理成固定格式的关键词解读
- **全网搜岗提示词**：根据方向尽量多地收集候选岗位

---

## 数据真的只在本地吗？

默认是。

```text
jobs/<id>.json      岗位信息
local/status.json   你的投递状态、备注和时间线
data/jobs.db        可随时重建的 SQLite 索引
cv/                 CV 与解读
config.json         本机配置
```

这些个人目录默认都已经写进 `.gitignore`：

- `jobs/`
- `local/`
- `cv/`（仅保留说明文件）
- `config.json`
- SQLite 索引

因此正常使用时，不会因为你 `git add .` 就把 CV 或投递记录直接提交出去。

不过仍建议：**公开 fork / 修改仓库前，先运行 `git status` 再确认一次。**

### 岗位信息和个人状态为什么分开？

| | 岗位层 | 个人层 |
|---|---|---|
| 内容 | 公司、岗位、JD、链接、截止日期 | 投递到哪一步、个人备注、时间线 |
| 位置 | `jobs/*.json` | `local/status.json` |
| Agent 可批量维护 | ✅ | 默认不应该 |

这也是 Jobstock 最重要的数据设计之一。

---

## 数据目录可以放到别的地方

默认数据就在 Jobstock 目录中。如果你想把数据单独放在文档盘或移动硬盘：

```bash
python install.py --data-dir "D:\jobs-data" --cv-dir "~/Documents/my-cv"
```

配置会写进本机的 `config.json`。

---

## 一些有用的小功能

### 截止日期 ≠ 投递状态

岗位截止日期是客观信息；你是否还打算投，是个人状态。

- 岗位停止招聘：`closed`
- 你自己决定不投：`已归档`

两者不会混在一起。

### 尽量填写官方职位号

`job_no` 是最可靠的去重信号。没有职位号时才依赖链接和其他字段判断重复。

### 分类可以自定义

安装时可以直接填写，也可以修改 `config.json`：

```json
{
  "categories": ["AI产品", "算法", "数据", "研究", "其他"],
  "my_name": "你的名字"
}
```

---

<details>
<summary><strong>给 Agent / 开发者：数据约定</strong></summary>

完整规则见 [`AGENTS.md`](AGENTS.md)。最重要的几条：

- Agent 写岗位时只修改岗位层字段；
- 不要把 `status` / `my_notes` / `history` 写进岗位 JSON；
- 修改岗位文件后运行 `python server.py --reindex`；
- 城市放 `locations[]`，招聘类型放 `recruit_type`，不要都塞进 tags；
- 不直接修改 SQLite；
- 不手改或清空 `jobs/.history/`；
- 删除文件前先征得用户同意。

</details>

<details>
<summary><strong>API</strong></summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs` | 岗位列表、筛选项、截止统计、CV 关键词 |
| GET | `/api/jobs/<id>` | 单条岗位 |
| POST | `/api/jobs` | 新增岗位 |
| PUT | `/api/jobs/<id>` | 修改岗位（使用 `base_rev` 乐观锁） |
| POST | `/api/jobs/<id>/status` | 修改本机投递状态 |
| POST | `/api/reindex` | 重建索引 |
| POST | `/api/dedupe` | 合并强信号重复岗位 |
| GET | `/api/backup` | 导出全量备份 zip |
| GET | `/api/cv` | CV 文件与解读 |
| GET | `/api/prompts` | 初始化 / 搜岗 / CV 解读提示词 |

</details>

<details>
<summary><strong>开发 / 自测</strong></summary>

```bash
python test_server.py
```

测试使用临时目录，不会碰你的真实岗位和投递数据。CI 会在 Ubuntu 和 Windows 上自动跑同一套测试。

项目坚持 **零第三方运行时依赖**：Python 标准库 + 原生 JavaScript，不需要 npm 构建。

</details>

---

## 当前版本

**v0.0.1**

Jobstock 仍处于早期版本。如果你遇到安装、数据迁移、岗位格式或 Agent 协作问题，欢迎提交 Issue。
