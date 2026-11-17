"""断点续采恢复业务规则。

修复点（对应「修复断点字段保存并重新校验恢复状态」）：

1. 断点字段（断点位置、已采集量、剩余量）与恢复状态在同一把锁、同一次写入里
   一起保存，任何一步校验失败都不落地，已确认水位（confirmed_watermark）
   只允许单调前进，任何失败都不能把它回退。
2. 每条断点维护恢复回执账本（receipts），完成回执与恢复回执共用严格递增的
   receipt_version，保留版本顺序，重复回执按回执号幂等去重。
3. 同一断点同时存在「完成回执」与「稍后到达的恢复事件」时，两种终态优先
   策略都保留：completion_first（完成终态优先，聚合计数立即增加完成量，
   恢复清单出现下一次待恢复项）与 recovery_first（恢复终态优先，恢复结果
   先更新水位，聚合计数在下一批追加完成量）。
4. 两个恢复请求并发时，只有一个批次（active_batch）生效，旧已采集数据不会
   重复写入聚合；失败重试从最后确认位置继续。
5. 续采恢复结果写入归并下游（resume_results），字段丢失写入修复断点字段
   丢失核对清单（repair_checklist），聚合任务按同一恢复回执核对归并计数。
6. 生命周期单向流转，状态变更与归档都不允许回到旧值。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "resume"

# 归并下游联动表名（merge 服务复用同一组常量，避免两边口径漂移）
RECEIPTS_TABLE = "resume_receipts"
RESULTS_TABLE = "resume_results"
CHECKLIST_TABLE = "repair_checklist"
FAILURES_TABLE = "resume_failures"

REQUIRED_FIELDS = ["断点编号", "采集计划", "断点位置"]
# 断点水位字段：缺失或无法解析即属于「修复断点字段丢失」，要进核对清单
WATERMARK_FIELDS = ["断点位置", "已采集量", "剩余量"]
NUMERIC_FIELDS = ["已采集量", "剩余量", "恢复耗时"]

STATUS_ORDER = ["已记录", "恢复中", "已恢复", "已放弃"]
# 生命周期单向流转：每个动作只允许从所列源状态进入，归档只置 archived 标记，
# 绝不把状态改回更早的序列值。
ACTION_RULES: dict[str, dict[str, Any]] = {
    "启动恢复": {"target": "恢复中", "sources": ["已记录"]},
    "放弃恢复": {"target": "已放弃", "sources": ["已记录", "恢复中"]},
    "归档记录": {"target": None, "sources": ["已恢复", "已放弃"]},  # 只归档，不改状态
}
CONFLICT_POLICIES = ("completion_first", "recovery_first")
RECEIPT_KINDS = ("completion", "recovery", "failure")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value: Any) -> int | None:
    """把入参宽松转成非负整数；无法解析时返回 None，由调用方按字段丢失处理。"""
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


def _next_id(table: str) -> int:
    return store.next_id(table)


class ResumeService:
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
            rows = [row for row in rows if keyword in str(row.get("断点编号", ""))]
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
            entry: dict[str, Any] = {"id": _next_id(MODULE)}
            for field in ("断点编号", "采集计划", "断点位置", "恢复策略"):
                entry[field] = values.get(field)
            entry["已采集量"] = _to_int(values.get("已采集量")) or 0
            entry["剩余量"] = _to_int(values.get("剩余量")) or 0
            entry["恢复耗时"] = _to_int(values.get("恢复耗时")) or 0
            entry["status"] = STATUS_ORDER[0]
            entry["记录状态"] = entry["status"]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["archived"] = False
            # 已确认水位：登记时即固化，后续任何处理都只能前进不能回退
            entry["confirmed_watermark"] = entry["已采集量"]
            entry["receipt_version"] = 0
            entry["conflict_policy"] = self._normalize_policy(values.get("conflict_policy"))
            entry["active_batch"] = None
            store.rows(MODULE).append(entry)
            return dict(entry), []

    # ---------------------------------------------------------------- 动作（单向状态机）
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于断点续采恢复可执行范围"
        rule = ACTION_RULES[action]
        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，生命周期已终结，不能再变更状态"
            current = str(entry.get("status"))
            if current not in rule["sources"]:
                return None, (
                    f"状态「{current}」不允许执行「{action}」，"
                    f"生命周期只能按 {' → '.join(STATUS_ORDER)} 单向流转"
                )
            if action == "归档记录":
                # 归档只打标记，状态保持终态，杜绝归档把状态改回旧值
                entry["archived"] = True
                entry["pending"] = False
            else:
                target = rule["target"]
                if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
                    return None, f"目标状态「{target}」早于当前状态「{current}」，不允许回退"
                entry["status"] = target
                entry["记录状态"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
            return dict(entry), f"断点记录已{action}"

    # ---------------------------------------------------------------- 断点字段 + 恢复状态原子保存
    def save_checkpoint(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """断点字段与恢复状态一起保存，并重新校验恢复状态。

        返回 (entry, message, checklist_items)。任一步失败都不写入，
        已确认水位不会被回退。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档", []
        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，不能再保存断点字段", []

            # 1) 先在暂存区完成全部计算与校验，全部通过才一次性提交，保证原子性
            staged = dict(entry)
            for field in ("断点编号", "采集计划", "断点位置", "恢复策略", "记录状态"):
                if field in values and values[field] not in (None, ""):
                    staged[field] = values[field]
            for field in NUMERIC_FIELDS:
                if field in values and values[field] not in (None, ""):
                    parsed = _to_int(values[field])
                    if parsed is None:
                        return None, f"字段「{field}」必须是非负整数，本次保存已整体放弃", []
                    staged[field] = parsed

            # 2) 状态变更同样走单向状态机，不允许借保存接口把状态改回旧值
            target_status = values.get("status")
            if target_status:
                target_status = str(target_status).strip()
                if target_status not in STATUS_ORDER:
                    return None, f"目标状态「{target_status}」不在允许的状态序列里，本次保存已整体放弃", []
                current = str(entry.get("status"))
                if STATUS_ORDER.index(target_status) < STATUS_ORDER.index(current):
                    return None, (
                        f"目标状态「{target_status}」早于当前状态「{current}」，"
                        "状态不允许回退，本次保存已整体放弃"
                    ), []
                staged["status"] = target_status
                staged["记录状态"] = target_status

            if "conflict_policy" in values and values["conflict_policy"]:
                staged["conflict_policy"] = self._normalize_policy(values["conflict_policy"])

            # 3) 水位单调校验：已确认水位只能前进，任何失败都不能回退
            new_collected = int(staged.get("已采集量") or 0)
            confirmed = int(entry.get("confirmed_watermark") or 0)
            if new_collected < confirmed:
                return None, (
                    f"已采集量 {new_collected} 小于最后确认水位 {confirmed}，"
                    "已确认水位不允许回退，本次保存已整体放弃"
                ), []

            # 4) 重新校验恢复状态：字段丢失 / 数值缺失 / 状态残留旧值
            issues = self._detect_issues(staged)
            staged["abnormal"] = bool(issues)

            # 5) 断点字段与已确认水位同批推进：保存成功即确认新水位，
            #    前面已保证新值不小于旧水位，因此这里只会前进不会回退
            staged["confirmed_watermark"] = new_collected

            # 6) 全部通过，一次性提交（断点字段与恢复状态同批落地）
            entry.update(staged)
            entry["pending"] = entry["status"] != STATUS_ORDER[-1] and not entry.get("archived")
            checklist = self._sync_checklist(entry, issues, trigger="save_checkpoint")
            return dict(entry), "断点字段与恢复状态已一起保存并完成重新校验", checklist

    def revalidate(self, entry_id: int) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """重新校验断点字段与恢复状态，把字段丢失项写入归并下游核对清单。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档", []
        with store.lock:
            issues = self._detect_issues(entry)
            entry["abnormal"] = bool(issues)
            checklist = self._sync_checklist(entry, issues, trigger="revalidate")
            message = (
                "恢复状态校验通过，断点字段齐全"
                if not issues
                else f"发现 {len(issues)} 项断点字段问题，已写入修复断点字段丢失核对清单"
            )
            return dict(entry), message, checklist

    # ---------------------------------------------------------------- 恢复事件（恢复回执）
    def submit_recovery(
        self,
        entry_id: int,
        *,
        batch_id: str,
        recovered_count: Any,
        watermark: Any,
        remaining: Any | None = None,
        conflict_policy: str | None = None,
        elapsed: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """处理一个恢复批次的恢复事件，产生恢复回执与续采恢复结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档", None
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return None, "缺少批次号 batch_id，恢复事件无法核对", None
        recovered = _to_int(recovered_count)
        new_watermark = _to_int(watermark)
        if recovered is None or new_watermark is None:
            return None, "recovered_count 与 watermark 必须是非负整数，恢复事件已拒绝", None

        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，恢复事件不再受理", None
            if entry.get("status") == "已放弃":
                return None, "断点记录已放弃，恢复事件不再受理", None

            # 幂等：同一批次的恢复回执重放直接返回原回执，不重复写入聚合
            duplicate = self._find_receipt(entry_id, "recovery", batch_id)
            if duplicate is not None:
                return dict(entry), f"批次 {batch_id} 的恢复回执已存在，按幂等忽略重复事件", duplicate

            confirmed = int(entry.get("confirmed_watermark") or 0)
            if new_watermark < confirmed:
                # 旧批次带着落后水位到达：拒绝写入，已确认水位不回退，转核对清单
                self._append_checklist(
                    entry, "水位回退",
                    f"恢复批次 {batch_id} 水位 {new_watermark} 落后于确认水位 {confirmed}",
                    batch_id,
                )
                return None, (
                    f"恢复水位 {new_watermark} 落后于最后确认水位 {confirmed}，"
                    "已确认水位不允许回退，事件已拒绝并登记核对清单"
                ), None

            policy = self._normalize_policy(conflict_policy or entry.get("conflict_policy"))
            entry["conflict_policy"] = policy
            completion = self._find_kind(entry_id, "completion")
            current = str(entry.get("status"))

            # 同一断点检查点：完成回执与稍后到达的恢复事件冲突，保留两种处理
            if completion is not None and current == "已恢复":
                if policy == "completion_first":
                    # 完成终态优先：完成量已计入聚合；本批恢复数据不再重复追加，
                    # 恢复清单出现下一次待恢复项
                    receipt = self._append_receipt(
                        entry, "recovery", batch_id,
                        watermark=new_watermark, amount=recovered,
                        note="completion_first：完成终态优先，恢复量不重复计入聚合，转为下一次待恢复项",
                    )
                    result = self._append_result(
                        entry, receipt, recovered, new_watermark,
                        merge_delta=0,
                        defer_reason="completion_first:completed",
                    )
                    self._append_checklist(
                        entry, "迟到恢复事件",
                        f"批次 {batch_id} 在完成回执 #{completion['receipt_no']} 后到达，"
                        "完成终态优先，已挂为下一次待恢复项",
                        batch_id,
                    )
                    entry["active_batch"] = None
                    return dict(entry), "完成终态优先：聚合计数已含完成量，恢复清单已登记下一次待恢复项", result

                # recovery_first：恢复结果先更新水位，聚合计数在下一批追加完成量
                receipt = self._append_receipt(
                    entry, "recovery", batch_id,
                    watermark=new_watermark, amount=recovered,
                    note="recovery_first：恢复终态优先，先更新水位，完成量递延到下一批追加",
                )
                effective = max(0, new_watermark - confirmed)
                self._advance_watermark(entry, new_watermark, remaining, elapsed)
                result = self._append_result(
                    entry, receipt, recovered, new_watermark,
                    merge_delta=effective,
                    defer_reason="recovery_first:deferred_completion",
                )
                self._apply_result_to_merge(entry, result)
                self._resolve_deferred_completion(entry, new_watermark)
                self._append_checklist(
                    entry, "完成量递延",
                    f"批次 {batch_id} 恢复终态优先，完成回执 #{completion['receipt_no']} "
                    f"的完成量 {completion['amount']} 递延至下一批追加",
                    batch_id,
                )
                entry["active_batch"] = None
                return dict(entry), "恢复终态优先：恢复结果已更新水位，完成量将在下一批追加", result

            # 常规路径：并发恢复请求只有一个批次生效
            active = entry.get("active_batch")
            if active and active != batch_id:
                self._append_checklist(
                    entry, "并发批次冲突",
                    f"批次 {batch_id} 与生效批次 {active} 并发，仅 {active} 生效，"
                    "旧已采集数据不重复写入聚合",
                    batch_id,
                )
                return None, (
                    f"断点已有生效恢复批次 {active}，并发批次 {batch_id} 被拒绝，"
                    "旧已采集数据不会重复写入聚合"
                ), None

            receipt = self._append_receipt(
                entry, "recovery", batch_id,
                watermark=new_watermark, amount=recovered,
                note="恢复回执",
            )
            effective = max(0, new_watermark - confirmed)
            self._advance_watermark(entry, new_watermark, remaining, elapsed)
            entry["status"] = "已恢复"
            entry["记录状态"] = "已恢复"
            entry["pending"] = False
            entry["active_batch"] = None
            result = self._append_result(
                entry, receipt, recovered, new_watermark,
                merge_delta=effective,
            )
            self._apply_result_to_merge(entry, result)
            self._resolve_deferred_completion(entry, new_watermark)
            issues = self._detect_issues(entry)
            entry["abnormal"] = bool(issues)
            self._sync_checklist(entry, issues, trigger="submit_recovery")
            return dict(entry), f"恢复批次 {batch_id} 已确认，水位推进至 {new_watermark}", result

    # ---------------------------------------------------------------- 完成回执
    def submit_completion(
        self,
        entry_id: int,
        *,
        completed_count: Any,
        watermark: Any,
        batch_id: str | None = None,
        conflict_policy: str | None = None,
        elapsed: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """处理断点检查点的完成回执。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档", None
        completed = _to_int(completed_count)
        new_watermark = _to_int(watermark)
        if completed is None or new_watermark is None:
            return None, "completed_count 与 watermark 必须是非负整数，完成回执已拒绝", None
        batch_id = str(batch_id or f"completion-{_now()}").strip()

        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，完成回执不再受理", None
            if entry.get("status") == "已放弃":
                return None, "断点记录已放弃，完成回执不再受理", None

            duplicate = self._find_receipt(entry_id, "completion", batch_id)
            if duplicate is not None:
                return dict(entry), f"批次 {batch_id} 的完成回执已存在，按幂等忽略", duplicate

            confirmed = int(entry.get("confirmed_watermark") or 0)
            if new_watermark < confirmed:
                self._append_checklist(
                    entry, "水位回退",
                    f"完成批次 {batch_id} 水位 {new_watermark} 落后于确认水位 {confirmed}",
                    batch_id,
                )
                return None, (
                    f"完成水位 {new_watermark} 落后于最后确认水位 {confirmed}，"
                    "已确认水位不允许回退，回执已拒绝并登记核对清单"
                ), None

            policy = self._normalize_policy(conflict_policy or entry.get("conflict_policy"))
            entry["conflict_policy"] = policy
            active = entry.get("active_batch")
            receipt = self._append_receipt(
                entry, "completion", batch_id,
                watermark=new_watermark, amount=completed,
                note=f"完成回执（冲突策略：{policy}）",
            )

            if policy == "completion_first":
                # 完成终态优先：聚合计数立即增加完成量
                effective = max(0, new_watermark - confirmed)
                self._advance_watermark(entry, new_watermark, 0, elapsed)
                entry["status"] = "已恢复"
                entry["记录状态"] = "已恢复"
                entry["pending"] = False
                entry["active_batch"] = None
                result = self._append_result(
                    entry, receipt, completed, new_watermark,
                    merge_delta=effective,
                )
                self._apply_result_to_merge(entry, result)
                if active:
                    # 进行中的恢复批次成为「下一次待恢复项」
                    self._append_checklist(
                        entry, "迟到恢复事件",
                        f"完成回执先于恢复批次 {active} 终态确认，完成终态优先，"
                        f"恢复批次 {active} 挂为下一次待恢复项",
                        active,
                    )
                message = "完成终态优先：聚合计数已立即增加完成量"
            else:
                # 恢复终态优先：完成回执只记账，水位与聚合计数等恢复事件到达后处理
                entry["status"] = "恢复中"
                entry["记录状态"] = "恢复中"
                entry["pending"] = True
                result = self._append_result(
                    entry, receipt, completed, new_watermark,
                    merge_delta=0,
                    defer_reason="recovery_first:await_recovery",
                )
                self._append_checklist(
                    entry, "完成回执等待恢复",
                    f"恢复终态优先，完成批次 {batch_id} 已记账，等待恢复事件更新水位后再追加完成量",
                    batch_id,
                )
                message = "恢复终态优先：完成回执已保留版本顺序，等待恢复事件先更新水位"

            issues = self._detect_issues(entry)
            entry["abnormal"] = bool(issues)
            self._sync_checklist(entry, issues, trigger="submit_completion")
            return dict(entry), message, result

    # ---------------------------------------------------------------- 失败与重试
    def record_failure(
        self,
        entry_id: int,
        *,
        batch_id: str,
        reason: str,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """记录恢复批次失败：不动水位、不写恢复结果，重试从最后确认位置继续。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档", None
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return None, "缺少批次号 batch_id，失败记录无法核对", None
        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，失败记录不再受理", None
            receipt = self._append_receipt(
                entry, "failure", batch_id,
                watermark=int(entry.get("confirmed_watermark") or 0),
                amount=0,
                note=f"失败回执：{reason}",
            )
            failure = {
                "id": _next_id(FAILURES_TABLE),
                "断点id": entry_id,
                "断点编号": entry.get("断点编号"),
                "batch_id": batch_id,
                "reason": str(reason or ""),
                "resume_from_watermark": int(entry.get("confirmed_watermark") or 0),
                "receipt_no": receipt["receipt_no"],
                "created_at": _now(),
            }
            store.rows(FAILURES_TABLE).append(failure)
            # 失败释放生效批次，水位保持最后确认值，状态回到可重试的「恢复中」；
            # 已恢复/已放弃是终态，单向状态机不允许回退
            entry["active_batch"] = None
            if entry.get("status") not in ("已恢复", "已放弃"):
                entry["status"] = "恢复中"
                entry["记录状态"] = "恢复中"
                entry["pending"] = True
            entry["abnormal"] = True
            return dict(entry), (
                f"批次 {batch_id} 失败已记录，已确认水位保持 "
                f"{entry.get('confirmed_watermark')}，重试将从该位置继续"
            ), failure

    def start_batch(self, entry_id: int, batch_id: str) -> tuple[dict[str, Any] | None, str]:
        """登记生效恢复批次；并发请求只有一个批次能拿到生效权。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"断点记录 {entry_id} 不存在或已归档"
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return None, "缺少批次号 batch_id"
        with store.lock:
            if entry.get("archived"):
                return None, "断点记录已归档，不能再启动恢复批次"
            active = entry.get("active_batch")
            if active and active != batch_id:
                self._append_checklist(
                    entry, "并发批次冲突",
                    f"批次 {batch_id} 启动时发现生效批次 {active}，仅 {active} 生效，"
                    "并发恢复请求被拒绝，旧已采集数据不会重复写入聚合",
                    batch_id,
                )
                return None, (
                    f"断点已有生效恢复批次 {active}，并发批次 {batch_id} 被拒绝，"
                    "只有一个批次能生效"
                )
            current = str(entry.get("status"))
            if current not in ("已记录", "恢复中"):
                return None, f"状态「{current}」下不能启动恢复批次"
            entry["active_batch"] = batch_id
            entry["status"] = "恢复中"
            entry["记录状态"] = "恢复中"
            entry["pending"] = True
            return dict(entry), f"批次 {batch_id} 已成为唯一生效恢复批次，从水位 {entry['confirmed_watermark']} 续采"

    # ---------------------------------------------------------------- 下游账本查询
    def list_receipts(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RECEIPTS_TABLE)
        if entry_id is not None:
            rows = [row for row in rows if row.get("断点id") == entry_id]
        return sorted(rows, key=lambda row: (row.get("断点id", 0), row.get("receipt_no", 0)))

    def list_results(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RESULTS_TABLE)
        if entry_id is not None:
            rows = [row for row in rows if row.get("断点id") == entry_id]
        return sorted(rows, key=lambda row: (row.get("断点id", 0), row.get("id", 0)))

    def list_checklist(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CHECKLIST_TABLE)
        if entry_id is not None:
            rows = [row for row in rows if row.get("断点id") == entry_id]
        return sorted(rows, key=lambda row: (row.get("断点id", 0), row.get("id", 0)))

    # ---------------------------------------------------------------- 内部辅助
    @staticmethod
    def _normalize_policy(value: Any) -> str:
        text = str(value or "").strip()
        if text == "recovery_first":
            return "recovery_first"
        return "completion_first"

    def _detect_issues(self, entry: dict[str, Any]) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []
        missing_fields = set()
        for field in WATERMARK_FIELDS:
            value = entry.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append({"field": field, "issue": "修复断点字段丢失"})
                missing_fields.add(field)
        for field in ("已采集量", "剩余量"):
            # 字段已丢失的不再重复报数值错误，避免一个问题产生两条核对项
            if field in missing_fields or field not in entry:
                continue
            if _to_int(entry.get(field)) is None:
                issues.append({"field": field, "issue": "断点字段不是合法数值，恢复状态无法核对"})
        # 状态残留旧值：记录状态与生命周期状态不一致即视为残留
        if entry.get("记录状态") and entry.get("记录状态") != entry.get("status"):
            issues.append({"field": "记录状态", "issue": "恢复字段丢失后状态残留旧值，与生命周期状态不一致"})
        return issues

    def _sync_checklist(
        self, entry: dict[str, Any], issues: list[dict[str, str]], *, trigger: str
    ) -> list[dict[str, Any]]:
        """把字段丢失问题同步到归并下游的修复断点字段丢失核对清单（幂等）。"""
        synced: list[dict[str, Any]] = []
        existing = {
            (item.get("field"), item.get("issue"))
            for item in store.rows(CHECKLIST_TABLE)
            if item.get("断点id") == entry["id"] and item.get("status") == "open"
        }
        for issue in issues:
            key = (issue["field"], issue["issue"])
            if key in existing:
                continue
            item = {
                "id": _next_id(CHECKLIST_TABLE),
                "断点id": entry["id"],
                "断点编号": entry.get("断点编号"),
                "采集计划": entry.get("采集计划"),
                "field": issue["field"],
                "issue": issue["issue"],
                "status": "open",
                "trigger": trigger,
                "created_at": _now(),
                "resolved_at": None,
            }
            store.rows(CHECKLIST_TABLE).append(item)
            synced.append(item)
        if not issues:
            # 字段恢复齐全后只关闭「字段校验类」开放核对项；
            # 回执/并发冲突类审计项（trigger=receipt）有独立生命周期，不能被顺手清掉
            for item in store.rows(CHECKLIST_TABLE):
                if (
                    item.get("断点id") == entry["id"]
                    and item.get("status") == "open"
                    and item.get("trigger") != "receipt"
                ):
                    item["status"] = "resolved"
                    item["resolved_at"] = _now()
        return synced

    def _append_checklist(
        self, entry: dict[str, Any], issue: str, detail: str, batch_id: str | None
    ) -> dict[str, Any]:
        item = {
            "id": _next_id(CHECKLIST_TABLE),
            "断点id": entry["id"],
            "断点编号": entry.get("断点编号"),
            "采集计划": entry.get("采集计划"),
            "field": "恢复事件",
            "issue": issue,
            "detail": detail,
            "batch_id": batch_id,
            "status": "open",
            "trigger": "receipt",
            "created_at": _now(),
            "resolved_at": None,
        }
        store.rows(CHECKLIST_TABLE).append(item)
        return item

    def _append_receipt(
        self,
        entry: dict[str, Any],
        kind: str,
        batch_id: str,
        *,
        watermark: int,
        amount: int,
        note: str,
    ) -> dict[str, Any]:
        # 完成回执与恢复回执共用严格递增版本号，保留版本顺序
        version = int(entry.get("receipt_version") or 0) + 1
        entry["receipt_version"] = version
        receipt = {
            "id": _next_id(RECEIPTS_TABLE),
            "receipt_no": version,
            "断点id": entry["id"],
            "断点编号": entry.get("断点编号"),
            "kind": kind,
            "batch_id": batch_id,
            "watermark": watermark,
            "amount": amount,
            "note": note,
            "created_at": _now(),
        }
        store.rows(RECEIPTS_TABLE).append(receipt)
        return receipt

    def _advance_watermark(
        self,
        entry: dict[str, Any],
        new_watermark: int,
        remaining: Any | None,
        elapsed: Any | None,
    ) -> None:
        """推进已确认水位：只增不减，断点位置/已采集量与水位同批更新。"""
        confirmed = int(entry.get("confirmed_watermark") or 0)
        if new_watermark < confirmed:
            # 防御性检查：正常流程在调用前已拦截，这里保证水位绝不回退
            raise RuntimeError("已确认水位不允许回退")
        entry["confirmed_watermark"] = new_watermark
        entry["已采集量"] = new_watermark
        if remaining is not None:
            parsed_remaining = _to_int(remaining)
            if parsed_remaining is not None:
                entry["剩余量"] = parsed_remaining
        if elapsed is not None:
            parsed_elapsed = _to_int(elapsed)
            if parsed_elapsed is not None:
                entry["恢复耗时"] = parsed_elapsed

    def _append_result(
        self,
        entry: dict[str, Any],
        receipt: dict[str, Any],
        recovered_count: int,
        watermark: int,
        *,
        merge_delta: int,
        defer_reason: str | None = None,
    ) -> dict[str, Any]:
        """续采恢复结果写入归并下游，按同一恢复回执核对。

        结果一律先落 pending，由聚合任务真正读取并追加归并计数后才改成
        applied；不能在归并发生前预标 applied，否则同一回执会被误判重复。
        """
        result = {
            "id": _next_id(RESULTS_TABLE),
            "断点id": entry["id"],
            "断点编号": entry.get("断点编号"),
            "采集计划": entry.get("采集计划"),
            "receipt_no": receipt["receipt_no"],
            "receipt_kind": receipt["kind"],
            "batch_id": receipt["batch_id"],
            "recovered_count": recovered_count,
            "watermark": watermark,
            "confirmed_watermark": int(entry.get("confirmed_watermark") or 0),
            "merge_delta": merge_delta,
            "defer_reason": defer_reason,
            "merge_status": "pending",
            "applied_merge_id": None,
            "created_at": _now(),
        }
        store.rows(RESULTS_TABLE).append(result)
        return result

    def _apply_result_to_merge(self, entry: dict[str, Any], result: dict[str, Any]) -> None:
        """把恢复结果按采集计划推给聚合任务归并（延迟导入避免循环依赖）。"""
        if result["merge_delta"] <= 0:
            return
        from app.services.merge import MergeService

        MergeService().apply_resume_result(result["id"])

    def _resolve_deferred_completion(
        self, entry: dict[str, Any], new_watermark: int
    ) -> None:
        """恢复终态优先：恢复批次覆盖递延完成回执的水位后，完成量在同一批追加。

        递延完成结果本身不再单独计入聚合（其增量已包含在恢复批次的
        merge_delta 里），只把状态标记为 applied，保证同一回执核对时
        聚合计数与断点水位对得上且不重复。
        """
        for result in store.rows(RESULTS_TABLE):
            if (
                result.get("断点id") == entry["id"]
                and result.get("defer_reason") == "recovery_first:await_recovery"
                and result.get("merge_status") == "pending"
                and int(result.get("watermark", 0)) <= new_watermark
            ):
                result["merge_status"] = "applied"
                result["defer_reason"] = "recovery_first:appended_in_recovery_batch"

    def _find_receipt(self, entry_id: int, kind: str, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(RECEIPTS_TABLE):
            if (
                row.get("断点id") == entry_id
                and row.get("kind") == kind
                and row.get("batch_id") == batch_id
            ):
                return row
        return None

    def _find_kind(self, entry_id: int, kind: str) -> dict[str, Any] | None:
        rows = [
            row for row in store.rows(RECEIPTS_TABLE)
            if row.get("断点id") == entry_id and row.get("kind") == kind
        ]
        return sorted(rows, key=lambda row: row.get("receipt_no", 0))[-1] if rows else None
