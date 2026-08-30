#!/usr/bin/env python3
"""job-stock 服务器：纯 Python 标准库，零依赖。

用法：
    python server.py [--port 8770]     # 启动 WebUI
    python server.py --reindex         # 只重建 sqlite 索引后退出

数据约定：
    jobs/<id>.json  每条岗位一个文件，是真相源（AI 可直接改）
    data/jobs.db    sqlite 列表索引，从 JSON 派生，可随时重建
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
CV_DIR = ROOT / "cv"
DB_PATH = ROOT / "data" / "jobs.db"
WEB_DIR = ROOT / "web"

STATUSES = ["待投递", "已投递", "笔试", "面试", "Offer", "已拒绝", "已归档"]
FIELDS = ["company", "position", "url", "location", "salary", "source",
          "status", "deadline", "tags", "notes", "jd"]

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


def parse_keywords(content):
    """从解读文件的 ## 关键词 小节提取关键词列表（最多 10 个）。"""
    m = re.search(r"^##\s*关键词[^\n]*\n+([^\n#]+)", content, re.M)
    if not m:
        return []
    line = m.group(1).strip().strip("（）()。 ")
    kws = [k.strip() for k in re.split(r"[、,，/／]", line)]
    return [k for k in kws if k][:10]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def slugify(text):
    s = re.sub(r"[^\w一-鿿-]+", "-", text.strip().lower()).strip("-")
    return s or "job"


def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, company TEXT, position TEXT, location TEXT,
        status TEXT, deadline TEXT, salary TEXT, source TEXT,
        tags TEXT, updated_at TEXT, created_at TEXT)""")
    return conn


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_job(job):
    JOBS_DIR.mkdir(exist_ok=True)
    (JOBS_DIR / f"{job['id']}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_index(conn, job):
    conn.execute("""INSERT OR REPLACE INTO jobs
        (id, company, position, location, status, deadline, salary, source, tags, updated_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (job["id"], job.get("company", ""), job.get("position", ""), job.get("location", ""),
         job.get("status", "待投递"), job.get("deadline", ""), job.get("salary", ""),
         job.get("source", ""), ",".join(job.get("tags", [])),
         job.get("updated_at", ""), job.get("created_at", "")))


def reindex():
    conn = get_db()
    conn.execute("DELETE FROM jobs")
    n = 0
    for f in sorted(JOBS_DIR.glob("*.json")):
        job = load_json(f)
        if job and job.get("id"):
            upsert_index(conn, job)
            n += 1
    conn.commit()
    conn.close()
    return n


def list_jobs(query):
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    status = query.get("status", [""])[0]
    q = query.get("q", [""])[0].strip()
    if status:
        sql += " AND status=?"; args.append(status)
    if q:
        sql += " AND (company LIKE ? OR position LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY updated_at DESC"
    conn = get_db()
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


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
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---- routes ----
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/jobs":
            return self.send_json({"jobs": list_jobs(query), "statuses": STATUSES})
        m = re.fullmatch(r"/api/jobs/([\w.-]+)", path)
        if m:
            job = load_json(JOBS_DIR / f"{m.group(1)}.json")
            return self.send_json(job or {"error": "not found"}, 200 if job else 404)
        if path == "/api/cv":
            CV_DIR.mkdir(exist_ok=True)
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
            f = CV_DIR / urllib.parse.unquote(m.group(1))
            if f.is_file() and f.resolve().parent == CV_DIR.resolve():
                return self.send_file(f, "application/octet-stream")
            return self.send_json({"error": "not found"}, 404)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        if path == "/api/reindex":
            return self.send_json({"ok": True, "count": reindex()})
        if path == "/api/jobs":
            data = self.read_body()
            if not data.get("company") or not data.get("position"):
                return self.send_json({"error": "company 和 position 必填"}, 400)
            base = slugify(f"{data['company']}-{data['position']}")
            jid, i = base, 2
            while (JOBS_DIR / f"{jid}.json").exists():
                jid, i = f"{base}-{i}", i + 1
            job = {"id": jid, "status": "待投递", "tags": [],
                   "created_at": now(), "updated_at": now()}
            for k in FIELDS:
                if k in data:
                    job[k] = data[k]
            save_job(job)
            conn = get_db(); upsert_index(conn, job); conn.commit(); conn.close()
            return self.send_json({"ok": True, "id": jid})
        m = re.fullmatch(r"/api/jobs/([\w.-]+)/status", path)
        if m:
            status = self.read_body().get("status", "")
            if status not in STATUSES:
                return self.send_json({"error": "非法状态"}, 400)
            job = load_json(JOBS_DIR / f"{m.group(1)}.json")
            if not job:
                return self.send_json({"error": "not found"}, 404)
            job["status"], job["updated_at"] = status, now()
            save_job(job)
            conn = get_db(); upsert_index(conn, job); conn.commit(); conn.close()
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        m = re.fullmatch(r"/api/jobs/([\w.-]+)",
                         urllib.parse.unquote(urllib.parse.urlparse(self.path).path))
        if not m:
            return self.send_json({"error": "not found"}, 404)
        job = load_json(JOBS_DIR / f"{m.group(1)}.json")
        if not job:
            return self.send_json({"error": "not found"}, 404)
        data = self.read_body()
        for k in FIELDS:
            if k in data:
                job[k] = data[k]
        job["updated_at"] = now()
        save_job(job)
        conn = get_db(); upsert_index(conn, job); conn.commit(); conn.close()
        self.send_json({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="job-stock 服务器")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--reindex", action="store_true", help="只重建索引后退出")
    args = ap.parse_args()
    if args.reindex:
        print(f"已重建索引：{reindex()} 条岗位")
        return
    n = reindex()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 被占用，换个端口：python server.py --port 8771")
        sys.exit(1)
    print(f"job-stock 已启动：http://localhost:{args.port}  （索引 {n} 条岗位，Ctrl+C 停止）")
    srv.serve_forever()


if __name__ == "__main__":
    main()
