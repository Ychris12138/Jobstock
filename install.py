#!/usr/bin/env python3
"""job-stock 一键安装/配置。

用法：
    python install.py                                   # 交互式（推荐）
    python install.py --yes                             # 全默认：数据/CV 都放仓库内
    python install.py --data-dir D:\\jobs-data --cv-dir ~/my-cv

安装做的事：
    1. 写入 config.json（记录数据目录 / CV 目录，相对路径基于本仓库）
    2. 创建所需目录
    3. 重建 sqlite 索引
    4. macOS 下给 start.command 加执行权限
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def ask(prompt):
    return input(prompt).strip()


def resolve(p):
    p = Path(p).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def main():
    ap = argparse.ArgumentParser(description="job-stock 一键安装")
    ap.add_argument("--data-dir", help="数据目录（岗位 JSON + sqlite 索引）")
    ap.add_argument("--cv-dir", help="CV 目录")
    ap.add_argument("--yes", action="store_true", help="不询问，直接用默认值（仓库内）")
    args = ap.parse_args()

    if sys.version_info < (3, 8):
        sys.exit("需要 Python 3.8 或更高版本。")

    print("job-stock 安装配置")
    print("（数据放仓库内可随 git 同步到多台电脑；放仓库外则各机器独立）\n")

    data_dir, cv_dir = args.data_dir, args.cv_dir
    if not args.yes:
        if data_dir is None:
            data_dir = ask("数据目录（岗位 JSON + sqlite，回车 = 仓库内）：")
        if cv_dir is None:
            cv_dir = ask("CV 目录（回车 = 仓库内 cv/）：")

    cfg = {}
    if data_dir:
        cfg["data_dir"] = data_dir
    if cv_dir:
        cfg["cv_dir"] = cv_dir
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    base = resolve(data_dir) if data_dir else ROOT
    (base / "jobs").mkdir(parents=True, exist_ok=True)
    (base / "data").mkdir(parents=True, exist_ok=True)
    cv = resolve(cv_dir) if cv_dir else ROOT / "cv"
    cv.mkdir(parents=True, exist_ok=True)

    # macOS/Linux：启动脚本加执行权限
    for name in ("start.command",):
        f = ROOT / name
        if f.exists():
            os.chmod(f, 0o755)

    subprocess.run([sys.executable, str(ROOT / "server.py"), "--reindex"], check=False)

    print("\n✅ 安装完成")
    print(f"  数据目录：{base}\n  CV 目录：{cv}")
    if sys.platform == "darwin":
        print("\n启动：双击 start.command（或 python3 server.py）")
    elif sys.platform.startswith("win"):
        print("\n启动：双击 start.bat（或 python server.py）")
    else:
        print("\n启动：python3 server.py")


if __name__ == "__main__":
    main()
