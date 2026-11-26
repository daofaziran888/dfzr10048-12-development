"""归档架位归档领域服务：把完成交付的媒体分片与字幕包按架位归档，保留窗口内可恢复读取，产出归档条目数据集供空间管理与恢复消费。"""
from __future__ import annotations

from typing import Any, Callable

from app.pipeline.archive_handoff import (
    ArchiveHandoffCenter,
    ArchiveHandoffService,
    HandoffReceiptService,
    ShelfHandoffService,
    ShardHandoffService,
    handoff_center,
)
from app.services.base import BaseModuleService


class ArchiveShelfService(BaseModuleService):
    MODULE = "archive_shelf"
    ENTITY = "归档条目"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["归档编号", "关联媒体源", "架位标识", "归档内容类别", "归档字节", "保留窗口", "可恢复标记", "归档时间"]
    REQUIRED_FIELDS = ["归档编号", "关联媒体源", "架位标识"]
    STATUS_ORDER = ["待归档", "已归档", "可恢复", "已清理"]
    ACTION_RULES = {"执行归档": "已归档", "标记可恢复": "可恢复", "确认清理": "已清理"}
    NEGATIVE_ACTIONS = []

    # 逐片交接能力由独立状态机承载；保留 __getattr__ 让常见交接方法直接可从
    # ArchiveShelfService 命名空间调用，避免 Web 层和测试关心具体拆分文件。
    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(handoff_center, name)

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        context = dict(context or {})
        row = self.get_entry(entry_id) or {}
        context.setdefault("shelf_id", row.get("归档编号"))
        context.setdefault("archive_id", row.get("归档编号"))
        context.setdefault("shelf_code", row.get("架位标识"))
        context.setdefault("media_id", row.get("关联媒体源"))

        result: dict[str, Any] | None = None
        message = ""
        try:
            if any(word in action for word in ("重送回执", "提交回执", "交接回执")):
                result = handoff_center.submit_receipt(context)
                message = str(result.get("message", "逐片交接回执已处理"))
            elif any(word in action for word in ("直接恢复", "恢复到补采", "恢复到任务")):
                result = handoff_center.restore_original_to_task(context)
                message = str(result.get("message", "原片恢复入口已处理"))
            elif "试领" in action:
                result = handoff_center.try_claim(context)
                message = str(result.get("message", "试领完成"))
            elif any(word in action for word in ("交给", "移交", "转交接", "交接给")):
                result = handoff_center.handoff_read(context)
                message = "架位逐片读取权限已交接"
            elif any(word in action for word in ("交接读取", "逐片交接", "发放读取", "授权读取")):
                result = handoff_center.grant_read(context)
                message = "已新增按架位逐片交接读取权限"
        except (KeyError, PermissionError, ValueError) as exc:
            result = {"ok": False, "message": str(exc)}
            message = str(exc)

        if result is not None:
            self._sync_archive_row(entry_id, row, result)
            return result, message
        return super().run_action(entry_id, action, context)

    def _sync_archive_row(self, entry_id: int, row: dict[str, Any], result: dict[str, Any]) -> None:
        if not row:
            return
        recoverer = result.get("recoverer")
        pending = result.get("pending_recoverer")
        if isinstance(pending, dict):
            recoverer = recoverer or pending.get("recoverer") or pending.get("name")
        row["交接恢复人"] = recoverer or row.get("交接恢复人", "-")
        row["逐片回执编号"] = result.get("receipt_id") or row.get("逐片回执编号", "-")
        row["交接处置"] = result.get("disposition") or result.get("status") or row.get("交接处置", "-")
        row["交接结论"] = result.get("message") or row.get("交接结论", "-")
        if result.get("settled") or result.get("claimable") or result.get("permission"):
            row["status"] = "可恢复"
            row["pending"] = not bool(result.get("claimable") or result.get("settled"))
            row["可恢复标记"] = "是"
        elif result.get("ok") is False:
            row["abnormal"] = True

    def handoff_snapshot(self) -> dict[str, Any]:
        return handoff_center.snapshot()

    def handoff_shelf_view(self, shelf_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        return handoff_center.shelf_view(shelf_id=shelf_id, task_id=task_id)

    def submit_chunk_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return handoff_center.submit_receipt(*args, **kwargs)


ArchiveShelfHandoffService = ArchiveHandoffCenter
ArchiveShelfHandoffCenter = ArchiveHandoffCenter

__all__ = [
    "ArchiveShelfService",
    "ArchiveShelfHandoffService",
    "ArchiveShelfHandoffCenter",
    "ArchiveHandoffCenter",
    "ArchiveHandoffService",
    "ShelfHandoffService",
    "ShardHandoffService",
    "HandoffReceiptService",
]
