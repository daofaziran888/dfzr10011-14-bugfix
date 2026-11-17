"""HTTP 层冒烟（标准库 urllib，直连真实 uvicorn 进程）。

先在 /workspace/backend 启动：
  PYTHONPATH=.venv:. python3 -m uvicorn app.main:app --port 8123
"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8125"


def call(method: str, path: str, values: dict | None = None) -> tuple[int, dict]:
    data = json.dumps({"values": values or {}}).encode()
    req = urllib.request.Request(
        BASE + path, data=data if method == "POST" else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def post(path: str, values: dict) -> dict:
    code, body = call("POST", path, values)
    assert code == 200, body
    return body


def get(path: str) -> dict:
    code, body = call("GET", path)
    assert code == 200, body
    return body


# 1. 登记断点与聚合任务（同一采集计划）
r = post("/api/resume", {
    "断点编号": "RESU-HTTP-1", "采集计划": "HTTP计划", "断点位置": "o-100",
    "已采集量": 100, "剩余量": 100,
})
assert r["ok"], r
eid = r["entry"]["id"]
r = post("/api/merge", {"任务编号": "MERG-HTTP-1", "采集计划": "HTTP计划", "分片总数": 2})
assert r["ok"], r
mid = r["entry"]["id"]

# 2. 静态路由不被 /{entry_id} 吞掉
assert get("/api/resume/export")["module"] == "resume"
assert get("/api/merge/export")["module"] == "merge"
assert get("/api/resume/receipts") == {"items": []}
assert get("/api/merge/repair-checklist") == {"items": []}
assert get("/api/merge/resume-results") == {"items": []}

# 3. 原子保存：水位回退被拒（HTTP 200 + ok=false，前端按 message 提示）
r = post(f"/api/resume/{eid}/checkpoint", {"已采集量": 50})
assert r["ok"] is False and "回退" in r["message"], r

# 4. 字段齐全时 revalidate 应直接通过、不产生核对项
r = post(f"/api/resume/{eid}/revalidate", {})
assert r["ok"] and r["checklist"] == [], r
assert get("/api/merge/repair-checklist?status=open")["items"] == []

# 5. 完成回执（完成终态优先）→ 聚合计数立即增加
r = post(f"/api/resume/{eid}/completion",
         {"completed_count": 60, "watermark": 160, "batch_id": "http-b1"})
assert r["ok"], r
assert get(f"/api/merge/{mid}")["已归并数"] == 60

# 6. 迟到恢复事件 → 不重复计入，清单出现待恢复项
r = post(f"/api/resume/{eid}/recovery",
         {"batch_id": "http-b2", "recovered_count": 70, "watermark": 170})
assert r["ok"], r
assert get(f"/api/merge/{mid}")["已归并数"] == 60
issues = {i["issue"] for i in get("/api/merge/repair-checklist")["items"]}
assert "迟到恢复事件" in issues, issues

# 7. 回执账本版本顺序
receipts = get("/api/resume/receipts")["items"]
assert [x["receipt_no"] for x in receipts] == [1, 2], receipts
assert [x["kind"] for x in receipts] == ["completion", "recovery"]

# 8. 同一恢复结果重复 apply → 幂等
result_id = get("/api/merge/resume-results")["items"][0]["id"]
r = post(f"/api/merge/resume-results/{result_id}/apply", {})
assert r["ok"] is False and "幂等" in r["message"], r

# 9. 核对视图
recon = get(f"/api/merge/{mid}/reconcile")
assert recon["ok"] and recon["merge_count"] == 60 and recon["confirmed_watermark"] == 160, recon

# 10. 状态机非法流转被拦
r = post(f"/api/merge/{mid}/actions", {"action": "重试归并"})
assert r["ok"] is False and "不允许" in r["message"], r

# 11. 失败回执与重试
e2 = post("/api/resume", {
    "断点编号": "RESU-HTTP-2", "采集计划": "HTTP计划2", "断点位置": "o-10",
    "已采集量": 10, "剩余量": 5,
})["entry"]["id"]
post(f"/api/resume/{e2}/batches", {"batch_id": "bx"})
r = post(f"/api/resume/{e2}/failures", {"batch_id": "bx", "reason": "超时"})
assert r["ok"] and r["failure"]["resume_from_watermark"] == 10, r
r = post(f"/api/resume/{e2}/batches", {"batch_id": "bx-retry"})
assert r["ok"] and "水位 10" in r["message"], r

# 12. 不存在的资源 → 404
code, _ = call("GET", "/api/resume/9999")
assert code == 404
code, _ = call("GET", "/api/merge/9999")
assert code == 404

print("HTTP 冒烟全部通过（12 组场景）")
