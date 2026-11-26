"""归档架位逐片交接服务（服务层入口）。

状态机放在 ``app.pipeline.archive_handoff``；这里再适配通用资源路由的
``list_entries/create_entry`` 契约，使 ``/api/archive_handoff`` 也可访问。
"""
from __future__ import annotations

from typing import Any

from app.pipeline.archive_handoff import (
    ActorRef,
    ArchiveHandoffCenter,
    ArchiveHandoffManager,
    ArchiveHandoffRegistry,
    ArchiveHandoffService as PipelineArchiveHandoffService,
    ShelfHandoffService,
    ShardHandoffService,
    handoff_center,
    media_shard_key,
    verify_original_checksum,
)
from app.services.base import BaseModuleService


class ArchiveHandoffService(BaseModuleService):
    MODULE = "archive_handoff"
    ENTITY = "逐片交接回执"
    LIST_FIELDS = ["回执编号", "补采任务", "分片", "恢复人", "原架位", "处置", "结算次数"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["读取中", "已填缺", "只读留档", "部分可读"]
    ACTION_RULES = {"试领": "可领", "重送回执": "已处理", "交接读取": "读取中"}

    def __init__(self, center: ArchiveHandoffCenter | None = None) -> None:
        self.center = center or handoff_center

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.center, name)

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        items = self.center.list_receipts()
        if status:
            items = [item for item in items if item.get("disposition") == status or item.get("status") == status]
        if keyword:
            needle = keyword.casefold()
            items = [item for item in items if needle in str(item).casefold()]
        for key, value in (filters or {}).items():
            items = [item for item in items if value.casefold() in str(item.get(key, "")).casefold()]
        total = len(items)
        start = (page - 1) * size
        return items[start:start + size], total

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            if any(key in values for key in ("receipt_id", "回执编号", "content", "content_b64", "checksum_verified")):
                return self.center.submit_receipt(values), []
            if any(key in values for key in ("to_recoverer", "交给", "transfer")):
                return self.center.handoff_read(values), []
            if any(key in values for key in ("recoverer", "恢复人", "reader")):
                return self.center.grant_read(values), []
            if any(key in values for key in ("shelf_code", "架位标识", "archive_id", "original_content")):
                return self.center.register_original(values), []
            return self.center.register_task(values), []
        except (KeyError, PermissionError, ValueError) as exc:
            return None, [str(exc)]

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        payload = dict(context or {})
        payload.setdefault("receipt_id", str(entry_id))
        if "试领" in action or "claim" in action.lower():
            result = self.center.try_claim(payload)
        elif "回执" in action or "receipt" in action.lower():
            result = self.center.submit_receipt(payload)
        else:
            result = self.center.grant_read(payload)
        return result, str(result.get("message", action))


class ArchiveHandoffDomainService(ArchiveHandoffService):
    """保留早期命名；默认仍操作全局交接状态。"""


archive_handoff_service = handoff_center

__all__ = [
    "ActorRef",
    "ArchiveHandoffCenter",
    "ArchiveHandoffDomainService",
    "ArchiveHandoffManager",
    "ArchiveHandoffRegistry",
    "ArchiveHandoffService",
    "PipelineArchiveHandoffService",
    "ShelfHandoffService",
    "ShardHandoffService",
    "archive_handoff_service",
    "handoff_center",
    "media_shard_key",
    "verify_original_checksum",
]
