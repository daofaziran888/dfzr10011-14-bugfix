"""结果聚合归并接口：维护聚合任务，覆盖启动归并、重试归并、归档任务等动作。

新增断点续采联动接口：
- 聚合任务读取续采恢复结果并按恢复回执更新归并计数（幂等）
- 修复断点字段丢失核对清单、续采恢复结果查询（其他业务入口联动）
- 按同一恢复回执核对聚合计数与断点水位
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.merge import MergeService

router = APIRouter(prefix="/api/merge", tags=["结果聚合归并"])

service = MergeService()

LIST_FIELDS = ["任务编号", "采集计划", "分片总数", "已归并数", "归并策略", "输出路径", "数据总量", "任务状态"]
STATUSES = ["待归并", "归并中", "已完成", "归并失败"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待归并、归并中、已完成、归并失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤结果聚合归并列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 静态路径必须声明在 /{entry_id} 之前，避免被当成聚合任务 id
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出结果聚合归并清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "merge", "total": total, "items": items}


@router.get("/repair-checklist")
def repair_checklist(
    status: str | None = Query(default=None, description="open / resolved"),
    plan: str | None = Query(default=None, description="按采集计划过滤"),
) -> dict[str, Any]:
    """修复断点字段丢失核对清单：归并下游统一视图，供其他业务入口联动。"""
    return {"items": service.list_repair_checklist(status=status, plan=plan)}


@router.get("/resume-results")
def resume_results(
    plan: str | None = Query(default=None, description="按采集计划过滤"),
    merge_status: str | None = Query(default=None, description="applied / pending / waiting_merge"),
) -> dict[str, Any]:
    """续采恢复结果数据：聚合任务按恢复回执核对归并计数的来源。"""
    return {"items": service.list_resume_results(plan=plan, merge_status=merge_status)}


@router.post("/resume-results/{result_id}/apply", response_model=ActionResult)
def apply_resume_result(result_id: int) -> ActionResult:
    """读取一条续采恢复结果并按恢复回执更新归并计数；同一回执幂等不重复计入。"""
    entry, message = service.apply_resume_result(result_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/apply-pending", response_model=ActionResult)
def apply_pending(entry_id: int) -> ActionResult:
    """聚合任务主动拉取本采集计划下所有待归并的续采恢复结果。"""
    if service.get_entry(entry_id) is None:
        return ActionResult(ok=False, message=f"聚合任务 {entry_id} 不存在或已归档")
    applied = service.apply_pending_results(entry_id)
    return ActionResult(
        ok=True,
        message=f"已按恢复回执归并 {len(applied)} 条续采恢复结果",
        entry=service.get_entry(entry_id),
    )


@router.get("/{entry_id}/reconcile")
def reconcile(entry_id: int) -> dict[str, Any]:
    """按同一恢复回执核对聚合计数与断点水位。"""
    return service.reconcile(entry_id)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条聚合任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条聚合任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="聚合任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条聚合任务执行启动归并、完成归并、重试归并、归档任务；非法流转会被拦下。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
