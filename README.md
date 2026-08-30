# job-stock

轻量求职数据仓库：每条岗位一个 JSON 文件 + 一个 sqlite 列表索引 + 一个无需 npm 的 WebUI。
纯 Python 标准库，零依赖，Windows / macOS 都能跑。

## 安装（新机器一键搞定）

前提：装有 Python 3.8+ 和 [GitHub CLI](https://cli.github.com/)（已 `gh auth login`）。

```bash
# Windows / macOS / Linux 通用
gh repo clone Ychris12138/job-stock && cd job-stock && python install.py
# macOS 若 python 不存在则用：python3 install.py
```

`install.py` 会交互式询问两个位置（回车 = 放仓库内，可随 git 同步）：

- **数据目录**：岗位 JSON + sqlite 索引
- **CV 目录**：CV 原文与解读文件

也可以非交互指定：

```bash
python install.py --data-dir "D:\jobs-data" --cv-dir "~/Documents/my-cv"
```

配置写入 `config.json`（不进 git，每台机器各自配置）。临时覆盖可用
`python server.py --data-dir ... --cv-dir ...`。

## 启动

- **Windows**：双击 `start.bat`
- **macOS**：双击 `start.command`（或终端 `python3 server.py`）
- 手动：`python server.py`（或 `--port 8771` 换端口）

浏览器打开 http://localhost:8770 。

## 数据结构（默认布局；data/cv 位置可在 install.py 中改到仓库外）

```
jobs/<id>.json   每条岗位一个文件 —— 真相源，AI 可以直接读/改/新建
data/jobs.db     sqlite 列表索引 —— 从 jobs/*.json 派生，随时可重建
cv/              CV 原文 + <名字>.reading.md 解读文件（格式见 cv/README.md）
```

岗位 JSON 字段：`id, company, position, url, location, salary, source,
status, deadline, tags[], notes, jd, created_at, updated_at`

状态枚举：`待投递 / 已投递 / 笔试 / 面试 / Offer / 已拒绝 / 已归档`（只归档，不删除）

## AI 协作约定

1. 新增/修改岗位：直接编辑 `jobs/*.json`（字段见上），然后运行
   `python server.py --reindex` 重建索引；若服务器正在运行，也可在
   WebUI 点「↻ 重建索引」或 `curl -X POST localhost:8770/api/reindex`。
2. **取值复用**：`company / location / position / source / tags` 是筛选维度，
   新增岗位前先查已有取值（`GET /api/jobs` 返回的 `facets`，或直接扫 `jobs/*.json`），
   同类岗位必须复用已有写法（如已有「杭州」就不要写「杭州市」）；
   确属新方向时才引入新值，并保持命名风格一致（简洁、无空格、不加标点）。
3. 日期一律 `YYYY-MM-DD`，日期时间 `YYYY-MM-DD HH:mm`。
4. 不删除历史岗位，把 `status` 改为 `已归档`。
5. sqlite 只读查询随意；写入请改 JSON 后 reindex，避免两边不一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs?status=&company=&location=&position=&deadline_before=&q=` | 列表（读 sqlite 索引），响应含 `facets`（已有公司/地点/岗位取值） |
| GET | `/api/jobs/<id>` | 单条完整 JSON |
| POST | `/api/jobs` | 新增（company/position 必填） |
| PUT | `/api/jobs/<id>` | 修改 |
| POST | `/api/jobs/<id>/status` | 快捷改状态 `{ "status": "已投递" }` |
| POST | `/api/reindex` | 从 JSON 重建 sqlite 索引 |
| GET | `/api/cv` | CV 原文件列表 + 解读文件（含解析出的关键词）+ 解读提示词 |
