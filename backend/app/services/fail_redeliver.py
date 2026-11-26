"""失败重投回填领域服务：登记转码或转写失败分片的重投单，只处理仍缺失分片不覆盖已成功产出，产出重投单数据集供水位推进消费。"""
from __future__ import annotations

from typing import Any, Callable

from app.pipeline.archive_handoff import handoff_center
from app.services.base import BaseModuleService
from app.store import store


class FailRedeliverService(BaseModuleService):
    MODULE = "fail_redeliver"
    ENTITY = "重投单"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["重投编号", "关联分片", "失败阶段", "失败原因", "重投次数", "重投策略", "重投结论", "重投时间"]
    REQUIRED_FIELDS = ["重投编号", "关联分片", "失败阶段"]
    STATUS_ORDER = ["待重投", "重投中", "已补齐", "重投放弃"]
    ACTION_RULES = {"启动重投": "重投中", "确认补齐": "已补齐", "放弃重投": "重投放弃"}
    NEGATIVE_ACTIONS = []

    def register_redeliver(
        self, redeliver_id: str, shard_id: str, fail_stage: str,
        fail_reason: str, strategy: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记失败重投单。"""
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "重投编号": redeliver_id, "关联分片": shard_id,
            "失败阶段": fail_stage, "失败原因": fail_reason,
            "重投次数": "0", "重投策略": strategy,
            "重投结论": "-", "重投时间": "-",
            "status": "待重投", "pending": True, "abnormal": False,
        }
        rows.append(entry)
        return entry, f"重投单 {redeliver_id} 已登记"

    def execute_redeliver(
        self, redeliver_id: str, success: bool, conclusion: str,
    ) -> tuple[bool, str]:
        """执行重投并记录结论。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("重投编号") == redeliver_id:
                count = int(r.get("重投次数", "0")) + 1
                r["重投次数"] = str(count)
                r["重投时间"] = "2026-11-01"
                if success:
                    r["重投结论"] = conclusion
                    r["status"] = "已补齐"
                    r["pending"] = False
                    return True, f"重投 {redeliver_id} 已补齐"
                else:
                    r["重投结论"] = conclusion
                    r["status"] = "待重投" if count < 3 else "重投放弃"
                    r["pending"] = count < 3
                    return False, f"重投 {redeliver_id} 第 {count} 次失败"
        return False, f"重投单 {redeliver_id} 不存在"

    def get_by_shard(self, shard_id: str) -> list[dict[str, Any]]:
        """获取指定分片的所有重投单。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联分片") == shard_id]

    def get_pending(self) -> list[dict[str, Any]]:
        """获取所有待重投的单据。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("status") == "待重投"]

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        context = dict(context or {})
        row = self.get_entry(entry_id) or {}
        context.setdefault("redeliver_id", row.get("重投编号"))
        context.setdefault("shard_id", row.get("关联分片"))
        context.setdefault("关联分片", row.get("关联分片"))

        try:
            if "试领" in action or "领取" in action:
                result = self.claim_shard_for_transcription(context)
                return result, str(result.get("message", "试领完成"))
            if any(word in action for word in ("交接回执", "重送回执", "提交回执")) or (
                action == "确认补齐" and (context.get("receipt_id") or context.get("content_b64"))
            ):
                result = self.settle_handoff_receipt(context)
                return result, str(result.get("message", "交接回执已结算"))
        except (KeyError, PermissionError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}, str(exc)
        return super().run_action(entry_id, action, context)

    # ---------- 归档架位逐片交接后的试领/结算 ----------

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(handoff_center, name)

    def claim_shard_for_transcription(self, payload: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        """重投页试领分片；可领时返回完整原片与采用依据，否则返回待结算恢复人。"""
        result = handoff_center.try_claim(payload, **kwargs)
        shard = result.get("关联分片") or kwargs.get("shard_id")
        if shard:
            self._sync_redeliver_row(str(shard), result)
        return result

    def try_claim(self, payload: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        return self.claim_shard_for_transcription(payload, **kwargs)

    def settle_handoff_receipt(self, payload: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        """提交逐片交接回执，并把首次有效完整回执同步为重投单已补齐。"""
        result = handoff_center.submit_receipt(payload, **kwargs)
        shard = result.get("关联分片") or result.get("shard_key")
        if shard:
            self._sync_redeliver_row(str(shard), result)
        return result

    def _sync_redeliver_row(self, shard_id: str, result: dict[str, Any]) -> None:
        rows = store.rows(self.MODULE)
        row = next((r for r in rows if r.get("关联分片") == shard_id), None)
        if row is None:
            return
        if result.get("claimable") or result.get("settled"):
            row["status"] = "已补齐"
            row["pending"] = False
            row["重投结论"] = f"凭原架位回执 {result.get('receipt_id') or result.get('settlement_receipt_id')} 补齐"
        elif result.get("gap_retained"):
            row["status"] = "重投中"
            row["pending"] = True
            row["重投结论"] = result.get("message", "实际可读内容不足，缺口保留")


FailRedeliveryClaimService = handoff_center.__class__
RedeliveryClaimService = handoff_center.__class__