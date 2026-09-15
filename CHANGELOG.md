# Changelog

## [0.1.0] — 2026-09-15

### Added
- **使用指南**页（WebUI 顶栏）：三步上手、Agent 提示词一键复制、分类定制、数据目录说明
- `POST /api/config/categories`：在指南页按求职方向定制岗位分类，写入本机 `config.json` 并热更新
- `LICENSE`（MIT）

### Security
- **历史版本路径边界**（#1）：`history_dir` / `history_version_file` 统一 `resolve()` 校验，拒绝 `../`、`..\`、多段路径与盘符形式
- **备份边界**（#2）：跳过 symlink，`resolve()` 后必须仍在 `jobs/`、`local/` 内才打进 zip
- **敏感响应头**（#2）：`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`

### Changed
- README 以更友好的上手路径为主（ZIP / Git / Agent 安装），版本标注 0.1.0
- 空库与筛选无结果的空状态文案区分引导

### Removed
- 岗位列表页顶部的「初始化引导」横幅（提示词改由使用指南 / API 提供）

## [0.0.1] — 2026-09-15

- 品牌改为 Jobstock；顶栏与 README 面向用户重写
- 内置初始化 / 全网搜岗 / CV 解读提示词
- 分类可由 `config.json` 配置
