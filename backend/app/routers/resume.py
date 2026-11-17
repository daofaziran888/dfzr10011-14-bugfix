"""断点续采恢复接口：维护断点记录，覆盖启动恢复、放弃恢复、归档记录等动作。

新增断点续采恢复链路接口：
- 断点字段与恢复状态原子保存 / 重新校验
- 恢复批次登记（并发只允许一个批次生效）
- 恢复事件（恢复回执）、完成回执、失败回执与失败重试
- 恢复回执账本、续采恢复结果、修复断点字段丢失核对清单查询
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.resume import ResumeService

router = APIRouter(prefix="/api/resume", tags=["断点续采恢复"])

service = ResumeService()

LIST_FIELDS = ["断点编号", "采集计划", "断点位置", "已采集量", "剩余量", "恢复策略", "恢复耗时", "记录状态"]
STATUSES = ["已记录", "恢复中", "已恢复", "已放弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按断点编号检索"),
    status: str | None = Query(default=None, description="已记录、恢复中、已恢复、已放弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按断点编号与状态过滤断点续采恢复列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 静态路径必须声明在 /{entry_id} 之前，否则 export/receipts 等会被当成断点 id
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出断点续采恢复清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "resume", "total": total, "items": items}


@router.get("/receipts")
def list_receipts(entry_id: int | None = Query(default=None)) -> dict[str, Any]:
    """恢复回执账本：完成回执与恢复回执共用严格递增版本号，保留版本顺序。"""
    return {"items": service.list_receipts(entry_id)}


@router.get("/results")
def list_results(entry_id: int | None = Query(default=None)) -> dict[str, Any]:
    """续采恢复结果数据：已写入归并下游，按恢复回执核对。"""
    return {"items": service.list_results(entry_id)}


@router.get("/checklist")
def list_checklist(entry_id: int | None = Query(default=None)) -> dict[str, Any]:
    """修复断点字段丢失核对清单：其他业务入口联动归并下游读取。"""
    return {"items": service.list_checklist(entry_id)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条断点记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"断点记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条断点记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="断点记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条断点记录执行启动恢复、放弃恢复、归档记录；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/checkpoint", response_model=ActionResult)
def save_checkpoint(entry_id: int, payload: EntryPayload) -> ActionResult:
    """断点字段与恢复状态一起保存并重新校验；任一步失败整体放弃，已确认水位不回退。"""
    entry, message, checklist = service.save_checkpoint(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, checklist=checklist)


@router.post("/{entry_id}/revalidate", response_model=ActionResult)
def revalidate(entry_id: int) -> ActionResult:
    """重新校验断点字段与恢复状态，字段丢失项写入归并下游核对清单。"""
    entry, message, checklist = service.revalidate(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, checklist=checklist)


@router.post("/{entry_id}/batches", response_model=ActionResult)
def start_batch(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记生效恢复批次；两个恢复请求并发时只有一个批次生效。"""
    batch_id = str(payload.values.get("batch_id") or "").strip()
    entry, message = service.start_batch(entry_id, batch_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/recovery", response_model=ActionResult)
def submit_recovery(entry_id: int, payload: EntryPayload) -> ActionResult:
    """恢复事件（恢复回执）：更新断点水位并把续采恢复结果写入归并下游。"""
    values = payload.values
    entry, message, result = service.submit_recovery(
        entry_id,
        batch_id=str(values.get("batch_id") or ""),
        recovered_count=values.get("recovered_count"),
        watermark=values.get("watermark"),
        remaining=values.get("remaining"),
        conflict_policy=values.get("conflict_policy"),
        elapsed=values.get("elapsed"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, result=result)


@router.post("/{entry_id}/completion", response_model=ActionResult)
def submit_completion(entry_id: int, payload: EntryPayload) -> ActionResult:
    """完成回执：与迟到恢复事件冲突时按 completion_first / recovery_first 两种策略处理。"""
    values = payload.values
    entry, message, result = service.submit_completion(
        entry_id,
        completed_count=values.get("completed_count"),
        watermark=values.get("watermark"),
        batch_id=values.get("batch_id"),
        conflict_policy=values.get("conflict_policy"),
        elapsed=values.get("elapsed"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, result=result)


@router.post("/{entry_id}/failures", response_model=ActionResult)
def record_failure(entry_id: int, payload: EntryPayload) -> ActionResult:
    """失败回执：不动已确认水位，重试从最后确认位置继续。"""
    values = payload.values
    entry, message, failure = service.record_failure(
        entry_id,
        batch_id=str(values.get("batch_id") or ""),
        reason=str(values.get("reason") or ""),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, failure=failure)
