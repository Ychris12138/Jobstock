# Changelog

## [0.1.0] — 2026-09-15

首个公开版本。

### Added
- 本地求职 Dashboard 核心功能：岗位、截止日期、投递进度、备注一页管理
  （待投递 → 已投递 → 笔试 → 面试 → Offer）
- 岗位层 / 个人层分层数据模型：岗位 JSON 可共享，投递状态与个人备注只存本机
- 搜索、筛选、疑似重复检测与合并、截止提醒、`.ics` 日历导出
- CV 关键词解读与岗位匹配命中；内置初始化 / 全网搜岗 / CV 解读 Agent 提示词
- 「使用指南」页与岗位分类定制（`POST /api/config/categories`，写入本机 config.json 并热更新）
- 岗位历史版本（改写自动留最近 3 份）、个人状态快照与全量 zip 备份
- **Windows 免安装便携版**（#4）：`scripts/build_windows_portable.py` 可复现构建
  `Jobstock-v0.1.0-Windows-x64.zip`——内置官方 CPython 3.12.10 embeddable x64
  （SHA256 固定校验），解压后双击 `Jobstock.bat` 即用，无需安装 Python / Git；
  打包过程含隐私扫描，不带入任何本机数据

### Security
- **历史版本路径边界**（#1）：`history_dir` / `history_version_file` 统一 `resolve()` 校验，
  拒绝 `../`、`..\`、多段路径与盘符形式
- **备份边界**（#2）：跳过 symlink，`resolve()` 后必须仍在 `jobs/`、`local/` 内才打进 zip
- **敏感响应头**（#2）：`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、
  `Referrer-Policy: no-referrer`
- README / WebUI 明确外部 Agent 隐私边界说明

### Fixed
- **Windows `start.bat` 双击启动**：cmd 按系统 OEM 代码页分块解析 bat，UTF-8 中文行可能被
  劈开当成命令执行（乱码报错）；脚本改为纯 ASCII，并用 `.gitattributes` 钉死 CRLF/LF
- **首次启动自动初始化**：`start.bat` / 便携版 `Jobstock.bat` 在没有 `config.json` 时
  自动先跑 `install.py`，不再直接报「数据目录不存在」
- **Windows 端口影子化**：`SO_REUSEADDR` 在 Windows 上允许重复 bind 同一端口；
  `pick_port` 先用裸 socket 探测，端口自动避让与 `--port` 占用报错恢复正确
- **`install.py`**：非 UTF-8 locale 的 Windows 重定向输出抛 `UnicodeEncodeError`
- **误开其它副本的页面**：`/api/jobs` 返回 `install_fp`（工具目录指纹），单实例探测
  必须指纹一致才认定「已在运行」，不再误开另一份 Jobstock

### Changed
- README 加入真实界面截图（`assets/docs/`），安装路径以普通用户优先
- 截止提醒条改为浅琥珀样式
- Windows 用户默认推荐免安装便携版
