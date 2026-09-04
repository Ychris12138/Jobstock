#!/usr/bin/env python3
"""job-stock 自测：用合成数据验证「共享/个人分层」、筛选语义与各种失败模式。

运行：python3 test_server.py

全部在临时目录里跑，不会碰你真实的 jobs/ 与 local/。

关于断言的鉴别力：写了断言不等于测到了东西。本文件末尾有一段「鉴别力自检」——
把 LIKE 转义整个关掉后重跑通配符相关的断言，它们必须失败。之前的版本里
把转义关掉仍有 32/33 条通过，等于那两条断言根本测不出问题。
同理，PUT 相关的用例一律用**前端真实发送的全字段 body**，而不是只发一两个字段的
简化形态——真实前端每次都发全部字段，用简化 body 测出来的「不产生 git diff」是假的。
"""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server

PASSED, FAILED = 0, 0


def check(cond, label):
    """断言并计数，失败不中断，跑完一次看全部结果。"""
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {label}")
    else:
        FAILED += 1
        print(f"  ❌ {label}")


def U(s):
    """把 id 编成合法 URL 片段（前端用的是 encodeURIComponent）。"""
    return urllib.parse.quote(str(s), safe="")


def req(method, path, body=None):
    """向被测服务器发一个请求，返回 (状态码, 响应 JSON)。"""
    data = json.dumps(body).encode() if body is not None else None
    # 路径里的中文必须 percent-encode（http.client 只收 ASCII）；safe 里带 % 是为了
    # 不把已经用 U() 编码过的部分再编一次
    head, sep, qs = path.partition("?")
    path = urllib.parse.quote(head, safe="/%") + sep + qs
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                               method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as f:
            return f.status, json.loads(f.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def query(*pairs, **kw):
    """构造筛选请求；重复键用位置参数，如 query(("tag","校招"), ("tag","AI4S"))。"""
    return req("GET", "/api/jobs?" + urllib.parse.urlencode(list(pairs) + list(kw.items())))[1]


def ids(resp):
    return {j["id"] for j in resp["jobs"]}


def add(**kw):
    """新增一条岗位，返回 id。"""
    code, d = req("POST", "/api/jobs", kw)
    assert code == 200, d
    return d["id"]


def write_raw(name, obj):
    """直接往 jobs/ 里写一个手写风格的 JSON（模拟 AI 或合作者手动新建的文件）。"""
    p = TMP / "jobs" / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# 前端 saveJob 实际发送的字段集合 —— 测 PUT 必须用这个形态，否则测不出真实契约
FRONTEND_FIELDS = ["company", "position", "url", "salary", "source",
                   "deadline", "notes", "jd"]


def frontend_body(job, **override):
    """构造一个「打开编辑框、什么都不改、点保存」的请求体。

    前端把所有输入框的值都发出来，空框发空串；这正是 no-op 保存也会污染
    共享 JSON 的那条路径。
    """
    body = {k: (job.get(k) or "") for k in FRONTEND_FIELDS}
    body["category"] = job.get("category") or ""
    body["recruit_type"] = job.get("recruit_type") or ""
    body["locations"] = list(job.get("locations") or [])
    body["tags"] = list(job.get("tags") or [])
    body["closed"] = bool(job.get("closed"))
    body["status"] = job.get("status") or "待投递"
    body["my_notes"] = job.get("my_notes") or ""
    body["base_rev"] = job.get("_rev") or ""
    body.update(override)
    return body


TMP = Path(tempfile.mkdtemp(prefix="jobstock-test-"))
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
(TMP / "jobs").mkdir(parents=True)
# server 自身也会跑 git（同步/推送/迁移广播）。给整个测试进程设上确定身份，
# 否则在没有 git config 的机器（CI）上 server 侧的 commit 会失败。
for _k, _v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t.test"),
               ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t.test")):
    os.environ.setdefault(_k, _v)

# ---- 1. 迁移 --------------------------------------------------------------
print("\n【1】旧数据迁移")
write_raw("老岗位.json", {
    "id": "老岗位", "company": "老公司", "position": "老岗位", "status": "面试",
    "notes": "公共情报", "tags": [], "priority": "高", "contact": "内推人 A",
    "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})

moved = server.migrate_status()
shared = json.loads((TMP / "jobs" / "老岗位.json").read_text(encoding="utf-8"))
local = json.loads((TMP / "local" / "status.json").read_text(encoding="utf-8"))
check(moved == ["老岗位"], "旧 JSON 被识别为需要迁移")
check("status" not in shared, "共享 JSON 里的 status 已移除")
check(shared["notes"] == "公共情报", "公共备注留在共享 JSON")
check(local["老岗位"]["status"] == "面试", "投递状态进了 local/status.json")
check(shared.get("priority") == "高" and shared.get("contact") == "内推人 A",
      "迁移保留合作者写的未知字段（不静默删字段）")
check(server.migrate_status() == [], "再跑一次不重复迁移（幂等）")

(TMP / "jobs" / "老岗位.json").write_text(json.dumps(
    {**shared, "status": "待投递"}, ensure_ascii=False), encoding="utf-8")
server.migrate_status()
local = json.loads((TMP / "local" / "status.json").read_text(encoding="utf-8"))
check(local["老岗位"]["status"] == "面试", "迁移不覆盖本地已有的真实进度")

# ---- 起服务器 --------------------------------------------------------------
server.reindex()
SRV = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()

# ---- 2. 共享层与个人层互不干扰 ---------------------------------------------
print("\n【2】共享/个人分层")
jid = add(company="测试公司", position="算法工程师", category="算法",
          locations=["北京"], tags=["校招", "AI4S", "急招"], notes="公共情报",
          status="已投递", my_notes="我的私密备注", jd="需要熟悉 PyTorch 与分布式训练")
path = TMP / "jobs" / f"{jid}.json"
saved = json.loads(path.read_text(encoding="utf-8"))
check("status" not in saved and "my_notes" not in saved, "新增岗位：个人字段不写进共享 JSON")
check(saved["notes"] == "公共情报", "新增岗位：公共备注写进共享 JSON")
check(list(saved.keys()) == [k for k in server.JSON_ORDER if k in saved],
      "共享 JSON 字段顺序与 JSON_ORDER 完全一致（不只是第一个 key）")

before = path.read_bytes()
code, _ = req("POST", f"/api/jobs/{U(jid)}/status", {"status": "面试"})
check(code == 200 and path.read_bytes() == before, "快捷改状态：共享 JSON 一个字节都没变")
code, d = req("GET", f"/api/jobs/{U(jid)}")
check(d["status"] == "面试" and d["my_notes"] == "我的私密备注", "单条查询返回合并后的完整视图")

# 关键回归：手写的精简 JSON（省略了空字段），用前端全字段 body 做一次 no-op 保存
write_raw("精简岗位.json", {
    "id": "精简岗位", "company": "简公司", "position": "简岗位", "tags": ["校招"],
    "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
req("POST", "/api/reindex")
mini = TMP / "jobs" / "精简岗位.json"
mini_before = mini.read_bytes()
code, d = req("PUT", "/api/jobs/精简岗位",
              frontend_body(json.loads(mini.read_text(encoding="utf-8"))))
check(code == 200 and d.get("shared_changed") is False,
      "no-op 保存：接口回报 shared_changed=False")
check(mini.read_bytes() == mini_before,
      "no-op 保存：精简 JSON 一个字节都没变（不塞空字段、不 bump updated_at）")

code, d = req("PUT", "/api/jobs/精简岗位",
              frontend_body(json.loads(mini.read_text(encoding="utf-8")), my_notes="只改个人备注"))
check(mini.read_bytes() == mini_before, "只改个人备注：共享 JSON 不变（不产生 git diff）")

_, jm = req("GET", "/api/jobs/精简岗位")
code, d = req("PUT", "/api/jobs/精简岗位", frontend_body(jm, salary="40-60K"))
check(d.get("shared_changed") is True and
      json.loads(mini.read_text(encoding="utf-8"))["salary"] == "40-60K", "改共享字段：写进共享 JSON")

# ---- 3. 枚举外的值不被静默归零 ---------------------------------------------
print("\n【3】枚举外的值与空值保护")
write_raw("新方向岗位.json", {
    "id": "新方向岗位", "company": "新公司", "position": "运营岗", "category": "运营",
    "tags": [], "created_at": "2026-02-01 10:00", "updated_at": "2026-02-01 10:00"})
req("POST", "/api/reindex")
_, j = req("GET", "/api/jobs/新方向岗位")
check(j.get("category") == "运营", "枚举外的存量分类被原样读出")
code, d = req("PUT", "/api/jobs/新方向岗位", frontend_body(j, salary="20K"))
kept = json.loads((TMP / "jobs" / "新方向岗位.json").read_text(encoding="utf-8"))
check(code == 200 and kept.get("category") == "运营",
      "原样回传枚举外的 category 不被 400 拒绝，值也保住了")
check("运营" in query()["facets"]["category"], "枚举外的存量分类仍出现在筛选候选里")
check(ids(query(category="运营")) == {"新方向岗位"}, "枚举外的存量分类筛得出来")

req("POST", f"/api/jobs/{U(jid)}/status", {"status": "面试"})
code, d = req("PUT", f"/api/jobs/{U(jid)}", {"status": ""})
_, j = req("GET", f"/api/jobs/{U(jid)}")
check(j["status"] == "面试", "PUT 传空 status 不清空投递进度（空串＝不改，不是清空）")
code, d = req("PUT", f"/api/jobs/{U(jid)}", {"status": "瞎写的状态"})
check(code == 400, "PUT 传非法 status 被拒绝（与 POST /status 路由口径一致）")
code, d = req("PUT", f"/api/jobs/{U(jid)}", {"deadline": "不是日期"})
check(code == 400, "非法日期格式被拒绝")

# 校验走归一值、落盘走原值的话，" 已归档 " 会造出一条既藏不掉也筛不出来的岗位
sp_id = add(company="空格状态公司", position="空格状态岗", status=" 已归档 ")
_, jsp = req("GET", f"/api/jobs/{U(sp_id)}")
check(jsp["status"] == "已归档", "新增时带空格的 status 被归一后落盘")
check(sp_id not in ids(query(hide_archived="1")), "带空格的已归档岗位能被正常隐藏")
check(sp_id in ids(query(status="已归档")), "带空格的已归档岗位能被正常筛出")

# ---- 4. 乐观锁 -------------------------------------------------------------
print("\n【4】并发编辑保护")
_, j = req("GET", "/api/jobs/精简岗位")
stale = frontend_body(j)                       # 模拟另一个标签页里打开的旧快照
req("PUT", "/api/jobs/精简岗位", frontend_body(j, locations=["上海"]))  # 别处先改了
code, d = req("PUT", "/api/jobs/精简岗位", {**stale, "salary": "99K"})
check(code == 409 and d.get("conflict"), "拿着过期快照保存 → 409 冲突，而不是静默覆盖")
check(json.loads(mini.read_text(encoding="utf-8"))["locations"] == ["上海"],
      "冲突时先前的改动被保住")
code, d = req("PUT", "/api/jobs/精简岗位", {"salary": "1K"})
check(code == 400, "改共享字段却不带 base_rev → 400（乐观锁不能被静默跳过）")
code, d = req("PUT", "/api/jobs/精简岗位", {"salary": "1K", "base_rev": "*"})
check(code == 200, 'base_rev="*" 可以给脚本显式强制覆盖')
code, d = req("PUT", "/api/jobs/精简岗位", {"my_notes": "只改个人字段"})
check(code == 200, "只改个人字段不需要 base_rev")

# ---- 5. 个人状态文件损坏保护 ------------------------------------------------
print("\n【5】个人状态文件损坏保护")
lp = TMP / "local" / "status.json"
good = lp.read_bytes()
lp.write_text('{"坏掉的 JSON": ', encoding="utf-8")      # 半截文件
code, d = req("POST", f"/api/jobs/{U(jid)}/status", {"status": "Offer"})
check(code == 500 and "拒绝" in d.get("error", ""), "状态文件损坏时拒绝写入并报错（不是静默清空）")
check(lp.read_text(encoding="utf-8") == '{"坏掉的 JSON": ', "损坏的文件原样保留，等用户处理")
code, r5 = req("GET", "/api/jobs")
check(code == 200 and len(r5["jobs"]) > 0, "状态文件损坏时列表页仍能显示岗位（只读路径降级）")
lp.write_text("", encoding="utf-8")      # 0 字节：里面没有进度可保护
code, d = req("POST", f"/api/jobs/{U(jid)}/status", {"status": "已投递"})
check(code == 200, "0 字节的状态文件当成空表处理，不拦住写入")
lp.write_bytes(good)

# ---- 6. reindex 的可见性 ---------------------------------------------------
print("\n【6】reindex 不静默吞数据")
(TMP / "jobs" / "冲突文件.json").write_text(
    "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> theirs\n", encoding="utf-8")
write_raw("没有id的岗位.json", {"company": "无 id 公司", "position": "无 id 岗",
                              "tags": [], "created_at": "2026-01-01 10:00",
                              "updated_at": "2026-01-01 10:00"})
write_raw("重复id-a.json", {"id": "撞车", "company": "甲", "position": "甲岗", "tags": []})
write_raw("重复id-b.json", {"id": "撞车", "company": "乙", "position": "乙岗", "tags": []})
code, d = req("POST", "/api/reindex")
check(any("冲突文件" in x for x in d["skipped"]), "含 git 冲突标记的文件被报告出来")
check(any("撞车" in x for x in d["skipped"]), "重复 id 被报告出来")
check(ids(query(q="无 id 公司")) == {"没有id的岗位"}, "缺 id 的岗位用文件名兜底，不再凭空消失")
code, d2 = req("POST", f"/api/jobs/{U('没有id的岗位')}/status", {"status": "已投递"})
check(code == 200, "缺 id 的岗位也能改状态（不再 500）")
check(d["count"] == len(query()["jobs"]), "reindex 报的条数与实际入库一致")
for f in ("冲突文件.json", "重复id-a.json", "重复id-b.json", "没有id的岗位.json"):
    (TMP / "jobs" / f).unlink(missing_ok=True)
req("POST", "/api/reindex")

# ---- 7. 日期归一 -----------------------------------------------------------
print("\n【7】截止日期归一")
write_raw("没补零岗位.json", {"id": "没补零岗位", "company": "丙", "position": "丙岗",
                            "deadline": "2026-9-1", "tags": [],
                            "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
write_raw("正常日期岗位.json", {"id": "正常日期岗位", "company": "丁", "position": "丁岗",
                              "deadline": "2026-09-15", "tags": [],
                              "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
code, d = req("POST", "/api/reindex")
check("2026-9-1" > "2026-10-01" and server.norm_date("2026-9-1") == "2026-09-01",
      "字符串比较确实会出错，norm_date 把它补成 2026-09-01")
got = ids(query(deadline_before="2026-10-01"))
check({"没补零岗位", "正常日期岗位"} <= got, "没补零的日期不再被漏掉")
check(ids(query(deadline_before="2026-09-10")) == {"没补零岗位"}, "归一后的日期筛选边界正确")

# ---- 8. 标签与维度值归一 ---------------------------------------------------
print("\n【8】标签归一")
write_raw("字符串标签岗位.json", {"id": "字符串标签岗位", "company": "戊 ", "position": "戊岗",
                                "locations": ["北京 "], "tags": "校招, AI4S",
                                "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
req("POST", "/api/reindex")
f8 = query()["facets"]
check(f8["tags"].count("AI4S") == 1 and " AI4S" not in f8["tags"],
      "带空格的标签不会分裂成两个肉眼难辨的取值")
check(f8["location"].count("北京") == 1 and "北京 " not in f8["location"],
      "带空格的地点不会分裂")
check(ids(query(tag="AI4S")) >= {"字符串标签岗位"},
      "手写成字符串的 tags 里，第一个之后的标签也筛得出来")
_, j8 = req("GET", "/api/jobs/字符串标签岗位")
check(isinstance(j8["tags"], list), "单条 GET 返回的 tags 也是数组（前端 join 不会炸）")
check(server.norm_list(["A,B"]) == ["A B"], "标签内的逗号被替换，不破坏整词匹配")

# ---- 9. 筛选语义 -----------------------------------------------------------
print("\n【9】分类与标签筛选")
a = add(company="A公司", position="后端开发", category="后端", locations=["杭州"], tags=["社招"])
b = add(company="B公司", position="量化研究员", category="量化", locations=["上海"],
        tags=["校招", "算法"])
c = add(company="C公司", position="数据挖掘", category="数据", locations=["杭州"],
        tags=["算法工程", "50%远程"])
# 对照组：不转义 LIKE 通配符的话，下面几条查询会把它们一起捞出来
e = add(company="E公司", position="对照岗", category="数据", locations=["厦门"],
        tags=["50X远程", "内招"], notes="团队 100 人全远程")
g = add(company="G公司", position="下划线岗", category="数据", locations=["厦门"], tags=["_招"])
d_id = add(company="D公司", position="归档岗", category="后端", locations=["北京"], tags=["社招"])
req("POST", f"/api/jobs/{U(d_id)}/status", {"status": "已归档"})

check(ids(query(category="后端")) == {a, d_id}, "按分类筛选")
# 「精简岗位」在第 4 节被改成了上海，这里一并算进期望值
check(ids(query(("location", "杭州"), ("location", "上海"))) == {a, b, c, "精简岗位"},
      "同一维度多值取 OR（杭州 或 上海）")
# 多地可选的岗位：任选其一都应该能筛到它
multi = add(company="多地公司", position="多地岗", category="研究",
            locations=["深圳", "北京", "上海"], recruit_type="校招")
check(multi in ids(query(location="深圳")) and multi in ids(query(location="北京"))
      and multi in ids(query(location="上海")), "多地可选的岗位在每个城市都筛得到")
check(multi not in ids(query(location="杭州")), "没写的城市不会误命中")
_, jm2 = req("GET", f"/api/jobs/{U(multi)}")
check(jm2["locations"] == ["深圳", "北京", "上海"], "多个地点原样保留，不再只留第一个")
check("深圳" in query()["facets"]["location"] and "上海" in query()["facets"]["location"],
      "地点候选项从多值列里拆出来")
check(ids(query(recruit_type="校招")) >= {multi}, "招聘类型可以单独筛")
code, _ = req("POST", "/api/jobs", {"company": "X", "position": "Y", "recruit_type": "瞎写"})
check(code == 400, "非法 recruit_type 被拒绝")
check(ids(query(category="后端", location="杭州")) == {a}, "不同维度之间取 AND")
check(ids(query(("tag", "AI4S"), ("tag", "急招"))) == {jid}, "多个标签取 AND（同时具备）")
# 「校招」这类招聘类型已经迁到 recruit_type，不该再出现在标签维度里
check(ids(query(tag="校招")) == set(), "招聘类型不再留在标签里（已迁到 recruit_type）")
check("校招" not in query()["facets"]["tags"], "标签候选项里没有招聘类型")
check(not ({"北京", "上海", "深圳", "杭州"} & set(query()["facets"]["tags"])),
      "标签候选项里没有城市名（已迁到 locations）")
check(ids(query(recruit_type="校招")) >= {jid, "字符串标签岗位"},
      "迁移后按 recruit_type 能筛到原先用标签标注的岗位")
check(ids(query(tag="算法")) == {b}, "标签整词匹配：「算法」不误命中「算法工程」")
check(d_id not in ids(query(hide_archived="1")), "隐藏已归档生效")
check(ids(query(hide_archived="1", status="已归档")) == {d_id, sp_id},
      "显式筛「已归档」时忽略隐藏开关")
check(ids(query(q="PyTorch")) == {jid}, "关键词搜索覆盖 JD 正文")
check(ids(query(q="公共情报")) >= {jid}, "关键词搜索覆盖公共备注")
check(ids(query(q="我的私密备注")) == {jid}, "关键词搜索覆盖个人备注")
check(len(query()["facets"]["tags"]) == len(set(query()["facets"]["tags"])), "标签候选项已去重")


def wildcard_results():
    """返回三条通配符相关查询的结果。转义生效时它们互不串味。"""
    return (ids(query(tag="50%远程")), ids(query(tag="_招")), ids(query(q="100%远程")))


w_tag_pct, w_tag_us, w_q_pct = wildcard_results()
check(w_tag_pct == {c}, "标签里的 % 不被当成通配符（对照组 50X远程 未被误命中）")
check(w_tag_us == {g}, "标签里的 _ 不被当成通配符（对照组 内招 未被误命中）")
check(w_q_pct == set(), "搜索里的 % 不被当成通配符（对照组「100 人全远程」未被误命中）")

# ---- 10. 校验与安全 --------------------------------------------------------
print("\n【10】校验与安全")
code, d = req("POST", "/api/jobs", {"company": "X", "position": "Y", "category": "不存在的分类"})
check(code == 400, "非法 category 被拒绝")
code, d = req("POST", f"/api/jobs/{U(jid)}/status", {"status": "瞎写的状态"})
check(code == 400, "非法 status 被拒绝")
code, d = req("POST", "/api/jobs", {"company": "只有公司"})
check(code == 400, "缺 position 被拒绝")
check(server.job_path("../../etc/passwd") is None, "路径穿越的 id 被挡住")
check(server.job_path("正常-id") is not None, "正常 id 不受影响")
write_raw("我的 岗位.json", {"id": "我的 岗位", "company": "空格公司", "position": "空格岗",
                           "tags": [], "created_at": "2026-01-01 10:00",
                           "updated_at": "2026-01-01 10:00"})
req("POST", "/api/reindex")
code, d = req("GET", f"/api/jobs/{U('我的 岗位')}")
check(code == 200, "id 含空格的手写岗位也能打开（路由不再比列表窄）")

# 服务端异常必须变成 500 JSON，否则前端只看到「点了没反应」
orig = server.reindex
server.reindex = lambda: (_ for _ in ()).throw(RuntimeError("故意炸一下"))
code, d = req("POST", "/api/reindex")
server.reindex = orig
check(code == 500 and "故意炸一下" in d.get("error", ""),
      "未捕获异常返回 500 JSON（而不是直接断开连接）")

# ---- 11. CV 关键词解析 -----------------------------------------------------
print("\n【11】CV 关键词解析")
kw = server.parse_keywords("## 关键词\n分子动力学、C#、CI/CD、Python、机器学习(ML)\n## 匹配建议\n")
check(kw == ["分子动力学", "C#", "CI/CD", "Python", "机器学习(ML)"],
      "关键词含 # / 斜杠 / 括号时不再被截断或拆错")
check(server.parse_keywords("## 关键词\n\n## 匹配建议\n") == [], "空的关键词小节返回空列表")
check(len(server.parse_keywords("## 关键词\n" + "、".join(f"k{i}" for i in range(20)))) == 10,
      "关键词最多取 10 个")

# ---- 12. 鉴别力自检 --------------------------------------------------------
print("\n【12】鉴别力自检（把转义关掉，上面的通配符断言必须失败）")
_orig_like = server._like
server._like = lambda s: s          # 故意退化：不转义 LIKE 通配符
broken = wildcard_results()
server._like = _orig_like
check(broken != (w_tag_pct, w_tag_us, w_q_pct),
      "关掉 LIKE 转义后结果确实改变 —— 说明那几条断言真的在测东西")
check(server._like("50%_a\\b") == "50\\%\\_a\\\\b", "_like 转义 % _ 与反斜杠")
check(wildcard_results() == (w_tag_pct, w_tag_us, w_q_pct), "自检后恢复原状")

# ---- 13. git 同步 ----------------------------------------------------------
print("\n【13】git 同步")


def git(*args, cwd):
    """跑 git，带上确定的身份，避免依赖本机 git config。"""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.test",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)


def write_job(repo, name, **fields):
    (repo / "jobs").mkdir(parents=True, exist_ok=True)
    (repo / "jobs" / f"{name}.json").write_text(
        json.dumps({"id": name, "tags": [], **fields}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def seed_gitignore(repo):
    """沙箱仓库补上真实仓库的 .gitignore。

    沙箱是从空 bare 仓库种出来的，不带 .gitignore；而 server 一跑就会生成
    data/jobs.db、jobs/.jobs.lock、local/ 这些本机文件。没有 ignore 的话
    add -A 会把它们提交进沙箱 origin，下一台「机器」pull 时就会撞上
    「untracked working tree files would be overwritten」。
    """
    (repo / ".gitignore").write_text(
        (Path(__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8")


GL = TMP / "gitlab"
GL.mkdir()
ORIGIN = GL / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(ORIGIN)], capture_output=True)
ALICE, BOB = GL / "alice", GL / "bob"
git("clone", str(ORIGIN), str(ALICE), cwd=GL)
seed_gitignore(ALICE)
write_job(ALICE, "共享岗", company="共享公司", position="共享岗位", salary="30K")
git("add", "-A", cwd=ALICE); git("commit", "-m", "init", cwd=ALICE)
git("push", "-u", "origin", "main", cwd=ALICE)
git("clone", str(ORIGIN), str(BOB), cwd=GL)

# 合作者新增一个岗位并推送
write_job(BOB, "合作者岗", company="乙公司", position="乙岗位")
git("add", "-A", cwd=BOB); git("commit", "-m", "add", cwd=BOB); git("push", cwd=BOB)

# 本机把工作区弄脏（在 WebUI 里编辑过岗位之后就是这个状态）
write_job(ALICE, "共享岗", company="共享公司", position="共享岗位", salary="35K")
server.configure(data_dir=str(ALICE), cv_dir=str(ALICE / "cv"))
r13 = server.git_sync()
check(r13["ok"] is True, "工作区脏时也能拉取（--autostash 生效，不再是退出码 128）")
check((ALICE / "jobs" / "合作者岗.json").exists(), "合作者的新岗位被拉下来了")
check(json.loads((ALICE / "jobs" / "共享岗.json").read_text(encoding="utf-8"))["salary"] == "35K",
      "本机未提交的改动在 autostash 后被完整还原")
check(r13.get("count") == 2, "拉取后重建索引，条数正确")
ch13 = r13.get("changes") or {}
check({x["id"] for x in ch13.get("new", [])} == {"合作者岗"}, "变更摘要：合作者的新岗位进 new 桶")
check(all(x["id"] != "共享岗" for x in ch13.get("updated", [])),
      "本机未提交的改动不进变更摘要（git diff 基准，不是索引快照）")

# 下架摘要：乙标记下架并推送；甲本机对该岗位还在「面试」，摘要必须把状态带出来
server.update_local("合作者岗", {"status": "面试"})
write_job(BOB, "合作者岗", company="乙公司", position="乙岗位", closed=True)
git("add", "-A", cwd=BOB)
git("commit", "-m", "bob 下架", cwd=BOB)
git("push", cwd=BOB)
r13s = server.git_sync()
closed13 = [x for x in (r13s.get("changes") or {}).get("closed", []) if x["id"] == "合作者岗"]
check(bool(closed13) and closed13[0]["my_status"] == "面试",
      "被下架的岗位进 closed 桶，且带上本机自己的投递状态")

# 双方改同一个岗位 → autostash 贴回来时冲突。git 这时退出码是 0，
# 不额外检查工作区的话会报「同步完成」，而共享 JSON 里已经写进了冲突标记。
git("add", "-A", cwd=ALICE); git("commit", "-m", "local", cwd=ALICE)
git("push", cwd=ALICE)
git("pull", cwd=BOB)
write_job(BOB, "共享岗", company="共享公司", position="共享岗位", salary="99K")
git("add", "-A", cwd=BOB); git("commit", "-m", "bob改薪资", cwd=BOB); git("push", cwd=BOB)
write_job(ALICE, "共享岗", company="共享公司", position="共享岗位", salary="88K")
r13b = server.git_sync()
check(r13b["ok"] is False, "autostash 贴回冲突时不报成功（git 退出码 0 也要判为失败）")
check("stash" in r13b["message"] and "共享岗" in r13b["message"],
      "冲突提示里说清楚了哪个文件、改动在 stash 里")

# 数据目录不在 git 仓库里
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
r13c = server.git_sync()
check(r13c["ok"] is False and "git 仓库" in r13c["message"], "非 git 仓库给出正确提示")
server.configure(data_dir=str(TMP / "不存在的盘"), cv_dir=str(TMP / "cv"))
r13d = server.git_sync()
check(r13d["ok"] is False and "数据目录不存在" in r13d["message"],
      "数据目录不存在时提示检查配置，而不是误报「找不到 git 命令」")

# ---- 14. 重复岗位 ------------------------------------------------------------
print("\n【14】重复岗位的识别与合并")
# 上一节为了测 git 同步把数据目录切走了，这里必须切回来，否则接口读写的
# 根本不是同一个目录（这个坑本身就值得留一条注释）
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
check(server.canonical_id({"company": "甲公司", "job_no": "A123", "position": "随便"})
      == server.canonical_id({"company": "甲公司", "job_no": "A123", "position": "写法不同"}),
      "有职位号时：岗位名写法不同也算出同一个 id")
check(server.canonical_id({"company": "甲公司", "position": "算法"})
      != server.canonical_id({"company": "甲公司", "position": "算法工程师"}),
      "没有职位号时：只能退回按岗位名算，写法不同就会漏判")

dup1 = add(company="重复公司", position="重复岗位 - 完整版", job_no="Z999", category="研究",
           locations=["深圳", "北京"], recruit_type="校招", tags=["AI4S"],
           source="官网", jd="完整的 JD", notes="官网抓来的信息")
code, d = req("POST", "/api/jobs", {"company": "重复公司", "position": "重复岗位",
                                    "job_no": "Z999"})
check(code == 409 and "已经录过" in d.get("error", ""), "同一个职位号再录一次会被拦下（409）")

# 跨机器的重复绕不过 API —— 合作者的文件是 git pull 进来的
write_raw("合作者录的重复岗.json", {
    "id": "合作者录的重复岗", "company": "重复公司", "position": "重复岗位",
    "job_no": "Z999", "locations": ["杭州"], "tags": ["内推"], "source": "牛客",
    "deadline": "2026-12-01", "notes": "合作者补充：有校友可以内推",
    "created_at": "2026-03-01 10:00", "updated_at": "2026-03-01 10:00"})
req("POST", f"/api/jobs/{U('合作者录的重复岗')}/status", {"status": "面试"})
req("PUT", f"/api/jobs/{U('合作者录的重复岗')}", {"my_notes": "合作者的笔记"})
req("PUT", f"/api/jobs/{U(dup1)}", {"my_notes": "我自己的笔记"})

code, r14 = req("POST", "/api/reindex")
grp = [g for g in r14["duplicates"] if set(g["ids"]) == {dup1, "合作者录的重复岗"}]
check(len(grp) == 1 and grp[0]["auto"], "同公司同职位号被识别为可自动合并的重复")

code, dd = req("POST", "/api/dedupe")
check(len(dd["merged"]) == 1 and dd["merged"][0]["keep"] == dup1,
      "合并保留信息更全的那条")
_, kept = req("GET", f"/api/jobs/{U(dup1)}")
check(sorted(kept["locations"]) == sorted(["深圳", "北京", "杭州"]), "地点取并集")
check(sorted(kept["tags"]) == sorted(["AI4S", "内推"]), "标签取并集")
check(kept.get("deadline") == "2026-12-01", "空字段被对方补上")
check(kept["source"] == "官网", "非空字段不被覆盖")
check("有校友可以内推" in kept["notes"] and "官网抓来的信息" in kept["notes"],
      "两边的公共备注都留着，不静默丢掉别人写的情报")
check(kept["status"] == "面试", "投递状态取进度更靠后的（面试 > 待投递）")
check("合作者的笔记" in kept["my_notes"] and "我自己的笔记" in kept["my_notes"],
      "个人备注拼接")
code, _ = req("GET", f"/api/jobs/{U('合作者录的重复岗')}")
check(code == 404, "被合并的那条已删除")
check("合作者录的重复岗" not in json.loads(
      (TMP / "local" / "status.json").read_text(encoding="utf-8")), "个人层的残留也清掉了")

# 弱信号：同公司同名但没有职位号，可能真是两个不同部门的岗位，不能自动合并
write_raw("弱重复A.json", {"id": "弱重复A", "company": "弱公司", "position": "算法工程师",
                          "tags": [], "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
write_raw("弱重复B.json", {"id": "弱重复B", "company": "弱公司", "position": "算法工程师 ",
                          "tags": [], "created_at": "2026-01-02 10:00", "updated_at": "2026-01-02 10:00"})
code, r14b = req("POST", "/api/reindex")
weak = [g for g in r14b["duplicates"] if set(g["ids"]) == {"弱重复A", "弱重复B"}]
check(len(weak) == 1 and not weak[0]["auto"], "名字相似只报告，不自动合并")
code, dd2 = req("POST", "/api/dedupe")
check(all(set(m["dropped"]) != {"弱重复B"} for m in dd2["merged"]), "弱信号不会被自动合并掉")

# 补了职位号之后 id 升级，个人状态要跟着搬
write_raw("待升级岗.json", {"id": "待升级岗", "company": "升级公司", "position": "某岗位",
                          "job_no": "U777", "tags": [],
                          "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
# 注意别在这里调 /api/reindex —— 那条路由内部就会跑 migrate_ids，
# 岗位会提前改名，后面用旧 id 设状态就 404 了
req("POST", f"/api/jobs/{U('待升级岗')}/status", {"status": "笔试"})
renamed = server.migrate_ids()
new_id = server.canonical_id({"company": "升级公司", "job_no": "U777"})
check(("待升级岗", new_id) in renamed, "补了职位号的岗位 id 升级为「公司-职位号」")
check(not (TMP / "jobs" / "待升级岗.json").exists(), "旧文件已改名")
server.reindex()
_, up = req("GET", f"/api/jobs/{U(new_id)}")
check(up["status"] == "笔试", "改名后投递进度没丢（个人状态的键跟着搬了）")

# ---- 15. 投递时间线 ----------------------------------------------------------
print("\n【15】投递时间线")
tl = add(company="时间线公司", position="时间线岗位")
_, t0 = req("GET", f"/api/jobs/{U(tl)}")
check(t0["history"] == [] and t0["applied_at"] == "", "新岗位没有时间线，也没有投递时间")
for st in ["已投递", "笔试", "面试"]:
    req("POST", f"/api/jobs/{U(tl)}/status", {"status": st})
_, t1 = req("GET", f"/api/jobs/{U(tl)}")
check([h["status"] for h in t1["history"]] == ["已投递", "笔试", "面试"],
      "每次状态变更按顺序记进时间线")
check(all(len(h.get("at", "")) == 16 for h in t1["history"]), "每条都带到分钟的时间戳")
check(t1["applied_at"] == t1["history"][0]["at"], "applied_at 取首次「已投递」的时间")

req("POST", f"/api/jobs/{U(tl)}/status", {"status": "面试"})
_, t2 = req("GET", f"/api/jobs/{U(tl)}")
check(len(t2["history"]) == 3, "状态没变时不追加历史（重复点同一个状态不该刷屏）")
req("PUT", f"/api/jobs/{U(tl)}", frontend_body(t2, my_notes="改个备注"))
_, t3 = req("GET", f"/api/jobs/{U(tl)}")
check(len(t3["history"]) == 3, "只改备注不写时间线")
check(t3["applied_at"] == t1["applied_at"], "后续状态变化不会把 applied_at 往后挪")

req("POST", f"/api/jobs/{U(tl)}/status", {"status": "已归档"})
_, t4 = req("GET", f"/api/jobs/{U(tl)}")
check(t4["applied_at"] == t1["applied_at"], "归档不影响「投出去的时间」")

# 时间线是个人层的东西，不能进 git
before = (TMP / "jobs" / f"{tl}.json").read_text(encoding="utf-8")
req("POST", f"/api/jobs/{U(tl)}/status", {"status": "Offer"})
check((TMP / "jobs" / f"{tl}.json").read_text(encoding="utf-8") == before,
      "时间线只写 local/，共享 JSON 一个字节都没变")

# 老记录（只有 status 没有 history）要补出起点
with server.local_lock():
    tbl = json.loads((TMP / "local" / "status.json").read_text(encoding="utf-8"))
    tbl["老记录"] = {"status": "面试", "updated_at": "2026-02-01 09:00"}
    (TMP / "local" / "status.json").write_text(json.dumps(tbl, ensure_ascii=False), encoding="utf-8")
check(server.migrate_local() == 1, "老的个人记录被补上时间线起点")
tbl = json.loads((TMP / "local" / "status.json").read_text(encoding="utf-8"))
check(tbl["老记录"]["history"] == [{"status": "面试", "at": "2026-02-01 09:00"}],
      "起点用记录里原有的时间，不是「现在」")
check(server.migrate_local() == 0, "再跑一次不重复补（幂等）")

# 上限：状态被反复改也不能把个人文件撑爆
for i in range(60):
    server.update_local("压测岗", {"status": "已投递" if i % 2 else "待投递"})
check(len(json.loads((TMP / "local" / "status.json").read_text(
      encoding="utf-8"))["压测岗"]["history"]) == server.HISTORY_MAX,
      f"时间线最多保留 {server.HISTORY_MAX} 条")

# ---- 16. 岗位下架标记 --------------------------------------------------------
print("\n【16】岗位下架标记（共享层）")
alive = add(company="下架公司", position="还开着的岗")
gone = add(company="下架公司", position="已经没了的岗")
_, g0 = req("GET", f"/api/jobs/{U(gone)}")
req("PUT", f"/api/jobs/{U(gone)}", frontend_body(g0, closed=True))
raw_gone = json.loads((TMP / "jobs" / f"{gone}.json").read_text(encoding="utf-8"))
check(raw_gone.get("closed") is True, "下架标记写进共享 JSON（合作者也能看到）")
check("closed" not in json.loads(
      (TMP / "jobs" / f"{alive}.json").read_text(encoding="utf-8")),
      "没下架的岗位不写 closed:false —— 不给每个文件都加一行 git 噪音")

# 下架 → 取消下架：文件里不能留下 "closed": false 这行残渣。
# 单测只覆盖「从没下架过」是不够的，这条路径要走一遍才暴露得出来
_, g_re = req("GET", f"/api/jobs/{U(gone)}")
req("PUT", f"/api/jobs/{U(gone)}", frontend_body(g_re, closed=False))
check("closed" not in json.loads((TMP / "jobs" / f"{gone}.json").read_text(encoding="utf-8")),
      "取消下架后字段被整个删掉，不留 closed:false")
check(json.loads((TMP / "jobs" / f"{gone}.json").read_text(encoding="utf-8")).keys()
      == json.loads((TMP / "jobs" / f"{alive}.json").read_text(encoding="utf-8")).keys(),
      "下架再取消之后，和从没下架过的岗位字段集合完全一致（rev 才对得上）")
req("PUT", f"/api/jobs/{U(gone)}", frontend_body(
    req("GET", f"/api/jobs/{U(gone)}")[1], closed=True))     # 改回下架，继续后面的用例

check(gone not in ids(query(hide_closed="1")), "默认隐藏已下架")
check(gone in ids(query()), "不勾隐藏时能看到已下架")
_, g1 = req("GET", f"/api/jobs/{U(gone)}")
check(g1["closed"] is True, "单条接口返回布尔而不是字符串")

# 「什么都没改就保存」不能把别人的下架标记冲掉，也不能凭空 bump updated_at
sig = (TMP / "jobs" / f"{gone}.json").read_text(encoding="utf-8")
code, noop = req("PUT", f"/api/jobs/{U(gone)}", frontend_body(g1))
check(noop.get("shared_changed") is False, "原样回传不算改动（布尔按布尔比，不按字符串比）")
check((TMP / "jobs" / f"{gone}.json").read_text(encoding="utf-8") == sig,
      "no-op 保存不产生 git diff")

# 手写 JSON 里的各种真值写法都要认
write_raw("手写下架.json", {"id": "手写下架", "company": "手写公司", "position": "手写岗位",
                          "closed": "是", "tags": [],
                          "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
req("POST", "/api/reindex")
check("手写下架" not in ids(query(hide_closed="1")), "手写的 closed:「是」也认（norm_bool）")
check(server.norm_bool("说不清") is False and server.norm_bool(None) is False,
      "读不懂的值当作「没下架」——误判成下架会让岗位凭空消失")

# 下架的岗位不该再出现在截止日期提醒里
req("PUT", f"/api/jobs/{U(gone)}", frontend_body(
    (req("GET", f"/api/jobs/{U(gone)}")[1]), deadline=server.now()[:10], closed=True))
req("POST", "/api/reindex")
st16 = query()["stats"]
check(all(x["id"] != gone for x in st16["soon"] + st16["overdue"]),
      "已下架的岗位不进截止提醒（它的 deadline 已经不重要了）")

# ---- 17. CV 关键词匹配 -------------------------------------------------------
print("\n【17】CV 关键词 × JD 匹配")
(TMP / "cv").mkdir(exist_ok=True)
(TMP / "cv" / "测试人.reading.md").write_text(
    "# CV 解读：测试人\n## 关键词\n分子动力学、LLM 可解释性、PyTorch、量化交易\n## 匹配建议\n",
    encoding="utf-8")
server._KW_CACHE["sig"] = None          # 文件是刚写的，绕开 mtime 缓存
check(server.cv_keywords() == ["分子动力学", "LLM 可解释性", "PyTorch", "量化交易"],
      "解读文件里的关键词被读出来")

hit3 = add(company="匹配公司", position="分子动力学算法研究员",
           jd="需要熟悉 pytorch 与 LLM可解释性方向的研究经验")
hit0 = add(company="匹配公司", position="行政专员", jd="负责会议室预定")
req("POST", "/api/reindex")
_, h3 = req("GET", f"/api/jobs/{U(hit3)}")
check(sorted(h3["match_kw"]) == sorted(["分子动力学", "LLM 可解释性", "PyTorch"]),
      "大小写不同（pytorch）、中英文之间少个空格（LLM可解释性）都算命中")
check("量化交易" not in h3["match_kw"], "没出现的关键词不算命中，不做同义词发挥")
rows = {j["id"]: j for j in query()["jobs"]}
check(rows[hit3]["match_hits"] == 3 and rows[hit0]["match_hits"] == 0, "列表里带上命中个数")
check(query(min_match="3") and hit3 in ids(query(min_match="3"))
      and hit0 not in ids(query(min_match="3")), "匹配度门槛能筛掉不相关的岗位")

# 个人笔记不参与匹配 —— 自己写的字反过来抬高匹配度是循环论证
_, hz = req("GET", f"/api/jobs/{U(hit0)}")
req("PUT", f"/api/jobs/{U(hit0)}", frontend_body(hz, my_notes="分子动力学 PyTorch 量化交易"))
req("POST", "/api/reindex")
check({j["id"]: j for j in query()["jobs"]}[hit0]["match_hits"] == 0,
      "个人备注里的关键词不算匹配（否则自己写几个词就能把分数刷满）")

# 鉴别力自检：把大小写/空格折叠退化掉，上面那条断言必须失败
_orig_fold = server.fold
server.fold = lambda x: (x or "")          # 退化：既不转小写也不去空格
_degraded = server.match_keywords(
    {"position": "分子动力学算法研究员", "jd": "需要熟悉 pytorch 与 LLM可解释性方向的研究经验"},
    server.cv_keywords())
server.fold = _orig_fold
check(sorted(_degraded) != sorted(["分子动力学", "LLM 可解释性", "PyTorch"]),
      "关掉大小写/空格折叠后命中结果确实变化 —— 说明上面那条断言真的在测东西")

# 匹配度属于个人层，改 CV 不该动共享 JSON
sig17 = (TMP / "jobs" / f"{hit3}.json").read_text(encoding="utf-8")
(TMP / "cv" / "测试人.reading.md").write_text(
    "# CV 解读：测试人\n## 关键词\n分子动力学\n## 匹配建议\n", encoding="utf-8")
server._KW_CACHE["sig"] = None
req("POST", "/api/reindex")
check((TMP / "jobs" / f"{hit3}.json").read_text(encoding="utf-8") == sig17,
      "改 CV 只影响索引，共享岗位 JSON 一个字节都没变")
check({j["id"]: j for j in query()["jobs"]}[hit3]["match_hits"] == 1, "关键词变少后匹配度跟着降")

# ---- 18. 截止日期提醒与排序 ---------------------------------------------------
print("\n【18】截止日期提醒与排序")
TODAY = server.now()[:10]
def shift(k):
    from datetime import datetime as _dt, timedelta as _td
    return (_dt.strptime(TODAY, "%Y-%m-%d") + _td(days=k)).strftime("%Y-%m-%d")

d_over = add(company="截止公司", position="过期岗", deadline=shift(-3))
d_soon = add(company="截止公司", position="后天截止岗", deadline=shift(2))
d_far  = add(company="截止公司", position="很久以后岗", deadline=shift(90))
d_none = add(company="截止公司", position="没写截止岗")
req("POST", "/api/reindex")
st = query()["stats"]
# 断言只针对本节新建的这几条 —— 前面的小节也往库里留了带截止日期的岗位，
# 拿整个列表做全等比较是把无关数据也焊进了断言里
soon_ids, over_ids = [x["id"] for x in st["soon"]], [x["id"] for x in st["overdue"]]
check(st["today"] == TODAY, "提醒条用服务端的「今天」，不依赖浏览器时区")
check(d_over in over_ids and d_over not in soon_ids, "过期的单独归一类")
check(d_soon in soon_ids, f"{st['soon_days']} 天内截止的进提醒")
check(d_far not in soon_ids + over_ids, "还早的不打扰")
check(d_none not in soon_ids + over_ids, "没写截止日期的不进提醒")
check(all(-x["days"] > 0 for x in st["overdue"]) and all(0 <= x["days"] <= st["soon_days"]
      for x in st["soon"]), "两类的剩余天数各自落在正确区间")

# 提醒不跟着筛选走：筛到别的公司也照样提醒
st_f = query(company="匹配公司")["stats"]
check([x["id"] for x in st_f["soon"]] == soon_ids
      and [x["id"] for x in st_f["overdue"]] == over_ids,
      "筛选之后提醒条内容不变（否则随手一筛提醒就消失了）")

req("POST", f"/api/jobs/{U(d_soon)}/status", {"status": "Offer"})
req("POST", "/api/reindex")
check(all(x["id"] != d_soon for x in query()["stats"]["soon"]),
      "拿到 Offer 之后不再催这条岗位的截止日期")
check(d_soon in ids(query()), "但它本身还在列表里 —— 只是不再催而已")

order = [j["id"] for j in query(sort="deadline")["jobs"] if j["company"] == "截止公司"]
check(order.index(d_over) < order.index(d_far), "按截止排序：早的在前")
check(order[-1] == d_none, "没写截止日期的排最后（是「不知道」，不是「不急」）")
rows18 = {j["id"]: j for j in query()["jobs"]}
check(rows18[d_over]["days_left"] == -3 and rows18[d_far]["days_left"] == 90,
      "剩余天数由服务端算好")

# 字符串比较的坑：未补零的日期会排错，norm_date 必须挡住
write_raw("怪日期岗.json", {"id": "怪日期岗", "company": "怪公司", "position": "怪岗位",
                          "deadline": "2026-9-1", "tags": [],
                          "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
req("POST", "/api/reindex")
check({j["id"]: j for j in query()["jobs"]}["怪日期岗"]["deadline"] == "2026-09-01",
      "非补零的日期入库时归一（否则 2026-9-1 > 2026-10-01）")

# 排序参数走白名单，注入不了
inj = query(sort="updated_at; DROP TABLE jobs--")
check("jobs" in inj and len(inj["jobs"]) > 0, "认不出的 sort 退回默认排序，不执行注入")
check([j["id"] for j in query(sort="match")["jobs"]][0] ==
      max(query()["jobs"], key=lambda j: j["match_hits"])["id"],
      "按匹配度排序：命中最多的排第一")

# ---- 19. 请求体严格校验 ------------------------------------------------------
print("\n【19】请求体严格校验")


def raw_http(method, path, headers=None, body=b""):
    """发一个手工构造的原始请求（测坏 JSON、伪造头这类 req() 造不出来的形态）。"""
    head, sep, qs = path.partition("?")
    path = urllib.parse.quote(head, safe="/%") + sep + qs
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data else {})
    finally:
        c.close()


code, d = raw_http("POST", "/api/jobs", {"Content-Type": "application/json"},
                   '{"company": "半截'.encode())
check(code == 400 and "JSON" in d.get("error", ""), "坏 JSON 请求体被显式拒绝（400），不再吞成空 dict")
code, d = raw_http("PUT", "/api/jobs/精简岗位", {"Content-Type": "application/json"}, b'[1,2,3]')
check(code == 400, "顶层不是对象的请求体被拒绝（PUT 不再返回假成功）")
mini_before19 = mini.read_bytes()
server.MAX_BODY = 100     # 故意调小，免得在测试里真发 5MB
code, d = raw_http("PUT", "/api/jobs/精简岗位", {"Content-Type": "application/json"},
                   json.dumps(frontend_body(req("GET", "/api/jobs/精简岗位")[1])).encode())
server.MAX_BODY = 5 * 1024 * 1024
check(code == 400, "超过大小上限的请求体被拒绝")
check(mini.read_bytes() == mini_before19, "被拒绝的请求一个字节都没写进共享 JSON")

# ---- 20. 同步/写入互斥与单实例守卫 --------------------------------------------
print("\n【20】同步/写入互斥与单实例守卫")
GL2 = TMP / "locklab"
GL2.mkdir()
ORIGIN2 = GL2 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(ORIGIN2)], capture_output=True)
LA = GL2 / "alice"
git("clone", str(ORIGIN2), str(LA), cwd=GL2)
seed_gitignore(LA)
write_job(LA, "锁岗", company="锁公司", position="锁岗位", salary="10K")
git("add", "-A", cwd=LA)
git("commit", "-m", "init", cwd=LA)
git("push", "-u", "origin", "main", cwd=LA)
server.configure(data_dir=str(LA), cv_dir=str(LA / "cv"))
server.reindex()

real_git20 = server._git
pull_started = threading.Event()
release_pull = threading.Event()


def fake_pull_git(args, cwd, timeout=120):
    """拦截 git pull：挂住等信号，然后模拟 checkout 改写工作区里的共享 JSON。"""
    if args[0] == "pull":
        pull_started.set()
        release_pull.wait(60)
        write_job(LA, "锁岗", company="锁公司", position="锁岗位", salary="99K")
        return subprocess.CompletedProcess(args, 0, "Already up to date.", "")
    return real_git20(args, cwd, timeout=timeout)


server._git = fake_pull_git
sync_result = {}
ts = threading.Thread(target=lambda: sync_result.update(r=server.git_sync()))
ts.start()
check(pull_started.wait(15), "同步线程已进入 pull")
put_result = {}
tp = threading.Thread(target=lambda: put_result.update(
    c=req("PUT", "/api/jobs/锁岗", {"salary": "50K", "base_rev": "*"})[0]))
tp.start()
time.sleep(1.5)    # 修复后 PUT 必须还卡在锁上；把锁退化掉的话它在这个窗口内早就完成了
check(not put_result, "pull 进行期间，写请求被阻塞（不会并发改写共享 JSON）")
release_pull.set()
ts.join(30)
tp.join(30)
server._git = real_git20
check(sync_result["r"]["ok"] is True, "同步正常完成")
check(put_result.get("c") == 200, "被阻塞的 PUT 在同步完成后完成")
final20 = json.loads((LA / "jobs" / "锁岗.json").read_text(encoding="utf-8"))
check(final20["salary"] == "50K", "checkout 与 PUT 不再互相覆盖（最终内容 = PUT 基于拉取后数据的写入）")

with server.FileLock(server.LOCAL_DIR / ".sync.lock", blocking=False):
    r_busy = server.git_sync()
check(r_busy["ok"] is False and "稍后再试" in r_busy["message"],
      "已有同步进行时，新的同步请求立即被拒绝而不是排队")

guard20 = server.FileLock(server.LOCAL_DIR / ".server.lock", blocking=False)
guard20.__enter__()
dup_rejected = False
try:
    server.acquire_instance_guard()
except SystemExit:
    dup_rejected = True
finally:
    guard20.__exit__()
check(dup_rejected, "数据目录被占用时第二个实例拒绝启动（不再静默双开）")
server.configure(data_dir=str(TMP / "guardlab"), cv_dir=str(TMP / "cv"))
server.acquire_instance_guard()      # 成功路径：正常持有，进程剩余时间都算「这个实例」
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 21. git_status 与一键推送 ------------------------------------------------
print("\n【21】git_status 与一键推送")
GL3 = TMP / "pushlab"
GL3.mkdir()
ORIGIN3 = GL3 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(ORIGIN3)], capture_output=True)
PA, PB = GL3 / "alice", GL3 / "bob"
git("clone", str(ORIGIN3), str(PA), cwd=GL3)
seed_gitignore(PA)
write_job(PA, "推送岗", company="推送公司", position="推送岗位", salary="10K")
git("add", "-A", cwd=PA)
git("commit", "-m", "init", cwd=PA)
git("push", "-u", "origin", "main", cwd=PA)
git("clone", str(ORIGIN3), str(PB), cwd=GL3)
server.configure(data_dir=str(PA), cv_dir=str(PA / "cv"))
server.reindex()

st0 = server.git_status()
check(st0["in_repo"] and st0["ahead"] == 0 and st0["upstream"] == "origin/main",
      "干净仓库：ahead=0 且认出 upstream")

pid21 = add(company="推送公司", position="第二个岗")     # WebUI 录入，未 commit
st1 = server.git_status()
check(any(pid21 in x for x in st1["dirty"]["untracked"]), "新增岗位出现在待提交清单里")
check(st1["ahead"] == 0, "还没 commit 时 ahead 仍是 0（dirty 和 ahead 是两回事）")
r21 = server.git_push("data: 测试推送")
check(r21["ok"] is True and r21["pushed"] is True, "一键提交并推送成功")
st2 = server.git_status()
check(st2["ahead"] == 0 and not st2["dirty"]["modified"] and not st2["dirty"]["untracked"],
      "推送后工作区干净、ahead 归零")
git("pull", cwd=PB)
check((PB / "jobs" / f"{pid21}.json").exists(), "合作者拉到了推送的岗位")

# push 被拒（远端更新）→ 自动拉平再推，双方改动都不丢
write_job(PB, "推送岗", company="推送公司", position="推送岗位", salary="99K")
git("add", "-A", cwd=PB)
git("commit", "-m", "bob 先推了一步", cwd=PB)
git("push", cwd=PB)
write_job(PA, "推送岗2", company="推送公司", position="第三岗")
r21b = server.git_push("data: 本地也有改动")
check(r21b["ok"] is True and r21b["pushed"] is True, "push 被拒后自动拉平并重试成功")
check(json.loads((PA / "jobs" / "推送岗.json").read_text(encoding="utf-8"))["salary"] == "99K",
      "拉平把合作者的改动带了回来")
git("pull", cwd=PB)
check((PB / "jobs" / "推送岗2.json").exists(), "本地的新岗位也一并推了上去，合作者拉得到")

server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
check(server.git_status()["in_repo"] is False, "非 git 仓库时 in_repo=False")
server.configure(data_dir=str(PA), cv_dir=str(PA / "cv"))
code, d = req("GET", "/api/git_status")
check(code == 200 and d["in_repo"] is True and "ahead" in d, "GET /api/git_status 路由可用")
_, jobs_resp = req("GET", "/api/jobs")
check(jobs_resp.get("server_version") == server.SERVER_VERSION,
      "列表响应带 server_version（前端检测网页新/后台旧）")
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 23. 合并不丢字段 ----------------------------------------------------------
print("\n【23】合并不丢字段")
# 甲乙各自录了同职位号的岗位：薪资/链接/截止都不同，JD 也各贴了一份，
# 甲还多写了一个本版本不认识的字段。合并必须一样都不丢。
write_raw("冲突A.json", {"id": "冲突A", "company": "冲突公司", "position": "冲突岗位",
                        "job_no": "C555", "salary": "30K",
                        "url": "https://jobs.example.com/555?utm_source=chat",
                        "deadline": "2026-10-01", "jd": "甲贴的 JD：负责分子动力学",
                        "notes": "甲的备注", "contact": "甲的联系人微信",
                        "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
write_raw("冲突B.json", {"id": "冲突B", "company": "冲突公司", "position": "冲突岗位",
                        "job_no": "C555", "salary": "45K",
                        "url": "https://jobs.example.com/555",
                        "deadline": "2026-10-15", "jd": "乙贴的 JD：负责量子化学",
                        "notes": "乙的备注", "extra_num": 7,
                        "created_at": "2026-01-02 10:00", "updated_at": "2026-01-02 10:00"})
req("POST", "/api/reindex")
code, dd = req("POST", "/api/dedupe")
m22 = [m for m in dd["merged"] if "冲突B" in m["dropped"]]
check(bool(m22), "同职位号的重复组被合并")
kept_id = m22[0]["keep"]
_, kept = req("GET", f"/api/jobs/{U(kept_id)}")
# 注意：带职位号的 冲突A 在 dedupe 前的 /api/reindex 里已被 migrate_ids
# 自动升级成规范 id（这本身就是既有功能），幸存者应以新 id 断言
check(kept_id == "冲突公司-c555", "打平时取先录进来的做幸存者（id 已按职位号升级）")
check(kept.get("salary") == "30K", f"冲突字段保留幸存者值（实际 {kept.get('salary')!r}）")
check("45K" in kept["notes"] and "2026-10-15" in kept["notes"],
      "败者的薪资与截止日期转存进公共备注，而不是静默消失")
check("乙贴的 JD" in kept["jd"] and "甲贴的 JD" in kept["jd"], "两份 JD 拼接保留")
check(kept.get("contact") == "甲的联系人微信", "未知字段空缺补齐（此前会随被删文件一起蒸发）")
check(kept.get("extra_num") == 7, "非字符串的自定义字段同样保留")
fields22 = sorted(c["field"] for c in (m22[0].get("conflicts") or []))
check(fields22 == ["deadline", "salary", "url"], f"冲突清单如实报告：{fields22}")

# ---- 24. 冲突自助引导 ----------------------------------------------------------
print("\n【24】冲突自助引导")
GL4 = TMP / "cflab"
GL4.mkdir()
ORIGIN4 = GL4 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(ORIGIN4)], capture_output=True)
SEED = GL4 / "seed"
git("clone", str(ORIGIN4), str(SEED), cwd=GL4)
seed_gitignore(SEED)
write_job(SEED, "冲突岗", company="冲突演公司", position="冲突演岗位", notes="初始")
git("add", "-A", cwd=SEED)
git("commit", "-m", "init", cwd=SEED)
git("push", "-u", "origin", "main", cwd=SEED)


def fresh_conflict(tag):
    """造一个 rebase 冲突现场：乙先推了改动，甲本地 commit 改了同一行。

    备注内容带上 tag：上一组把 origin 推进到「乙写的」之后，同内容的 commit
    会变成 no-op，冲突就造不出来了。
    """
    A, B = GL4 / f"alice{tag}", GL4 / f"bob{tag}"
    git("clone", str(ORIGIN4), str(A), cwd=GL4)
    git("clone", str(ORIGIN4), str(B), cwd=GL4)
    write_job(B, "冲突岗", company="冲突演公司", position="冲突演岗位", notes=f"乙写的{tag}")
    git("add", "-A", cwd=B)
    git("commit", "-m", f"乙改备注{tag}", cwd=B)
    git("push", cwd=B)
    write_job(A, "冲突岗", company="冲突演公司", position="冲突演岗位", notes=f"甲写的{tag}")
    git("add", "-A", cwd=A)
    git("commit", "-m", f"甲改备注{tag}", cwd=A)
    return A


A24 = fresh_conflict("1")
server.configure(data_dir=str(A24), cv_dir=str(A24 / "cv"))
r24 = server.git_sync()
check(r24["ok"] is False and r24.get("conflict") is True and r24.get("phase") == "rebase",
      "rebase 冲突保留现场交给页面处理（不再自动 abort 导致同步按钮永久瘫痪）")
check(r24["files"] == ["jobs/冲突岗.json"], "冲突文件清单正确")
code, j24 = req("GET", "/api/jobs/冲突岗")
check(code == 409 and j24.get("conflict_file") == "jobs/冲突岗.json",
      "冲突文件的单条 GET 返回 409 引导，而不是裸 404 让人以为岗位丢了")
r24m = server.conflict_resolve("jobs/冲突岗.json", "mine")
check(r24m["ok"] is True and r24m["finished"] is True, "「保留我的」处理完自动 rebase --continue")
check(json.loads((A24 / "jobs" / "冲突岗.json").read_text(encoding="utf-8"))["notes"] == "甲写的1",
      "「保留我的」落盘的是本机改动")
check(server.git_status()["ahead"] == 1, "rebase --continue 后本机提交还在，可正常推送")

A24b = fresh_conflict("2")
server.configure(data_dir=str(A24b), cv_dir=str(A24b / "cv"))
server.git_sync()
r24t = server.conflict_resolve("jobs/冲突岗.json", "theirs")
check(r24t["finished"] is True and
      json.loads((A24b / "jobs" / "冲突岗.json").read_text(encoding="utf-8"))["notes"] == "乙写的2",
      "「保留对方的」落盘合作者的版本")
r24x = server.conflict_resolve("jobs/冲突岗.json", "mine")
check(r24x["ok"] is False, "处理完再调用会被告知不在冲突清单（幂等防线）")

A24c = fresh_conflict("3")
server.configure(data_dir=str(A24c), cv_dir=str(A24c / "cv"))
server.git_sync()
r24b = server.conflict_resolve("jobs/冲突岗.json", "both")
saved24 = json.loads((A24c / "jobs" / "冲突岗.json").read_text(encoding="utf-8"))
check(r24b["finished"] is True and "甲写的3" in saved24["notes"] and "乙写的3" in saved24["notes"],
      "「两边拼接」做字段级合并，双方信息都保留")

# stash 相位：沿用第 13 节留下的 autostash 冲突现场（ALICE 沙箱）
server.configure(data_dir=str(ALICE), cv_dir=str(ALICE / "cv"))
r24s = server.conflict_resolve("jobs/共享岗.json", "both")
check(r24s["ok"] is True and r24s["finished"] is True, "stash 相位：两边拼接处理完成并清理现场")
saved24s = json.loads((ALICE / "jobs" / "共享岗.json").read_text(encoding="utf-8"))
check(saved24s["salary"] == "88K", "stash 相位「我的」值（88K）保留")
check("99K" in saved24s.get("notes", ""), "stash 相位对方的 99K 转存进备注")
check(git("stash", "list", cwd=ALICE).stdout.strip() == "",
      "本次同步新建的 stash 已自动 drop，不留垃圾")
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 25. id 迁移广播（跨机器搬家个人进度） --------------------------------------
print("\n【25】id 迁移广播")
GL5 = TMP / "miglab"
GL5.mkdir()
ORIGIN5 = GL5 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(ORIGIN5)], capture_output=True)
MA, MB = GL5 / "alice", GL5 / "bob"
git("clone", str(ORIGIN5), str(MA), cwd=GL5)
seed_gitignore(MA)
write_job(MA, "待广播岗", company="广播公司", position="广播岗位")
git("add", "-A", cwd=MA)
git("commit", "-m", "init", cwd=MA)
git("push", "-u", "origin", "main", cwd=MA)
git("clone", str(ORIGIN5), str(MB), cwd=GL5)

# 乙（另一台机器）在旧 id 上投了 —— 旧版里，甲一补职位号，这块进度就静默归零
server.configure(data_dir=str(MB), cv_dir=str(MB / "cv"))
server.reindex()
server.update_local("待广播岗", {"status": "已投递", "my_notes": "乙的记录"})

# 甲补职位号 → 触发改名 + 写广播记录（随 git 同步给所有人）
server.configure(data_dir=str(MA), cv_dir=str(MA / "cv"))
server.reindex()
write_job(MA, "待广播岗", company="广播公司", position="广播岗位", job_no="B001")
renamed25 = server.migrate_ids()
NEW_ID25 = server.canonical_id({"company": "广播公司", "job_no": "B001"})   # 职位号小写化
check(("待广播岗", NEW_ID25) in renamed25, "补职位号触发 id 升级改名")
check((MA / "jobs" / ".id-migrations").exists() and
      {"old": "待广播岗", "new": NEW_ID25} in
      [{k: m[k] for k in ("old", "new")} for m in server.read_id_migrations()],
      "改名时写入迁移广播记录（JSONL，随 jobs/ 进 git）")
git("add", "-A", cwd=MA)
git("commit", "-m", "feat: 补职位号，id 升级", cwd=MA)
git("push", cwd=MA)

# 乙同步 → 挂在旧 id 上的进度自动搬到新 id
server.configure(data_dir=str(MB), cv_dir=str(MB / "cv"))
r25 = server.git_sync()
tbl_b = json.loads((MB / "local" / "status.json").read_text(encoding="utf-8"))
print("PROBE r25:", {k: v for k, v in r25.items() if k in ("ok", "migrations", "message")}, file=__import__("sys").stderr)
print("PROBE tbl_b keys:", list(tbl_b.keys()), file=__import__("sys").stderr)
print("PROBE migrations file:", (MB / "jobs" / ".id-migrations").exists(), file=__import__("sys").stderr)
print("PROBE jobs:", [p.name for p in (MB / "jobs").iterdir()], file=__import__("sys").stderr)
check("待广播岗" not in tbl_b and tbl_b.get(NEW_ID25, {}).get("status") == "已投递",
      "乙 pull 后旧 id 上的投递进度自动搬到新 id（旧版静默归零且报成功）")
check(tbl_b[NEW_ID25]["my_notes"] == "乙的记录", "个人备注一并搬过去")
check(r25.get("migrations") == [], "搬完无告警")
r25b = server.git_sync()
check(r25b.get("migrations") == [], "再同步不重复搬（幂等）")

# 新旧键并存时不擅自动手：机器无权裁决两边各有内容的记录，只告警
server.update_local("待广播岗", {"status": "笔试"})
w25 = server.apply_id_migrations()
check(any("并存" in w for w in w25), "新旧键并存时不自动合并，只告警让人裁决")
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 26. 录入防重前移与变音符归一 ------------------------------------------------
print("\n【26】录入防重前移与变音符归一")
u26 = add(company="防重公司", position="防重岗位",
          url="https://app.moka.example.com/300544?jobId=abc&spm=sm")
code, d = req("POST", "/api/jobs", {"company": "别的写法公司", "position": "随便",
                                    "url": "https://APP.MOKA.EXAMPLE.com/300544?jobId=abc&spm=x&utm_source=chat"})
check(code == 409 and d.get("existing_id") == u26,
      "链接归一（大小写/跟踪参数）后重复录入被 409 拦下")
code, d = req("POST", "/api/jobs", {"company": "防重公司", "position": "另一个岗",
                                    "url": "https://app.moka.example.com/300544?jobId=DIFFERENT"})
check(code == 200, "jobId 这类业务参数参与比对：不同岗位的链接不误判")
code, d = req("POST", "/api/jobs", {"company": "防重公司", "position": "防重岗位2"})
check(code == 409 and d.get("need_confirm") == "confirm_duplicate" and d.get("candidates"),
      "同公司几乎同名且无职位号 → 409 疑似重复并附候选")
code, d = req("POST", "/api/jobs", {"company": "防重公司", "position": "防重岗位2",
                                    "confirm_duplicate": True})
check(code == 200, "带 confirm_duplicate 放行（不同部门同名岗位是真实场景）")
code, d = req("POST", "/api/jobs", {"company": "重复公司", "position": "重复岗位",
                                    "job_no": "Z999", "confirm_duplicate": True})
check(code == 409 and "已经录过" in d.get("error", ""),
      "job_no 精确命中的 409 不被 confirm_duplicate 绕过（出路是去重合并）")
check(server.slugify("Schrödinger-ML") == "schrodinger-ml", "slugify 去变音符（NFKD）")
check(server.fuzzy_key("Schrödinger") == server.fuzzy_key("Schrodinger"), "fuzzy_key 折叠变音符")
check(server.canonical_id({"company": "Schrödinger", "position": "X"})
      == server.canonical_id({"company": "Schrodinger", "position": "X"}),
      "变音符公司算出同一个 id，重复拦得住")

SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*46}\n通过 {PASSED} 项，失败 {FAILED} 项\n{'='*46}")
raise SystemExit(1 if FAILED else 0)
