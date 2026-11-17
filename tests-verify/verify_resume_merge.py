"""断点续采恢复 / 结果聚合归并 修复点全链路验证。

运行：
  PYTHONPATH=/workspace/backend/.venv:/workspace/backend \
  python3 -I /workspace/tests-verify/verify_resume_merge.py

每个用例独立重建内存仓库，断言覆盖：
1. 断点字段与恢复状态原子保存、已确认水位不可回退
2. 修复断点字段丢失 → 核对清单 → 字段补齐后清单关闭
3. 完成终态优先 / 恢复终态优先两种冲突处理
4. 并发恢复请求只有一个批次生效，旧数据不重复写入聚合
5. 失败重试从最后确认位置继续，回执版本严格递增且保留顺序
6. 聚合任务按同一恢复回执幂等更新归并计数
7. 生命周期单向流转，归档不回旧值
"""
from __future__ import annotations

import sys

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

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reset_store() -> None:
    """每个用例重建仓库，避免互相污染。"""
    store._tables.clear()
    for name in ("resume", "merge", RECEIPTS_TABLE, RESULTS_TABLE,
                 CHECKLIST_TABLE, "resume_failures"):
        store._tables[name] = []


_SEQ = 0


def _uid() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def make_resume(plan: str | None = None, collected: int = 100, remaining: int = 50,
                policy: str = "completion_first") -> dict:
    seq = _uid()
    plan = plan or f"计划{seq}"
    entry, missing = ResumeService().create_entry({
        "断点编号": f"RESU-T-{seq}",
        "采集计划": plan,
        "断点位置": "shard-1#offset-100",
        "已采集量": collected,
        "剩余量": remaining,
        "conflict_policy": policy,
    })
    assert not missing, missing
    return entry


def make_merge(plan: str | None = None) -> dict:
    seq = _uid()
    plan = plan or f"计划{seq}"
    entry, missing = MergeService().create_entry({
        "任务编号": f"MERG-T-{seq}",
        "采集计划": plan,
        "分片总数": 4,
    })
    assert not missing, missing
    return entry


# ---------------------------------------------------------------- 用例1：原子保存与水位不可回退
def case_atomic_save_and_watermark() -> None:
    print("用例1：断点字段与恢复状态原子保存，已确认水位不可回退")
    reset_store()
    svc = ResumeService()
    entry = make_resume()
    eid = entry["id"]

    # 非法数值 → 整体放弃，字段与状态都不变
    before = dict(svc.get_entry(eid))
    _, msg, _ = svc.save_checkpoint(eid, {"已采集量": "abc", "status": "恢复中"})
    after = svc.get_entry(eid)
    check("非法数值时保存整体失败", after["已采集量"] == before["已采集量"], msg)
    check("失败时状态未被改动", after["status"] == before["status"])

    # 状态回退被拒
    svc.run_action(eid, "启动恢复")  # 已记录 → 恢复中
    _, msg, _ = svc.save_checkpoint(eid, {"status": "已记录"})
    check("保存接口不允许状态回退", svc.get_entry(eid)["status"] == "恢复中", msg)

    # 水位回退被拒，已确认水位保持
    _, msg, _ = svc.save_checkpoint(eid, {"已采集量": 90})
    check("已采集量低于确认水位时拒绝", svc.get_entry(eid)["confirmed_watermark"] == 100, msg)

    # 正常保存：字段与状态同批落地，水位推进
    _, msg, _ = svc.save_checkpoint(eid, {
        "断点位置": "shard-1#offset-150", "已采集量": 150, "剩余量": 0, "status": "恢复中",
    })
    saved = svc.get_entry(eid)
    check("字段与状态一起保存成功", saved["断点位置"] == "shard-1#offset-150"
          and saved["已采集量"] == 150 and saved["confirmed_watermark"] == 150, msg)


# ---------------------------------------------------------------- 用例2：字段丢失核对清单
def case_field_loss_checklist() -> None:
    print("用例2：修复断点字段丢失写入核对清单，补齐后关闭，列表与详情一致")
    reset_store()
    svc = ResumeService()
    entry = make_resume(plan="计划X")
    eid = entry["id"]
    make_merge("计划X")

    # 手动制造字段丢失（模拟下游拿到缺字段的断点）
    raw = svc.get_entry(eid)
    raw["已采集量"] = None
    raw["剩余量"] = None
    _, msg, checklist = svc.revalidate(eid)
    check("重新校验发现字段丢失", len(checklist) == 2, msg)
    check("断点标记为异常", svc.get_entry(eid)["abnormal"] is True)
    merge_list = MergeService().list_repair_checklist()
    check("核对清单已同步到归并下游", len(merge_list) == 2
          and all(item["status"] == "open" for item in merge_list))

    # 状态残留旧值检测
    raw["记录状态"] = "恢复中"  # status 仍是 已记录
    _, _, checklist2 = svc.revalidate(eid)
    check("状态残留旧值进核对清单",
          any(i["issue"].startswith("恢复字段丢失后状态残留") for i in checklist2))

    # 补齐字段并对齐状态 → 清单关闭
    _, msg, _ = svc.save_checkpoint(eid, {
        "已采集量": 120, "剩余量": 30, "记录状态": "已记录",
    })
    check("补齐后异常清除", svc.get_entry(eid)["abnormal"] is False, msg)
    remaining_open = MergeService().list_repair_checklist(status="open")
    check("字段补齐后核对清单关闭", len(remaining_open) == 0)

    # 列表与详情必须对得上
    items, _ = svc.list_entries(page=1, size=20)
    detail = svc.get_entry(eid)
    check("列表与详情数据一致",
          any(row["id"] == eid and row["断点位置"] == detail["断点位置"]
              and row["confirmed_watermark"] == detail["confirmed_watermark"]
              for row in items))


# ---------------------------------------------------------------- 用例3：完成终态优先
def case_completion_first() -> None:
    print("用例3：完成终态优先——聚合计数立即增加完成量，迟到恢复转下一次待恢复项")
    reset_store()
    svc = ResumeService()
    merge_svc = MergeService()
    entry = make_resume(plan="计划X", policy="completion_first")
    eid = entry["id"]
    merge_entry = make_merge("计划X")
    mid = merge_entry["id"]

    # 先完成回执
    _, msg, result = svc.submit_completion(
        eid, completed_count=80, watermark=180, batch_id="b-complete-1")
    check("完成回执后断点进入已恢复", svc.get_entry(eid)["status"] == "已恢复", msg)
    merge_entry = merge_svc.get_entry(mid)
    check("聚合计数立即增加完成量(180-100=80)", merge_entry["已归并数"] == 80,
          f"实际 {merge_entry['已归并数']}")
    check("回执版本号为1", result["receipt_no"] == 1)

    # 稍后到达的恢复事件
    _, msg, late = svc.submit_recovery(
        eid, batch_id="b-recovery-late", recovered_count=90, watermark=190)
    merge_entry = merge_svc.get_entry(mid)
    check("迟到恢复不重复增加聚合计数", merge_entry["已归并数"] == 80,
          f"实际 {merge_entry['已归并数']}")
    check("迟到恢复结果标记为完成优先递延",
          late["defer_reason"] == "completion_first:completed")
    checklist = merge_svc.list_repair_checklist()
    check("恢复清单出现下一次待恢复项",
          any(i["issue"] == "迟到恢复事件" for i in checklist))
    check("迟到恢复仍推进回执版本(2)", svc.get_entry(eid)["receipt_version"] == 2)
    # 完成终态已把水位确认到 180；迟到恢复不允许把已确认水位回退或重复改写
    check("已确认水位不被迟到恢复回退",
          svc.get_entry(eid)["confirmed_watermark"] == 180)

    # 同一批次重复投递 → 幂等
    _, msg, dup = svc.submit_recovery(
        eid, batch_id="b-recovery-late", recovered_count=90, watermark=190)
    check("重复恢复回执幂等去重", dup["receipt_no"] == 2, msg)
    check("幂等重放不改变聚合计数", merge_svc.get_entry(mid)["已归并数"] == 80)


# ---------------------------------------------------------------- 用例4：恢复终态优先
def case_recovery_first() -> None:
    print("用例4：恢复终态优先——恢复结果先更新水位，聚合计数下一批追加完成量")
    reset_store()
    svc = ResumeService()
    merge_svc = MergeService()
    entry = make_resume(plan="计划X", policy="recovery_first")
    eid = entry["id"]
    merge_entry = make_merge("计划X")
    mid = merge_entry["id"]

    # 完成回执先到：只记账，不计聚合，状态停在恢复中
    _, msg, comp_result = svc.submit_completion(
        eid, completed_count=80, watermark=180, batch_id="b-complete-1",
        conflict_policy="recovery_first")
    check("恢复优先下完成回执不改终态", svc.get_entry(eid)["status"] == "恢复中", msg)
    check("完成回执不立即计入聚合", merge_svc.get_entry(mid)["已归并数"] == 0)
    check("完成结果标记等待恢复",
          comp_result["defer_reason"] == "recovery_first:await_recovery")

    # 恢复事件后到：先更新水位，增量在本批追加
    _, msg, rec_result = svc.submit_recovery(
        eid, batch_id="b-recovery-1", recovered_count=80, watermark=180,
        conflict_policy="recovery_first")
    check("恢复后水位先更新", svc.get_entry(eid)["confirmed_watermark"] == 180, msg)
    check("聚合计数在下一批追加完成量(80)",
          merge_svc.get_entry(mid)["已归并数"] == 80,
          f"实际 {merge_svc.get_entry(mid)['已归并数']}")
    check("递延完成结果随恢复批次核销",
          comp_result["merge_status"] == "applied")
    check("恢复结果自身已归并", rec_result["merge_status"] == "applied")

    # 回执版本顺序：完成 #1，恢复 #2
    receipts = svc.list_receipts(eid)
    check("完成/恢复回执版本顺序保留",
          [r["receipt_no"] for r in receipts] == [1, 2]
          and [r["kind"] for r in receipts] == ["completion", "recovery"])

    # 核对视图：聚合计数与断点水位按同一回执对齐
    recon = merge_svc.reconcile(eid)
    check("回执核对一致", recon["receipt_consistent"] is True, str(recon))
    check("核对视图水位=180/计数=80",
          recon["confirmed_watermark"] == 180 and recon["merge_count"] == 80)


# ---------------------------------------------------------------- 用例5：并发批次只有一个生效
def case_concurrent_batches() -> None:
    print("用例5：两个恢复请求并发只有一个批次生效，旧已采集数据不重复写入聚合")
    reset_store()
    svc = ResumeService()
    merge_svc = MergeService()
    entry = make_resume(plan="计划X")
    eid = entry["id"]
    merge_entry = make_merge("计划X")
    mid = merge_entry["id"]

    _, msg = svc.start_batch(eid, "batch-A")
    check("批次A拿到生效权", "唯一生效" in msg, msg)
    _, msg = svc.start_batch(eid, "batch-B")
    check("并发批次B被拒绝", msg is not None and "拒绝" in msg, msg)

    # B 仍然强行提交恢复事件 → 被并发冲突拦截
    _, msg, _ = svc.submit_recovery(
        eid, batch_id="batch-B", recovered_count=30, watermark=130)
    check("并发批次恢复事件被拦截", "拒绝" in msg, msg)
    check("被拦批次未写入聚合", merge_svc.get_entry(mid)["已归并数"] == 0)
    check("并发冲突进入核对清单",
          any(i["issue"] == "并发批次冲突" for i in merge_svc.list_repair_checklist()))

    # A 正常提交 → 生效
    _, msg, result = svc.submit_recovery(
        eid, batch_id="batch-A", recovered_count=30, watermark=130)
    check("批次A恢复生效", result["merge_status"] == "applied", msg)
    check("聚合计数只增加A的增量(30)", merge_svc.get_entry(mid)["已归并数"] == 30)

    # A 重放 → 幂等，不重复写入
    _, msg, _ = svc.submit_recovery(
        eid, batch_id="batch-A", recovered_count=30, watermark=130)
    check("批次A重放幂等", merge_svc.get_entry(mid)["已归并数"] == 30, msg)


# ---------------------------------------------------------------- 用例6：失败重试从确认水位继续
def case_failure_retry() -> None:
    print("用例6：失败重试从最后确认位置继续，失败/完成/恢复回执版本有序")
    reset_store()
    svc = ResumeService()
    entry = make_resume(plan="计划X")
    eid = entry["id"]
    make_merge("计划X")

    svc.start_batch(eid, "batch-1")
    _, msg, failure = svc.record_failure(eid, batch_id="batch-1", reason="节点超时")
    check("失败后已确认水位不变", failure["resume_from_watermark"] == 100, msg)
    check("失败时未产生恢复结果",
          all(r.get("batch_id") != "batch-1" or r["receipt_kind"] != "recovery"
              for r in svc.list_results(eid)))

    # 重试批次从确认水位 100 继续
    _, msg = svc.start_batch(eid, "batch-1-retry")
    check("重试批次从最后确认位置继续", "水位 100" in msg, msg)
    _, msg, result = svc.submit_recovery(
        eid, batch_id="batch-1-retry", recovered_count=40, watermark=140)
    check("重试成功推进水位到140",
          svc.get_entry(eid)["confirmed_watermark"] == 140, msg)
    check("重试恢复结果只计入一次", result["merge_delta"] == 40)

    receipts = svc.list_receipts(eid)
    check("回执版本严格递增", [r["receipt_no"] for r in receipts] == [1, 2])
    check("失败回执版本顺序保留", receipts[0]["kind"] == "failure"
          and receipts[1]["kind"] == "recovery")


# ---------------------------------------------------------------- 用例7：聚合幂等与单向状态机
def case_merge_idempotent_and_lifecycle() -> None:
    print("用例7：聚合按回执幂等归并；生命周期单向流转，归档不回旧值")
    reset_store()
    svc = ResumeService()
    merge_svc = MergeService()
    entry = make_resume(plan="计划X")
    eid = entry["id"]
    merge_entry = make_merge("计划X")
    mid = merge_entry["id"]

    _, _, r1 = svc.submit_recovery(
        eid, batch_id="b1", recovered_count=20, watermark=120)
    # 聚合任务再次主动读取同一恢复结果 → 幂等
    _, msg = merge_svc.apply_resume_result(r1["id"])
    check("同一恢复结果重复读取幂等跳过", "幂等" in msg, msg)
    check("归并计数未翻倍", merge_svc.get_entry(mid)["已归并数"] == 20)

    # 待归并被恢复结果推动到归并中（只前进）
    check("恢复结果推动待归并→归并中", merge_svc.get_entry(mid)["status"] == "归并中")

    # 非法流转被拒
    _, msg = merge_svc.run_action(mid, "启动归并")  # 已在归并中
    check("归并中不允许再次启动归并", "不允许" in msg, msg)
    _, msg = merge_svc.run_action(mid, "重试归并")  # 非失败不能重试
    check("非失败状态不允许重试", "不允许" in msg, msg)

    # 正常前进到已完成后归档
    merge_svc.run_action(mid, "完成归并")
    check("归并中→已完成", merge_svc.get_entry(mid)["status"] == "已完成")
    _, msg = merge_svc.run_action(mid, "归档任务")
    check("已完成可归档", merge_svc.get_entry(mid)["archived"] is True, msg)
    _, msg = merge_svc.run_action(mid, "完成归并")
    check("归档后不能再变更状态", "归档" in msg, msg)
    check("归档不改回旧状态", merge_svc.get_entry(mid)["status"] == "已完成")

    # resume 侧单向状态机
    r2 = make_resume(plan="计划Y")
    rid = r2["id"]
    _, msg = svc.run_action(rid, "归档记录")  # 已记录不能直接归档
    check("断点非终态不能归档", "不允许" in msg, msg)
    svc.run_action(rid, "启动恢复")
    svc.run_action(rid, "放弃恢复")  # 恢复中→已放弃
    check("放弃后为终态", svc.get_entry(rid)["status"] == "已放弃")
    _, msg = svc.run_action(rid, "启动恢复")
    check("已放弃不能回到恢复中", "不允许" in msg, msg)
    _, msg, _ = svc.submit_recovery(
        rid, batch_id="bx", recovered_count=1, watermark=101)
    check("已放弃断点拒收恢复事件", "放弃" in msg, msg)


# ---------------------------------------------------------------- 用例8：聚合主动拉取 + 无匹配任务挂起
def test_pending_pull_and_waiting() -> None:
    print("用例8：无聚合任务时恢复结果挂起，任务出现后主动拉取按回执归并")
    reset_store()
    svc = ResumeService()
    merge_svc = MergeService()
    entry = make_resume(plan="计划Z")
    eid = entry["id"]

    _, _, r1 = svc.submit_recovery(
        eid, batch_id="b1", recovered_count=25, watermark=125)
    check("无聚合任务时结果挂起", r1["merge_status"] == "waiting_merge")

    merge_entry = make_merge("计划Z")
    mid = merge_entry["id"]
    applied = merge_svc.apply_pending_results(mid)
    check("聚合任务主动拉取挂起结果", len(applied) == 1)
    check("拉取后归并计数正确(25)", merge_svc.get_entry(mid)["已归并数"] == 25)
    check("挂起结果转为已归并",
          merge_svc.list_resume_results(plan="计划Z")[0]["merge_status"] == "applied")


def main() -> int:
    for case in (
        case_atomic_save_and_watermark,
        case_field_loss_checklist,
        case_completion_first,
        case_recovery_first,
        case_concurrent_batches,
        case_failure_retry,
        case_merge_idempotent_and_lifecycle,
        test_pending_pull_and_waiting,
    ):
        case()
        print()
    print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
