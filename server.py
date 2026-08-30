#!/usr/bin/env python3
"""job-stock 服务器：纯 Python 标准库，零依赖。

用法：
    python server.py [--port 8770]     # 启动 WebUI
    python server.py --reindex         # 只重建 sqlite 索引后退出

数据分两层，这是本项目的核心约定：

    共享层  <data>/jobs/<id>.json      招聘信息本身，进 git，团队共同维护
    个人层  <data>/local/status.json   投递状态 + 个人备注，不进 git，只留在本机
    索引层  <data>/data/jobs.db        由上面两层派生的 sqlite 索引，可随时重建

改共享层要走 git 同步；改个人层只影响自己，绝不产生 git diff。

一条贯穿全文件的原则：**本版本表达不了的值，保留并告警，绝不静默归零。**
多人可能跑着不同版本的本文件（枚举不同），岗位 JSON 也可能是手写的，
所以「控件读不出来」不等于「用户想清空」。
"""
import argparse
import functools
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"

# 数据位置：默认都在仓库内；可用 config.json 或 CLI 参数 --data-dir / --cv-dir 覆盖。
JOBS_DIR = ROOT / "jobs"
LOCAL_DIR = ROOT / "local"
CV_DIR = ROOT / "cv"
DB_PATH = ROOT / "data" / "jobs.db"

# 写锁：HTTP 服务是多线程的，两个请求同时写同一个文件会互相覆盖
_LOCAL_LOCK = threading.RLock()   # 个人状态文件
_JOBS_LOCK = threading.RLock()    # 共享岗位 JSON（尤其是新增时的 id 分配）


class LocalStateError(Exception):
    """个人状态文件损坏。宁可报错也不能当成空表继续写 —— 那会整表覆盖掉全部投递进度。"""


class FileLock:
    """跨进程文件锁。

    _LOCAL_LOCK 只在进程内有效，而「UI 开着又在终端跑了一次 --reindex」「起了第二个
    实例」「数据目录放在云盘上被两台机器写」都会让两个进程同时做读-改-写，
    实测 3 进程 × 60 次改状态会丢掉三分之一的更新，且完全静默。
    拿不到锁时降级成只有进程内锁（总比直接失败好）。
    """

    def __init__(self, path):
        self.path, self.fh = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if not self.fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self.fh.close()
        self.fh = None


def local_lock():
    return FileLock(LOCAL_DIR / ".status.lock")


def _resolve(p):
    p = Path(p).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def configure(data_dir=None, cv_dir=None):
    """应用数据/CV 目录配置。优先级：CLI 参数 > config.json > 默认（仓库内）。"""
    global JOBS_DIR, LOCAL_DIR, CV_DIR, DB_PATH
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    dd = data_dir or cfg.get("data_dir")
    cd = cv_dir or cfg.get("cv_dir")
    base = _resolve(dd) if dd else ROOT
    JOBS_DIR = base / "jobs"
    LOCAL_DIR = base / "local"
    DB_PATH = base / "data" / "jobs.db"
    CV_DIR = _resolve(cd) if cd else ROOT / "cv"


STATUSES = ["待投递", "已投递", "笔试", "面试", "Offer", "已拒绝", "已归档"]
# 投递进度的先后：合并重复岗位时取「走得更远」的那个状态。不能直接用上面的下标，
# 那会让「已拒绝」排在「Offer」之后；归档不代表进度，权重最低。
STATUS_RANK = {"已归档": -1, "待投递": 0, "已投递": 1, "笔试": 2, "面试": 3, "Offer": 4, "已拒绝": 4}

# 岗位一级分类：受控枚举，用于粗粒度筛选。要加新方向就改这个列表，前后端同时生效。
# 注意：枚举外的存量值不会被清掉，前端会把它当成临时选项显示出来。
CATEGORIES = ["算法", "研究", "数据", "量化", "后端", "Infra", "前端", "硬件", "产品", "其他"]

# 招聘类型：独立成一个维度，不要混进 tags —— 「校招」和「AI4S」不是同一类东西
RECRUIT_TYPES = ["校招", "社招", "实习"]

# 常见工作地点。只用于一次性迁移：早期版本把多城市塞进了 tags，靠这张表把它们
# 识别出来归位到 locations。日常录入直接写 locations 字段，不依赖这张表。
KNOWN_CITIES = ["北京", "上海", "深圳", "杭州", "广州", "成都", "南京", "苏州", "武汉",
                "西安", "香港", "厦门", "天津", "重庆", "长沙", "青岛", "大连", "合肥",
                "珠海", "无锡", "澳门", "台北", "新加坡", "东京", "首尔", "伦敦",
                "纽约", "西雅图", "远程"]

# 共享字段 —— 写进 jobs/<id>.json，随 git 同步给所有人
# locations 是数组：一个岗位常常多地可选，塞进单值字段会丢信息
SHARED_FIELDS = ["company", "position", "job_no", "category", "recruit_type", "url",
                 "locations", "salary", "source", "deadline", "tags", "notes", "jd"]
# 个人字段 —— 写进 local/status.json，只留在本机
LOCAL_FIELDS = ["status", "my_notes"]
# JSON 落盘的字段顺序：固定下来，多人协作时 git diff 才干净
JSON_ORDER = ["id"] + SHARED_FIELDS + ["created_at", "updated_at"]

# sqlite 索引表的列。改这里不需要迁移脚本 —— reindex 会 DROP 重建整张表。
INDEX_COLUMNS = ["id", "company", "position", "job_no", "category", "recruit_type", "locations",
                 "salary", "source", "url", "deadline", "tags", "notes", "jd",
                 "status", "my_notes", "status_updated_at", "updated_at", "created_at"]
# 列表接口返回的列：不含 jd/notes 这类大字段，它们只参与关键词搜索。
# url 要返回 —— 列表里的公司名是可以直接点开岗位页的外链。
LIST_COLUMNS = ["id", "company", "position", "job_no", "category", "recruit_type", "locations",
                "salary", "source", "url", "status", "deadline", "tags", "my_notes",
                "status_updated_at", "updated_at", "created_at"]
# 单值维度：精确等值筛选（同一维度内多值取 OR）
FILTER_COLUMNS = ["status", "company", "position", "category", "recruit_type", "source"]
# 多值列：索引层存成逗号拼接串，筛选走整词匹配，facets 拆开统计
MULTI_COLUMNS = ["locations", "tags"]
# 生成筛选下拉候选项的单值维度（多值列的候选项另外算）
FACET_COLUMNS = ["company", "position", "category", "recruit_type", "source"]
# 入库前需要 strip 的文本维度（查询侧也 strip，两边必须对称，否则永远筛不出来）
STRIP_COLUMNS = ["company", "position", "job_no", "category", "recruit_type", "source", "status"]

CV_PROMPT = """请阅读我提供的 CV，生成一份「CV 解读文件」，保存为 cv/<名字>.reading.md，格式严格如下：

# CV 解读：<名字>
## 目标方向
（求职方向清单，按优先级）
## 核心技能
（技能 + 一句话证据）
## 偏好与约束
（城市/薪资/工作类型等硬性偏好）
## 硬伤与短板
（投简历前必须知道的弱点）
## 关键词
（恰好 10 个，顿号分隔，单独占一行，不要序号不要解释。例如：分子动力学、Python、冰川建模
系统会把这一行提取成标签云展示，务必严格遵守格式）
## 匹配建议
（什么样的岗位值得投，什么样的直接跳过）

要求：只写 CV 里有据可查的内容，不要虚构。"""


# ---------------------------------------------------------------- 归一化工具

def norm_text(v):
    """文本维度归一：去首尾空白。写入侧和查询侧必须用同一套，否则「北京 」和「北京」会分裂成两个取值。"""
    return (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def norm_list(v):
    """多值字段归一（locations / tags）：接受 list 或逗号分隔字符串，去空白、去空项、去重。

    索引层把它们存成逗号拼接串，所以单个取值自身不能含逗号 —— 含了就换成空格，
    否则整词匹配会失效（存 ["A,B"] 时 tag=A / tag=B / tag=A,B 会全部命中）。
    """
    if isinstance(v, str):
        items = v.split(",")
    elif isinstance(v, (list, tuple)):
        items = [x if isinstance(x, str) else str(x) for x in v]
    else:
        items = []
    out = []
    for t in items:
        t = t.replace(",", " ").replace("，", " ").strip()
        if t and t not in out:
            out.append(t)
    return out


DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")


def norm_date(v):
    """日期归一成 YYYY-MM-DD。无法解析返回 ''。

    索引层必须存补零后的格式：deadline 的筛选是字符串比较，
    "2026-9-1" > "2026-10-01"（第 6 个字符 '9' > '1'），不归一就会漏掉快截止的岗位。
    """
    s = norm_text(v)
    if not s:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_keywords(content):
    """从解读文件的 ## 关键词 小节提取关键词列表（最多 10 个）。

    取小节后的第一个非空行整行 —— 不能用 [^\\n#]+ 之类的字符类去截，
    那会让 C# 这样的关键词把整行截断。分隔符不含 /，否则 CI/CD 会被拆成两个词。
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if not re.match(r"^##\s*关键词", line):
            continue
        for raw in lines[i + 1:]:
            t = raw.strip()
            if not t:
                continue
            if t.startswith("#"):      # 空小节，直接撞上了下一个标题
                return []
            # 整行被括号包起来时才脱括号，避免啃掉「机器学习(ML)」结尾的右括号
            if t[:1] in "（(" and t[-1:] in "）)":
                t = t[1:-1]
            t = t.strip("。 \t")
            return [k.strip() for k in re.split(r"[、,，]", t) if k.strip()][:10]
    return []


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def slugify(text):
    s = re.sub(r"[^\w一-鿿-]+", "-", text.strip().lower()).strip("-")
    return s or "job"


def canonical_id(job):
    """由岗位内容算出规范 id。

    有官方职位号就用「公司-职位号」：两个人各自录同一个岗位时必然算出同一个 id，
    于是重复会变成 git 冲突（看得见、要处理），而不是两条静默共存的记录。
    没有职位号时退回「公司-岗位名」，这时写法稍有出入就会漏判，只能靠去重检测兜底。
    """
    company = norm_text(job.get("company"))
    job_no = norm_text(job.get("job_no"))
    if company and job_no:
        return slugify(f"{company}-{job_no}")
    return slugify(f"{company}-{norm_text(job.get('position'))}")


def fuzzy_key(s):
    """把文本压成用于比对的指纹：去掉空白、标点、大小写差异。

    「AI分子动力学算法研究员 - AI for Science」和「AI分子动力学算法研究员-AI for science」
    应该被认成同一个岗位。
    """
    return re.sub(r"[^\w一-鿿]+", "", norm_text(s).lower())


def load_json(path):
    """宽松读 JSON，失败返回 None。调用方必须自己判断 None 并给出提示，不能静默跳过。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_atomic(path, data):
    """先写临时文件再 rename：中途崩溃不会留下半截 JSON。

    临时文件名带 pid + 线程 id —— 否则并发写会抢同一个 .tmp 文件，
    触发 FileNotFoundError 并丢更新。fsync 是为了掉电后不留下零长度文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 共享层：岗位 JSON

def job_path(job_id):
    """由 id 推出 JSON 路径；任何越界写法（路径穿越）返回 None。"""
    jid = norm_text(job_id)
    if not jid:
        return None
    try:
        p = (JOBS_DIR / f"{jid}.json").resolve()
    except (OSError, ValueError):
        return None
    return p if p.parent == JOBS_DIR.resolve() else None


def load_shared(job_id):
    """读一条共享岗位。id 缺失时用文件名兜底，保证各处对同一文件的 id 认定一致。"""
    p = job_path(job_id)
    if not p:
        return None
    job = load_json(p)
    if not isinstance(job, dict):
        return None
    job["id"] = norm_text(job.get("id")) or p.stem
    return job


def ordered_job(job):
    """按固定顺序整理共享字段；未知字段保留在后面。

    保留未知字段很重要：合作者可能跑着更新的版本、多写了几个字段，
    本机不该在改一次状态时把它们无声删掉。
    """
    ordered = {k: job[k] for k in JSON_ORDER if k in job}
    for k, v in job.items():
        if k not in ordered and k not in LOCAL_FIELDS:
            ordered[k] = v
    return ordered


def job_rev(job):
    """共享层内容指纹，用作乐观锁版本号。

    不能用 updated_at 充当版本号：它只精确到分钟，同一分钟内的两次修改指纹相同，
    冲突检测会失效。
    """
    payload = json.dumps(ordered_job(job), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def save_job(job):
    """写共享岗位 JSON。"""
    p = job_path(job.get("id"))
    if not p:
        raise ValueError(f"非法的岗位 id：{job.get('id')!r}")
    for k in MULTI_COLUMNS:
        if k in job:
            job[k] = norm_list(job[k])
    write_json_atomic(p, ordered_job(job))


# ---------------------------------------------------------------- 个人层：本地状态

def local_path():
    return LOCAL_DIR / "status.json"


def read_local_strict():
    """严格读个人状态表。文件不存在返回 {}；存在但读不出来抛 LocalStateError。

    这个区分是关键：把「解析失败」当成「空表」，接着的整表写回就会永久抹掉
    全部投递进度，而个人层不进 git，没有任何备份可回滚。
    """
    p = local_path()
    if not p.exists():
        return {}
    if not p.read_text(encoding="utf-8", errors="replace").strip():
        return {}      # 0 字节 / 全空白：没有进度可丢，当成空表而不是拦住整个服务
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise LocalStateError(
            f"个人状态文件解析失败：{p}\n（{e}）\n"
            f"为避免覆盖掉你的投递进度，本次写入已拒绝。请修复该文件，"
            f"或先把它改名备份后重试。") from e
    if not isinstance(d, dict):
        raise LocalStateError(f"个人状态文件顶层不是对象：{p}，本次写入已拒绝。")
    return d


def load_local():
    """宽松读（只读路径用）：损坏时退化成空表，保证列表页还能把岗位显示出来。"""
    try:
        return read_local_strict()
    except LocalStateError:
        return {}


def update_local(job_id, patch):
    """原子更新单个岗位的个人状态。注意：不碰共享 JSON，因此不会产生 git diff。"""
    with _LOCAL_LOCK, local_lock():
        table = read_local_strict()       # 损坏就抛错，绝不整表覆盖
        rec = table.get(job_id)
        rec = dict(rec) if isinstance(rec, dict) else {}
        rec.update(patch)
        rec["updated_at"] = now()
        table[job_id] = rec
        write_json_atomic(local_path(), table)
        return rec


def merge_local(job, table=None):
    """把个人状态并进共享岗位记录，得到「我看到的」完整视图。"""
    rec = (load_local() if table is None else table).get(job.get("id"))
    if not isinstance(rec, dict):     # 手改坏了某一条也不该让整个接口 500
        rec = {}
    merged = dict(job)
    merged["_rev"] = job_rev(job)                 # 前端保存时回传，用于冲突检测
    for k in MULTI_COLUMNS:                       # 列表接口和单条接口的类型必须一致
        merged[k] = norm_list(job.get(k))
    merged["status"] = rec.get("status") or "待投递"
    merged["my_notes"] = rec.get("my_notes", "")
    merged["status_updated_at"] = rec.get("updated_at", "")
    return merged


def _upgrade_shared(job):
    """把一条旧格式的共享岗位升级到当前字段结构。返回 True 表示确实改了。

    处理三件事（都幂等）：
      1. location（单值字符串）→ locations（数组）—— 一个岗位常常多地可选，
         单值字段只能存第一个，其余信息就丢了
      2. tags 里混进来的城市名移除 —— 它们已经在 locations 里，重复出现会污染标签栏
      3. tags 里的招聘类型（校招/社招/实习）移到 recruit_type —— 「校招」和「AI4S」
         不是同一类东西，混在一个标签栏里没法用
    """
    changed = False
    if "location" in job:
        locs = norm_list(job.pop("location"))
        job["locations"] = norm_list(list(job.get("locations") or []) + locs)
        changed = True
    locs = norm_list(job.get("locations"))
    keep, rt = [], norm_text(job.get("recruit_type"))
    for t in norm_list(job.get("tags")):
        if t in locs:                      # 城市已经在 locations 里了，去重
            changed = True
        elif t in KNOWN_CITIES:            # 早期版本把多城市塞进了 tags，归位
            locs.append(t)
            changed = True
        elif t in RECRUIT_TYPES:
            if not rt:
                rt = t                     # 招聘类型提升为独立字段
            changed = True
        else:
            keep.append(t)
    if changed:
        job["tags"] = keep
        job["locations"] = locs
        if rt:
            job["recruit_type"] = rt
    return changed


def migrate():
    """把旧版共享 JSON 升级到当前结构（幂等），返回被改动的岗位 id 列表。

    两类升级：
      - status / my_notes 搬进个人状态文件（私人数据，不该进 git）
      - 共享字段的结构升级，见 _upgrade_shared

    合作者用旧版本写出的 JSON 被 pull 下来后，也会在下次启动时自动处理。
    """
    moved = []
    # 也要拿 _JOBS_LOCK：这个函数会重写共享 JSON，和 POST/PUT 走的是同一批文件
    with _JOBS_LOCK, _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        local_dirty = False
        for f in sorted(JOBS_DIR.glob("*.json")):
            job = load_json(f)
            if not isinstance(job, dict):
                continue
            jid = norm_text(job.get("id")) or f.stem
            stale = [k for k in LOCAL_FIELDS if k in job]
            if stale:
                # 本地已有记录以本地为准，只从共享文件里摘掉字段，避免覆盖真实进度
                if jid not in table:
                    rec = {k: job[k] for k in stale}
                    rec["updated_at"] = job.get("updated_at", now())
                    table[jid] = rec
                    local_dirty = True
                for k in stale:
                    job.pop(k)
            if not (stale or _upgrade_shared(job)):
                continue
            job["id"] = jid
            # 按文件原路径写回（文件名可能和 id 不一致），并保留未知字段
            write_json_atomic(f, ordered_job(job))
            moved.append(jid)
        if local_dirty:
            write_json_atomic(local_path(), table)
    return moved


migrate_status = migrate   # 旧名字，保持向后兼容


def rename_job(old_id, new_id):
    """给岗位换 id：共享文件改名 + 个人状态的键跟着搬。

    两边必须一起改 —— 只改文件名的话，投递进度就跟岗位脱节了（状态还挂在旧 id 上，
    界面上看到的会变回「待投递」）。目标 id 已被占用时返回 False 不动手，
    那种情况说明真撞车了，交给去重流程处理。
    """
    old_p, new_p = job_path(old_id), job_path(new_id)
    if not old_p or not new_p or not old_p.exists() or new_p.exists():
        return False
    job = load_json(old_p)
    if not isinstance(job, dict):
        return False
    job["id"] = new_id
    write_json_atomic(new_p, ordered_job(job))
    old_p.unlink(missing_ok=True)
    with _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        if old_id in table:
            table[new_id] = {**table.get(new_id, {}), **table.pop(old_id)}
            write_json_atomic(local_path(), table)
    return True


def migrate_ids():
    """把补了官方职位号的岗位升级到规范 id（文件随之改名）。

    只在「有 job_no 且当前 id 不是按它算出来的」时才动，所以改公司名或岗位名
    不会引发改名 —— id 保持稳定，只有补上职位号时会换一次。
    """
    renamed = []
    with _JOBS_LOCK:
        for f in sorted(JOBS_DIR.glob("*.json")):
            job = load_json(f)
            if not isinstance(job, dict) or not norm_text(job.get("job_no")):
                continue
            old = norm_text(job.get("id")) or f.stem
            new = canonical_id(job)
            if old != new and rename_job(old, new):
                renamed.append((old, new))
    return renamed


def find_duplicates():
    """扫描共享岗位，找出重复组。

    三个信号，可靠性递减：
      - 同公司 + 同官方职位号  → 铁定是同一个岗位，可以自动合并
      - 同一个投递链接        → 同上
      - 同公司 + 岗位名指纹相同 → 只是疑似（同一家公司不同部门可能有同名岗位），
                                只报告，不自动合并
    返回 [{"reason", "ids", "auto"}]，auto=True 的才允许自动合并。
    """
    jobs = []
    for f in sorted(JOBS_DIR.glob("*.json")):
        job = load_json(f)
        if isinstance(job, dict):
            job["id"] = norm_text(job.get("id")) or f.stem
            jobs.append(job)

    groups, seen = [], set()

    def collect(keyfn, reason, auto):
        buckets = {}
        for j in jobs:
            k = keyfn(j)
            if k:
                buckets.setdefault(k, []).append(j["id"])
        for k, ids in buckets.items():
            if len(ids) < 2 or frozenset(ids) in seen:
                continue
            seen.add(frozenset(ids))
            groups.append({"reason": reason.format(k=k), "ids": ids, "auto": auto})

    collect(lambda j: (f"{norm_text(j.get('company'))}|{norm_text(j.get('job_no'))}"
                       if norm_text(j.get("company")) and norm_text(j.get("job_no")) else ""),
            "同一家公司的同一个职位号（{k}）", True)
    collect(lambda j: norm_text(j.get("url")), "投递链接完全相同（{k}）", True)
    collect(lambda j: (f"{fuzzy_key(j.get('company'))}|{fuzzy_key(j.get('position'))}"
                       if fuzzy_key(j.get("company")) and fuzzy_key(j.get("position")) else ""),
            "公司与岗位名几乎一样（{k}）", False)
    return groups


def _pick_survivor(jobs):
    """挑出重复组里要保留的那条：信息最全的；打平时取先录进来的。"""
    def filled(j):
        return sum(1 for k in SHARED_FIELDS if j.get(k) not in (None, "", [], {}))
    return sorted(jobs, key=lambda j: (-filled(j), norm_text(j.get("created_at")) or "9999",
                                       j["id"]))[0]


def merge_group(ids):
    """把一组重复岗位合并成一条，返回 (保留的 id, 被删掉的 id 列表)。

    共享层：空字段用其他条补齐；locations / tags 取并集；notes 内容不同就拼起来，
    宁可留着让人删，也不要悄悄丢掉别人写的情报。
    个人层：状态取走得最远的那个（见 STATUS_RANK），个人备注拼接。
    """
    jobs = [j for j in (load_shared(i) for i in ids) if j]
    if len(jobs) < 2:
        return None, []
    keep = _pick_survivor(jobs)
    others = [j for j in jobs if j["id"] != keep["id"]]

    merged = dict(keep)
    for j in others:
        for k in SHARED_FIELDS:
            if k in MULTI_COLUMNS:
                merged[k] = norm_list(list(merged.get(k) or []) + list(j.get(k) or []))
            elif not norm_text(merged.get(k)) and norm_text(j.get(k)):
                merged[k] = j[k]
            elif k == "notes" and norm_text(j.get(k)) and norm_text(j[k]) not in norm_text(merged.get(k)):
                merged[k] = f"{merged[k]}\n{j[k]}".strip()
    merged["updated_at"] = now()

    with _JOBS_LOCK, _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        recs = [table.get(i) for i in ids if isinstance(table.get(i), dict)]
        if recs:
            best = max(recs, key=lambda r: (STATUS_RANK.get(r.get("status", ""), 0),
                                            r.get("updated_at", "")))
            notes = [r["my_notes"] for r in recs if norm_text(r.get("my_notes"))]
            rec = dict(best)
            if notes:
                rec["my_notes"] = "\n".join(dict.fromkeys(notes))
            table[keep["id"]] = rec
        for j in others:
            table.pop(j["id"], None)
            p = job_path(j["id"])
            if p:
                p.unlink(missing_ok=True)
        write_json_atomic(local_path(), table)
        save_job(merged)
    return keep["id"], [j["id"] for j in others]


def split_tag_fields(data):
    """把请求里误写进 tags 的城市与招聘类型归位到各自的字段。

    写入侧也要做这件事，否则「新增时写进 tags 的校招」会一直留到下次 reindex 才被
    迁移清理，两条路径的行为不一致。

    城市只在请求同时提交了 locations 时才归位 —— PUT 是部分更新，不能因为整理
    tags 就把请求里没提到的 locations 冲掉；没法安全归位时就留在 tags 里，交给
    migrate 兜底。
    """
    if "tags" not in data:
        return
    keep, cities, rt = [], [], norm_text(data.get("recruit_type"))
    for t in norm_list(data["tags"]):
        if t in KNOWN_CITIES:
            cities.append(t)
        elif t in RECRUIT_TYPES:
            rt = rt or t
        else:
            keep.append(t)
    if rt:
        data["recruit_type"] = rt
    if cities and "locations" in data:
        data["locations"] = norm_list(list(data["locations"] or []) + cities)
    else:
        keep.extend(cities)
    data["tags"] = keep


# ---------------------------------------------------------------- 索引层：sqlite

def _create_index_table(conn):
    cols = ", ".join(f"{c} TEXT" + (" PRIMARY KEY" if c == "id" else "") for c in INDEX_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 读不被写阻塞，减少 database is locked
    _create_index_table(conn)
    return conn


def upsert_index(conn, merged):
    """把「共享岗位 + 个人状态」的合并视图写进索引表，写入侧统一归一。"""
    row = []
    for c in INDEX_COLUMNS:
        v = merged.get(c, "")
        if c in MULTI_COLUMNS:
            v = ",".join(norm_list(v))
        elif c == "deadline":
            v = norm_date(v)
        elif c in STRIP_COLUMNS:
            v = norm_text(v)
        else:
            v = v if isinstance(v, str) else ("" if v is None else str(v))
        row.append(v)
    conn.execute("INSERT OR REPLACE INTO jobs (%s) VALUES (%s)" %
                 (", ".join(INDEX_COLUMNS), ",".join("?" * len(INDEX_COLUMNS))), row)


def index_one(job):
    """单条岗位增量入索引（新增/编辑后调用，省得整库重建）。"""
    conn = get_db()
    try:
        upsert_index(conn, merge_local(job))
        conn.commit()
    finally:
        conn.close()


def reindex():
    """整库重建索引。返回 {count, skipped, warnings}。

    读不出来的文件必须报出来 —— 静默跳过会让岗位凭空消失，而用户看到的
    还是一句「索引已重建：N 条」。
    """
    table = load_local()
    conn = get_db()
    skipped, warnings, seen = [], [], {}
    try:
        conn.execute("DROP TABLE IF EXISTS jobs")
        _create_index_table(conn)
        for f in sorted(JOBS_DIR.glob("*.json")):
            job = load_json(f)
            if not isinstance(job, dict):
                skipped.append(f"{f.name}：读不出来（JSON 格式错误，或含 git 冲突标记）")
                continue
            jid = norm_text(job.get("id")) or f.stem   # 缺 id 用文件名兜底，不丢数据
            if jid in seen:
                skipped.append(f"{f.name}：id「{jid}」与 {seen[jid]} 重复")
                continue
            seen[jid] = f.name
            job["id"] = jid
            if norm_text(job.get("deadline")) and not norm_date(job.get("deadline")):
                warnings.append(f"{f.name}：截止日期「{job['deadline']}」格式无法识别，"
                                f"该岗位不会出现在按截止日期的筛选里（应为 YYYY-MM-DD）")
            if job.get("recruit_type") and norm_text(job["recruit_type"]) not in RECRUIT_TYPES:
                warnings.append(f"{f.name}：招聘类型「{job['recruit_type']}」不在本版本枚举内，已保留原值")
            if job.get("category") and norm_text(job["category"]) not in CATEGORIES:
                warnings.append(f"{f.name}：分类「{job['category']}」不在本版本枚举内，"
                                f"已保留原值（筛选下拉里可以选到它）")
            upsert_index(conn, merge_local(job, table))
        conn.commit()
    finally:
        conn.close()
    return {"count": len(seen), "skipped": skipped, "warnings": warnings,
            "duplicates": find_duplicates()}


def _like(s):
    """转义 LIKE 通配符，避免标签/关键词里的 % _ 被当成通配符。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_jobs(query):
    """按筛选条件查索引表。

    筛选语义：同一维度内多个取值取 OR（北京 或 杭州），不同维度之间取 AND；
    标签是例外，多个标签取 AND（同时具备这几个标签）。
    """
    def vals(key):
        return [v.strip() for v in query.get(key, []) if v.strip()]

    def one(key):
        return (query.get(key, [""])[0] or "").strip()

    sql = f"SELECT {', '.join(LIST_COLUMNS)} FROM jobs WHERE 1=1"
    args = []
    for key in FILTER_COLUMNS:
        vs = vals(key)
        if vs:
            sql += f" AND {key} IN ({','.join('?' * len(vs))})"
            args += vs
    # 地点是多值列（一个岗位可能多地可选）。同维度多个取值取 OR：
    # 选了「上海」和「深圳」= 这两地任意一个能去的岗位。
    locs = vals("location")
    if locs:
        sql += " AND (" + " OR ".join(
            "(','||locations||',') LIKE ? ESCAPE '\\'" for _ in locs) + ")"
        args += [f"%,{_like(v)},%" for v in locs]
    # 标签也是多值列，但多个取值取 AND：勾了两个标签＝两个都得有。
    # 两侧补逗号做整词匹配，避免「算法」命中「算法工程」。
    for t in vals("tag"):
        sql += " AND (','||tags||',') LIKE ? ESCAPE '\\'"
        args.append(f"%,{_like(t)},%")
    dl = norm_date(one("deadline_before"))
    if dl:
        sql += " AND deadline!='' AND deadline<=?"
        args.append(dl)
    # 已归档默认收起来，除非用户显式筛「已归档」
    if one("hide_archived") == "1" and "已归档" not in vals("status"):
        sql += " AND status!='已归档'"
    q = one("q")
    if q:
        cols = ["company", "position", "locations", "category", "recruit_type",
                "tags", "notes", "jd", "my_notes"]
        sql += " AND (" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")"
        args += [f"%{_like(q)}%"] * len(cols)
    # 加次级排序键：updated_at 只精确到分钟，同分钟的多条否则顺序不定
    sql += " ORDER BY updated_at DESC, created_at DESC, id"

    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        # 各维度已有取值：供筛选下拉与新增岗位时的输入建议（鼓励复用已有写法）
        facets = {c: [r[0] for r in conn.execute(
            f"SELECT DISTINCT {c} FROM jobs WHERE {c}!='' ORDER BY {c}")] for c in FACET_COLUMNS}
        # 多值列的候选项要拆开统计。地点的 facet key 用单数 location，
        # 和查询参数名保持一致（?location=上海）。
        for col, key in (("locations", "location"), ("tags", "tags")):
            vs = set()
            for (v,) in conn.execute(f"SELECT {col} FROM jobs WHERE {col}!=''"):
                vs.update(x for x in v.split(",") if x)
            facets[key] = sorted(vs)
    finally:
        conn.close()
    # 枚举外的存量取值也要能筛（比如合作者跑着更新的版本，加了新方向）
    for key, enum in (("category", CATEGORIES), ("recruit_type", RECRUIT_TYPES)):
        facets[key] = sorted(set(facets[key]) | set(enum),
                             key=lambda v: (v not in enum, enum.index(v) if v in enum else 0, v))
    for r in rows:
        for k in MULTI_COLUMNS:
            r[k] = [x for x in (r.get(k) or "").split(",") if x]
    return {"jobs": rows, "facets": facets}


# ---------------------------------------------------------------- git 同步

def _git(args, cwd, timeout=120):
    """跑一条 git 命令。禁掉交互式认证，否则会挂在提示符上直到超时。"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")}
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout, env=env)


def _unmerged_paths(repo):
    """列出处于未合并（冲突）状态的文件。"""
    try:
        p = _git(["status", "--porcelain"], cwd=repo, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in p.stdout.splitlines():
        # 冲突态的两位状态码：UU / AA / DD，或任一位是 U
        if len(line) > 3 and (line[0] == "U" or line[1] == "U" or line[:2] in ("AA", "DD")):
            out.append(line[3:].strip())
    return out


def _rebase_in_progress(repo):
    """仓库是否停在 rebase 中间态。

    不能直接看 repo/.git/rebase-merge —— 在 linked worktree 里 .git 是文件不是目录，
    真实状态在 .git/worktrees/<name>/ 下面，必须问 git 要路径。
    """
    for name in ("rebase-merge", "rebase-apply"):
        try:
            p = _git(["rev-parse", "--git-path", name], cwd=repo, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode != 0:
            continue
        path = Path(p.stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def git_sync():
    """git pull --rebase --autostash 拉取合作者维护的招聘数据，然后重建索引。

    只拉不推：推送涉及个人判断（写什么 commit message、要不要先 review），留给人做。

    --autostash 是必需的：在 WebUI 里编辑过任何岗位后工作区就是脏的，
    不 autostash 的话 git 会以退出码 128 拒绝 rebase，同步按钮等于常年失效。
    """
    if not JOBS_DIR.is_dir():
        return {"ok": False, "message": f"数据目录不存在：{JOBS_DIR}\n"
                                        f"（外接硬盘没插？目录被改名？检查 config.json）"}
    try:
        p = _git(["rev-parse", "--show-toplevel"], cwd=JOBS_DIR, timeout=20)
    except FileNotFoundError:
        return {"ok": False, "message": "找不到 git 命令，请先安装 git。"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": f"无法定位 git 仓库：{e}"}
    if p.returncode != 0:
        return {"ok": False, "message": f"{JOBS_DIR} 不在任何 git 仓库里，无法同步。\n"
                                        f"（数据目录若配置在仓库外，就只能各机器独立使用）"}
    repo = Path(p.stdout.strip())

    try:
        p = _git(["pull", "--rebase", "--autostash"], cwd=repo)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "git pull 超时（120 秒）。可能在等认证，"
                                        "请到终端手动执行一次 git pull 完成认证。"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": f"git pull 执行失败：{e}"}

    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        # 失败时可能停在 rebase 中间态（detached HEAD + 冲突标记），必须恢复干净状态，
        # 否则用户接着点「重建索引」会看到岗位凭空消失
        tail = ""
        if _rebase_in_progress(repo):
            try:
                ab = _git(["rebase", "--abort"], cwd=repo, timeout=60)
                tail = ("\n\n仓库有冲突，已自动回滚到同步前的状态（git rebase --abort），"
                        "你的改动已还原。请到终端手动处理冲突。"
                        if ab.returncode == 0 else
                        "\n\n⚠️ 自动回滚失败，仓库仍停在 rebase 中间态，"
                        "请到终端执行：git rebase --abort")
            except (OSError, subprocess.SubprocessError):
                tail = "\n\n⚠️ 仓库可能停在 rebase 中间态，请到终端执行 git rebase --abort。"
        return {"ok": False, "message": (out or f"git pull 失败（退出码 {p.returncode}）") + tail}

    # 退出码 0 不等于干净：fast-forward 成功、但 autostash 把本地改动贴回来时冲突，
    # git 也返回 0。此时共享 JSON 里已经写进了冲突标记，改动被留在 stash 里。
    conflicted = _unmerged_paths(repo)
    if conflicted:
        return {"ok": False, "message":
                "已拉到合作者的改动，但你本地未提交的改动在贴回来时和它冲突了。\n\n"
                "冲突文件：\n  " + "\n  ".join(conflicted) + "\n\n"
                "你的改动安全地存在 git stash 里（终端跑 git stash list 能看到）。\n"
                "请到终端处理这些文件里的冲突标记，然后 git add，再 git stash drop。\n"
                "在处理完之前，这些岗位在列表里是看不到的。\n\n" + out}

    migrate()
    migrate_ids()
    return {"ok": True, "message": out, **reindex()}


# ---------------------------------------------------------------- HTTP

def safe_route(fn):
    """把未捕获异常转成 500 JSON。

    不这么做的话 BaseHTTPRequestHandler 会直接关闭连接、一个字节都不发，
    前端的 fetch 直接 reject，用户看到的是「点了没反应」。
    """
    @functools.wraps(fn)
    def wrapper(self):
        try:
            return fn(self)
        except LocalStateError as e:
            self.safe_error(str(e))
        except Exception as e:
            traceback.print_exc()
            self.safe_error(f"服务器内部错误：{type(e).__name__}: {e}")
    return wrapper


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默日志
        pass

    # ---- helpers ----
    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def safe_error(self, msg):
        """尽力发出错误响应；若响应已经发出去了就只能作罢。"""
        try:
            self.send_json({"error": msg}, 500)
        except Exception:
            pass

    def send_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    def bad_values(self, data):
        """校验受控枚举与日期格式。返回错误信息，合法则返回 None。

        只对「显式传了且非空」的值校验：空串一律当成「不设置」而不是「清空」。
        编辑已有岗位时只校验**真正改动过**的字段（见 do_PUT）——否则合作者用更新
        版本写的分类（本机枚举里没有）会在原样回传时被 400 拦下，逼着前端清空它。
        """
        c = norm_text(data.get("category"))
        if c and c not in CATEGORIES:
            return f"category 必须是以下之一：{'/'.join(CATEGORIES)}"
        r = norm_text(data.get("recruit_type"))
        if r and r not in RECRUIT_TYPES:
            return f"recruit_type 必须是以下之一：{'/'.join(RECRUIT_TYPES)}"
        s = norm_text(data.get("status"))
        if s and s not in STATUSES:
            return f"status 必须是以下之一：{'/'.join(STATUSES)}"
        d = norm_text(data.get("deadline"))
        if d and not norm_date(d):
            return f"截止日期「{d}」格式不对，应为 YYYY-MM-DD"
        return None

    @staticmethod
    def shared_changed(data, job):
        """挑出真正发生变化的共享字段。

        None 与空串必须视为相等：前端每次提交全部字段，而手写的岗位 JSON 常常
        省略空字段，不归一就会把 7 个 "" 塞进别人的文件并 bump updated_at。
        """
        out = {}
        for k in SHARED_FIELDS:
            if k not in data:
                continue
            new, old = data[k], job.get(k)
            if k in MULTI_COLUMNS:
                if norm_list(new) != norm_list(old):
                    out[k] = norm_list(new)
            elif (new or "") != (old or ""):
                out[k] = new
        return out

    # ---- routes ----
    @safe_route
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/jobs":
            d = list_jobs(query)
            return self.send_json({**d, "statuses": STATUSES, "categories": CATEGORIES,
                                   "recruit_types": RECRUIT_TYPES})
        m = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if m:
            job = load_shared(m.group(1))
            return self.send_json(merge_local(job) if job else {"error": "not found"},
                                  200 if job else 404)
        if path == "/api/cv":
            CV_DIR.mkdir(parents=True, exist_ok=True)
            originals, readings = [], []
            for f in sorted(CV_DIR.iterdir()):
                if not f.is_file() or f.name == "README.md":
                    continue
                info = {"name": f.name, "size": f.stat().st_size,
                        "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                if f.name.endswith(".reading.md"):
                    content = f.read_text(encoding="utf-8", errors="replace")
                    info["content"] = content
                    info["keywords"] = parse_keywords(content)
                    readings.append(info)
                else:
                    originals.append(info)
            return self.send_json({"files": originals, "readings": readings, "prompt": CV_PROMPT})
        m = re.fullmatch(r"/api/cv/file/(.+)", path)
        if m:
            f = CV_DIR / m.group(1)      # path 已经 unquote 过一次，不能再解一次
            if f.is_file() and f.resolve().parent == CV_DIR.resolve():
                return self.send_file(f, "application/octet-stream")
            return self.send_json({"error": "not found"}, 404)
        self.send_json({"error": "not found"}, 404)

    @safe_route
    def do_POST(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path == "/api/reindex":
            migrate()
            migrate_ids()
            return self.send_json({"ok": True, **reindex()})
        if path == "/api/sync":
            return self.send_json(git_sync())
        if path == "/api/dedupe":
            # 只合并强信号的重复组（同职位号 / 同链接）。名字相似属于疑似，
            # 同一家公司不同部门完全可能有同名岗位，那种要人来判断。
            merged = []
            for g in find_duplicates():
                if not g["auto"]:
                    continue
                keep, dropped = merge_group(g["ids"])
                if keep:
                    merged.append({"keep": keep, "dropped": dropped, "reason": g["reason"]})
            return self.send_json({"ok": True, "merged": merged, **reindex()})
        if path == "/api/jobs":
            data = self.read_body()
            split_tag_fields(data)
            if not norm_text(data.get("company")) or not norm_text(data.get("position")):
                return self.send_json({"error": "company 和 position 必填"}, 400)
            err = self.bad_values(data)
            if err:
                return self.send_json({"error": err}, 400)
            # 先探一次个人层：它坏了就别写共享层，否则会留下一个谁也删不掉的孤儿岗位
            read_local_strict()
            with _JOBS_LOCK:      # id 分配 + 落盘要一起做，否则并发新增会互相覆盖
                base = canonical_id(data)
                # 有职位号时 base 是稳定的，撞上说明这个岗位已经录过了 —— 与其造一条
                # 「-2」的重复记录，不如直接告诉用户去哪看
                if norm_text(data.get("job_no")) and (JOBS_DIR / f"{base}.json").exists():
                    return self.send_json(
                        {"error": f"这个岗位已经录过了（职位号 {data['job_no']}），"
                                  f"在列表里搜「{norm_text(data.get('company'))}」就能找到。",
                         "existing_id": base}, 409)
                jid, i = base, 2
                while (JOBS_DIR / f"{jid}.json").exists():
                    jid, i = f"{base}-{i}", i + 1
                job = {"id": jid, "locations": [], "tags": [],
                       "created_at": now(), "updated_at": now()}
                for k in SHARED_FIELDS:
                    if k in data:
                        job[k] = data[k]
                save_job(job)
            # 落盘要用归一值：校验走的是 norm_text，落盘若用原值，" 已归档 " 这种
            # 带空格的状态会造出一条既藏不掉也筛不出来的岗位
            patch = {k: norm_text(data[k]) for k in LOCAL_FIELDS if norm_text(data.get(k))}
            if patch:
                update_local(jid, patch)
            index_one(job)
            return self.send_json({"ok": True, "id": jid})
        m = re.fullmatch(r"/api/jobs/([^/]+)/status", path)
        if m:
            status = norm_text(self.read_body().get("status"))
            if status not in STATUSES:
                return self.send_json({"error": "非法状态"}, 400)
            job = load_shared(m.group(1))
            if not job:
                return self.send_json({"error": "not found"}, 404)
            update_local(job["id"], {"status": status})   # 只写本地，共享 JSON 不动
            index_one(job)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    @safe_route
    def do_PUT(self):
        m = re.fullmatch(r"/api/jobs/([^/]+)",
                         urllib.parse.unquote(urllib.parse.urlparse(self.path).path))
        if not m:
            return self.send_json({"error": "not found"}, 404)
        job = load_shared(m.group(1))
        if not job:
            return self.send_json({"error": "not found"}, 404)
        data = self.read_body()
        split_tag_fields(data)
        err = self.bad_values({k: data[k] for k in LOCAL_FIELDS if k in data})
        if err:
            return self.send_json({"error": err}, 400)

        with _JOBS_LOCK:
            job = load_shared(m.group(1))    # 拿锁后重读，避免用过期快照回写
            if not job:
                return self.send_json({"error": "not found"}, 404)
            touched = self.shared_changed(data, job)
            err = self.bad_values(touched)   # 只校验改动过的共享字段
            if err:
                return self.send_json({"error": err}, 400)
            # 乐观锁：客户端回传打开编辑框时的内容指纹，对不上说明这条被别人（或
            # 另一个标签页、或刚同步下来的合作者改动）改过，拒绝盲写
            base = norm_text(data.get("base_rev"))
            if touched and not base:
                return self.send_json({
                    "error": "改共享字段必须带 base_rev（先 GET 这条岗位拿 _rev），"
                             "否则可能覆盖掉别人的改动。确实要强制覆盖就传 base_rev=\"*\"。"}, 400)
            if touched and base != "*" and base != job_rev(job):
                return self.send_json({
                    "error": "这条岗位在你打开编辑框之后被改过了（可能来自另一个标签页，"
                             "或刚同步下来的合作者改动）。请关闭弹窗重新打开这条岗位，再改一次。",
                    "conflict": True}, 409)
            if touched:
                job.update(touched)
                job["updated_at"] = now()
                save_job(job)

        patch = {}
        if norm_text(data.get("status")):     # 空 status 视为「不改」，绝不清空投递进度
            patch["status"] = norm_text(data["status"])
        if "my_notes" in data:                # 个人备注允许清空
            patch["my_notes"] = data["my_notes"]
        if patch:
            update_local(job["id"], patch)
        index_one(job)
        self.send_json({"ok": True, "shared_changed": bool(touched)})


def main():
    ap = argparse.ArgumentParser(description="job-stock 服务器")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data-dir", help="数据目录（内含 jobs/ local/ data/），覆盖 config.json")
    ap.add_argument("--cv-dir", help="CV 目录，覆盖 config.json")
    ap.add_argument("--reindex", action="store_true", help="只重建索引后退出")
    args = ap.parse_args()
    configure(args.data_dir, args.cv_dir)

    # 数据目录不存在就停下来问清楚，不要静默建一个空的然后显示「0 条岗位」——
    # 外接硬盘没插、config.json 写错时，用户会以为岗位全丢了
    if not JOBS_DIR.is_dir():
        sys.exit(f"数据目录不存在：{JOBS_DIR}\n"
                 f"请检查 {CONFIG_PATH} 里的 data_dir，或先跑一次：python install.py")

    try:
        moved = migrate()
    except LocalStateError as e:
        # 只读路径本来就能降级，不该因为写不了就连岗位列表都打不开
        print(f"⚠️  {e}\n服务器照常启动，但个人层是只读的：列表能看，改状态会报错。")
        moved = []
    for old, new in migrate_ids():
        print(f"岗位 id 升级为职位号形式：{old} → {new}")
    if moved:
        print(f"已升级 {len(moved)} 条岗位的数据格式（投递状态搬到 {local_path()}；"
              f"地点改为多值字段；标签里的城市与招聘类型归位）")

    r = reindex()
    for line in r["skipped"]:
        print(f"⚠️  跳过 {line}")
    for line in r["warnings"]:
        print(f"⚠️  {line}")
    for g in r["duplicates"]:
        print(f"⚠️  疑似重复（{g['reason']}）：{' / '.join(g['ids'])}"
              f"{'  ← 可在网页点「合并重复」自动处理' if g['auto'] else '  ← 需人工确认'}")
    if args.reindex:
        print(f"已重建索引：{r['count']} 条岗位（{JOBS_DIR}）")
        return
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 被占用，换个端口：python server.py --port 8771")
        sys.exit(1)
    print(f"job-stock 已启动：http://localhost:{args.port}  （索引 {r['count']} 条岗位，Ctrl+C 停止）")
    print(f"  共享招聘数据：{JOBS_DIR}\n  个人状态（不进 git）：{local_path()}\n  CV 目录：{CV_DIR}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
