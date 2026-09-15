# Changelog

## [Unreleased]

### Fixed
- **Windows `start.bat` 双击启动**：cmd 按系统 OEM 代码页分块解析 bat，UTF-8 中文行可能被从中间劈开当成命令执行（乱码报错）；脚本改为纯 ASCII（中文文案留在 Python 侧），并用 `.gitattributes` 钉死 `*.bat` CRLF、`*.command` LF
- **首次启动**：`start.bat` 在没有 `config.json` 时自动先跑 `install.py`，不再直接报「数据目录不存在」
- **Windows 端口影子化**：`SO_REUSEADDR` 在 Windows 上允许重复 bind 同一端口，后启动的实例「bind 成功但永远收不到连接」；`pick_port` 先用不开 REUSEADDR 的裸 socket 探测，端口自动避让与 `--port` 占用报错在 Windows 恢复正确（§34 回归测试）
- **`install.py`**：非 UTF-8 locale 的 Windows 重定向输出时打印「⇩」等字符抛 `UnicodeEncodeError`（与 server.py 同一 reconfigure 处理）

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
