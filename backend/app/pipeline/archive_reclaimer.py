"""归档回收器：架位归档与保留窗口管理。

核心行为：
- 把完成交付的媒体分片与字幕包按架位归档
- 保留窗口到期前仍可恢复读取
- 到期清理后不可恢复
"""
from __future__ import annotations

from typing import Any, Callable
from datetime import datetime, timedelta

from app.pipeline.archive_handoff import (
    ArchiveHandoff,
    ArchiveHandoffCenter,
    ArchiveHandoffService,
    ShelfHandoff,
    ShelfHandoffService,
    ShardHandoff,
)


class ArchiveEntry:
    """归档条目：记录架位信息与保留窗口。"""

    def __init__(
        self, archive_id: str, media_id: str, shelf_label: str,
        content_category: str, archive_bytes: int, retention_days: int,
    ):
        self.archive_id = archive_id
        self.media_id = media_id
        self.shelf_label = shelf_label
        self.content_category = content_category
        self.archive_bytes = archive_bytes
        self.retention_days = retention_days
        self.archived_at = datetime.now()
        self.expires_at = self.archived_at + timedelta(days=retention_days)
        self.revocable = True
        self.cleaned = False

    def can_restore(self, as_of: datetime | None = None) -> bool:
        """检查在指定时刻是否仍可恢复。"""
        check = as_of or datetime.now()
        return not self.cleaned and check <= self.expires_at

    def days_until_expiry(self, as_of: datetime | None = None) -> int:
        """距离到期还有多少天。"""
        check = as_of or datetime.now()
        delta = (self.expires_at - check).days
        return max(0, delta)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "media_id": self.media_id,
            "shelf_label": self.shelf_label,
            "content_category": self.content_category,
            "archive_bytes": self.archive_bytes,
            "retention_days": self.retention_days,
            "archived_at": self.archived_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revocable": self.revocable,
            "cleaned": self.cleaned,
        }


class ArchiveReclaimer:
    """归档回收器。"""

    def __init__(self) -> None:
        self._archives: list[ArchiveEntry] = []
        # 逐片交接/回执结算是归档恢复链路上的独立状态机。
        self.handoff_center = ArchiveHandoffCenter()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.handoff_center, name)

    def archive_media(
        self, archive_id: str, media_id: str, shelf_label: str,
        content_category: str, archive_bytes: int, retention_days: int = 90,
    ) -> ArchiveEntry:
        """归档一个媒体源的内容。"""
        entry = ArchiveEntry(
            archive_id=archive_id, media_id=media_id,
            shelf_label=shelf_label, content_category=content_category,
            archive_bytes=archive_bytes, retention_days=retention_days,
        )
        self._archives.append(entry)
        return entry

    def restore(self, archive_id: str) -> tuple[bool, str]:
        """尝试恢复归档内容。"""
        for entry in self._archives:
            if entry.archive_id == archive_id:
                if entry.cleaned:
                    return False, f"归档 {archive_id} 已清理，不可恢复"
                if not entry.can_restore():
                    return False, f"归档 {archive_id} 保留窗口已过期，不可恢复"
                return True, f"归档 {archive_id} 可恢复，剩余 {entry.days_until_expiry()} 天"
        return False, f"归档 {archive_id} 不存在"

    def clean_expired(self) -> list[str]:
        """清理已过期的归档条目。"""
        now = datetime.now()
        cleaned = []
        for entry in self._archives:
            if entry.cleaned:
                continue
            if now > entry.expires_at:
                entry.cleaned = True
                entry.revocable = False
                cleaned.append(entry.archive_id)
        return cleaned

    def snapshot(self) -> dict[str, Any]:
        return {
            "archives": [a.to_dict() for a in self._archives],
            "total_archived": len(self._archives),
            "restorable": sum(1 for a in self._archives if a.can_restore()),
            "cleaned": sum(1 for a in self._archives if a.cleaned),
        }