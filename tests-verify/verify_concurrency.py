"""并发压测：生效批次存在后，多个恢复请求并发竞争，验证只有一个批次生效。"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/workspace/backend")
sys.path.insert(0, "/workspace/backend/.venv")

from app.store import store  # noqa: E402
from app.services.resume import (  # noqa: E402
    CHECKLIST_TABLE,
    RECEIPTS_TABLE,
    RESULTS_TABLE,
    ResumeService,
)
from app.services.merge import MergeService  # noqa: E402

CONTENDERS = 20

store._tables.clear()
for name in ("resume", "merge", RECEIPTS_TABLE, RESULTS_TABLE,
             CHECKLIST_TABLE, "resume_failures"):
    store._tables[name] = []

svc = ResumeService()
merge_svc = MergeService()
entry, _ = svc.create_entry({
    "断点编号": "RESU-CONC", "采集计划": "并发计划", "断点位置": "o-0",
    "已采集量": 0, "剩余量": 1000,
})
eid = entry["id"]
merge_entry, _ = merge_svc.create_entry({
    "任务编号": "MERG-CONC", "采集计划": "并发计划", "分片总数": 8,
})
mid = merge_entry["id"]

# 阶段一：先有一个批次拿到唯一生效权（真实场景里恢复请求先登记批次）
ok, msg = svc.start_batch(eid, "batch-incumbent")
assert ok, msg

# 竞争者在生效窗口内强行提交恢复事件：被生效批次拦截，不写恢复结果、不动聚合计数
ok, msg, early = svc.submit_recovery(
    eid, batch_id="batch-sneak", recovered_count=99, watermark=99,
)
assert ok is None and "拒绝" in msg, msg
assert early is None
assert merge_svc.get_entry(mid)["已归并数"] == 0
assert svc.list_results(eid) == []

# 阶段二：20 个恢复请求在生效批次未完成时并发竞争
barrier = threading.Barrier(CONTENDERS)
lock = threading.Lock()
accepted: list[str] = []
rejected: list[str] = []


def contender(idx: int) -> None:
    batch_id = f"batch-{idx}"
    barrier.wait()
    got, _ = svc.start_batch(eid, batch_id)
    with lock:
        if got is not None:
            accepted.append(batch_id)
        else:
            rejected.append(batch_id)


threads = [threading.Thread(target=contender, args=(i,)) for i in range(CONTENDERS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert accepted == [], f"并发阶段不应有新批次生效: {accepted}"
assert len(rejected) == CONTENDERS

# 生效批次完成恢复，旧已采集数据只写入一次聚合
ok, msg, result = svc.submit_recovery(
    eid, batch_id="batch-incumbent", recovered_count=50, watermark=50,
)
assert ok and result["merge_status"] == "applied", msg

final = svc.get_entry(eid)
merge_final = merge_svc.get_entry(mid)
print(f"生效批次: batch-incumbent；并发竞争被拒: {len(rejected)} 个")
print(f"已确认水位: {final['confirmed_watermark']}（期望 50）")
print(f"聚合计数: {merge_final['已归并数']}（期望 50，无重复写入）")
print(f"恢复结果条数: {len(svc.list_results(eid))}（期望 1）")
assert final["confirmed_watermark"] == 50
assert merge_final["已归并数"] == 50
assert len(svc.list_results(eid)) == 1

# 每个被拒并发请求都在核对清单留痕（含窗口内抢跑的 batch-sneak 与 20 个竞争者）
conflicts = [i for i in merge_svc.list_repair_checklist() if i["issue"] == "并发批次冲突"]
print(f"并发冲突核对项: {len(conflicts)} 条（期望 {CONTENDERS + 1}）")
assert len(conflicts) == CONTENDERS + 1

# 生效批次完成后断点进入「已恢复」终态，单向生命周期不允许再开新批次
ok, msg = svc.start_batch(eid, "batch-next-round")
assert ok is None and "不能启动恢复批次" in msg, msg

print("并发压测通过：只有一个批次生效，旧已采集数据未重复写入聚合，冲突全部留痕")
