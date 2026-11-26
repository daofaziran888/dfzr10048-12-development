"""失败重投引擎：只处理仍缺失分片，不覆盖已成功产出。

核心行为：
- 转码/转写失败分片登记重投
- 重投时只选择仍未产出的分片
- 已成功分片的产出不可覆盖
- 重试上限控制
"""
from __future__ import annotations

from typing import Any, Callable

from app.pipeline.archive_handoff import ArchiveHandoffCenter


class RedeliverEntry:
    """重投条目：记录失败分片的重投状态。"""

    def __init__(
        self, entry_id: str, shard_id: str, fail_stage: str,
        fail_reason: str, max_retries: int = 3,
    ):
        self.entry_id = entry_id
        self.shard_id = shard_id
        self.fail_stage = fail_stage
        self.fail_reason = fail_reason
        self.retry_count = 0
        self.max_retries = max_retries
        self.resolved = False
        self.strategy = "仅补缺"

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "shard_id": self.shard_id,
            "fail_stage": self.fail_stage,
            "fail_reason": self.fail_reason,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "resolved": self.resolved,
            "strategy": self.strategy,
        }


class RedeliverEngine:
    """重投引擎：管理重投队列和已成功保护。"""

    def __init__(self) -> None:
        self._entries: list[RedeliverEntry] = []
        self._success_set: set[str] = set()
        self._failed_set: set[str] = set()
        # 逐片交接回执/实际可读字节结算能力由专门状态机承载。
        self.handoff_center = ArchiveHandoffCenter()
        self.partial_readable_bytes: dict[str, int] = {}
        self.gap_bytes: dict[str, int] = {}
        self.settled_receipts: dict[str, str] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.handoff_center, name)

    def settle_readable_content(
        self,
        shard_id: str,
        readable_bytes: int,
        required_bytes: int | None = None,
        *,
        receipt_id: str | None = None,
        checksum_verified: bool = False,
        source_shelf_id: str | None = None,
    ) -> dict[str, Any]:
        """按实际可读内容结算缺口；不完整或校验未过时缺口保留。"""
        readable = max(0, int(readable_bytes))
        self.partial_readable_bytes[shard_id] = readable
        if required_bytes is not None:
            self.gap_bytes[shard_id] = int(required_bytes)
        required = self.gap_bytes.get(shard_id, int(required_bytes or 0))
        complete = required > 0 and readable >= required
        already = shard_id in self._success_set
        settled = bool(complete and checksum_verified and not already)
        if settled and receipt_id:
            if receipt_id in self.settled_receipts:
                settled = False
            else:
                self.settled_receipts[receipt_id] = shard_id
                self._success_set.add(shard_id)
        return {
            "ok": settled or already,
            "settled": settled,
            "already_settled": already and not settled,
            "shard_id": shard_id,
            "readable_bytes": readable,
            "required_bytes": required,
            "missing_bytes": max(0, required - readable),
            "gap_retained": not (complete and checksum_verified),
            "checksum_verified": checksum_verified,
            "source_shelf_id": source_shelf_id,
            "receipt_id": receipt_id,
        }

    def register_success(self, shard_id: str) -> None:
        """登记一个已成功产出的分片。"""
        self._success_set.add(shard_id)

    def register_failed(
        self, shard_id: str, fail_stage: str, fail_reason: str,
    ) -> RedeliverEntry:
        """登记失败分片并创建重投条目。"""
        self._failed_set.add(shard_id)
        entry = RedeliverEntry(
            entry_id=f"REDL-{shard_id}-{len(self._entries) + 1:04d}",
            shard_id=shard_id,
            fail_stage=fail_stage,
            fail_reason=fail_reason,
        )
        self._entries.append(entry)
        return entry

    def get_redeliver_candidates(self) -> list[RedeliverEntry]:
        """获取可重投的条目：只包含仍缺失且未超过重试上限的分片，排除已成功分片。"""
        candidates = []
        for entry in self._entries:
            if entry.resolved:
                continue
            if entry.shard_id in self._success_set:
                continue
            if not entry.can_retry():
                continue
            candidates.append(entry)
        return candidates

    def execute_retry(self, entry_id: str, outcome: str) -> tuple[bool, str]:
        """执行重投并记录结论。"""
        for entry in self._entries:
            if entry.entry_id == entry_id:
                if entry.resolved:
                    return False, f"重投条目 {entry_id} 已补齐"
                if entry.shard_id in self._success_set:
                    entry.resolved = True
                    return False, f"分片 {entry.shard_id} 已由其他路径成功，跳过重投"
                entry.retry_count += 1
                if outcome == "success":
                    entry.resolved = True
                    self._success_set.add(entry.shard_id)
                    return True, f"分片 {entry.shard_id} 重投成功"
                elif outcome == "fail":
                    if entry.retry_count >= entry.max_retries:
                        return False, (
                            f"分片 {entry.shard_id} 重试 {entry.retry_count}"
                            f"/{entry.max_retries} 次，已达上限"
                        )
                    return False, (
                        f"分片 {entry.shard_id} 重投失败，"
                        f"剩余 {entry.max_retries - entry.retry_count} 次"
                    )
        return False, f"重投条目 {entry_id} 不存在"

    def get_missing_shards(self) -> set[str]:
        """获取所有仍未成功的分片 ID。"""
        missing = set()
        for entry in self._entries:
            if entry.resolved:
                continue
            if entry.shard_id in self._success_set:
                continue
            missing.add(entry.shard_id)
        for shard_id in self._failed_set:
            if shard_id not in self._success_set:
                missing.add(shard_id)
        return missing

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self._entries],
            "success_count": len(self._success_set),
            "failed_count": len(self._failed_set),
            "missing_count": len(self.get_missing_shards()),
        }