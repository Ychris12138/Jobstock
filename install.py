#!/usr/bin/env python3
"""job-stock 一键安装/配置。

用法：
    python install.py                                   # 交互式（推荐）
    python install.py --yes                             # 全默认：数据/CV 都放仓库内
    python install.py --data-dir D:\\jobs-data --cv-dir ~/my-cv

安装做的事：
    1. 写入 config.json（记录数据目录 / CV 目录，相对路径基于本仓库）
    2. 创建所需目录（jobs/ 共享招聘信息、local/ 个人状态、data/ 索引、cv/）
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
    print("（数据放仓库内可随 git 同步到多台电脑；放仓库外则各机器独立）")
    print("注意：投递状态存在 <数据目录>/local/ 下，永远不进 git，只属于你自己。\n")

    # 读现有配置作为默认值：重跑安装一路回车不该把已配置的目录清掉
    old = {}
    if CONFIG.exists():
        try:
            old = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            print(f"⚠️  现有 {CONFIG.name} 读不出来，将按新配置重写。")

    data_dir, cv_dir = args.data_dir, args.cv_dir
    if not args.yes:
        if data_dir is None:
            cur = old.get("data_dir")
            data_dir = ask(f"数据目录（岗位 JSON + 个人状态 + sqlite，回车 = {cur or '仓库内'}）：") or cur
        if cv_dir is None:
            cur = old.get("cv_dir")
            cv_dir = ask(f"CV 目录（回车 = {cur or '仓库内 cv/'}）：") or cur
        cur_name = old.get("my_name", "")
        name = ask(f"你的名字（写入新岗位的 created_by，队友就知道谁录的；回车 = "
                   f"{cur_name or '跳过，运行时用 git 用户名'}）：")
        if name:
            old["my_name"] = name
    else:
        data_dir = data_dir or old.get("data_dir")
        cv_dir = cv_dir or old.get("cv_dir")

    cfg = {}
    if old.get("my_name"):
        cfg["my_name"] = old["my_name"]
    if data_dir:
        cfg["data_dir"] = data_dir
    if cv_dir:
        cfg["cv_dir"] = cv_dir

    base = resolve(data_dir) if data_dir else ROOT
    (base / "jobs").mkdir(parents=True, exist_ok=True)    # 共享：招聘信息
    (base / "local").mkdir(parents=True, exist_ok=True)   # 个人：投递状态，不进 git
    (base / "data").mkdir(parents=True, exist_ok=True)    # 派生：sqlite 索引
    cv = resolve(cv_dir) if cv_dir else ROOT / "cv"
    cv.mkdir(parents=True, exist_ok=True)

    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # macOS/Linux：启动脚本加执行权限
    for name in ("start.command",):
        f = ROOT / name
        if f.exists():
            os.chmod(f, 0o755)

    subprocess.run([sys.executable, str(ROOT / "server.py"), "--reindex"], check=False)

    print("\n✅ 安装完成")
    print(f"  共享招聘数据：{base / 'jobs'}")
    print(f"  个人状态（不进 git）：{base / 'local' / 'status.json'}")
    print(f"  CV 目录：{cv}")
    if sys.platform == "darwin":
        print("\n启动：双击 start.command（或 python3 server.py）")
    elif sys.platform.startswith("win"):
        print("\n启动：双击 start.bat（或 python server.py）")
    else:
        print("\n启动：python3 server.py")


if __name__ == "__main__":
    main()
