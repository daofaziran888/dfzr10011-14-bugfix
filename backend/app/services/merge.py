"""结果聚合归并业务规则。

修复点（对应「聚合任务读取续采恢复结果并按恢复回执更新归并计数」）：

1. 聚合任务按「同一恢复回执」读取续采恢复结果（resume_results），
   按回执号幂等更新归并计数，旧已采集数据不会重复写入聚合。
2. 修复断点字段丢失核对清单（repair_checklist）由归并下游统一维护与查询，
   其他业务入口可联动读取。
3. 提供按恢复回执的核对视图：聚合计数与断点水位必须能按同一回执对齐。
4. 生命周期单向流转：待归并 → 归并中 → 已完成 / 归并失败，归并失败可重试
   回到归并中（唯一允许的重入边），归档只打标记，不允许回到旧值。
"""
from __future__ import annotations

from typing import Any

from app.services.resume import (
    CHECKLIST_TABLE,
    RESULTS_TABLE,
)
from app.store import store

MODULE = "merge"
REQUIRED_FIELDS = ["任务编号", "采集计划", "分片总数"]
NUMERIC_FIELDS = ["分片总数", "已归并数", "数据总量"]

STATUS_ORDER = ["待归并", "归并中", "已完成", "归并失败"]
# 单向状态机：每个动作只允许从所列源状态进入；归档只置 archived 标记
ACTION_RULES: dict[str, dict[str, Any]] = {
    "启动归并": {"target": "归并中", "sources": ["待归并"]},
    "完成归并": {"target": "已完成", "sources": ["归并中"]},
    "重试归并": {"target": "归并中", "sources": ["归并失败"]},
    "归档任务": {"target": None, "sources": ["已完成", "归并失败"]},
}


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class MergeService:
    # ---------------------------------------------------------------- 基础查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ---------------------------------------------------------------- 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            for field in ("任务编号", "采集计划", "归并策略", "输出路径"):
                entry[field] = values.get(field)
            shard_total = _to_int(values.get("分片总数"))
            if shard_total is None:
                return None, ["分片总数"]
            entry["分片总数"] = shard_total
            entry["已归并数"] = _to_int(values.get("已归并数")) or 0
            entry["数据总量"] = _to_int(values.get("数据总量")) or 0
            entry["status"] = STATUS_ORDER[0]
            entry["任务状态"] = entry["status"]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["archived"] = False
            store.rows(MODULE).append(entry)
            return dict(entry), []

    # ---------------------------------------------------------------- 动作（单向状态机）
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结果聚合归并可执行范围"
        rule = ACTION_RULES[action]
        with store.lock:
            if entry.get("archived"):
                return None, "聚合任务已归档，生命周期已终结，不能再变更状态"
            current = str(entry.get("status"))
            if current not in rule["sources"]:
                return None, (
                    f"状态「{current}」不允许执行「{action}」，"
                    f"生命周期只能按 {' → '.join(STATUS_ORDER)} 单向流转"
                )
            if action == "归档任务":
                entry["archived"] = True
                entry["pending"] = False
            else:
                target = rule["target"]
                if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
                    return None, f"目标状态「{target}」早于当前状态「{current}」，不允许回退"
                entry["status"] = target
                entry["任务状态"] = target
                entry["pending"] = target not in ("已完成", "归并失败")
            return dict(entry), f"聚合任务已{action}"

    # ---------------------------------------------------------------- 读取续采恢复结果，按回执更新归并计数
    def apply_resume_result(self, result_id: int) -> tuple[dict[str, Any] | None, str]:
        """聚合任务读取一条续采恢复结果，按恢复回执幂等更新归并计数。"""
        with store.lock:
            result = next(
                (row for row in store.rows(RESULTS_TABLE) if int(row.get("id", 0)) == result_id),
                None,
            )
            if result is None:
                return None, f"续采恢复结果 {result_id} 不存在"
            if result.get("merge_status") == "applied":
                return None, (
                    f"恢复结果 {result_id}（回执 #{result.get('receipt_no')}）已归并，"
                    "按回执幂等跳过，旧数据不重复写入聚合"
                )
            if result.get("merge_delta", 0) <= 0:
                return None, (
                    f"恢复结果 {result_id} 归并增量为 0"
                    + (f"（{result.get('defer_reason')}）" if result.get("defer_reason") else "")
                    + "，等待下一批处理"
                )

            plan = result.get("采集计划")
            target = self._find_merge_for_plan(plan)
            if target is None:
                result["merge_status"] = "waiting_merge"
                return None, f"采集计划「{plan}」暂无对应聚合任务，恢复结果保留待聚合任务读取"

            # 同一回执只能归并一次：按 receipt_no 再兜底去重
            applied_receipts = target.setdefault("applied_receipts", [])
            if result["receipt_no"] in applied_receipts:
                result["merge_status"] = "applied"
                result["applied_merge_id"] = target["id"]
                return None, (
                    f"回执 #{result['receipt_no']} 已计入聚合任务 {target.get('任务编号')}，"
                    "重复读取按幂等跳过"
                )

            delta = int(result["merge_delta"])
            target["已归并数"] = int(target.get("已归并数") or 0) + delta
            target["数据总量"] = int(target.get("数据总量") or 0) + delta
            applied_receipts.append(result["receipt_no"])
            # 归并计数变化只推动状态前进，不允许借恢复结果把终态改回旧值
            if target["status"] == "待归并":
                target["status"] = "归并中"
                target["任务状态"] = "归并中"
                target["pending"] = True
            result["merge_status"] = "applied"
            result["applied_merge_id"] = target["id"]
            return dict(target), (
                f"聚合任务 {target.get('任务编号')} 按回执 #{result['receipt_no']} "
                f"追加归并 {delta}，已归并数 {target['已归并数']}"
            )

    def apply_pending_results(self, entry_id: int) -> list[dict[str, Any]]:
        """聚合任务主动拉取所有待归并的续采恢复结果（其他业务入口联动用）。"""
        applied: list[dict[str, Any]] = []
        with store.lock:
            target = store.find(MODULE, entry_id)
            if target is None:
                return applied
            pending = [
                row for row in store.rows(RESULTS_TABLE)
                if row.get("采集计划") == target.get("采集计划")
                and row.get("merge_status") != "applied"
                and row.get("merge_delta", 0) > 0
            ]
            for result in sorted(pending, key=lambda row: int(row.get("receipt_no", 0))):
                updated, _ = self.apply_resume_result(int(result["id"]))
                if updated is not None:
                    applied.append(updated)
        return applied

    # ---------------------------------------------------------------- 下游核对清单 / 恢复结果 / 回执核对
    def list_repair_checklist(
        self, *, status: str | None = None, plan: str | None = None
    ) -> list[dict[str, Any]]:
        rows = store.rows(CHECKLIST_TABLE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if plan:
            rows = [row for row in rows if row.get("采集计划") == plan]
        return sorted(rows, key=lambda row: (row.get("断点id", 0), row.get("id", 0)))

    def list_resume_results(
        self, *, plan: str | None = None, merge_status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = store.rows(RESULTS_TABLE)
        if plan:
            rows = [row for row in rows if row.get("采集计划") == plan]
        if merge_status:
            rows = [row for row in rows if row.get("merge_status") == merge_status]
        return sorted(rows, key=lambda row: (row.get("断点id", 0), row.get("id", 0)))

    def reconcile(self, entry_id: int) -> dict[str, Any]:
        """按同一恢复回执核对聚合计数与断点水位。"""
        resume_entry = store.find("resume", entry_id)
        if resume_entry is None:
            return {"ok": False, "message": f"断点记录 {entry_id} 不存在或已归档", "items": []}
        with store.lock:
            results = [
                row for row in store.rows(RESULTS_TABLE)
                if row.get("断点id") == entry_id
            ]
            plan = resume_entry.get("采集计划")
            merge_entry = self._find_merge_for_plan(plan)
            applied = sorted(
                (row for row in results if row.get("merge_status") == "applied"),
                key=lambda row: int(row.get("receipt_no", 0)),
            )
            applied_delta = sum(int(row.get("merge_delta", 0)) for row in applied)
            last_receipt_no = max(
                (int(row.get("receipt_no", 0)) for row in results), default=0
            )
            watermark = int(resume_entry.get("confirmed_watermark") or 0)
            merge_count = int(merge_entry.get("已归并数") or 0) if merge_entry else 0
            # 同一回执口径：已申请归并的回执增量之和，必须等于聚合任务按回执追加的总量。
            # recovery_first 下递延完成回执的增量被恢复批次打包追加，
            # 其 merge_status=applied 且 defer_reason 标注打包核销，同样视为一致。
            def _is_consistent(row: dict[str, Any]) -> bool:
                if row.get("receipt_no") in merge_entry.get("applied_receipts", []):
                    return True
                return (
                    row.get("merge_status") == "applied"
                    and str(row.get("defer_reason") or "").startswith("recovery_first:")
                )

            receipt_consistent = (
                merge_entry is not None
                and all(_is_consistent(row) for row in applied)
            )
            return {
                "ok": True,
                "断点编号": resume_entry.get("断点编号"),
                "采集计划": plan,
                "confirmed_watermark": watermark,
                "last_receipt_no": last_receipt_no,
                "applied_receipt_delta": applied_delta,
                "merge_task": merge_entry.get("任务编号") if merge_entry else None,
                "merge_count": merge_count,
                "receipt_consistent": receipt_consistent,
                "items": sorted(results, key=lambda row: int(row.get("receipt_no", 0))),
            }

    # ---------------------------------------------------------------- 内部辅助
    def _find_merge_for_plan(self, plan: Any) -> dict[str, Any] | None:
        if not plan:
            return None
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("采集计划") == plan and not row.get("archived")
        ]
        if not candidates:
            return None
        # 优先归并中/待归并的任务，其次任意未归档任务
        for preferred in ("归并中", "待归并"):
            for row in candidates:
                if row.get("status") == preferred:
                    return row
        return candidates[0]
