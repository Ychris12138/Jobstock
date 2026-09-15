#!/usr/bin/env python3
"""Jobstock Windows 便携版构建脚本（零第三方依赖，Python 3.8+ 即可运行）。

用法：
    python scripts/build_windows_portable.py                  # 标准构建
    python scripts/build_windows_portable.py --refresh-runtime  # 重新下载 runtime
    python scripts/build_windows_portable.py --keep-staging     # 保留 staging 目录便于检查

产物：dist/Jobstock-v<版本>-Windows-x64.zip

runtime 用的是 python.org 官方 embeddable package，版本与 SHA256 固定在下方
常量里——每次构建拉同一个文件、校验同一个哈希，产物才可复现。Jobstock 本体
只有标准库 + 原生 JS，embedded Python 足够且透明，不需要 PyInstaller。
"""
import argparse
import hashlib
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"

# 固定的 Windows x64 runtime：3.12 与 CI（setup-python '3.12'）同一大版本，
# 3.12.10 是 3.12 最后一个带 Windows 二进制的 bugfix 发布（其后的 3.12.x 是
# 仅源码的安全发布）。哈希来自 python.org 官方文件实测，改动它 = 换 runtime。
PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
PYTHON_EMBED_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
                    f"python-{PYTHON_VERSION}-embed-amd64.zip")

# 进包的源码文件：显式白名单，新增文件不会自动进包——这是隐私防线的第一道。
APP_FILES = (
    "server.py",
    "install.py",
    "web/index.html",
    "schema/job.schema.json",
    "cv/README.md",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "AGENTS.md",
)

# 打包后的禁区：任何路径段命中即判失败。staging 由白名单生成，这里只是兜底。
FORBIDDEN_PARTS = {".git", ".github", "jobs", "local", "data",
                   "__pycache__", "node_modules", "scripts", "build", "dist"}
FORBIDDEN_FILES = {"config.json", "jobs.db", ".server.lock", ".jobs.lock",
                   ".status.lock"}
FORBIDDEN_SUFFIXES = (".pyc", ".db", ".lock", ".tmp")


def app_version(root=ROOT):
    """版本号的唯一真相源是 server.py 的 SERVER_VERSION。"""
    m = re.search(r'^SERVER_VERSION\s*=\s*"([0-9][0-9.]*)"',
                  (root / "server.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("server.py 里找不到 SERVER_VERSION，无法确定打包版本")
    return m.group(1)


def dist_name(version):
    return f"Jobstock-v{version}-Windows-x64"


def launcher_bat():
    """便携版双击入口。必须是纯 ASCII + CRLF：cmd 按系统 OEM 代码页分块解析
    bat，UTF-8 中文行可能被从中间劈开当成命令执行（见 start.bat 与 CHANGELOG）。
    所有中文文案由 Python 侧输出。"""
    return (
        "@echo off\r\n"
        "rem Jobstock portable launcher. Keep this file ASCII-only:\r\n"
        "rem cmd.exe parses .bat via the OEM codepage and can mis-split\r\n"
        "rem multi-byte lines into garbage commands.\r\n"
        "cd /d %~dp0\r\n"
        'set "PY=%~dp0runtime\\python.exe"\r\n'
        'if not exist "%PY%" (\r\n'
        "    echo [Jobstock] runtime\\python.exe not found - the package looks incomplete.\r\n"
        "    echo Please re-extract the ZIP in full, then double-click again.\r\n"
        "    pause\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "rem First run (no config.json yet): one-time setup (data dirs, config, index).\r\n"
        'if not exist "%~dp0app\\config.json" (\r\n'
        '    "%PY%" "%~dp0app\\install.py"\r\n'
        "    if errorlevel 1 (\r\n"
        "        echo [Jobstock] Setup did not finish - see the message above, then retry.\r\n"
        "        pause\r\n"
        "        exit /b 1\r\n"
        "    )\r\n"
        ")\r\n"
        '"%PY%" "%~dp0app\\server.py"\r\n'
        "if errorlevel 1 echo [Jobstock] Server exited with an error - see the message above.\r\n"
        "pause\r\n"
    )


def readme_windows_txt(version):
    """包内最短说明。生成时加 BOM + CRLF：老版本记事本才能正确显示中文与换行。"""
    lines = [
        "Jobstock 便携版（Windows x64）",
        "=" * 30,
        "",
        "使用：",
        "1. 双击 Jobstock.bat",
        "2. 首次启动会做一次性初始化（数据目录、你的名字、岗位分类；一路回车用默认即可）",
        "3. 浏览器自动打开 http://localhost:8770",
        "4. 以后每次用：双击 Jobstock.bat；已在运行时会直接打开页面，不会起第二份",
        "",
        "数据在哪：",
        "- 全部只存本机 app/ 目录内（jobs/ 岗位、local/ 投递状态、data/ 索引）",
        "- 整个文件夹可整体复制/移动，数据跟着走",
        "- 网页里的「⇩ 全量备份」可导出 zip 存档；CV 放进 cv/ 目录",
        "",
        "无需安装 Python / Git / npm；不写 PATH、不写注册表、不需要管理员权限。",
        "",
        f"版本：v{version}（内置 Python {PYTHON_VERSION} embed-amd64）",
        "项目主页与反馈：https://github.com/Ychris12138/Jobstock",
    ]
    return "﻿" + "\r\n".join(lines) + "\r\n"  # 开头 U+FEFF 是 BOM，勿删


def stage_app(staging, root=ROOT):
    """把白名单内的源码复制到 staging/app/。返回复制的文件数。"""
    app = staging / "app"
    n = 0
    for rel in APP_FILES:
        src = root / rel
        if not src.is_file():
            raise SystemExit(f"白名单文件缺失：{rel}")
        dst = app / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    return n


def fetch_runtime(cache_dir=BUILD_DIR / "cache", url=PYTHON_EMBED_URL,
                  sha256=PYTHON_EMBED_SHA256):
    """下载（或复用缓存）官方 embeddable runtime，并校验 SHA256。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    z = cache_dir / Path(url).name
    if not z.is_file():
        print(f"下载 runtime：{url}")
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(z, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:
            z.unlink(missing_ok=True)
            raise SystemExit(f"runtime 下载失败：{e}")
    got = hashlib.sha256(z.read_bytes()).hexdigest()
    if got != sha256:
        z.unlink(missing_ok=True)
        raise SystemExit(f"runtime 哈希不匹配：期望 {sha256}，实际 {got}（已删除，防止误用）")
    return z


def stage_runtime(staging, runtime_zip):
    """官方 embeddable 不解剖、不裁剪，原样解到 runtime/ —— 首版稳定优先。"""
    dst = staging / "runtime"
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(runtime_zip) as z:
        z.extractall(dst)
    if not (dst / "python.exe").is_file():
        raise SystemExit("runtime 解包后没有 python.exe，包内容异常")


def privacy_scan(staging, root=ROOT):
    """宁可误杀不可漏放：文件名/路径段黑名单 + 文本内容里的本机路径残留。"""
    bad = []
    for f in staging.rglob("*"):
        rel = f.relative_to(staging)
        parts = set(rel.parts)
        if parts & FORBIDDEN_PARTS or f.name in FORBIDDEN_FILES \
                or f.suffix.lower() in FORBIDDEN_SUFFIXES:
            bad.append(f"禁区文件：{rel}")
    home = str(Path.home())
    needles = [str(root), home, home.replace("\\", "/")]
    for f in staging.rglob("*"):
        if not f.is_file() or f.suffix.lower() in (".zip", ".dll", ".pyd", ".exe"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in needles:
            if needle and needle in text:
                bad.append(f"文件 {f.relative_to(staging)} 含本机路径 {needle}")
    if bad:
        raise SystemExit("隐私扫描未通过：\n" + "\n".join(bad))


def make_zip(staging, out_zip):
    """固定时间戳 + 排序文件名：同输入产出同字节，构建可复现。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(staging.rglob("*")):
            if not f.is_file():
                continue
            arc = f"{staging.name}/{f.relative_to(staging).as_posix()}"
            info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, f.read_bytes())
    return out_zip


def build(version=None, refresh_runtime=False, keep_staging=False):
    version = version or app_version()
    name = dist_name(version)
    staging = BUILD_DIR / name
    cache_zip = BUILD_DIR / "cache" / Path(PYTHON_EMBED_URL).name
    if refresh_runtime:
        cache_zip.unlink(missing_ok=True)
    runtime_zip = fetch_runtime()
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    n = stage_app(staging)
    stage_runtime(staging, runtime_zip)
    (staging / "Jobstock.bat").write_bytes(launcher_bat().encode("ascii"))
    (staging / "README-Windows.txt").write_bytes(
        readme_windows_txt(version).encode("utf-8"))
    privacy_scan(staging)
    out = make_zip(staging, DIST_DIR / f"{name}.zip")
    size_zip = out.stat().st_size / (1024 * 1024)
    size_raw = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"✅ 构建完成：{out}")
    print(f"   源码文件 {n} 个；ZIP {size_zip:.1f} MB；解压后约 {size_raw:.1f} MB；"
          f"内置 Python {PYTHON_VERSION}（embed-amd64）")
    if not keep_staging:
        shutil.rmtree(staging, ignore_errors=True)
    return out


def main():
    # 与 server.py 同一处理：非 UTF-8 locale 的 Windows 重定向输出时 emoji/中文会炸
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Jobstock Windows 便携版构建")
    ap.add_argument("--version", help="覆盖版本号（默认读 server.py 的 SERVER_VERSION）")
    ap.add_argument("--refresh-runtime", action="store_true", help="重新下载 embeddable runtime")
    ap.add_argument("--keep-staging", action="store_true", help="保留 staging 目录便于检查")
    args = ap.parse_args()
    build(args.version, args.refresh_runtime, args.keep_staging)


if __name__ == "__main__":
    main()
