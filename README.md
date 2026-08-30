# job-stock

多人共建的求职信息仓库：**招聘信息共享，投递进度私有**。
每条岗位一个 JSON 文件 + 一个 sqlite 列表索引 + 一个无需 npm 的 WebUI。
纯 Python 标准库，零依赖，Windows / macOS 都能跑。

## 数据分三层（本项目最重要的约定）

```
jobs/<id>.json     共享层 —— 招聘信息本身，进 git，所有人共同维护、都能看到
local/status.json  个人层 —— 投递状态 + 个人备注，不进 git，只留在本机
data/jobs.db       索引层 —— 从上面两层派生的 sqlite，可随时重建
cv/                CV 原文 + <名字>.reading.md 解读文件（格式见 cv/README.md）
                   —— 和个人层一样不进 git，任何人的都不上传
```

改共享层要走 git 同步；改个人层只影响自己，**绝不产生 git diff** ——
所以你把某个岗位标成「已投递」，合作者那边什么都不会变，也看不到。

| | 共享（进 git） | 个人（不进 git） |
|---|---|---|
| 字段 | `company / position / job_no / category / recruit_type / url / locations[] / salary / source / deadline / tags[] / notes / jd` | `status / my_notes` |
| 含义 | 客观招聘信息 + 对大家都有用的公共备注（如「内推码到 9 月底」「卡 985」） | 我投到哪一步了、我自己的记录 |
| 谁能看到 | 所有合作者 | 只有本机 |

三个维度各管各的，**不要混进 tags** —— 「上海」「校招」「AI4S」不是同一类东西，
混在一个标签栏里就没法用了：

| 维度 | 字段 | 取值 | 筛选语义 |
|---|---|---|---|
| 工作地点 | `locations[]` | 自由，多值 | 选「上海」能筛出**所有可在上海**的岗位；多选取 **OR** |
| 招聘类型 | `recruit_type` | 受控枚举：`校招 / 社招 / 实习` | 单选 |
| 岗位分类 | `category` | 受控枚举：`算法 / 研究 / 数据 / 量化 / 后端 / Infra / 前端 / 硬件 / 产品 / 其他` | 单选 |
| 主题属性 | `tags[]` | 自由，多值 | 多选取 **AND**（同时具备），如 `AI4S / 2027届 / 分子动力学` |

`locations` 是数组：一个岗位常常多地可选（如「深圳、北京、上海」），单值字段会丢信息。
列表页把它渲染成彩色 chip，城市与颜色固定对应，方便扫视。

枚举要加值就改 `server.py` 里的 `CATEGORIES` / `RECRUIT_TYPES`，前后端同时生效。

状态枚举：`待投递 / 已投递 / 笔试 / 面试 / Offer / 已拒绝 / 已归档`（只归档，不删除）

### 岗位 id 与去重

id 由内容算出来，**有官方职位号就用它**：

```
有 job_no：  <公司>-<职位号>      例：字节跳动-a212198a
没有   ：    <公司>-<岗位名>      例：小米-infra岗
```

这是为了对付多人协作时最烦的问题：**两个人各自录了同一个岗位**。
填了职位号，两人算出的 id 必然相同，于是重复会变成 git 冲突（看得见、必须处理）；
不填就只能按岗位名算，写法差一个字就会变成两条静默共存的记录。
**所以录岗位时尽量把官方职位号填上。**

系统还有两道兜底：

1. **新增时拦截**：同一个职位号再录一次直接返回 409，告诉你已经有了，不会造出「-2」的副本
2. **重建索引时检测**：按「同公司同职位号」「同投递链接」「公司+岗位名指纹相同」三个信号
   找出重复组，显示在页面顶部。前两个是强信号，可以点「合并」自动处理；第三个只报告
   ——同一家公司不同部门完全可能有同名岗位，那种要人来判断

自动合并的规则：保留信息最全的那条（打平时取先录进来的），**空字段用另一条补齐、
`locations` 与 `tags` 取并集、`notes` 内容不同就拼接**（宁可留着让人删，也不要悄悄丢掉
合作者写的情报）；个人层取走得最远的那个状态，个人备注拼接。

补 `job_no` 会让 id 变化，文件随之改名，个人状态的键会一起搬过去，投递进度不会丢。
只有「有职位号且当前 id 不是按它算的」时才改名 —— 改公司名或岗位名不会引发改名。

## 安装（新机器一键搞定）

前提：装有 Python 3.8+ 和 [GitHub CLI](https://cli.github.com/)（已 `gh auth login`）。

```bash
gh repo clone Ychris12138/job-stock && cd job-stock && python install.py
# macOS 若 python 不存在则用：python3 install.py
```

`install.py` 会交互式询问两个位置（回车 = 放仓库内，可随 git 同步）：

- **数据目录**：岗位 JSON + 个人状态 + sqlite 索引
- **CV 目录**：CV 原文与解读文件

也可以非交互指定：

```bash
python install.py --data-dir "D:\jobs-data" --cv-dir "~/Documents/my-cv"
```

配置写入 `config.json`（不进 git，每台机器各自配置）。临时覆盖可用
`python server.py --data-dir ... --cv-dir ...`。

> 从旧版本升级：旧的 `jobs/*.json` 里带 `status` 字段，服务器启动时会**自动**
> 把它搬进 `local/status.json` 并从共享 JSON 中移除，无需手工处理。这个迁移是
> 幂等的，不会覆盖你本地已有的进度，也不会删掉合作者写的未知字段。

> 版本不一致的容错：合作者可能跑着改过 `CATEGORIES` 的版本，岗位 JSON 也可能是
> 手写的。凡是本版本「枚举里没有 / 控件装不下 / 解析不了」的值，一律**保留并告警**，
> 绝不静默归零 —— 分类会照常出现在筛选下拉里，编辑框里会标注「本版本枚举外，保留原值」，
> 格式不对的截止日期会在重建索引时报出来。

> 字段结构升级（同样自动、幂等）：旧的单值 `location` 会变成 `locations` 数组；
> 早期版本混进 `tags` 的城市名和招聘类型，会分别归位到 `locations` 和 `recruit_type`。
> 城市名靠 `server.py` 里的 `KNOWN_CITIES` 表识别，这张表只在迁移时用到。

## 启动

- **Windows**：双击 `start.bat`
- **macOS**：双击 `start.command`（或终端 `python3 server.py`）
- 手动：`python server.py`（或 `--port 8771` 换端口）

浏览器打开 http://localhost:8770 。

## 三种录入招聘信息的方式

1. **WebUI**：点「＋ 新增岗位」，公司和岗位必填，其余可留空。
2. **让 AI 直接写 JSON**：告诉 AI「加一个 XX 公司的 YY 岗」，它按下面的约定新建
   `jobs/*.json`，然后在网页点「↻ 重建索引」（或它自己 `curl -X POST
   localhost:8770/api/reindex`），刷新即可看到。
3. **拉合作者的**：点「⇩ 拉取合作者数据」，等价于 `git pull --rebase --autostash`
   + 重建索引。带 `--autostash` 是必需的：在 WebUI 里编辑过岗位后工作区就是脏的，
   不 autostash 的话 git 会直接拒绝 rebase。失败时（例如 rebase 冲突）会自动
   `git rebase --abort` 把仓库恢复到同步前的状态，不会把你留在 detached HEAD 上。
   **推送仍然手动做**（`git add jobs/ && git commit && git push`）—— 按钮只拉不推。

## 协作流程

```
合作者新增岗位 → git push
       ↓
你点「⇩ 拉取合作者数据」→ 新岗位出现在列表里（状态一律是「待投递」，因为状态是你自己的）
       ↓
你投了 → 点「快捷状态」改成「已投递」→ 只写 local/status.json，git status 干净
```

一岗一文件的设计让 git 冲突基本只发生在「两人同时改同一个岗位」时。
两人各自新增岗位不会冲突；两人对同一岗位的投递进度也不会冲突（各存各的）。

编辑岗位时有**乐观锁**：打开编辑框会记下这条岗位的内容指纹（`_rev`），保存时若发现
它已被改动（另一个标签页、或刚拉下来的合作者改动），会返回 409 并提示重新打开，
而不是把别人的改动静默覆盖掉。用脚本调 `PUT` 改共享字段时必须带 `base_rev`
（先 `GET` 拿 `_rev`），确实要强制覆盖就传 `base_rev="*"`。

> ⚠️ **同一份数据目录同时只跑一个 server**。个人状态文件有跨进程文件锁，
> 但云盘（iCloud/Dropbox）同步的目录被两台机器同时写仍可能丢状态更新。

## AI 协作约定

1. 新增/修改岗位：直接编辑 `jobs/*.json`（只写共享字段，**不要写 `status` /
   `my_notes`**），然后运行 `python server.py --reindex` 重建索引；若服务器正在
   运行，也可在 WebUI 点「↻ 重建索引」或 `curl -X POST localhost:8770/api/reindex`。
2. **取值复用**：`company / locations / position / category / recruit_type / source / tags`
   都是筛选维度，新增岗位前先查已有取值（`GET /api/jobs` 返回的 `facets`，或直接扫
   `jobs/*.json`），同类岗位必须复用已有写法（如已有「杭州」就不要写「杭州市」）；
   确属新方向时才引入新值，并保持命名风格一致（简洁、无空格、不加标点）。
3. `category` 与 `recruit_type` 必须是枚举之一，写错会被接口拒绝（400）。
   **城市写进 `locations`、招聘类型写进 `recruit_type`，不要写进 `tags`** ——
   写错了接口会自动归位，但别指望它，一开始就写对。
   多地可选的岗位把城市全列进 `locations`，不要只留第一个。
4. **官方职位号填进 `job_no`**（如字节的 A212198A）—— 这是去重的主要依据，
   填了就不会和合作者录重。
5. 日期一律 `YYYY-MM-DD`，日期时间 `YYYY-MM-DD HH:mm`。
6. 不删除历史岗位，把 `status` 改为 `已归档`（这是本地操作）。
7. sqlite 只读查询随意；写入请改 JSON 后 reindex，避免两边不一致。

## 筛选

工具栏上的分类 / 公司 / 地点 / 岗位 / 来源 / 状态六个下拉，选项都由现有数据自动生成。
标签在下面一行，**点击切换、可多选**。语义是：

- 同一维度内多个取值取 **OR**（接口层支持，如 `?location=北京&location=杭州`）
- 不同维度之间取 **AND**
- 多个标签取 **AND**（同时具备这几个标签）
- 关键词搜索覆盖 公司 / 岗位 / 地点 / 分类 / 标签 / 公共备注 / **JD 正文** / 个人备注
- 「隐藏已归档」默认勾选；显式筛「已归档」时该开关自动让位

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs?status=&company=&location=&position=&category=&recruit_type=&source=&tag=&deadline_before=&q=&hide_archived=1` | 列表（读 sqlite 索引），响应含 `facets`（各维度已有取值）、`statuses`、`categories`、`recruit_types`。除 `deadline_before/q/hide_archived` 外，参数均可重复传多个值；`location` 多值取 OR，`tag` 多值取 AND |
| GET | `/api/jobs/<id>` | 单条完整 JSON（共享字段 + 本机状态的合并视图） |
| POST | `/api/jobs` | 新增（company/position 必填）；`status`/`my_notes` 会被分流到本地 |
| PUT | `/api/jobs/<id>` | 修改；共享字段**真的变了**才写 JSON 并更新 `updated_at`（空串与缺失字段视为相等，所以 no-op 保存不产生 git diff）。改共享字段必须带 `base_rev`（乐观锁，不匹配返回 409；`"*"` 强制覆盖），只改个人字段不需要 |
| POST | `/api/jobs/<id>/status` | 快捷改状态 `{ "status": "已投递" }` —— **只写本地** |
| POST | `/api/reindex` | 从 JSON + 本地状态重建索引；响应含 `skipped`（读不出来/id 重复的文件）、`warnings`（日期格式、枚举外的取值）与 `duplicates`（重复岗位组），**不会静默吞掉岗位** |
| POST | `/api/dedupe` | 合并强信号的重复岗位组（同职位号 / 同投递链接），返回合并了哪些 |
| POST | `/api/sync` | `git pull --rebase --autostash` 拉取合作者数据后重建索引（只拉不推）。rebase 失败会自动 `--abort` 恢复；autostash 贴回冲突时（git 此时退出码是 0）也判为失败并说清楚改动在 stash 里 |
| GET | `/api/cv` | CV 原文件列表 + 解读文件（含解析出的关键词）+ 解读提示词 |

## 自测

```bash
python3 test_server.py
```

用合成数据在临时目录里跑 112 条断言，不会碰你真实的 `jobs/` 与 `local/`。覆盖：
数据分层与 no-op 保存不产生 diff、迁移幂等且保留未知字段、多地点筛选、
枚举外的值不被静默清空、
乐观锁冲突、个人状态文件损坏时拒绝写入、reindex 对坏文件/缺 id/重复 id 的报告、
日期归一、标签归一、筛选语义、LIKE 通配符转义、异常转 500 JSON、CV 关键词解析，
git 同步（临时建一个 bare origin + 两个 clone，覆盖「工作区脏时能拉」和
「autostash 冲突时不误报成功」），以及重复岗位的识别与合并（含跨机器场景：
合作者的文件是 git pull 进来的，绕过了新增时的拦截）。

最后一节是**鉴别力自检**：把 LIKE 转义整个关掉后重跑通配符相关的断言，它们必须失败。
之前的版本里关掉转义仍有 32/33 条通过 —— 断言写了，但构造的输入让它无法失败。
