# AGENTS.md —— job-stock AI 协作协议

> 任何 AI agent 进入本目录工作前，**先读本文件**。
> job-stock 是零依赖的多人共建求职仓库：**招聘信息共享，投递进度私有**。
> 纯 Python 标准库后端 + 原生 JS 前端，无 npm、无构建，Windows / macOS 通用。
> 用户可读的完整文档在 `README.md`，本文件只写 agent 必须遵守的部分。

## 1. 数据分三层（动手前必须理解）

```
jobs/<id>.json     共享层 —— 招聘信息本身，进 git（本仓库公开，别写入任何个人信息）
local/status.json  个人层 —— 投递状态 + 个人备注 + 时间线，不进 git，只留本机
data/jobs.db       索引层 —— 派生物，随时可 DROP 重建，永远不是真相源
cv/                CV 与解读文件 —— 不进 git（个人信息），只保留 cv/README.md
```

**agent 写岗位数据时只碰共享层的字段**（见 `schema/job.schema.json`）：
`company / position / job_no / category / recruit_type / url / locations[] / salary /
source / deadline / tags[] / notes / jd / closed`（`created_by` / `updated_by` 由
server 在写入时自动注入，不用手写）。
**不要写 `status` / `my_notes` / `history`** —— 它们是个人层，写进共享 JSON 会在
启动时被自动搬走，还会产生本不该存在的 git diff。

数据目录可能被 `config.json` 指到仓库外，先确认实际位置：

```bash
python server.py --reindex    # 输出里打印 jobs 目录路径，顺带重建索引
```

## 2. 写数据的正确姿势

**改 JSON → 重建索引 → 看报告**，三步缺一不可：

```bash
python server.py --reindex
# 响应/输出里必须检查：
#   skipped   —— 读不出来或 id 重复的文件（这些岗位不会出现在列表里）
#   warnings  —— 日期格式错、枚举外的取值等（值被保留，但你要告诉用户）
#   duplicates—— 疑似重复岗位组（强信号可在网页点「合并重复」）
```

服务器运行中（默认 8770）可 `curl -X POST localhost:8770/api/reindex`，或走 API
写入（`POST/PUT /api/jobs`，服务端会校验，非法值直接 400）。
改共享字段的 `PUT` 必须带 `base_rev`（先 `GET` 拿 `_rev`；`"*"` 强制覆盖）。

**禁止**：直接写 sqlite。它只用于只读查询，reindex 会覆盖一切手写改动。

server 的所有写路径遵守一条全局锁序（详见 `server.py` 顶部注释）：
`.sync.lock` → `.jobs.lock` → 进程内 RLock → `.status.lock`。新增写接口或改写
路径时必须按这个顺序拿锁；个人状态的 flock 不可重入，不要在持它的 with 块里
再取第二把。同一份数据目录同时只允许一个 server 实例（程序已强制）。

## 3. 数据规则（硬性）

- **必填**：`id`、`company`、`position`。id 规则：有官方职位号用 `<公司>-<职位号小写>`，
  没有用 `<公司>-<岗位名>`。**尽量填 `job_no`** —— 这是多人协作去重的主要依据。
- **三个维度各管各的**：城市 → `locations[]`（多值，多地全列）；招聘类型 →
  `recruit_type`（枚举：校招/社招/实习）；岗位分类 → `category`（受控枚举，见
  `server.py` 的 `CATEGORIES`）。**不要把城市或招聘类型塞进 `tags`**。
- **取值复用**：所有维度字段都是筛选依据。新增前先查已有取值（`GET /api/jobs`
  的 `facets`，或扫 `jobs/*.json`），同类岗位复用已有写法；新方向才引入新值，
  命名保持简洁、无空格、无标点。
- **本版本表达不了的值，保留并告警，绝不静默归零** —— 这是全项目的原则。
  你读到枚举外的值就原样留着，报告给用户，不要"帮忙"改掉。
- **日期** `YYYY-MM-DD`，**日期时间** `YYYY-MM-DD HH:mm`。
- **不删除**：岗位下架 → 共享字段 `closed: true`（所有人可见，为假不落盘）；
  用户自己不投了 → 那是个人层状态，别替用户在共享层做任何标记。
  删除任何文件前必须征得用户同意。
- `jobs/.id-migrations` 是系统维护的 id 迁移日志（**无 `.json` 后缀**，扫描不会
  把它当成岗位）。不要手改、不要删；手写岗位 JSON 时也不要模仿它。
- **凭证、CV、个人状态一律不进 git**（仓库是公开的）。本地路径配置在
  `config.json`（已 gitignore）。

## 4. CV 目录约定

- 解读文件命名 `<名字>.reading.md`，格式（含 `## 关键词` 恰好 10 个、顿号分隔、
  独占一行）见 `cv/README.md`；WebUI「CV 与解读」页有一键复制的提示词。
- 关键词会参与岗位匹配度计算（`match_hits`），改了解读文件要点一次「↻ 重建索引」。
- agent 不替用户生成正式解读；只做格式检查（`GET /api/cv` 看 keywords 解析结果）。

## 5. 改代码的规则

- **保持零依赖**：只用 Python 标准库 + 原生 JS。不引入 npm、pip 包、构建步骤。
- 改数据结构相关逻辑后，同步更新 `schema/job.schema.json`、`README.md` 和本文件。
- **改完必须跑自测**：`python test_server.py`（合成数据，不碰真实 jobs/ 与
  local/；断言总数以运行输出为准）。断言红了不许交付。CI 会在 ubuntu 与
  windows 双平台自动跑同一套断言。
- Windows 与 macOS 都要能跑：路径一律 `pathlib`。

## 6. 常用命令速查

```bash
python server.py                 # 启动 WebUI（启动时自动迁移 + 重建索引 + 打印提醒）
python server.py --reindex       # 只重建索引（含 skipped/warnings/duplicates 报告）
python test_server.py            # 自测
python install.py                # 配置数据/CV 目录
sqlite3 data/jobs.db "SELECT ..."  # 只读查询
```

API 完整列表与协作流程见 `README.md`。
