#!/usr/bin/env python3
"""job-stock 服务器：纯 Python 标准库，零依赖的本地求职管理工具。

用法：
    python server.py [--port 8770]     # 启动 WebUI（默认自动打开浏览器）
    python server.py --no-open         # 启动但不自动打开浏览器
    python server.py --reindex         # 只重建 sqlite 索引后退出

数据全在本机，不依赖任何外部服务或版本库：

    岗位层  <data>/jobs/<id>.json       招聘信息；每次改写前自动留 3 份历史版本
                                       （jobs/.history/<id>/，替代 git 的后悔药）
    个人层  <data>/local/status.json    投递状态 + 个人备注；写前快照进 local/backups/
    索引层  <data>/data/jobs.db         由上面两层派生的 sqlite 索引，可随时重建

一条贯穿全文件的原则：**本版本表达不了的值，保留并告警，绝不静默归零。**
岗位 JSON 也可能是手写的，「控件读不出来」不等于「用户想清空」。
"""
import argparse
import difflib
import functools
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import traceback
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta as _timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"
SERVER_VERSION = "0.4.0"   # 随功能性改动一起更新；前端用它检测「网页新、后台旧」

_my_name_cache = {"n": None, "done": False}


def my_name():
    """本机身份：config.json 的 my_name，没有就返回空串。

    只用于 created_by / updated_by 的自动注入。缓存一次结果 ——
    每次保存都重读一遍配置文件没有必要。
    """
    if _my_name_cache["done"]:
        return _my_name_cache["n"]
    n = ""
    if CONFIG_PATH.exists():
        try:
            n = norm_text(json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("my_name"))
        except Exception:
            n = ""
    _my_name_cache["n"], _my_name_cache["done"] = n, True
    return n

# 数据位置：默认都在工具目录内；可用 config.json 或 CLI 参数 --data-dir / --cv-dir 覆盖。
JOBS_DIR = ROOT / "jobs"
LOCAL_DIR = ROOT / "local"
CV_DIR = ROOT / "cv"
DB_PATH = ROOT / "data" / "jobs.db"

# 写锁。全局锁序（所有写路径必须一致遵守，否则死锁）：
# → jobs_lock()                              岗位层跨进程锁（可重入）
# → _JOBS_LOCK（RLock）                      岗位层进程内锁
# → _LOCAL_LOCK（RLock）                     个人层进程内锁
# → local_lock()＝FileLock(".status.lock")   个人层跨进程锁（最内层；flock 按
#                                            打开的文件描述计，同进程不可重入，
#                                            绝不在持它的 with 块里再取第二把）
_LOCAL_LOCK = threading.RLock()   # 个人状态文件
_JOBS_LOCK = threading.RLock()    # 共享岗位 JSON（尤其是新增时的 id 分配）
_jobs_flock_depth = threading.local()


class LocalStateError(Exception):
    """个人状态文件损坏。宁可报错也不能当成空表继续写 —— 那会整表覆盖掉全部投递进度。"""


class BadRequest(Exception):
    """请求体不合法（不是 JSON / 顶层不是对象 / 超长）。

    必须显式转成 400，绝不能吞掉：吞成空 dict 的话，PUT 会返回 200
    「保存成功」但什么都没写，POST 会报出误导性的「company 必填」。
    """


# 请求体大小上限。JD 全文再长也就几 KB，5MB 足够宽裕；设上限是防异常客户端
# 用超大 body 把线程和内存耗住。
MAX_BODY = 5 * 1024 * 1024


class LockBusy(Exception):
    """非阻塞模式下，锁被另一个进程持有。"""


class FileLock:
    """跨进程文件锁。

    _LOCAL_LOCK 只在进程内有效，而「UI 开着又在终端跑了一次 --reindex」「起了第二个
    实例」「数据目录放在云盘上被两台机器写」都会让两个进程同时做读-改-写，
    实测 3 进程 × 60 次改状态会丢掉三分之一的更新，且完全静默。

    blocking=True（默认）：拿不到锁时降级成只有进程内锁 —— 总比直接失败好，
    但必须大声告警，不允许无声无息地降级。
    blocking=False：拿不到锁立刻抛 LockBusy。同步/推送这类一拿就是几十秒的锁，
    「排队等」不如「告诉你现在忙」。
    """

    def __init__(self, path, blocking=True):
        self.path, self.fh, self.blocking = path, None, blocking

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(),
                               msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(),
                            fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as e:
            if self.fh:
                self.fh.close()
                self.fh = None
            if not self.blocking:
                raise LockBusy(f"另一个进程正持有锁：{self.path}") from e
            print(f"⚠️ 跨进程文件锁不可用（{e}），降级为仅进程内锁：{self.path}", file=sys.stderr)
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


class _ReentrantJobsLock:
    """jobs_lock() 的可重入代理：同线程嵌套进入时只有最外层真正加/解锁。"""

    def __enter__(self):
        d = getattr(_jobs_flock_depth, "d", 0)
        if d == 0:
            self._fl = FileLock(JOBS_DIR / ".jobs.lock")
            self._fl.__enter__()
        else:
            self._fl = None
        _jobs_flock_depth.d = d + 1
        return self

    def __exit__(self, *exc):
        _jobs_flock_depth.d -= 1
        if _jobs_flock_depth.d == 0 and self._fl:
            self._fl.__exit__()


def jobs_lock():
    """岗位层（jobs/*.json）的跨进程写锁，同线程可重入。

    可重入是必需的：migrate_ids 全程持锁，其内部调用的 rename_job 也要
    拿同一把锁 —— flock 按「打开的文件描述」计，同一进程开第二个 fd 再锁同一
    文件会直接死锁，所以同线程的重复进入只在最外层真正加/解锁。
    """
    return _ReentrantJobsLock()


_INSTANCE_GUARD = None


def acquire_instance_guard():
    """进程整个生命周期持有 .server.lock：同一份数据目录只允许一个实例。

    以前靠 README 里一句「同时只跑一个 server」的自觉，而 Windows 的
    SO_REUSEADDR 还允许同端口二次绑定 —— 双实例各自读写同一批 JSON，正是
    静默丢状态的那条路径。拿不到锁直接退出，比双开好得多。
    """
    global _INSTANCE_GUARD
    fl = FileLock(LOCAL_DIR / ".server.lock", blocking=False)
    try:
        fl.__enter__()
    except LockBusy:
        sys.exit(f"数据目录已被另一个 job-stock 实例占用：{LOCAL_DIR}\n"
                 f"同一份数据同时只跑一个 server；先停掉旧的那个（关掉它的终端窗口即可）。")
    _INSTANCE_GUARD = fl      # 保住引用，进程存活期间不释放；退出时由操作系统兜底


def _resolve(p):
    p = Path(p).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def _read_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _load_categories(cfg):
    """岗位分类：config.json 的 categories 优先；没有则用默认列表。

    不写死在代码里的原因：不同求职方向的分类完全不同（算法/量化 vs 产品/运营）。
    枚举外的存量值仍会保留并告警 —— 只是录入下拉与校验跟 config 走。
    """
    raw = cfg.get("categories")
    if isinstance(raw, (list, tuple)):
        out = []
        for c in raw:
            t = norm_text(c)
            if t and t not in out:
                out.append(t)
        if out:
            return out
    return list(DEFAULT_CATEGORIES)


def configure(data_dir=None, cv_dir=None):
    """应用数据/CV 目录与分类配置。优先级：CLI 参数 > config.json > 默认（仓库内）。"""
    global JOBS_DIR, LOCAL_DIR, CV_DIR, DB_PATH, CATEGORIES
    cfg = _read_config()
    dd = data_dir or cfg.get("data_dir")
    cd = cv_dir or cfg.get("cv_dir")
    base = _resolve(dd) if dd else ROOT
    JOBS_DIR = base / "jobs"
    LOCAL_DIR = base / "local"
    DB_PATH = base / "data" / "jobs.db"
    CV_DIR = _resolve(cd) if cd else ROOT / "cv"
    CATEGORIES = _load_categories(cfg)


STATUSES = ["待投递", "已投递", "笔试", "面试", "Offer", "已拒绝", "已归档"]
# 投递进度的先后：合并重复岗位时取「走得更远」的那个状态。不能直接用上面的下标，
# 那会让「已拒绝」排在「Offer」之后；归档不代表进度，权重最低。
STATUS_RANK = {"已归档": -1, "待投递": 0, "已投递": 1, "笔试": 2, "面试": 3, "Offer": 4, "已拒绝": 4}

# 岗位一级分类：受控枚举，用于粗粒度筛选。
# 真正生效的列表来自 config.json 的 categories（见 _load_categories / configure）。
# 这里只是未配置时的兜底；枚举外的存量值不会被清掉，前端会当成临时选项显示。
DEFAULT_CATEGORIES = ["产品", "算法", "研究", "数据", "量化", "后端", "前端", "Infra",
                      "硬件", "设计", "运营", "其他"]
CATEGORIES = list(DEFAULT_CATEGORIES)

# 招聘类型：独立成一个维度，不要混进 tags —— 「校招」和「AI4S」不是同一类东西
RECRUIT_TYPES = ["校招", "社招", "实习"]

# 常见工作地点。只用于一次性迁移：早期版本把多城市塞进了 tags，靠这张表把它们
# 识别出来归位到 locations。日常录入直接写 locations 字段，不依赖这张表。
KNOWN_CITIES = ["北京", "上海", "深圳", "杭州", "广州", "成都", "南京", "苏州", "武汉",
                "西安", "香港", "厦门", "天津", "重庆", "长沙", "青岛", "大连", "合肥",
                "珠海", "无锡", "澳门", "台北", "新加坡", "东京", "首尔", "伦敦",
                "纽约", "西雅图", "远程"]

# 岗位字段 —— 写进 jobs/<id>.json，只存在本机
# locations 是数组：一个岗位常常多地可选，塞进单值字段会丢信息
# created_by / updated_by 由服务端在写入时自动注入（config.json 的 my_name），
# agent 与手写 JSON 都不需要填
SHARED_FIELDS = ["company", "position", "job_no", "category", "recruit_type", "url",
                 "locations", "salary", "source", "deadline", "tags", "notes", "jd", "closed",
                 "created_by", "updated_by"]
# 布尔字段。closed = 岗位已下架/关闭：客观事实，列表里默认收起来。
# 它和「已归档」不是一回事 —— 归档是个人层的「我不投了」，下架是岗位没了。
BOOL_FIELDS = ["closed"]
# 个人字段 —— 写进 local/status.json，只留在本机
LOCAL_FIELDS = ["status", "my_notes"]
# JSON 落盘的字段顺序：固定下来，历史版本之间比对 diff 才干净
JSON_ORDER = ["id"] + SHARED_FIELDS + ["created_at", "updated_at"]

# sqlite 索引表的列。改这里不需要迁移脚本 —— reindex 会 DROP 重建整张表。
INDEX_COLUMNS = ["id", "company", "position", "job_no", "category", "recruit_type", "locations",
                 "salary", "source", "url", "deadline", "tags", "notes", "jd", "closed",
                 "created_by", "updated_by",
                 "status", "my_notes", "status_updated_at", "applied_at",
                 "match_hits", "match_kw", "updated_at", "created_at"]
# 列表接口返回的列：不含 jd/notes 这类大字段，它们只参与关键词搜索。
# url 要返回 —— 列表里的公司名是可以直接点开岗位页的外链。
LIST_COLUMNS = ["id", "company", "position", "job_no", "category", "recruit_type", "locations",
                "salary", "source", "url", "status", "deadline", "tags", "my_notes", "closed",
                "created_by", "updated_by",
                "status_updated_at", "applied_at", "match_hits", "match_kw",
                "updated_at", "created_at"]
# 单值维度：精确等值筛选（同一维度内多值取 OR）
FILTER_COLUMNS = ["status", "company", "position", "category", "recruit_type", "source"]
# 多值列：索引层存成逗号拼接串，筛选走整词匹配，facets 拆开统计
MULTI_COLUMNS = ["locations", "tags"]
# 生成筛选下拉候选项的单值维度（多值列的候选项另外算）
FACET_COLUMNS = ["company", "position", "category", "recruit_type", "source"]
# 入库前需要 strip 的文本维度（查询侧也 strip，两边必须对称，否则永远筛不出来）
STRIP_COLUMNS = ["company", "position", "job_no", "category", "recruit_type", "source", "status"]

# 排序白名单。查询参数 sort 只能取这里的键 —— 直接把参数拼进 ORDER BY 是注入口子。
# 每种排序都补足次级键：updated_at 只精确到分钟，光靠它同分钟的多条顺序不定。
SORTS = {
    "updated":  "updated_at DESC, created_at DESC, id",
    # 没填截止日期的排最后：它们不是「最不急」，而是「不知道急不急」，
    # 混在最前面会把真正快截止的挤下去
    "deadline": "(deadline='') ASC, deadline ASC, updated_at DESC, id",
    "match":    "CAST(match_hits AS INTEGER) DESC, deadline!='' DESC, deadline ASC, id",
    "created":  "created_at DESC, id",
    "company":  "company ASC, position ASC, id",
}

# 已经不需要再操心的投递状态：截止日期提醒会跳过它们
DONE_STATUSES = ("已归档", "Offer", "已拒绝")

# 投递时间线最多保留多少条。状态可以被反复改（点错了改回来），不设上限的话
# 个人状态文件会被一条岗位的历史撑爆。
HISTORY_MAX = 50

# CV 关键词参与匹配的岗位字段。不含 my_notes —— 自己写的笔记里出现关键词是
# 循环论证，会把匹配度刷高。
MATCH_FIELDS = ["position", "category", "tags", "notes", "jd", "company"]

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

# 全网搜岗：基于 CV 解读尽量捞全，宁多勿漏；去重与格式化交给 jobs/ 写入规范。
JOB_SEARCH_PROMPT = """你是 job-stock 的职位猎手。请基于本仓库 cv/*.reading.md（若无则先要 CV 并生成解读），
做一轮**尽可能全面**的全网职位搜索，把匹配岗位写入 jobs/*.json。

## 目标
宁可多捞再筛，不要只给「最像的 5 个」。每个目标方向至少覆盖多渠道、多关键词组合。

## 第一步：读懂求职者
1. 读 cv/*.reading.md 全部内容，提取：
   - 目标方向（按优先级）
   - 核心技能与可迁移经验
   - 城市 / 远程 / 招聘类型（校招/社招/实习）偏好
   - 硬伤（避免推明显过不了的岗）
   - 10 个匹配关键词
2. 扫一眼 jobs/*.json 里已有的 company + position + job_no，建立防重清单。

## 第二步：多渠道广搜（并行/分批，尽量穷尽）
对每个目标方向，用不同关键词组合多轮搜索，渠道包括但不限于：
- 目标公司官网招聘站（大厂 / 独角兽 / 垂直领域公司 careers 页）
- 综合招聘与社区：牛客、Boss 直聘、猎聘、实习僧、LinkedIn、拉勾
- 校招/社招专题页、提前批公告、公众号/行业媒体转载的 HC 信息
- 搜索引擎宽搜：`岗位关键词 + 城市 + 校招|社招|实习`、`公司名 + AI|AIGC|产品 等方向词`

关键词策略（举例，按 CV 方向替换）：
- 方向本体 + 岗位族：如「AIGC 产品经理」「AI 产品」「创作工具产品」
- 场景词：电商素材、生图、Agent、Coding、设计工具、商家后台…
- 公司清单：把 CV 匹配建议里「值得投」的公司名逐个搜一遍
- 变体：全称/简称、中英文、带不带「工程师/经理/实习」

宽进：只要沾边就先记下；严出放在下一步。

## 第三步：过滤与分级
跳过：明确要求多年经验且 CV 硬伤对不上、纯算法/工程岗而 CV 无工程背景、
城市完全不可接受且无远程。
保留并标注优先级：P0 高度对口 / P1 可迁移 / P2 长期目标或需内推。

## 第四步：按 job-stock 规范落盘
每条岗位一个 JSON 文件 jobs/<id>.json，遵守 schema 与 AGENTS.md：
- 必填：id / company / position；尽量填 job_no
- city → locations[]；招聘类型 → recruit_type；分类 → category（用 config.json 的 categories）
- url 给可投递或详情页；jd 尽量粘贴原文快照；notes 写客观情报（入口、门槛、截止）
- **不要**写 status / my_notes / history（那是个人层）
- id 规则：有职位号用 `<公司>-<职位号小写>`，否则 `<公司>-<岗位名>`
- 与已有岗位疑似重复时：有 job_no 就更新原文件；没有则对比 url/公司+岗位名，避免平行两份

## 第五步：重建与汇报
1. 运行 `python server.py --reindex`，检查 skipped / warnings / duplicates
2. 向用户汇报：新增 N 条、按方向分布、Top 匹配岗位、建议优先投递清单
3. 提醒用户到 WebUI 看匹配度，个人投递状态自己在页面上标

禁止编造职位号、薪资、截止日期；搜不到就如实说该渠道无结果。"""

BOOTSTRAP_PROMPT = """你是 job-stock 工具的初始化助手。当前工作目录就是工具根目录。

请按顺序完成初始化，并在每一步向用户确认关键选择：

## 1. 理解工具
- 读 README.md 与 AGENTS.md，遵守数据分层与「不写个人层字段」等约定
- 确认 Python 可用；必要时先 `python install.py`（或 python3）

## 2. 配置身份与分类（可选）
- 若 config.json 为空，可询问用户：
  - my_name（写入 created_by，可跳过）
  - categories：按其求职方向定制分类列表（如 产品/算法/设计/运营），写入 config.json
- 不要替用户填真实姓名以外的隐私

## 3. 收集并解读 CV
- 检查 cv/ 目录是否已有 CV 原文（pdf/md/docx）
- 没有则向用户索要，保存到 cv/
- 若已有 CV 但没有 *.reading.md：使用工具内置 CV 解读提示词生成解读，
  保存为 cv/<名字>.reading.md，**把关键词与目标方向读给用户确认**后再继续

## 4. 询问是否开始第一轮全网搜岗
必须明确问用户：
「是否根据 CV 解读结果开始第一轮全网职位搜索？」
- 用户拒绝 → 停止，告诉用户之后可在 WebUI「CV 与解读」页复制搜岗提示词再发起
- 用户同意 → 使用内置 JOB_SEARCH_PROMPT 执行搜索、写入 jobs/、reindex 并汇报

## 5. 收尾
- 建议用户：`python server.py` 打开 WebUI；数据只在本机，重要节点点「⇩ 全量备份」
- 若 jobs/ 里只有示例岗位，提醒可删除示例后开始真实录入

全程用中文；涉及删除文件必须再次征得用户同意。"""


def prompts_payload():
    """前端「复制提示词」用的三件套。分类列表一并返回，方便 UI 展示当前枚举。"""
    return {
        "cv_prompt": CV_PROMPT,
        "job_search_prompt": JOB_SEARCH_PROMPT,
        "bootstrap_prompt": BOOTSTRAP_PROMPT,
        "categories": list(CATEGORIES),
        "statuses": list(STATUSES),
        "recruit_types": list(RECRUIT_TYPES),
    }


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


TRUE_WORDS = {"1", "true", "yes", "y", "是", "已下架", "已关闭", "closed"}


def norm_bool(v):
    """布尔字段归一：JSON 里手写成 true / "1" / "是" / "已下架" 都认。

    只有明确为真才算真，读不懂的值一律当假 —— 把「下架」误判成真会让岗位从
    默认列表里消失，比漏标严重得多。
    """
    if isinstance(v, bool):
        return v
    return norm_text(v).lower() in TRUE_WORDS


def is_empty(v):
    """字段是否「没填」。

    不能直接写 `not v`：False 要算没填（未下架），但 0 和 "0" 是有意义的取值。
    """
    return v is None or v is False or v in ("", [], {})


def fold(s):
    """比对用的折叠形式：转小写 + 去掉所有空白。

    「LLM 可解释性」和 JD 里的「LLM可解释性」应该算命中 —— 中英文之间加不加空格
    纯属排版习惯，不该影响匹配结果。
    """
    return re.sub(r"\s+", "", (s or "")).lower()


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


_KW_CACHE = {"sig": None, "keywords": []}


def cv_keywords():
    """本机 cv/*.reading.md 里的关键词，合并去重。

    CV 解读文件只在本机，所以这份关键词天然是「本机这个人的」——
    匹配度因此属于个人层：改 CV 只影响自己看到的排序。
    按 (文件名, mtime, 大小) 缓存：整库重建索引时每条岗位都重读一遍文件没必要。
    """
    try:
        files = sorted(f for f in CV_DIR.glob("*.reading.md") if f.is_file())
        sig = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    except OSError:
        return _KW_CACHE["keywords"]
    if sig == _KW_CACHE["sig"]:
        return _KW_CACHE["keywords"]
    out = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for k in parse_keywords(content):
            if k not in out:
                out.append(k)
    _KW_CACHE["sig"], _KW_CACHE["keywords"] = sig, out
    return out


def match_keywords(job, keywords):
    """CV 关键词 × 岗位文本，返回命中的关键词列表（保持 CV 里的原顺序）。

    只做折叠空白与大小写后的子串匹配，不做分词、不做同义词扩展 —— 那类扩展会
    造出说不清来源的命中，而这个数字是要拿来排序、拿来决定投不投的。
    命中了哪几个词会一并展示，能不能算数由人自己判断。
    """
    parts = []
    for k in MATCH_FIELDS:
        v = job.get(k)
        parts.append(",".join(norm_list(v)) if isinstance(v, (list, tuple)) else str(v or ""))
    text = fold(" ".join(parts))
    return [k for k in keywords if fold(k) and fold(k) in text]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def days_between(a, b):
    """b - a 的天数（两个 YYYY-MM-DD 字符串）。任一无法解析返回 None。"""
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _deaccent(s):
    """NFKD 分解后去掉变音符号：Schrödinger 与 Schrodinger 应该算同一家公司。"""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch))


def slugify(text):
    s = re.sub(r"[^\w一-鿿-]+", "-", _deaccent(text).strip().lower()).strip("-")
    return s or "job"


def canonical_id(job):
    """由岗位内容算出规范 id。

    有官方职位号就用「公司-职位号」：同一个岗位不管录几次都算出同一个 id，
    重复会被录入防重拦下，而不是两条静默共存的记录。
    没有职位号时退回「公司-岗位名」，这时写法稍有出入就会漏判，只能靠去重检测兜底。
    """
    company = norm_text(job.get("company"))
    job_no = norm_text(job.get("job_no"))
    if company and job_no:
        return slugify(f"{company}-{job_no}")
    return slugify(f"{company}-{norm_text(job.get('position'))}")


def fuzzy_key(s):
    """把文本压成用于比对的指纹：去掉空白、标点、大小写、变音符差异。

    「AI分子动力学算法研究员 - AI for Science」和「AI分子动力学算法研究员-AI for science」
    应该被认成同一个岗位；Schrödinger 和 Schrodinger 也该是同一家公司。
    """
    return re.sub(r"[^\w一-鿿]+", "", _deaccent(norm_text(s).lower()))


# 录入去重用的链接归一：只剔除已知的跟踪参数，其余 query 原样保留 ——
# moka / 飞书链接里的 jobId 类参数是岗位身份的一部分，动了就撞不出重复
TRACK_PARAMS = {"spm", "gclid", "fbclid", "from", "tt_from", "share_token", "share_source"}


def norm_url(u):
    """投递链接的比对形态：小写 scheme/host、去 fragment 与尾斜杠、剔除跟踪参数。"""
    u = norm_text(u)
    if not u:
        return ""
    try:
        sp = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    host = (sp.hostname or "").lower()
    if not host:
        return u          # 不是标准链接就不比了
    try:
        port = sp.port    # 非法端口（如 :abc）在这里抛 ValueError，按「不比」处理
    except ValueError:
        return u
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
          if k.lower() not in TRACK_PARAMS and not k.lower().startswith("utm_")]
    netloc = host if not port else f"{host}:{port}"
    return urllib.parse.urlunsplit((sp.scheme.lower(), netloc, sp.path.rstrip("/") or "/",
                                    urllib.parse.urlencode(qs), ""))


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


# ---------------------------------------------------------------- 岗位层：JSON 文件

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
    """按固定顺序整理岗位字段；未知字段保留在后面。

    保留未知字段很重要：文件可能是手写的或旧版本写的、多写了几个字段，
    改一次状态时把它们无声删掉就再也找不回来了。
    """
    # 值为假的布尔字段不落盘：给每条岗位都写一行 "closed": false 只会制造噪音，
    # 而且「没有这个字段」和「字段为 false」本来就该是同一个意思。
    # drop 必须同时挡住下面那个「保留未知字段」的循环 —— 否则刚丢掉的字段会被它加回来，
    # 表现为「标记下架再取消，文件里就多出一行 closed: false」。
    drop = {k for k in BOOL_FIELDS if k in job and not norm_bool(job[k])}
    ordered = {k: job[k] for k in JSON_ORDER if k in job and k not in drop}
    for k, v in job.items():
        if k not in ordered and k not in LOCAL_FIELDS and k not in drop:
            ordered[k] = v
    return ordered


def job_rev(job):
    """岗位内容指纹，用作乐观锁版本号。

    不能用 updated_at 充当版本号：它只精确到分钟，同一分钟内的两次修改指纹相同，
    冲突检测会失效。
    """
    payload = json.dumps(ordered_job(job), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 版本历史

# 每个岗位保留的历史版本数。改写/删除岗位文件前，旧内容先进 jobs/.history/<id>/。
# 这是「没有 git 之后的后悔药」：改坏了、误合并了、删掉了，都能从这里恢复。
HISTORY_KEEP = 3
HISTORY_DIR = ".history"
# 版本文件名：v-<微秒时间戳>.json。字典序 = 时间序，轮转与列举都不用额外排序键
VERSION_RE = re.compile(r"^v-(\d{8})-(\d{6})-(\d{6})\.json$")


def history_dir(job_id):
    return JOBS_DIR / HISTORY_DIR / norm_text(job_id)


def snapshot_version(job_id, src):
    """把岗位文件的当前内容存进历史目录，保留最近 HISTORY_KEEP 份。

    任何一步失败只告警，绝不阻塞主写 —— 和 write_local 的备份一样，
    备份是保险丝，主写失败才是真事故。
    """
    try:
        hdir = history_dir(job_id)
        hdir.mkdir(parents=True, exist_ok=True)
        n = datetime.now()
        dst = hdir / f"v-{n.strftime('%Y%m%d-%H%M%S-%f')}.json"
        while dst.exists():
            n += _timedelta(microseconds=1)
            dst = hdir / f"v-{n.strftime('%Y%m%d-%H%M%S-%f')}.json"
        shutil.copy2(src, dst)
        olds = sorted(hdir.glob("v-*.json"))
        for old in olds[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
    except OSError as e:
        print(f"⚠️ 岗位历史版本保存失败（本次写入照常进行）：{e}", file=sys.stderr)


def version_files(job_id):
    """某岗位的历史版本文件，新→旧。id 不合法或没有历史返回 []。"""
    hdir = history_dir(job_id)
    if not norm_text(job_id) or not hdir.is_dir():
        return []
    return sorted((f for f in hdir.iterdir() if VERSION_RE.match(f.name)), reverse=True)


def version_meta(f):
    """把版本文件名解析成 {file, at, size} 供前端展示。"""
    m = VERSION_RE.match(f.name)
    at = (f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} "
          f"{m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}") if m else ""
    try:
        size = f.stat().st_size
    except OSError:
        size = 0
    return {"file": f.name, "at": at, "size": size}


def save_job(job):
    """写岗位 JSON。覆盖已有文件前先留一份历史版本。"""
    p = job_path(job.get("id"))
    if not p:
        raise ValueError(f"非法的岗位 id：{job.get('id')!r}")
    for k in MULTI_COLUMNS:
        if k in job:
            job[k] = norm_list(job[k])
    for k in BOOL_FIELDS:
        if k in job:
            job[k] = norm_bool(job[k])
    if p.exists():
        snapshot_version(job.get("id"), p)
    write_json_atomic(p, ordered_job(job))


# ---------------------------------------------------------------- 个人层：本地状态

def local_path():
    return LOCAL_DIR / "status.json"


def read_local_strict():
    """严格读个人状态表。文件不存在返回 {}；存在但读不出来抛 LocalStateError。

    这个区分是关键：把「解析失败」当成「空表」，接着的整表写回就会永久抹掉
    全部投递进度（写前快照只在成功写入时才发生，救不了这一步）。
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


BACKUP_KEEP = 10          # local/backups/ 里保留的最近快照份数


def write_local(table):
    """个人状态表落盘，写前把旧文件快照进 local/backups/（保留最近 BACKUP_KEEP 份）。

    个人进度是全系统最不可再生的数据（岗位 JSON 有 .history、索引可 DROP 重建，
    而误删、磁盘故障、手改写坏的防线只有这里的写前快照）。
    快照任何一步失败只告警，绝不阻塞主写：备份是保险丝，
    主写失败才是真事故。
    """
    p = local_path()
    try:
        if p.exists():
            bdir = LOCAL_DIR / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            # 文件名带微秒：字典序 = 时间序，轮转的「删最旧」和恢复的「取最新」
            # 才都成立。同名（同一微秒）就 +1µs 避让，绝不覆盖上一份快照
            n = datetime.now()
            dst = bdir / f"status-{n.strftime('%Y%m%d-%H%M%S-%f')}.json"
            while dst.exists():
                n += _timedelta(microseconds=1)
                dst = bdir / f"status-{n.strftime('%Y%m%d-%H%M%S-%f')}.json"
            shutil.copy2(p, dst)
            olds = sorted(bdir.glob("status-*.json"))
            for old in olds[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
    except OSError as e:
        print(f"⚠️ 个人状态备份失败（本次写入照常进行）：{e}", file=sys.stderr)
    write_json_atomic(p, table)


def clean_history(rec):
    """取出记录里合法的时间线条目。手改坏一条不该让整个接口炸掉。"""
    return [h for h in (rec.get("history") or [])
            if isinstance(h, dict) and norm_text(h.get("status"))]


def applied_at(rec):
    """首次「投出去」的时间：时间线里第一条进度达到「已投递」及以后的记录。

    从时间线派生而不是单独存一个字段 —— 两处存同一件事早晚会对不上。
    """
    for h in clean_history(rec):
        if STATUS_RANK.get(norm_text(h.get("status")), -1) >= STATUS_RANK["已投递"]:
            return norm_text(h.get("at"))
    return ""


def update_local(job_id, patch):
    """原子更新单个岗位的个人状态。注意：不碰岗位 JSON。

    状态确实发生变化时往 history 追加一条，这就是投递时间线的唯一来源。
    只改备注不写历史 —— 否则时间线会被无意义的重复条目淹没。
    """
    with _LOCAL_LOCK, local_lock():
        table = read_local_strict()       # 损坏就抛错，绝不整表覆盖
        rec = table.get(job_id)
        rec = dict(rec) if isinstance(rec, dict) else {}
        old = norm_text(rec.get("status")) or "待投递"
        rec.update(patch)
        new = norm_text(rec.get("status")) or "待投递"
        if new != old:
            rec["history"] = (clean_history(rec) + [{"status": new, "at": now()}])[-HISTORY_MAX:]
        rec["updated_at"] = now()
        table[job_id] = rec
        write_local(table)
        return rec


def migrate_local():
    """给还没有时间线的个人记录补一条起点，返回补了几条。

    老版本只存 status + updated_at。用这两个值补出时间线的第一条，此后的变更
    才有参照物。补出来的这条时间不精确（是最后一次改动的时间），但比空时间线有用。

    ⚠️ 不要在 migrate() 的 with 块里调用它：local_lock() 是 flock，同一进程
    对同一文件的第二个 fd 加锁会直接死锁（flock 按打开文件描述计，不认进程）。
    """
    seeded = 0
    with _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        for rec in table.values():
            if not isinstance(rec, dict) or rec.get("history"):
                continue
            st = norm_text(rec.get("status"))
            if not st or st == "待投递":     # 还没动过，没有历史可补
                continue
            rec["history"] = [{"status": st, "at": norm_text(rec.get("updated_at")) or now()}]
            seeded += 1
        if seeded:
            write_local(table)
    return seeded


def merge_local(job, table=None):
    """把个人状态并进共享岗位记录，得到「我看到的」完整视图。"""
    rec = (load_local() if table is None else table).get(job.get("id"))
    if not isinstance(rec, dict):     # 手改坏了某一条也不该让整个接口 500
        rec = {}
    merged = dict(job)
    merged["_rev"] = job_rev(job)                 # 前端保存时回传，用于冲突检测
    for k in MULTI_COLUMNS:                       # 列表接口和单条接口的类型必须一致
        merged[k] = norm_list(job.get(k))
    merged["closed"] = norm_bool(job.get("closed"))
    merged["status"] = rec.get("status") or "待投递"
    merged["my_notes"] = rec.get("my_notes", "")
    merged["status_updated_at"] = rec.get("updated_at", "")
    merged["history"] = clean_history(rec)
    merged["applied_at"] = applied_at(rec)
    return merged


def _upgrade_shared(job):
    """把一条旧格式的岗位 JSON 升级到当前字段结构。返回 True 表示确实改了。

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
    """把旧版岗位 JSON 升级到当前结构（幂等），返回被改动的岗位 id 列表。

    两类升级：
      - status / my_notes 搬进个人状态文件（私人数据，不该留在岗位 JSON 里）
      - 岗位字段的结构升级，见 _upgrade_shared

    改写前给原文件留一份历史版本 —— 结构升级也是改动，改坏了要能回退。
    """
    moved = []
    # 也要拿岗位层锁：这个函数会重写岗位 JSON，和 POST/PUT 走的是同一批文件
    with jobs_lock(), _JOBS_LOCK, _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        local_dirty = False
        for f in sorted(JOBS_DIR.glob("*.json")):
            job = load_json(f)
            if not isinstance(job, dict):
                continue
            jid = norm_text(job.get("id")) or f.stem
            stale = [k for k in LOCAL_FIELDS if k in job]
            if stale:
                # 本地已有记录以本地为准，只从岗位文件里摘掉字段，避免覆盖真实进度
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
            snapshot_version(jid, f)
            write_json_atomic(f, ordered_job(job))
            moved.append(jid)
        if local_dirty:
            write_local(table)
    migrate_local()      # 注意：在 with 块之外，flock 不可重入
    return moved


migrate_status = migrate   # 旧名字，保持向后兼容


def rename_job(old_id, new_id):
    """给岗位换 id：岗位文件改名 + 个人状态的键跟着搬。

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
    snapshot_version(old_id, old_p)     # 旧 id 名下留底，改名前的内容可追溯
    write_json_atomic(new_p, ordered_job(job))
    old_p.unlink(missing_ok=True)
    with _LOCAL_LOCK, local_lock():
        table = read_local_strict()
        if old_id in table:
            table[new_id] = {**table.get(new_id, {}), **table.pop(old_id)}
            write_local(table)
    return True


def migrate_ids():
    """把补了官方职位号的岗位升级到规范 id（文件随之改名）。

    只在「有 job_no 且当前 id 不是按它算出来的」时才动，所以改公司名或岗位名
    不会引发改名 —— id 保持稳定，只有补上职位号时会换一次。
    """
    renamed = []
    with jobs_lock(), _JOBS_LOCK:
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
        return sum(1 for k in SHARED_FIELDS if not is_empty(j.get(k)))
    return sorted(jobs, key=lambda j: (-filled(j), norm_text(j.get("created_at")) or "9999",
                                       j["id"]))[0]


def merge_two_jobs(a, b):
    """字段级合并两条岗位，返回 (merged, conflicts)。

    原则与 merge_group 声明的一致：空字段用对方补齐；locations / tags 取并集；
    closed 取或；notes 与 jd 双方都有就拼接 —— 宁可留着让人删，也不要悄悄丢掉
    任何一边写的情报。其余文本字段（url/salary/deadline/job_no/source…）双方都有
    且不同时，保留 a 的值、b 的值转存进 notes 并记入 conflicts；未知字段同样
    空缺补齐（此前它们会随被删文件一起蒸发）。冲突必须可见 —— 静默丢掉对方
    的 JD 曾是合并逻辑的头号罪过。
    """
    merged = dict(a)
    conflicts = []
    keys = list(SHARED_FIELDS) + [k for k in b.keys() if k not in SHARED_FIELDS
                                  and k not in ("id", "created_at", "updated_at")]
    for k in keys:
        bv = b.get(k)
        if k in MULTI_COLUMNS:
            merged[k] = norm_list(list(merged.get(k) or []) + list(bv or []))
        elif k in BOOL_FIELDS:
            # 取或：只要有一个人标了下架，合并后就是下架
            merged[k] = norm_bool(merged.get(k)) or norm_bool(bv)
        elif is_empty(merged.get(k)) and not is_empty(bv):
            merged[k] = bv
        elif k in ("notes", "jd"):
            if not is_empty(bv) and norm_text(bv) not in norm_text(merged.get(k)):
                merged[k] = f"{merged.get(k) or ''}\n───\n{bv}".strip()
        elif k == "created_by":
            # 谁先录的就记谁：按 created_at 取更早的那位，而不是机械地保留幸存者的
            if not is_empty(bv) and norm_text(bv) != norm_text(merged.get(k)) \
                    and norm_text(b.get("created_at") or "9999") < norm_text(merged.get("created_at") or "9999"):
                merged[k] = bv
        elif k == "updated_by":
            if not is_empty(bv) and norm_text(bv) != norm_text(merged.get(k)) \
                    and norm_text(b.get("updated_at") or "") > norm_text(merged.get("updated_at") or ""):
                merged[k] = bv
        elif not is_empty(bv) and norm_text(bv) != norm_text(merged.get(k)):
            conflicts.append({"id": b.get("id"), "field": k,
                              "value": bv if isinstance(bv, str)
                              else json.dumps(bv, ensure_ascii=False)})
            if isinstance(bv, str):
                merged["notes"] = (f"{merged.get('notes') or ''}"
                                   f"\n{k}（另一条记录「{b.get('id')}」）：{bv}").strip()
    return merged, conflicts


def merge_group(ids):
    """把一组重复岗位合并成一条，返回 (保留的 id, 被删掉的 id 列表, 冲突清单)。

    岗位层：空字段用其他条补齐；locations / tags 取并集；notes 内容不同就拼起来，
    宁可留着让人删，也不要悄悄丢掉信息。
    个人层：状态取走得最远的那个（见 STATUS_RANK），个人备注拼接。
    被合并掉的文件在删除前先留历史版本 —— 合并错了可以在编辑框里恢复回来。

    岗位快照必须在**拿锁之后**读：锁外读到的快照可能在写回前被并发的 PUT 改掉，
    用陈旧快照算出的合并结果写回 = 静默回滚刚保存的编辑。
    """
    with jobs_lock(), _JOBS_LOCK, _LOCAL_LOCK, local_lock():
        jobs = [j for j in (load_shared(i) for i in ids) if j]
        if len(jobs) < 2:
            return None, [], [], []
        keep = _pick_survivor(jobs)
        others = [j for j in jobs if j["id"] != keep["id"]]

        merged, conflicts = dict(keep), []
        for j in others:
            merged, cf = merge_two_jobs(merged, j)
            conflicts.extend(cf)
        merged["updated_at"] = now()

        table = read_local_strict()
        recs = [table.get(i) for i in ids if isinstance(table.get(i), dict)]
        if recs:
            best = max(recs, key=lambda r: (STATUS_RANK.get(r.get("status", ""), 0),
                                            r.get("updated_at", "")))
            notes = [r["my_notes"] for r in recs if norm_text(r.get("my_notes"))]
            rec = dict(best)
            if notes:
                rec["my_notes"] = "\n".join(dict.fromkeys(notes))
            # 时间线取并集按时间排序：两条记录各自投过一次，合并后应该看得到全过程
            hist, seen_h = [], set()
            for h in sorted((h for r in recs for h in clean_history(r)),
                            key=lambda h: norm_text(h.get("at"))):
                k = (norm_text(h.get("at")), norm_text(h.get("status")))
                if k not in seen_h:
                    seen_h.add(k)
                    hist.append(h)
            if hist:
                rec["history"] = hist[-HISTORY_MAX:]
            table[keep["id"]] = rec
        for j in others:
            table.pop(j["id"], None)
            p = job_path(j["id"])
            if p and p.exists():
                snapshot_version(j["id"], p)
                p.unlink(missing_ok=True)
        write_local(table)
        save_job(merged)
    return keep["id"], [j["id"] for j in others], conflicts


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


def upsert_index(conn, merged, keywords=None):
    """把「共享岗位 + 个人状态」的合并视图写进索引表，写入侧统一归一。

    匹配度在这里算好存进索引，列表接口就不必每次重算。代价是改了 CV 之后要
    重建一次索引才生效 —— 页面上的「↻ 重建索引」按钮就是干这个的。
    """
    kws = cv_keywords() if keywords is None else keywords
    hits = match_keywords(merged, kws)
    row = []
    for c in INDEX_COLUMNS:
        v = merged.get(c, "")
        if c in MULTI_COLUMNS:
            v = ",".join(norm_list(v))
        elif c in BOOL_FIELDS:
            v = "1" if norm_bool(v) else ""
        elif c == "match_hits":
            v = str(len(hits))
        elif c == "match_kw":
            v = ",".join(hits)
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
    kws = cv_keywords()          # 整库重建时只读一次 CV，不必每条岗位都问一遍
    conn = get_db()
    skipped, warnings, seen = [], [], {}
    try:
        conn.execute("DROP TABLE IF EXISTS jobs")
        _create_index_table(conn)
        for f in sorted(JOBS_DIR.glob("*.json")):
            job = load_json(f)
            if not isinstance(job, dict):
                skipped.append(f"{f.name}：读不出来（JSON 格式错误）")
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
            upsert_index(conn, merge_local(job, table), kws)
        conn.commit()
        # 孤儿个人状态键：本地记录挂在已不存在的岗位 id 上 —— 多半是别的机器改了
        # 岗位 id 而广播表没跟上（或记录被手工删了）。报出来让人决定去留，绝不静默清掉
        for lid in table:
            if lid not in seen:
                warnings.append(f"个人状态里有一条挂在不存在的岗位「{lid}」上的记录"
                                f"（岗位可能已被删除或改名），未做任何改动")
    finally:
        conn.close()
    return {"count": len(seen), "skipped": skipped, "warnings": warnings,
            "cv_keywords": kws, "duplicates": find_duplicates()}


DUE_SOON_DAYS = 7          # 「快截止了」的口径：今天起 7 天内（含第 7 天）


def deadline_stats(conn, today=None):
    """统计需要尽快处理的岗位，用于页面顶部的提醒条。

    刻意**不受当前筛选条件影响**：筛到「深圳」时也该提醒北京那个明天截止的岗位，
    否则提醒会随手一筛就消失，等于没有。

    只看还需要行动的：已下架的、已归档 / 已 Offer / 已拒绝的都跳过 —— 它们的
    截止日期已经不重要了。没填截止日期的不算，那是「不知道」而不是「不急」。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, company, position, deadline, status FROM jobs "
        f"WHERE deadline!='' AND closed='' AND status NOT IN ({','.join('?' * len(DONE_STATUSES))}) "
        "ORDER BY deadline, id", list(DONE_STATUSES)).fetchall()
    overdue, soon = [], []
    for r in rows:
        d = days_between(today, r["deadline"])
        if d is None:
            continue
        item = {"id": r["id"], "company": r["company"], "position": r["position"],
                "deadline": r["deadline"], "days": d}
        if d < 0:
            overdue.append(item)
        elif d <= DUE_SOON_DAYS:
            soon.append(item)
    return {"today": today, "soon": soon, "overdue": overdue,
            "soon_days": DUE_SOON_DAYS}


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
    # 已下架的岗位同理默认收起来。它是客观事实，不是个人选择
    if one("hide_closed") == "1":
        sql += " AND closed=''"
    # 匹配度门槛：match_hits 在索引里是 TEXT，必须 CAST，否则 '10' < '2' 是真
    mm = one("min_match")
    if mm.isdigit() and int(mm) > 0:
        sql += " AND CAST(match_hits AS INTEGER)>=?"
        args.append(int(mm))
    q = one("q")
    if q:
        cols = ["company", "position", "locations", "category", "recruit_type",
                "tags", "notes", "jd", "my_notes"]
        sql += " AND (" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")"
        args += [f"%{_like(q)}%"] * len(cols)
    # 排序键走白名单，认不出来的一律退回默认 —— 参数直接拼进 ORDER BY 是注入口子
    sort = one("sort")
    sql += " ORDER BY " + SORTS.get(sort, SORTS["updated"])

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
        stats = deadline_stats(conn)
    finally:
        conn.close()
    # 枚举外的存量取值也要能筛（手写 JSON 可能用了新方向，旧枚举里没有）
    for key, enum in (("category", CATEGORIES), ("recruit_type", RECRUIT_TYPES)):
        facets[key] = sorted(set(facets[key]) | set(enum),
                             key=lambda v: (v not in enum, enum.index(v) if v in enum else 0, v))
    today = stats["today"]
    for r in rows:
        for k in MULTI_COLUMNS:
            r[k] = [x for x in (r.get(k) or "").split(",") if x]
        r["match_kw"] = [x for x in (r.get("match_kw") or "").split(",") if x]
        r["match_hits"] = int(r.get("match_hits") or 0)
        r["closed"] = bool(r.get("closed"))
        # 剩余天数在服务端算：前端各机器时区可能不同，同一条岗位不该显示成不同天数
        r["days_left"] = days_between(today, r["deadline"]) if r.get("deadline") else None
    return {"jobs": rows, "facets": facets, "stats": stats,
            "sorts": list(SORTS), "cv_keywords": cv_keywords()}


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
        except BadRequest as e:
            self.safe_json_error(str(e), 400)
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

    def safe_json_error(self, msg, code):
        try:
            self.send_json({"error": msg}, code)
        except Exception:
            pass

    # 浏览器访问本机服务时的合法 Host（去端口后）。服务只绑 127.0.0.1，但 DNS
    # rebinding 可以让恶意网页的域名解析到 127.0.0.1 —— 浏览器视其为同源，
    # 能读走投递记录、个人备注，甚至 /api/cv/file/ 的 CV 原文。校验 Host 一条 if 挡掉。
    LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

    def guard_request(self):
        """入口统一校验（Host / Sec-Fetch-Site / Content-Type）。返回 True 表示已拒绝。"""
        h = self.headers.get("Host") or ""
        if h.startswith("["):                     # IPv6 字面量 [::1]:8770
            host = h[1:h.index("]")].lower() if "]" in h else h.lower()
        else:
            host = h.split(":")[0].strip().lower()
        if host and host not in self.LOCAL_HOSTS:
            self.send_json({"error": "拒绝访问：Host 不是本机地址（若你在反向代理后面访问，"
                                     "请把 Host 头改写为 localhost）"}, 403)
            return True
        # 浏览器跨站请求必带 Sec-Fetch-Site。挡的是「恶意网页对 localhost 发简单
        # POST」—— /api/sync、/api/dedupe 这类不读 body 的写端点，Content-Type
        # 校验拦不住空 body 的跨站触发。curl / 脚本不发这个头，完全不受影响
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            self.send_json({"error": "拒绝跨站请求"}, 403)
            return True
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > 0:
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct and ct != "application/json":
                self.send_json({"error": "Content-Type 必须是 application/json"}, 415)
                return True
        return False

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

    def send_backup(self):
        """全量备份：把 jobs/（含历史版本）与 local/ 打成 zip 下载。

        不含 sqlite 索引（可随时重建）与 CV 原文（体积大且涉及个人隐私，
        用户自己知道 CV 在哪）。内存里打 zip —— 数据量是几百个几 KB 的 JSON，
        不会有压力。
        """
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for base, name in ((JOBS_DIR, "jobs"), (LOCAL_DIR, "local")):
                if not base.is_dir():
                    continue
                for f in sorted(base.rglob("*")):
                    if not f.is_file():
                        continue
                    # 根目录下的锁文件（.jobs.lock 等）跳过；
                    # .history/ 子目录里的历史版本要带上（备份的意义就在这）
                    if f.name.startswith(".") and f.parent == base:
                        continue
                    z.write(f, f"{name}/{f.relative_to(base)}")
        body = buf.getvalue()
        fname = f"jobstock-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def restore_version(self, job_id, fname):
        """把某个历史版本恢复为当前内容（当前内容会先留一份新历史）。"""
        job_id = norm_text(job_id)
        f = history_dir(job_id) / fname
        if not VERSION_RE.match(fname) or not f.is_file():
            return self.send_json({"error": "not found"}, 404)
        d = load_json(f)
        if not isinstance(d, dict):
            return self.send_json({"error": "这个历史版本文件读不出来（JSON 格式错误）"}, 500)
        with jobs_lock(), _JOBS_LOCK:
            job = dict(d)
            job["id"] = job_id
            job["updated_at"] = now()      # 恢复也是一次改动，列表的「最近更新」要反映它
            save_job(job)                  # 覆盖前自动把当前内容留进历史
        index_one(load_shared(job_id))
        return self.send_json({"ok": True})

    def read_body(self):
        """读并解析 JSON 请求体；不合法抛 BadRequest（safe_route 转 400）。

        旧版本把解析失败吞成空 dict —— 那会让 PUT 一路走到「没有任何共享字段
        变化」并返回 ok:true，用户以为保存成功了，实际一个字节都没写。
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise BadRequest("Content-Length 头不合法")
        if n < 0:
            raise BadRequest("Content-Length 头不合法")
        if n > MAX_BODY:
            raise BadRequest(f"请求体超过上限（{MAX_BODY // (1024 * 1024)}MB）")
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            d = json.loads(raw or b"{}")
        except Exception as e:
            raise BadRequest(f"请求体不是合法 JSON：{e}") from e
        if not isinstance(d, dict):
            raise BadRequest("请求体顶层必须是 JSON 对象")
        return d

    def bad_values(self, data):
        """校验受控枚举与日期格式。返回错误信息，合法则返回 None。

        只对「显式传了且非空」的值校验：空串一律当成「不设置」而不是「清空」。
        编辑已有岗位时只校验**真正改动过**的字段（见 do_PUT）——否则手写 JSON
        里的枚举外分类（本机枚举里没有）会在原样回传时被 400 拦下，逼着前端清空它。
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
            elif k in BOOL_FIELDS:
                # 「字段不存在」和「字段为 false」是同一个意思，不能按字符串比 ——
                # 那样每次保存都会把 closed:false 写进别人的文件
                if norm_bool(new) != norm_bool(old):
                    out[k] = norm_bool(new)
            elif (new or "") != (old or ""):
                out[k] = new
        return out

    # ---- routes ----
    @safe_route
    def do_GET(self):
        if self.guard_request():
            return
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/jobs":
            d = list_jobs(query)
            return self.send_json({**d, "statuses": STATUSES, "categories": CATEGORIES,
                                   "recruit_types": RECRUIT_TYPES,
                                   "server_version": SERVER_VERSION})
        m = re.fullmatch(r"/api/jobs/([^/]+)/versions", path)
        if m:
            return self.send_json({"versions": [version_meta(f)
                                                for f in version_files(m.group(1))]})
        m = re.fullmatch(r"/api/jobs/([^/]+)/versions/([^/]+)", path)
        if m:
            f = history_dir(m.group(1)) / m.group(2)
            if not VERSION_RE.match(m.group(2)) or not f.is_file():
                return self.send_json({"error": "not found"}, 404)
            d = load_json(f)
            if not isinstance(d, dict):
                return self.send_json({"error": "这个历史版本文件读不出来（JSON 格式错误）"}, 500)
            return self.send_json(d)
        if path == "/api/backup":
            return self.send_backup()
        m = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if m:
            job = load_shared(m.group(1))
            if not job:
                p = job_path(m.group(1))
                if p and p.exists():
                    # 文件在、读不出（JSON 格式错误）。裸 404 会让用户以为岗位丢了
                    return self.send_json({"error": "这个岗位的文件读不出来（JSON 格式错误），"
                                                    "点「↻ 重建索引」可以在提示里看到文件名。"}, 409)
                return self.send_json({"error": "not found"}, 404)
            merged = merge_local(job)
            kws = cv_keywords()
            # 弹窗里要显示「命中了哪几个词」，光有个数说服不了人
            merged["match_kw"] = match_keywords(merged, kws)
            merged["cv_keywords"] = kws
            return self.send_json(merged)
        if path == "/api/prompts":
            return self.send_json(prompts_payload())
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
        if self.guard_request():
            return
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path == "/api/reindex":
            migrate()
            migrate_ids()
            return self.send_json({"ok": True, **reindex()})
        m = re.fullmatch(r"/api/jobs/([^/]+)/versions/([^/]+)/restore", path)
        if m:
            return self.restore_version(m.group(1), m.group(2))
        if path == "/api/dedupe":
            # 只合并强信号的重复组（同职位号 / 同链接）。名字相似属于疑似，
            # 同一家公司不同部门完全可能有同名岗位，那种要人来判断。
            merged = []
            for g in find_duplicates():
                if not g["auto"]:
                    continue
                keep, dropped, conflicts = merge_group(g["ids"])
                if keep:
                    merged.append({"keep": keep, "dropped": dropped, "reason": g["reason"],
                                   "conflicts": conflicts})
            return self.send_json({"ok": True, "merged": merged, **reindex()})
        if path == "/api/jobs":
            data = self.read_body()
            split_tag_fields(data)
            if not norm_text(data.get("company")) or not norm_text(data.get("position")):
                return self.send_json({"error": "company 和 position 必填"}, 400)
            err = self.bad_values(data)
            if err:
                return self.send_json({"error": err}, 400)
            # 先探一次个人层：它坏了就别写岗位层，否则会留下一个谁也删不掉的孤儿岗位
            read_local_strict()
            with jobs_lock(), _JOBS_LOCK:      # id 分配 + 落盘要一起做，否则并发新增会互相覆盖
                base = canonical_id(data)
                # 有职位号时 base 是稳定的，撞上说明这个岗位已经录过了 —— 与其造一条
                # 「-2」的重复记录，不如直接告诉用户去哪看
                if norm_text(data.get("job_no")) and (JOBS_DIR / f"{base}.json").exists():
                    return self.send_json(
                        {"error": f"这个岗位已经录过了（职位号 {data['job_no']}），"
                                  f"在列表里搜「{norm_text(data.get('company'))}」就能找到。",
                         "existing_id": base}, 409)
                # 录入端防重前移：职位号只覆盖填了的岗位（存量三分之二都没有），
                # 链接归一 + 公司岗位名相似度把兜底从「reindex 的事后报告」前移到写入前。
                # 两个人前后录同一个岗位、岗位名还差一个字，正是最常见也最隐蔽的重复。
                nu = norm_url(data.get("url"))
                fuzzy_cands = []
                fc26 = fuzzy_key(data.get("company"))
                fp26 = fuzzy_key(data.get("position"))
                for f in sorted(JOBS_DIR.glob("*.json")):
                    j = load_json(f)
                    if not isinstance(j, dict):
                        continue
                    if nu and norm_url(j.get("url")) == nu:
                        return self.send_json(
                            {"error": f"这个投递链接已经录过了"
                                      f"（{norm_text(j.get('company'))} {norm_text(j.get('position'))}），"
                                      f"搜「{norm_text(data.get('company'))}」就能找到。",
                             "existing_id": norm_text(j.get("id")) or f.stem}, 409)
                    if (not norm_text(data.get("job_no")) and fc26 and fp26
                            and fuzzy_key(j.get("company")) == fc26
                            and difflib.SequenceMatcher(None, fp26, fuzzy_key(j.get("position"))
                                                        ).ratio() >= 0.87):
                        fuzzy_cands.append({"id": norm_text(j.get("id")) or f.stem,
                                            "company": norm_text(j.get("company")),
                                            "position": norm_text(j.get("position"))})
                if fuzzy_cands and not data.get("confirm_duplicate"):
                    return self.send_json(
                        {"error": "疑似已存在同公司、几乎同名的岗位。确实是不同部门/批次的岗位，"
                                  "就再提交一次并带上 confirm_duplicate=true。",
                         "need_confirm": "confirm_duplicate", "candidates": fuzzy_cands[:5]}, 409)
                jid, i = base, 2
                while (JOBS_DIR / f"{jid}.json").exists():
                    jid, i = f"{base}-{i}", i + 1
                job = {"id": jid, "locations": [], "tags": [],
                       "created_at": now(), "updated_at": now()}
                for k in SHARED_FIELDS:
                    if k not in data:
                        continue
                    if k in BOOL_FIELDS:
                        if norm_bool(data[k]):    # 假值不写进文件，见 ordered_job
                            job[k] = True
                        continue
                    job[k] = data[k]
                mn = my_name()
                if mn:
                    job["created_by"] = mn
                    job["updated_by"] = mn
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
        if self.guard_request():
            return
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

        with jobs_lock(), _JOBS_LOCK:
            job = load_shared(m.group(1))    # 拿锁后重读，避免用过期快照回写
            if not job:
                return self.send_json({"error": "not found"}, 404)
            touched = self.shared_changed(data, job)
            err = self.bad_values(touched)   # 只校验改动过的共享字段
            if err:
                return self.send_json({"error": err}, 400)
            # 乐观锁：客户端回传打开编辑框时的内容指纹，对不上说明这条被别人（或
            # 另一个标签页、或恢复过的旧版本）改过，拒绝盲写
            base = norm_text(data.get("base_rev"))
            if touched and not base:
                return self.send_json({
                    "error": "改共享字段必须带 base_rev（先 GET 这条岗位拿 _rev），"
                             "否则可能覆盖掉别人的改动。确实要强制覆盖就传 base_rev=\"*\"。"}, 400)
            if touched and base != "*" and base != job_rev(job):
                return self.send_json({
                    "error": "这条岗位在你打开编辑框之后被改过了（可能是另一个标签页，"
                             "或刚恢复过历史版本）。请关闭弹窗重新打开这条岗位，再改一次。",
                    "conflict": True}, 409)
            if touched:
                job.update(touched)
                job["updated_at"] = now()
                mn = my_name()
                if mn:
                    job["updated_by"] = mn
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


def _server_alive(port):
    """探测本机端口上是否已经跑着一个 job-stock（GET /api/jobs 通即为活）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _open_browser(url):
    """延迟一点再开浏览器：先让 bind 完成，页面才不会打开成「连接被拒」。"""
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()


def main():
    # 非 UTF-8 locale 的 Windows 重定向输出时，启动横幅里的 emoji/中文会炸。
    # reconfigure 只在新式 TextIOWrapper 上存在，老式包装（如某些 IDE）没有就跳过
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="job-stock 服务器")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data-dir", help="数据目录（内含 jobs/ local/ data/），覆盖 config.json")
    ap.add_argument("--cv-dir", help="CV 目录，覆盖 config.json")
    ap.add_argument("--reindex", action="store_true", help="只重建索引后退出")
    ap.add_argument("--no-open", action="store_true",
                    help="启动后不自动打开浏览器（默认会打开）")
    args = ap.parse_args()
    configure(args.data_dir, args.cv_dir)

    # 数据目录不存在就停下来问清楚，不要静默建一个空的然后显示「0 条岗位」——
    # 外接硬盘没插、config.json 写错时，用户会以为岗位全丢了
    if not JOBS_DIR.is_dir():
        sys.exit(f"数据目录不存在：{JOBS_DIR}\n"
                 f"请检查 {CONFIG_PATH} 里的 data_dir，或先跑一次：python install.py")

    # 一键启动的关键路径：已经有一个实例在跑时，双击启动脚本不该报错吓人，
    # 直接把浏览器指到活着的那个实例上就好（探测默认端口段）。
    try:
        acquire_instance_guard()
    except SystemExit:
        for p in range(args.port, args.port + 10):
            if _server_alive(p):
                url = f"http://localhost:{p}"
                print(f"job-stock 已经在运行：{url}\n（同一份数据只能开一个实例，这次只帮你打开页面。）")
                if not args.no_open:
                    webbrowser.open(url)
                return
        raise

    try:
        # 时间线的补种在 migrate() 内部做，不要在这里再调一次 migrate_local()：
        # 那既是死代码（migrate 已经调过），又会落在下面的 except 之外 ——
        # 个人状态文件一坏，服务器就连岗位列表都打不开了
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
    if r.get("cv_keywords"):
        print(f"CV 关键词（{len(r['cv_keywords'])} 个）已载入，列表里会显示每个岗位的匹配度："
              f"{'、'.join(r['cv_keywords'])}")
    else:
        print("cv/ 里没有 *.reading.md，匹配度一栏不显示。生成方法见网页「CV 与解读」页。")
    for line in r["skipped"]:
        print(f"⚠️  跳过 {line}")
    for line in r["warnings"]:
        print(f"⚠️  {line}")
    for g in r["duplicates"]:
        print(f"⚠️  疑似重复（{g['reason']}）：{' / '.join(g['ids'])}"
              f"{'  ← 可在网页点「合并重复」自动处理' if g['auto'] else '  ← 需人工确认'}")
    conn = get_db()
    try:
        st = deadline_stats(conn)
    finally:
        conn.close()
    for it in st["overdue"]:
        print(f"⏰ 已过期 {-it['days']} 天：{it['company']} {it['position']}（{it['deadline']}）")
    for it in st["soon"]:
        when = "今天截止" if it["days"] == 0 else f"还剩 {it['days']} 天"
        print(f"⏰ {when}：{it['company']} {it['position']}（{it['deadline']}）")
    if args.reindex:
        print(f"已重建索引：{r['count']} 条岗位（{JOBS_DIR}）")
        return

    # 端口默认自动避让：8770 被别的程序占了就用 8771…，双击启动永远能打开。
    # 显式传 --port 则尊重用户选择，占用时报错退出（脚本/定时任务依赖固定端口）。
    explicit = any(a == "--port" or a.startswith("--port=") for a in sys.argv)
    ports = [args.port] if explicit else range(args.port, args.port + 10)
    port, srv = None, None
    for p in ports:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        hint = f"python server.py --port {args.port + 10}"
        print(f"端口 {args.port}" + ("" if explicit else f"～{args.port + 9}")
              + f" 被占用，换个端口：{hint}")
        sys.exit(1)
    url = f"http://localhost:{port}"
    print(f"job-stock 已启动：{url}  （索引 {r['count']} 条岗位，关掉本窗口或 Ctrl+C 停止）")
    print(f"  岗位数据（自动留 3 份历史版本）：{JOBS_DIR}\n"
          f"  个人状态：{local_path()}\n  CV 目录：{CV_DIR}")
    if not args.no_open:
        _open_browser(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
