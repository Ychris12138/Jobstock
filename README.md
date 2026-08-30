# job-stock

轻量求职数据仓库：每条岗位一个 JSON 文件 + 一个 sqlite 列表索引 + 一个无需 npm 的 WebUI。
纯 Python 标准库，零依赖，Windows / macOS 都能跑。

## 启动

- **Windows**：双击 `start.bat`
- **macOS**：`chmod +x start.command` 一次，之后双击它（或终端 `python3 server.py`）
- 手动：`python server.py`（或 `python3 server.py --port 8771` 换端口）

浏览器打开 http://localhost:8770 。

## 数据结构

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
2. 日期一律 `YYYY-MM-DD`，日期时间 `YYYY-MM-DD HH:mm`。
3. 不删除历史岗位，把 `status` 改为 `已归档`。
4. sqlite 只读查询随意；写入请改 JSON 后 reindex，避免两边不一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/jobs?status=&q=` | 列表（读 sqlite 索引） |
| GET | `/api/jobs/<id>` | 单条完整 JSON |
| POST | `/api/jobs` | 新增（company/position 必填） |
| PUT | `/api/jobs/<id>` | 修改 |
| POST | `/api/jobs/<id>/status` | 快捷改状态 `{ "status": "已投递" }` |
| POST | `/api/reindex` | 从 JSON 重建 sqlite 索引 |
| GET | `/api/cv` | CV 原文件列表 + 解读文件（含解析出的关键词）+ 解读提示词 |
