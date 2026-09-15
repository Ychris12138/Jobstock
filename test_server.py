#!/usr/bin/env python3
"""job-stock 自测：用合成数据验证「岗位/个人分层」、筛选语义、版本历史与各种失败模式。

运行：python3 test_server.py

全部在临时目录里跑，不会碰你真实的 jobs/ 与 local/。

关于断言的鉴别力：写了断言不等于测到了东西。本文件末尾有一段「鉴别力自检」——
把 LIKE 转义整个关掉后重跑通配符相关的断言，它们必须失败。之前的版本里
把转义关掉仍有 32/33 条通过，等于那两条断言根本测不出问题。
同理，PUT 相关的用例一律用**前端真实发送的全字段 body**，而不是只发一两个字段的
简化形态——真实前端每次都发全部字段，用简化 body 测出来的「不产生 git diff」是假的。
"""
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server

# 非 UTF-8 locale 的 Windows（如英文版 cp1252）重定向输出时，打印【⏰ 等字符
# 会直接 UnicodeEncodeError 崩掉，CI 的 windows runner 就是这样红的。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

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
    """直接往 jobs/ 里写一个手写风格的 JSON（模拟 AI 或人手动新建的文件）。"""
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
      "迁移保留手写的未知字段（不静默删字段）")
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
print("\n【2】岗位/个人分层")
jid = add(company="测试公司", position="算法工程师", category="算法",
          locations=["北京"], tags=["校招", "AI4S", "急招"], notes="公共情报",
          status="已投递", my_notes="我的私密备注", jd="需要熟悉 PyTorch 与分布式训练")
path = TMP / "jobs" / f"{jid}.json"
saved = json.loads(path.read_text(encoding="utf-8"))
check("status" not in saved and "my_notes" not in saved, "新增岗位：个人字段不写进岗位 JSON")
check(saved["notes"] == "公共情报", "新增岗位：公共备注写进岗位 JSON")
check(list(saved.keys()) == [k for k in server.JSON_ORDER if k in saved],
      "共享 JSON 字段顺序与 JSON_ORDER 完全一致（不只是第一个 key）")

before = path.read_bytes()
code, _ = req("POST", f"/api/jobs/{U(jid)}/status", {"status": "面试"})
check(code == 200 and path.read_bytes() == before, "快捷改状态：岗位 JSON 一个字节都没变")
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
check(mini.read_bytes() == mini_before, "只改个人备注：岗位 JSON 不变")

_, jm = req("GET", "/api/jobs/精简岗位")
code, d = req("PUT", "/api/jobs/精简岗位", frontend_body(jm, salary="40-60K"))
check(d.get("shared_changed") is True and
      json.loads(mini.read_text(encoding="utf-8"))["salary"] == "40-60K", "改岗位字段：写进岗位 JSON")

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
check(any("冲突文件" in x for x in d["skipped"]), "读不出来的坏文件被报告出来")
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

# ---- 14. 重复岗位 ------------------------------------------------------------
print("\n【14】重复岗位的识别与合并")
# 显式钉住数据目录：后面有别的用例会切走目录，这里确保接口读写的是主沙箱
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

# 绕过 API 的重复 —— 手写直接放进 jobs/ 的文件
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

# 时间线是个人层的东西，不能写进岗位 JSON
before = (TMP / "jobs" / f"{tl}.json").read_text(encoding="utf-8")
req("POST", f"/api/jobs/{U(tl)}/status", {"status": "Offer"})
check((TMP / "jobs" / f"{tl}.json").read_text(encoding="utf-8") == before,
      "时间线只写 local/，岗位 JSON 一个字节都没变")

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
check(raw_gone.get("closed") is True, "下架标记写进岗位 JSON")
check("closed" not in json.loads(
      (TMP / "jobs" / f"{alive}.json").read_text(encoding="utf-8")),
      "没下架的岗位不写 closed:false —— 不给每个文件都加一行噪音")

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
      "no-op 保存不产生任何 diff")

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

# ---- 20. 单实例守卫 ------------------------------------------------------------
print("\n【20】单实例守卫")
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
(TMP / "guardlab" / "local").mkdir(parents=True, exist_ok=True)
server.acquire_instance_guard()      # 成功路径：正常持有，进程剩余时间都算「这个实例」
server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 21. 版本号与列表契约 -------------------------------------------------------
print("\n【21】版本号与列表契约")
_, jobs_resp = req("GET", "/api/jobs")
check(jobs_resp.get("server_version") == server.SERVER_VERSION,
      "列表响应带 server_version（前端检测网页新/后台旧）")

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
check(server.norm_url("https://x.example.com:abc/path") ==
      "https://x.example.com:abc/path", "非法端口的 URL 不炸 norm_url（按原样返回）")
code, _ = req("POST", "/api/jobs", {"company": "坏端口公司", "position": "坏端口岗",
                                    "url": "https://x.example.com:abc/path"})
check(code == 200, "非法端口 URL 的录入走正常流程而不是 500")

# ---- 27. 个人层滚动备份 ----------------------------------------------------------
print("\n【27】个人层滚动备份")
server.update_local("备份岗", {"status": "已投递"})
server.update_local("备份岗", {"status": "笔试"})
for i in range(14):     # 连续写入：轮转应稳定在 BACKUP_KEEP 份
    server.update_local("备份岗", {"status": "已投递" if i % 2 else "待投递"})
bdir27 = TMP / "local" / "backups"
snaps = sorted(bdir27.glob("status-*.json"))
check(len(snaps) == server.BACKUP_KEEP, f"连续写入后备份恰好保留最近 {server.BACKUP_KEEP} 份")
newest = json.loads(snaps[-1].read_text(encoding="utf-8"))
check(newest.get("备份岗", {}).get("status") in ("已投递", "待投递"),
      "最新一份快照是写入前的完整状态（可恢复）")
shutil.copy2(snaps[-1], TMP / "local" / "status.json")   # 恢复演练：快照改名即回到过去
restored = json.loads((TMP / "local" / "status.json").read_text(encoding="utf-8"))
check(restored["备份岗"]["status"] == newest["备份岗"]["status"], "快照可直接恢复")
# 备份目录不可用时（用同名文件挡住）主写仍要成功
bdir27.rename(TMP / "local" / "backups.bak")
(bdir27).write_text("挡路", encoding="utf-8")
j27 = add(company="备份公司", position="备份岗位")   # 状态端点要求岗位真实存在
code = req("POST", "/api/jobs/" + U(j27) + "/status", {"status": "面试"})[0]
check(code == 200, "备份失败不阻塞主写（主写成功才是硬要求）")
(bdir27).unlink()
(TMP / "local" / "backups.bak").rename(bdir27)

# ---- 27b. 岗位版本历史 ---------------------------------------------------------
print("\n【27b】岗位版本历史（每次写入自动留 3 份旧版本）")
vh = add(company="版本公司", position="版本岗位", salary="10K")
_, v0 = req("GET", f"/api/jobs/{U(vh)}")
code, vl0 = req("GET", f"/api/jobs/{U(vh)}/versions")
check(code == 200 and vl0["versions"] == [], "新岗位还没有历史版本")


def edit_salary(jid, val):
    _, j = req("GET", f"/api/jobs/{jid}")
    return req("PUT", f"/api/jobs/{jid}", frontend_body(j, salary=val))


for i, val in enumerate(["11K", "12K", "13K", "14K"], start=1):
    edit_salary(vh, val)
    _, vlist = req("GET", f"/api/jobs/{U(vh)}/versions")
    check(len(vlist["versions"]) == min(i, server.HISTORY_KEEP),
          f"第 {i} 次修改后历史恰好保留 {min(i, server.HISTORY_KEEP)} 份")

_, vlist = req("GET", f"/api/jobs/{U(vh)}/versions")
ats = [v["at"] for v in vlist["versions"]]
check(ats == sorted(ats, reverse=True), "版本列表新→旧排列")
# 连续保存发生在同一秒内：靠微秒文件名轮转，4 次写入后必须恰好挤掉最旧的一份
hist_dir = TMP / "jobs" / ".history" / vh
check(len(list(hist_dir.glob("v-*.json"))) == server.HISTORY_KEEP, "磁盘上也只有 3 份历史文件")

# 内容抽查：最新一份历史是「改成 14K 之前」的内容（13K）
code, latest = req("GET", f"/api/jobs/{U(vh)}/versions/{vlist['versions'][0]['file']}")
check(code == 200 and latest["salary"] == "13K", "最新历史版本是上次覆盖前的内容")

# 恢复：当前内容（14K）会先留底，然后回到 13K
code, d = req("POST", f"/api/jobs/{U(vh)}/versions/{vlist['versions'][0]['file']}/restore")
_, restored = req("GET", f"/api/jobs/{U(vh)}")
check(code == 200 and restored["salary"] == "13K", "历史版本恢复为当前内容")
_, vlist2 = req("GET", f"/api/jobs/{U(vh)}/versions")
check(len(vlist2["versions"]) == server.HISTORY_KEEP
      and req("GET", f"/api/jobs/{U(vh)}/versions/{vlist2['versions'][0]['file']}")[1]["salary"] == "14K",
      "恢复前先留底：被顶掉的 14K 进了历史，恢复操作可再撤销")
check(restored["company"] == "版本公司", "恢复后索引同步刷新（列表口径一致）")

# 路径安全：历史接口不接受奇怪的文件名 / id
check(req("GET", f"/api/jobs/{U(vh)}/versions/../../status.json")[0] in (404, 400),
      "历史版本文件名不合法时拒绝（404，不给穿越机会）")
check(req("GET", "/api/jobs/..%2F..%2Fetc/versions")[0] == 404, "越界 id 的版本列表返回 404")

# 合并重复也留历史：被合并掉的岗位可从历史里恢复回来（undo merge）
write_raw("版本重复A.json", {"id": "版本重复A", "company": "版本重复公司", "position": "重复岗",
                           "job_no": "V001", "salary": "1K", "tags": [],
                           "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
write_raw("版本重复B.json", {"id": "版本重复B", "company": "版本重复公司", "position": "重复岗",
                           "job_no": "V001", "salary": "9K", "notes": "B 的情报", "tags": [],
                           "created_at": "2026-01-02 10:00", "updated_at": "2026-01-02 10:00"})
req("POST", "/api/reindex")
code, dd27 = req("POST", "/api/dedupe")
dropped27 = [x for m in dd27["merged"] for x in m["dropped"]]
check(bool(dropped27), "重复组被合并")
_, vlist3 = req("GET", f"/api/jobs/{U(dropped27[0])}/versions")
check(len(vlist3["versions"]) >= 1, "被合并删除的岗位在历史里留了底")
code, d = req("POST", f"/api/jobs/{U(dropped27[0])}/versions/{vlist3['versions'][0]['file']}/restore")
check(code == 200 and req("GET", f"/api/jobs/{U(dropped27[0])}")[0] == 200,
      "从历史恢复被合并掉的岗位（合并可撤销）")

# 全量备份 zip：包含岗位、历史与个人状态，不含锁文件
import zipfile, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/backup") as r:
    check(r.status == 200 and r.headers.get("Content-Type") == "application/zip",
          "全量备份接口返回 zip")
    zf = zipfile.ZipFile(io.BytesIO(r.read()))
names = zf.namelist()
check(any(n.startswith("jobs/") and n.endswith(".json") for n in names), "备份里有岗位 JSON")
check(any(".history/" in n for n in names), "备份里有历史版本")
check("local/status.json" in names, "备份里有个人状态")
check(not any(n.endswith(".lock") for n in names), "备份不含锁文件")

# ---- 28. 入口校验三件套 ----------------------------------------------------------
print("\n【28】入口校验三件套")
code, d = raw_http("GET", "/api/jobs", {"Host": "evil.com"})
check(code == 403, "Host 不是本机地址被拒绝（DNS rebinding 拿不到数据）")
code, d = raw_http("GET", "/api/jobs", {"Host": f"localhost:{PORT}"})
check(code == 200, "localhost 带端口照常放行")
code, d = raw_http("POST", "/api/dedupe", {"Host": f"127.0.0.1:{PORT}",
                                           "Sec-Fetch-Site": "cross-site"}, b"{}")
check(code == 403, "浏览器跨站触发的写端点被拒绝（空 body 也逃不过 Sec-Fetch-Site）")
code, d = raw_http("POST", "/api/reindex", {"Host": f"127.0.0.1:{PORT}",
                                            "Sec-Fetch-Site": "same-origin"}, b"{}")
check(code == 200, "同源的正常请求不受影响")
code, d = raw_http("PUT", "/api/jobs/精简岗位",
                   {"Host": f"127.0.0.1:{PORT}", "Content-Type": "text/plain"}, b"{}")
check(code == 415, "非 JSON 的 Content-Type 被拒绝")
code, d = raw_http("POST", "/api/reindex", {"Host": f"127.0.0.1:{PORT}"})   # 无 body 无 CT
check(code == 200, "无 body 的 curl 式 POST 不受影响（AGENTS.md 的用法保持可用）")

# ---- 29. 贡献者字段 created_by / updated_by --------------------------------------
print("\n【29】贡献者字段 created_by / updated_by")
orig_name = server.my_name
server.my_name = lambda: "测试员"
cb29 = add(company="署名公司", position="署名岗位", salary="10K")
saved29 = json.loads((TMP / "jobs" / f"{cb29}.json").read_text(encoding="utf-8"))
check(saved29.get("created_by") == "测试员" and saved29.get("updated_by") == "测试员",
      "新增时自动注入 created_by / updated_by（无需任何人手写）")
_, j29 = req("GET", "/api/jobs/" + U(cb29))
code, _ = req("PUT", "/api/jobs/" + U(cb29), frontend_body(j29, salary="20K"))
saved29b = json.loads((TMP / "jobs" / f"{cb29}.json").read_text(encoding="utf-8"))
check(saved29b.get("salary") == "20K" and saved29b.get("updated_by") == "测试员",
      "本机编辑后 updated_by 保持本机身份")
server.my_name = lambda: "同事乙"
_, j29b = req("GET", "/api/jobs/" + U(cb29))
code, _ = req("PUT", "/api/jobs/" + U(cb29), frontend_body(j29b, salary="30K"))
saved29c = json.loads((TMP / "jobs" / f"{cb29}.json").read_text(encoding="utf-8"))
check(saved29c.get("updated_by") == "同事乙" and saved29c.get("created_by") == "测试员",
      "编辑时 updated_by 刷新为当前身份，created_by 保持录入人")
sig29 = (TMP / "jobs" / f"{cb29}.json").read_bytes()
server.my_name = lambda: "同事丙"
_, j29d = req("GET", "/api/jobs/" + U(cb29))
code, d = req("PUT", "/api/jobs/" + U(cb29), frontend_body(j29d))
check(d.get("shared_changed") is False and (TMP / "jobs" / f"{cb29}.json").read_bytes() == sig29,
      "no-op 保存不刷新 updated_by、不产生 diff")
server.my_name = lambda: ""
nc29 = add(company="无名公司", position="无名岗位")
saved29e = json.loads((TMP / "jobs" / f"{nc29}.json").read_text(encoding="utf-8"))
check("created_by" not in saved29e and "updated_by" not in saved29e,
      "本机没有任何身份信息时不写字段（不落空串）")
server.my_name = orig_name
# 合并：created_by 取 created_at 更早的录入人，而不是机械保留幸存者的
write_raw("署名甲.json", {"id": "署名甲", "company": "署名冲突公司", "position": "署名岗位",
                        "job_no": "S001", "created_by": "甲", "updated_by": "甲",
                        "tags": [], "created_at": "2026-01-01 10:00", "updated_at": "2026-01-01 10:00"})
write_raw("署名乙.json", {"id": "署名乙", "company": "署名冲突公司", "position": "署名岗位",
                        "job_no": "S001", "salary": "9K", "created_by": "乙", "updated_by": "乙",
                        "tags": [], "created_at": "2026-02-01 10:00", "updated_at": "2026-02-01 10:00"})
req("POST", "/api/reindex")
code, dd29 = req("POST", "/api/dedupe")
# 字典序在前的署名乙会被 migrate_ids 先升级成规范 id，dropped 里是升级后的 id
m29 = [m for m in dd29["merged"] if "S001" in m["reason"]]
# 乙多写了 salary → 信息更全成为幸存者；但 created_by 必须仍归先录入的甲
check(bool(m29) and m29[0]["keep"] != "署名甲", "信息更全的后录者成为幸存者")

_, kept29 = req("GET", "/api/jobs/" + U(m29[0]["keep"]))
check(kept29.get("created_by") == "甲", "合并后 created_by 取先录入的人（而非机械保留幸存者的）")

# ---- 30. 内置提示词 + config 分类 ------------------------------------------------
print("\n【30】内置提示词 + config 分类")
code, p30 = req("GET", "/api/prompts")
check(code == 200 and isinstance(p30.get("cv_prompt"), str) and "reading.md" in p30["cv_prompt"],
      "/api/prompts 返回 CV 解读提示词")
check("尽可能" in p30.get("job_search_prompt", "") or "穷尽" in p30.get("job_search_prompt", "")
      or "宁" in p30.get("job_search_prompt", ""),
      "全网搜岗提示词强调尽量捞全")
check("初始化" in p30.get("bootstrap_prompt", "") and "第一轮" in p30.get("bootstrap_prompt", ""),
      "初始化提示词含 agent 自动化与首轮搜岗确认")
check("产品" in p30.get("categories") and "算法" in p30.get("categories"),
      "prompts 接口带回当前分类枚举")

# config.json 自定义分类：configure 后生效。把 CONFIG_PATH 指到临时文件，
# 避免测试写坏仓库里真实的 config.json。
real_cfg_path = server.CONFIG_PATH
cfg_path = TMP / "config.json"
try:
    server.CONFIG_PATH = cfg_path
    cfg_path.write_text(json.dumps({"categories": ["AI产品", "增长", "设计"]},
                                   ensure_ascii=False), encoding="utf-8")
    server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
    check(server.CATEGORIES == ["AI产品", "增长", "设计"],
          "config.json 的 categories 覆盖默认分类")
    code, bad = req("POST", "/api/jobs", {"company": "C", "position": "P", "category": "算法"})
    check(code == 400, "自定义分类后，默认枚举外的旧分类被拒绝")
    code, ok = req("POST", "/api/jobs", {"company": "C2", "position": "P2", "category": "AI产品"})
    check(code == 200, "自定义分类可录入")
    code, pj = req("GET", "/api/jobs")
    check("AI产品" in pj.get("categories", []), "列表接口返回自定义分类")
    cfg_path.write_text("{}", encoding="utf-8")
    server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))
    check(server.CATEGORIES == list(server.DEFAULT_CATEGORIES),
          "空 config 时分类回到默认列表")
finally:
    server.CONFIG_PATH = real_cfg_path
    cfg_path.unlink(missing_ok=True)
    server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 31. 历史版本路径穿越（Issue #1）------------------------------------------
print("\n【31】历史版本路径穿越")
h_ok = add(company="历史安全公司", position="正常岗")
# 正常历史：改两次，应能列出
_, hj = req("GET", f"/api/jobs/{U(h_ok)}")
req("PUT", f"/api/jobs/{U(h_ok)}", frontend_body(hj, salary="10K"))
_, hj2 = req("GET", f"/api/jobs/{U(h_ok)}")
req("PUT", f"/api/jobs/{U(h_ok)}", frontend_body(hj2, salary="20K"))
code, hv = req("GET", f"/api/jobs/{U(h_ok)}/versions")
check(code == 200 and len(hv.get("versions", [])) >= 1, "正常中文 id 可列出历史版本")
if hv.get("versions"):
    vf = hv["versions"][0]["file"]
    code, _ = req("GET", f"/api/jobs/{U(h_ok)}/versions/{vf}")
    check(code == 200, "正常历史版本文件可读取")

for bad_id, label in (
    ("../x", "父目录段 ../x"),
    ("..", "单独的 .."),
    (".", "单独的 ."),
):
    check(server.history_dir(bad_id) is None, f"history_dir 拒绝 {label}")
    check(server.history_version_file(bad_id, "v-20260101-120000-000001.json") is None,
          f"history_version_file 拒绝非法 id {label}")

# Windows 语义：反斜杠与混合分隔符
check(server.history_dir("..\\x") is None, "history_dir 拒绝 ..\\x（Windows）")
check(server.history_dir("foo/bar") is None, "history_dir 拒绝多段路径 foo/bar")
check(server.history_dir("foo\\bar") is None, "history_dir 拒绝多段路径 foo\\bar")
check(server.history_dir("C:evil") is None, "history_dir 拒绝盘符形式")

# 合法 id + 非法版本文件名
good_id = h_ok
check(server.history_version_file(good_id, "../v-x.json") is None,
      "版本文件名含 ../ 被拒绝")
check(server.history_version_file(good_id, "v-20260101-120000-000001.json") is not None
      or server.history_dir(good_id) is not None,
      "合法 id 的 helper 不误杀（无历史时 version_file 仍可构造路径或返回 None 均可，但目录应可解析）")

# HTTP 层：URL 编码的穿越
code, _ = req("GET", f"/api/jobs/{U('../x')}/versions")
check(code in (200, 404), "URL 中的 ../x 列表不炸服务")
code, _ = req("GET", "/api/jobs/%2e%2e%2fx/versions")
check(code in (200, 404), "URL 编码 %2e%2e%2f 不炸服务")
code, _ = req("GET", f"/api/jobs/{U(h_ok)}/versions/..%2F..%2Fetc")
check(code in (404, 400), "版本文件名穿越返回 404/400")

# 确认 .history 外没有被写出奇怪文件
hist_root = TMP / "jobs" / ".history"
outside = TMP / "pwned.json"
check(not outside.exists(), "穿越未在数据目录外创建文件")

# ---- 32. 安全响应头与备份 symlink（Issue #2）------------------------------------
print("\n【32】安全响应头与备份边界")

def raw_headers(method, path):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request(method, path)
    r = conn.getresponse()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    body = r.read()
    conn.close()
    return r.status, hdrs, body

st, hdrs, _ = raw_headers("GET", "/api/jobs")
check(hdrs.get("cache-control") == "no-store", "/api/jobs 带 Cache-Control: no-store")
check(hdrs.get("x-content-type-options") == "nosniff", "/api/jobs 带 X-Content-Type-Options")
check(hdrs.get("referrer-policy") == "no-referrer", "/api/jobs 带 Referrer-Policy")

st, hdrs, _ = raw_headers("GET", "/api/cv")
check(hdrs.get("cache-control") == "no-store", "/api/cv 带 Cache-Control: no-store")

st, hdrs, body = raw_headers("GET", "/api/backup")
check(st == 200 and hdrs.get("cache-control") == "no-store", "/api/backup 带 no-store")

# 备份：正常文件在 zip 里；symlink 不进包
import zipfile as _zipfile
normal = TMP / "jobs" / "备份正常岗.json"
normal.write_text(json.dumps({"id": "备份正常岗", "company": "备份公司", "position": "岗",
                              "tags": []}, ensure_ascii=False), encoding="utf-8")
outside_secret = TMP / "outside-secret.txt"
outside_secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
link_path = TMP / "jobs" / "evil-link.json"
try:
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    os.symlink(outside_secret, link_path)
    can_symlink = True
except (OSError, NotImplementedError):
    can_symlink = False
    print("  ⚠️ 当前环境无法创建 symlink，跳过 symlink 备份用例")

st, _, zbody = raw_headers("GET", "/api/backup")
zf = _zipfile.ZipFile(io.BytesIO(zbody))
names = zf.namelist()
check(any(n.endswith("备份正常岗.json") for n in names), "备份包含正常岗位文件")
if can_symlink:
    leaked = False
    for n in names:
        if "evil-link" in n or "outside-secret" in n:
            leaked = True
        try:
            data = zf.read(n)
            if b"SECRET-OUTSIDE" in data:
                leaked = True
        except Exception:
            pass
    check(not leaked, "备份不跟随 symlink，也不含目录外内容")

# ---- 33. 使用指南里保存分类 ----------------------------------------------------
print("\n【33】POST /api/config/categories")
real_cfg_path = server.CONFIG_PATH
server.CONFIG_PATH = TMP / "config-guide.json"
try:
    code, d = req("POST", "/api/config/categories", {"categories": ["AI产品", "增长", "AI产品"]})
    check(code == 200 and d.get("categories") == ["AI产品", "增长"],
          "保存分类去重并返回")
    check(server.CATEGORIES == ["AI产品", "增长"], "热更新内存中的 CATEGORIES")
    code, d = req("POST", "/api/config/categories", {"categories": []})
    check(code == 200 and d.get("categories") == list(server.DEFAULT_CATEGORIES),
          "空列表恢复默认分类")
finally:
    server.CONFIG_PATH = real_cfg_path
    server.configure(data_dir=str(TMP), cv_dir=str(TMP / "cv"))

# ---- 34. 端口避让：Windows 下被 REUSEADDR 影子化的端口必须视为被占 ----------------
print("\n【34】端口避让（Windows 影子占用防护）")
import socket as _sock

hold = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
# 先绑者开 REUSEADDR，模拟另一个在跑的 Jobstock 实例：Windows 上此后 HTTPServer
# 仍能 bind 成功但被影子化（收不到连接），不加裸探测时本用例在 Windows 必红。
hold.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
hold.bind(("127.0.0.1", 0))
hold.listen(1)
base = hold.getsockname()[1]
try:
    check(not server._port_free(base), "裸探测识别被占端口")
    p1, s1 = server.pick_port(base, 3)
    check(s1 is not None and p1 != base, "被占端口被跳过，避让到下一个空闲端口")
    if s1 is not None:
        s1.server_close()
    p2, s2 = server.pick_port(base, 1)   # 显式 --port 的扫描范围只有 1
    check(s2 is None and p2 is None, "唯一候选端口被占时报不可用（显式 --port 语义）")
finally:
    hold.close()

# ---- 35. 单实例探测必须比对 install_fp（防打开别的副本）--------------------------
print("\n【35】install_fp 区分不同工具目录")
fp_a = server.install_fingerprint()
check(isinstance(fp_a, str) and len(fp_a) == 12, "install_fingerprint 返回 12 位短指纹")
_, jlist = req("GET", "/api/jobs")
check(jlist.get("install_fp") == fp_a, "/api/jobs 带上本安装的 install_fp")
# 只注入「期望指纹」——不能 monkeypatch install_fingerprint 本身，
# 同一进程里的 Handler 也会跟着变，两边又对上了。
check(server._server_alive(PORT, fp="deadbeef0000") is False,
      "指纹不一致时 _server_alive 为 False（不会误开旧副本页面）")
check(server._server_alive(PORT) is True, "指纹一致时 _server_alive 为 True")

SRV.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*46}\n通过 {PASSED} 项，失败 {FAILED} 项\n{'='*46}")
raise SystemExit(1 if FAILED else 0)
