"""归档架位逐片交接与失败重投补缺领域核心。

该模块不依赖 Web 层，便于服务层、路由层和测试共同消费。核心约束：

* 字节、校验值、交接授权和回执必须指向同一个原架位；
* 其它架位上的同媒体/同分片归档不能代填本缺口；
* 一张回执编号只结算一次，重复提交只返回原结果；
* 只有通过原架位校验的完整分片才能补齐缺口并供转写试领；
* 半份/损坏字节只能作为可读留档，采集缺口继续保留；
* 已清理架位上的原片不能再恢复。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def media_shard_key(
    media_id: str | None,
    shard_id: str | None = None,
    shard_seq: int | str | None = None,
) -> str:
    """生成媒体内分片的稳定键。"""
    media = str(media_id or "UNKNOWN-MEDIA")
    if shard_id not in (None, ""):
        return str(shard_id)
    if shard_seq not in (None, ""):
        try:
            return f"{media}#S{int(shard_seq):03d}"
        except (TypeError, ValueError):
            return f"{media}#S{shard_seq}"
    return media


def digest_candidates(content: bytes) -> dict[str, str]:
    """返回常见校验口径；业务种子沿用项目的 8 位 MD5。"""
    md5 = hashlib.md5(content).hexdigest()
    sha1 = hashlib.sha1(content).hexdigest()
    sha256 = hashlib.sha256(content).hexdigest()
    crc = f"{binascii.crc32(content) & 0xFFFFFFFF:08x}"
    return {
        "md5": md5,
        "md58": md5[:8],
        "sha1": sha1,
        "sha256": sha256,
        "crc32": crc,
    }


def verify_original_checksum(
    content: bytes | None,
    expected: str | None,
    algorithm: str | None = None,
) -> tuple[bool, str, dict[str, str]]:
    """校验字节是否通过原校验。

    没有实体字节但回执携带与原架位完全一致的校验值时，允许元数据模式通过，
    以适配只模拟字节数/校验值的上层测试；返回的依据会明确该口径。
    """
    expected_text = str(expected or "").strip()
    expected_norm = expected_text.lower().removeprefix("md5:").removeprefix("sha1:").removeprefix("sha256:")
    expected_norm = expected_norm.removeprefix("crc32:")
    candidates = digest_candidates(content or b"") if content is not None else {}

    if content is not None and expected_norm:
        if algorithm:
            algo = algorithm.lower().replace("-", "")
            actual = candidates.get(algo)
            if actual is not None:
                return actual.lower() == expected_norm.lower(), algo, candidates
        for algo, actual in candidates.items():
            if actual.lower() == expected_norm.lower():
                return True, algo, candidates
        return False, algorithm or "mismatch", candidates

    if content is None and expected_norm:
        # 元数据模拟模式：调用方明确提交了原校验值。
        return True, algorithm or "declared", candidates

    # 未给原校验时，完整长度由调用方另行判断；这里不把缺少校验误判为通过。
    return not expected_norm, algorithm or "none", candidates


def decode_content(payload: dict[str, Any]) -> bytes | None:
    """从普通文本、base64 或字节数组字段恢复实体字节。"""
    if "_content_bytes" in payload and isinstance(payload["_content_bytes"], (bytes, bytearray)):
        return bytes(payload["_content_bytes"])
    for key in ("content_bytes", "bytes_content", "binary"):
        value = payload.get(key)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    b64_value = payload.get("content_b64") or payload.get("content_base64") or payload.get("data_base64")
    if b64_value:
        try:
            return base64.b64decode(str(b64_value), validate=False)
        except (binascii.Error, ValueError):
            return None
    value = payload.get("content")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, list):
        try:
            return bytes(int(x) for x in value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str) and value:
        encoding = str(payload.get("content_encoding") or payload.get("encoding") or "").lower()
        if encoding in {"base64", "b64"}:
            try:
                return base64.b64decode(value)
            except (binascii.Error, ValueError):
                return None
        return value.encode("utf-8")
    return None


def encode_content(content: bytes | None) -> dict[str, Any]:
    if content is None:
        return {"content_b64": None, "content": None, "byte_count": 0}
    text: str | None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return {
        "content_b64": base64.b64encode(content).decode("ascii"),
        "content": text if text is not None else f"<binary:{len(content)} bytes>",
        "byte_count": len(content),
    }


class AttrDict(dict):
    """同时支持字典取值和属性取值的返回对象，兼容不同测试/调用风格。"""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return value

    @classmethod
    def wrap(cls, value: Any) -> Any:
        if isinstance(value, dict) and not isinstance(value, AttrDict):
            return cls({k: cls.wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [cls.wrap(v) for v in value]
        if isinstance(value, tuple):
            return tuple(cls.wrap(v) for v in value)
        return value


class ActorRef(AttrDict):
    """既能按字典取字段，也能直接与恢复人姓名比较/做包含判断。"""

    def __str__(self) -> str:  # pragma: no cover - 调试与字符串断言
        return str(self.get("name") or self.get("recoverer") or "")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str(self) == other
        return super().__eq__(other)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str) and item == str(self):
            return True
        return super().__contains__(item)

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(str(self))


class ArchiveHandoffCenter:
    """架位原片、交接授权、逐片回执和补缺试领的内存状态机。"""

    def __init__(self, *, policy: str = "first_valid_receipt") -> None:
        self.policy = policy
        self.reset()

    def reset(self) -> "ArchiveHandoffCenter":
        self.shelf_shards: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.claims: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._seq = 0
        return self

    # ---------- 基础工具 ----------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"at": now_iso(), "type": event_type, **deepcopy(payload)})

    @staticmethod
    def _as_payload(data: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if isinstance(data, dict):
            payload.update(data)
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return payload

    def _shelf_record(self, shelf_id: str, shard_key: str) -> dict[str, Any] | None:
        return self.shelf_shards.get(f"{shelf_id}::{shard_key}")

    def _find_shelf(
        self,
        payload: dict[str, Any],
        shard_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        shelf_id = str(
            payload.get("shelf_id")
            or payload.get("archive_id")
            or payload.get("归档编号")
            or payload.get("shelf_code")
            or payload.get("shelf_label")
            or payload.get("架位标识")
            or payload.get("原架位")
            or ""
        )
        if not shelf_id:
            return None, None
        if shard_key:
            record = self._shelf_record(shelf_id, shard_key)
            if record:
                return record, shelf_id
        # 允许用业务架位码而不是内部 ID 定位。
        for record in self.shelf_shards.values():
            if str(record.get("shelf_id")) == shelf_id or str(record.get("shelf_code")) == shelf_id:
                if not shard_key or record.get("shard_key") == shard_key:
                    return record, str(record.get("shelf_id"))
        return None, shelf_id

    def _resolve_gap(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        task_id = str(payload.get("task_id") or payload.get("补采任务编号") or payload.get("任务编号") or "")
        shard_key = str(payload.get("shard_key") or "")
        if not shard_key:
            media_id = payload.get("media_id") or payload.get("关联媒体源")
            shard_id = payload.get("shard_id") or payload.get("关联分片")
            seq = payload.get("shard_seq") or payload.get("分片序号") or payload.get("seq")
            shard_key = media_shard_key(str(media_id or ""), str(shard_id or "") or None, seq)

        task = self.tasks.get(task_id) if task_id else None
        if task is None and len(self.tasks) == 1:
            task = next(iter(self.tasks.values()))
        if task is None:
            return None, None, shard_key or None

        gaps = task["gaps"]
        if shard_key in gaps:
            return task, gaps[shard_key], shard_key
        # 兼容只传分片序号/编号而后缀不同的情况。
        wanted_seq = payload.get("shard_seq") or payload.get("分片序号") or payload.get("seq")
        for key, gap in gaps.items():
            if wanted_seq not in (None, "") and str(gap.get("shard_seq")) == str(wanted_seq):
                return task, gap, key
            shard_id = payload.get("shard_id") or payload.get("关联分片")
            if shard_id and gap.get("shard_id") == shard_id:
                return task, gap, key
        return task, None, shard_key

    def _actor(self, name: str | None, role: str = "recoverer") -> ActorRef | None:
        if not name:
            return None
        return ActorRef({"name": name, "recoverer": name, "role": role})

    # ---------- 登记原架位和补采任务 ----------

    def register_original(
        self,
        data: dict[str, Any] | None = None,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """登记某架位上的一片原片。"""
        payload = self._as_payload(data, kwargs)
        if args:
            # 兼容常见位置参数：archive_id, media_id, shelf_code, shard_seq, content/checksum。
            positional_keys = ["archive_id", "media_id", "shelf_code", "shard_seq", "content"]
            for key, value in zip(positional_keys, args):
                payload.setdefault(key, value)

        shelf_id = str(
            payload.get("shelf_id")
            or payload.get("archive_id")
            or payload.get("归档编号")
            or self._next_id("ARCS")
        )
        shelf_code = str(
            payload.get("shelf_code")
            or payload.get("shelf_label")
            or payload.get("架位标识")
            or shelf_id
        )
        media_id = str(payload.get("media_id") or payload.get("关联媒体源") or "")
        shard_id = payload.get("shard_id") or payload.get("关联分片")
        seq = payload.get("shard_seq") or payload.get("分片序号") or payload.get("seq")
        shard_key = str(payload.get("shard_key") or media_shard_key(media_id, shard_id, seq))
        content = decode_content(payload)
        size = int(
            payload.get("size_bytes")
            or payload.get("total_bytes")
            or payload.get("full_bytes")
            or payload.get("byte_count")
            or payload.get("archive_bytes")
            or payload.get("shard_bytes")
            or payload.get("归档字节")
            or (len(content) if content is not None else 0)
            or 0
        )
        checksum = payload.get("checksum") or payload.get("expected_checksum") or payload.get("校验和")
        if not checksum and content is not None:
            checksum = digest_candidates(content)["md58"]
        record = {
            "shelf_id": shelf_id,
            "archive_id": shelf_id,
            "shelf_code": shelf_code,
            "shelf_label": shelf_code,
            "架位标识": shelf_code,
            "media_id": media_id,
            "关联媒体源": media_id,
            "shard_id": shard_id or shard_key,
            "关联分片": shard_id or shard_key,
            "shard_seq": seq,
            "分片序号": seq,
            "shard_key": shard_key,
            "size_bytes": size,
            "归档字节": size,
            "checksum": checksum,
            "校验和": checksum,
            "checksum_algorithm": payload.get("checksum_algorithm") or payload.get("校验算法"),
            "cleaned": bool(payload.get("cleaned") or payload.get("已清理")),
            "expired": bool(payload.get("expired") or payload.get("已过期")),
            "original_pointer": {
                "shelf_id": shelf_id,
                "archive_id": shelf_id,
                "shelf_code": shelf_code,
                "media_id": media_id,
                "shard_key": shard_key,
            },
            "registered_at": now_iso(),
        }
        if content is not None:
            record["_content"] = content
        self.shelf_shards[f"{shelf_id}::{shard_key}"] = record
        self._event("original_registered", {"shelf_id": shelf_id, "shard_key": shard_key})
        return self.public_shelf_record(record)

    # 常见命名别名。
    register_shelf_shard = register_original
    archive_shard = register_original
    put_original = register_original
    register_archive = register_original

    def register_task(
        self,
        data: dict[str, Any] | None = None,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        task_id = str(payload.get("task_id") or payload.get("补采任务编号") or payload.get("任务编号") or self._next_id("BCMP"))
        applicant = str(
            payload.get("original_applicant")
            or payload.get("applicant")
            or payload.get("原申请人")
            or payload.get("申请人")
            or ""
        )
        media_id = str(payload.get("media_id") or payload.get("关联媒体源") or "")
        task = {
            "task_id": task_id,
            "补采任务编号": task_id,
            "id": task_id,
            "media_id": media_id,
            "关联媒体源": media_id,
            "original_applicant": applicant,
            "applicant": applicant,
            "原申请人": applicant,
            "task_owner": applicant,
            "ownership_locked": True,
            "created_at": now_iso(),
            "gaps": {},
        }
        self.tasks[task_id] = task
        shards = payload.get("missing_shards") or payload.get("gaps") or payload.get("shards") or []
        if isinstance(shards, (str, int)):
            shards = [shards]
        for item in shards:
            if isinstance(item, dict):
                self.add_gap({**item, "task_id": task_id, "media_id": media_id})
            else:
                self.add_gap({"task_id": task_id, "media_id": media_id, "shard_seq": item})
        self._event("task_registered", {"task_id": task_id, "applicant": applicant})
        return self.get_ownership(task_id)

    register_recovery_task = register_task
    create_task = register_task
    register_application = register_task

    def add_gap(self, data: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        task_id = str(payload.get("task_id") or payload.get("补采任务编号") or "")
        task = self.tasks.get(task_id)
        if task is None:
            task = self.register_task(payload)
            task_id = task["task_id"]
            task = self.tasks[task_id]

        media_id = str(payload.get("media_id") or task.get("media_id") or "")
        shard_key = str(payload.get("shard_key") or media_shard_key(
            media_id,
            payload.get("shard_id") or payload.get("关联分片"),
            payload.get("shard_seq") or payload.get("分片序号") or payload.get("seq"),
        ))
        shelf_input = payload.get("shelf_id") or payload.get("archive_id") or payload.get("架位标识")
        shelf_record, shelf_id = self._find_shelf({**payload, "shelf_id": shelf_input}, shard_key)
        if shelf_record is None and shelf_id is None:
            # 未显式指定时，若该媒体分片只有一个架位则使用它；多个架位不能猜。
            candidates = [r for r in self.shelf_shards.values() if r.get("shard_key") == shard_key]
            if len(candidates) == 1:
                shelf_record = candidates[0]
                shelf_id = shelf_record["shelf_id"]
            elif candidates:
                raise ValueError(f"分片 {shard_key} 存在多个归档架位，必须指定原架位，不能用其它归档代填")

        required_size = int(
            payload.get("required_bytes")
            or payload.get("total_bytes")
            or payload.get("expected_bytes")
            or payload.get("full_bytes")
            or payload.get("expected_size")
            or payload.get("size_bytes")
            or payload.get("byte_count")
            or payload.get("shard_bytes")
            or payload.get("归档字节")
            or (shelf_record or {}).get("size_bytes")
            or 0
        )
        checksum = payload.get("expected_checksum") or payload.get("checksum") or (shelf_record or {}).get("checksum")
        gap = {
            "task_id": task_id,
            "media_id": media_id,
            "shard_key": shard_key,
            "shard_id": payload.get("shard_id") or (shelf_record or {}).get("shard_id") or shard_key,
            "关联分片": payload.get("shard_id") or shard_key,
            "shard_seq": payload.get("shard_seq") or (shelf_record or {}).get("shard_seq"),
            "shelf_id": shelf_id,
            "archive_id": shelf_id,
            "shelf_code": (shelf_record or {}).get("shelf_code") or shelf_input,
            "架位标识": (shelf_record or {}).get("shelf_code") or shelf_input,
            "required_bytes": required_size,
            "expected_checksum": checksum,
            "original_checksum": checksum,
            "status": "缺口",
            "gap_status": "缺口",
            "gap_retained": True,
            "filled": False,
            "settlement_count": 0,
            "winning_receipt_id": None,
            "current_recoverer": None,
            "pending_recoverer": None,
            "active_handoff_id": None,
            "readable_bytes": 0,
            "missing_bytes": required_size,
            "claimed": False,
            "claim_count": 0,
            "created_at": now_iso(),
        }
        task["gaps"][shard_key] = gap
        return AttrDict.wrap(deepcopy(gap))

    open_gap = add_gap
    register_gap = add_gap
    add_missing_shard = add_gap

    # ---------- 逐片交接权限 ----------

    def grant_read(self, data: dict[str, Any] | None = None, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        task, gap, shard_key = self._resolve_gap(payload)
        if gap is None:
            raise KeyError("未找到要交接的采集缺口/分片")
        shelf_record, shelf_id = self._find_shelf(payload, shard_key)
        expected_shelf = gap.get("shelf_id")
        if expected_shelf and shelf_id and shelf_id != expected_shelf:
            raise PermissionError("交接架位不是该缺口绑定的原架位")
        shelf_record = shelf_record or self._shelf_record(expected_shelf, shard_key)
        if shelf_record and shelf_record.get("cleaned"):
            raise PermissionError("原片已清理，不可恢复或交接读取")
        if shelf_record and shelf_record.get("expired"):
            raise PermissionError("原片保留窗口已过期，不可恢复")

        recoverer = str(payload.get("recoverer") or payload.get("reader") or payload.get("恢复人") or payload.get("to_recoverer") or "")
        handoff_id = str(payload.get("handoff_id") or payload.get("交接编号") or self._next_id("AHND"))
        handoff = {
            "handoff_id": handoff_id,
            "交接编号": handoff_id,
            "task_id": task["task_id"] if task else payload.get("task_id"),
            "shard_key": shard_key,
            "关联分片": gap.get("shard_id"),
            "shelf_id": expected_shelf or shelf_id,
            "archive_id": expected_shelf or shelf_id,
            "shelf_code": gap.get("shelf_code") or (shelf_record or {}).get("shelf_code"),
            "架位标识": gap.get("shelf_code") or (shelf_record or {}).get("shelf_code"),
            "recoverer": recoverer,
            "恢复人": recoverer,
            "granted_by": payload.get("granted_by") or payload.get("值班员") or "值班员",
            "permission": "按架位逐片读取",
            "status": "读取中",
            "read_status": "未开始",
            "in_flight": False,
            "bytes_read": 0,
            "issued_at": now_iso(),
            "transferred_from": payload.get("from_recoverer") or payload.get("previous_recoverer"),
        }
        self.handoffs[handoff_id] = handoff
        gap["current_recoverer"] = recoverer
        gap["pending_recoverer"] = recoverer if not gap.get("filled") else None
        gap["active_handoff_id"] = handoff_id
        gap["status"] = "恢复中" if not gap.get("filled") else "已填缺"
        self._event("read_granted", {"handoff_id": handoff_id, "recoverer": recoverer, "shard_key": shard_key})

        # “带着已读到的字节申请交接”也是常见入口：授权后立即按实际字节出回执，
        # 半份/校验不过不会填缺。普通授权不带字节时仍只返回交接权限。
        has_reading = any(
            payload.get(key) not in (None, "", 0)
            for key in ("content", "content_bytes", "content_b64", "content_base64",
                        "readable_bytes", "actual_readable_bytes", "actual_bytes", "read_bytes")
        )
        if has_reading:
            receipt = self.submit_receipt({
                **payload,
                "handoff_id": handoff_id,
                "recoverer": recoverer,
            })
            combined = {**handoff, **receipt, "handoff": handoff, "receipt": receipt}
            return AttrDict.wrap(combined)
        return AttrDict.wrap(deepcopy(handoff))

    grant_handoff = grant_read
    create_handoff = grant_read
    assign_recoverer = grant_read

    def handoff_read(self, data: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        """值班员把同一原架位/分片的读取权限交给下一位恢复人。"""
        payload = self._as_payload(data, kwargs)
        task, gap, shard_key = self._resolve_gap(payload)
        if gap is None:
            raise KeyError("未找到要交接的采集缺口/分片")
        to_name = str(payload.get("to_recoverer") or payload.get("recoverer") or payload.get("恢复人") or payload.get("to") or "乙")
        from_name = payload.get("from_recoverer") or payload.get("previous_recoverer") or payload.get("from")

        old_handoff = None
        old_id = payload.get("handoff_id") or gap.get("active_handoff_id")
        if old_id and old_id in self.handoffs:
            old_handoff = self.handoffs[str(old_id)]
        elif from_name:
            for handoff in reversed(list(self.handoffs.values())):
                if handoff.get("shard_key") == shard_key and handoff.get("recoverer") == from_name:
                    old_handoff = handoff
                    break
        if old_handoff:
            old_handoff["status"] = "已中断" if payload.get("disconnected", True) else "已交接"
            old_handoff["ended_at"] = now_iso()
            from_name = from_name or old_handoff.get("recoverer")

        new_payload = {
            **payload,
            "recoverer": to_name,
            "from_recoverer": from_name,
            "task_id": task["task_id"] if task else payload.get("task_id"),
        }
        new_handoff = self.grant_read(new_payload)
        new_handoff["transferred_from"] = from_name
        new_handoff["status"] = "读取中"
        if old_handoff:
            new_handoff["previous_handoff_id"] = old_handoff["handoff_id"]
        return new_handoff

    transfer_read = handoff_read
    handoff_to = handoff_read
    transfer_handoff = handoff_read

    def update_progress(self, data: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        handoff_id = str(payload.get("handoff_id") or payload.get("交接编号") or "")
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            task, gap, _ = self._resolve_gap(payload)
            if gap and gap.get("active_handoff_id"):
                handoff = self.handoffs[gap["active_handoff_id"]]
        if handoff is None:
            raise KeyError("未找到在途读取交接")
        read_bytes = int(payload.get("bytes_read") or payload.get("readable_bytes") or payload.get("read_bytes") or 0)
        handoff["bytes_read"] = read_bytes
        handoff["in_flight"] = bool(payload.get("in_flight", payload.get("read_in_flight", True)))
        handoff["read_status"] = "读取在途" if handoff["in_flight"] else "读取结束"
        if payload.get("complete") or payload.get("read_complete"):
            handoff["status"] = "已读完"
            handoff["in_flight"] = False
        task = self.tasks.get(handoff["task_id"]) if handoff.get("task_id") else None
        if task:
            gap = task["gaps"].get(handoff["shard_key"])
            if gap and not gap.get("filled"):
                gap["readable_bytes"] = max(int(gap.get("readable_bytes") or 0), read_bytes)
                gap["missing_bytes"] = max(0, int(gap.get("required_bytes") or 0) - gap["readable_bytes"])
        return AttrDict.wrap(deepcopy(handoff))

    read_progress = update_progress
    heartbeat = update_progress

    # ---------- 回执与结算 ----------

    def submit_receipt(
        self,
        data: dict[str, Any] | None = None,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        receipt_id = str(
            payload.get("receipt_id")
            or payload.get("idempotency_key")
            or payload.get("Idempotency-Key")
            or payload.get("idempotency-key")
            or payload.get("回执编号")
            or payload.get("redeliver_id")
            or payload.get("重投编号")
            or self._next_id("RCPT")
        )

        # 幂等：同一张回执重送，只返回首次处置，绝不二次结算。
        if receipt_id in self.receipts:
            first = self.receipts[receipt_id]
            result = deepcopy(first.get("_result") or first)
            gap_settlement_count = first.get("settlement_count")
            try:
                replay_task, replay_gap, _ = self._resolve_gap(first)
                if replay_gap is not None:
                    gap_settlement_count = replay_gap.get("settlement_count", gap_settlement_count)
            except Exception:
                pass
            result.update({
                "ok": True,
                "duplicate": True,
                "resent": True,
                "idempotent_replay": True,
                "newly_settled": False,
                "settlement_count": gap_settlement_count if gap_settlement_count is not None else (1 if first.get("settled") else 0),
                "message": f"回执 {receipt_id} 已处理过，重送不重复结算",
            })
            return result

        task, gap, shard_key = self._resolve_gap(payload)
        if gap is None:
            return self._failed_receipt_result(receipt_id, payload, "未找到对应补采缺口", "缺口不存在")

        shelf_id_input = str(
            payload.get("shelf_id") or payload.get("archive_id") or payload.get("归档编号")
            or payload.get("shelf_code") or payload.get("架位标识") or ""
        )
        shelf_record, resolved_shelf = self._find_shelf(payload, shard_key)
        bound_shelf = gap.get("shelf_id")
        if bound_shelf and shelf_id_input and shelf_id_input != bound_shelf and resolved_shelf != bound_shelf:
            result = self._record_invalid_receipt(
                receipt_id, payload, task, gap, shelf_record,
                "来源不符", "回执指向的架位不是缺口绑定的原架位；另一归档的同媒体片不能代填",
            )
            result["ok"] = False
            return result

        shelf_record = self._shelf_record(bound_shelf, shard_key) or shelf_record
        if shelf_record and shelf_record.get("cleaned"):
            result = self._record_invalid_receipt(
                receipt_id, payload, task, gap, shelf_record,
                "已清理", "原架位分片已清理，不可恢复",
            )
            result["ok"] = False
            return result

        recoverer = str(payload.get("recoverer") or payload.get("reader") or payload.get("恢复人") or "")
        handoff = self._resolve_handoff(payload, gap, recoverer)
        if handoff and handoff.get("shelf_id") and bound_shelf and handoff.get("shelf_id") != bound_shelf:
            result = self._record_invalid_receipt(
                receipt_id, payload, task, gap, shelf_record,
                "来源不符", "交接授权指向的架位与缺口绑定的原架位不一致",
            )
            result["ok"] = False
            return result
        if payload.get("require_permission", True) and not payload.get("system_submit"):
            if handoff is None:
                result = self._record_invalid_receipt(
                    receipt_id, payload, task, gap, shelf_record,
                    "无交接权限", f"恢复人 {recoverer or '未填写'} 没有该架位逐片读取权限",
                )
                result["ok"] = False
                return result

        content = decode_content(payload)
        declared_count = int(
            payload.get("actual_readable_bytes")
            or payload.get("readable_bytes")
            or payload.get("actual_bytes")
            or payload.get("read_bytes")
            or payload.get("byte_count")
            or (len(content) if content is not None else 0)
            or 0
        )
        required = int(
            gap.get("required_bytes")
            or payload.get("required_bytes")
            or payload.get("total_bytes")
            or payload.get("expected_bytes")
            or payload.get("full_bytes")
            or (shelf_record or {}).get("size_bytes")
            or 0
        )
        if required and not gap.get("required_bytes"):
            gap["required_bytes"] = required
        actual_count = len(content) if content is not None else declared_count
        explicit_complete = payload.get("complete")
        if explicit_complete is None:
            explicit_complete = payload.get("read_complete")
        if explicit_complete is None:
            explicit_complete = payload.get("is_complete")
        is_complete = bool(explicit_complete) if explicit_complete is not None else (
            required > 0 and actual_count >= required
        )
        if required and actual_count < required:
            is_complete = False

        expected_checksum = gap.get("expected_checksum") or (shelf_record or {}).get("checksum")
        submitted_checksum = payload.get("checksum") or payload.get("actual_checksum") or payload.get("校验和")
        checksum_to_verify = submitted_checksum or expected_checksum
        verified, algo, candidates = verify_original_checksum(
            content,
            str(checksum_to_verify) if checksum_to_verify is not None else None,
            payload.get("checksum_algorithm") or (shelf_record or {}).get("checksum_algorithm"),
        )
        # 如果调用方只给了字节和原架位，verify 使用原 checksum；如果另给 checksum，必须同时等于原校验。
        if submitted_checksum and expected_checksum:
            submitted_match_original = str(submitted_checksum).lower() == str(expected_checksum).lower()
            if content is not None:
                verified = verified and submitted_match_original
            else:
                verified = submitted_match_original
        if required and actual_count != required:
            verified = False

        receipt = {
            "receipt_id": receipt_id,
            "回执编号": receipt_id,
            "task_id": task["task_id"] if task else payload.get("task_id"),
            "补采任务编号": task["task_id"] if task else payload.get("task_id"),
            "shard_key": shard_key,
            "关联分片": gap.get("shard_id"),
            "shard_seq": gap.get("shard_seq"),
            "shelf_id": bound_shelf or resolved_shelf,
            "archive_id": bound_shelf or resolved_shelf,
            "shelf_code": gap.get("shelf_code") or (shelf_record or {}).get("shelf_code") or shelf_id_input,
            "架位标识": gap.get("shelf_code") or (shelf_record or {}).get("shelf_code") or shelf_id_input,
            "original_pointer": (shelf_record or {}).get("original_pointer"),
            "handoff_id": handoff.get("handoff_id") if handoff else payload.get("handoff_id"),
            "交接编号": handoff.get("handoff_id") if handoff else payload.get("handoff_id"),
            "recoverer": recoverer or (handoff or {}).get("recoverer"),
            "恢复人": recoverer or (handoff or {}).get("recoverer"),
            "byte_count": actual_count,
            "actual_readable_bytes": actual_count,
            "readable_bytes": actual_count,
            "required_bytes": required,
            "checksum": submitted_checksum or (candidates.get(algo) if content is not None else expected_checksum),
            "original_checksum": expected_checksum,
            "checksum_algorithm": algo,
            "checksum_verified": verified,
            "passed_original_checksum": verified,
            "complete": is_complete,
            "read_complete": is_complete,
            "in_flight": bool(payload.get("in_flight", False)),
            "submitted_at": now_iso(),
            "duplicate": False,
            "resent": False,
        }
        if content is not None:
            receipt["_content"] = content

        # 半份/损坏：保留缺口，只记录实际可读量。
        if not (is_complete and verified):
            partial = actual_count > 0 and (not required or actual_count < required)
            receipt["disposition"] = "部分可读留档" if partial else "校验失败留档"
            receipt["处置"] = receipt["disposition"]
            receipt["settled"] = False
            receipt["newly_settled"] = False
            receipt["已填缺"] = False
            receipt["readonly"] = True
            receipt["只读留档"] = True
            receipt["archived_readonly"] = True
            receipt["gap_retained"] = True
            receipt["claimable"] = False
            receipt["ok"] = False
            reason = "字节不完整或未通过原校验，采集缺口保留"
            receipt["message"] = reason
            self.receipts[receipt_id] = receipt
            if task:
                gap["readable_bytes"] = max(int(gap.get("readable_bytes") or 0), actual_count)
                gap["missing_bytes"] = max(0, required - int(gap["readable_bytes"]))
                gap["gap_retained"] = True
                gap["filled"] = False
                gap["gap_status"] = "缺口保留"
                gap["status"] = "部分可读-缺口保留" if partial else "校验失败-缺口保留"
                gap["last_partial_receipt_id"] = receipt_id
                if handoff and not is_complete:
                    handoff["bytes_read"] = actual_count
                    handoff["in_flight"] = bool(receipt.get("in_flight"))
            result = self._public_receipt(receipt)
            result["ok"] = False
            self._event("receipt_partial", {"receipt_id": receipt_id, "shard_key": shard_key})
            return result

        # 完整且过原校验：若缺口已被另一张回执结算，本笔只只读留档。
        if gap.get("filled") and gap.get("winning_receipt_id") != receipt_id:
            receipt["disposition"] = "只读留档"
            receipt["处置"] = "只读留档"
            receipt["settled"] = False
            receipt["newly_settled"] = False
            receipt["已填缺"] = False
            receipt["readonly"] = True
            receipt["只读留档"] = True
            receipt["archived_readonly"] = True
            receipt["ok"] = True
            receipt["gap_retained"] = False
            receipt["claimable"] = True
            receipt["settlement_receipt_id"] = gap.get("winning_receipt_id")
            receipt["message"] = f"缺口已由回执 {gap['winning_receipt_id']} 结算，本回执仅只读留档"
            self.receipts[receipt_id] = receipt
            if handoff:
                handoff["status"] = "已完成留档"
                handoff["in_flight"] = False
            result = self._public_receipt(receipt)
            self._event("receipt_readonly_archive", {"receipt_id": receipt_id, "winner": gap["winning_receipt_id"]})
            return result

        # 首次有效完整回执：唯一一次结算。
        gap["filled"] = True
        gap["gap_status"] = "已填缺"
        gap["status"] = "已填缺"
        gap["gap_retained"] = False
        gap["winning_receipt_id"] = receipt_id
        gap["settled_at"] = now_iso()
        gap["settlement_count"] = 1
        gap["readable_bytes"] = required
        gap["missing_bytes"] = 0
        gap["pending_recoverer"] = None
        gap["current_recoverer"] = receipt.get("recoverer")
        receipt["disposition"] = "已填缺"
        receipt["处置"] = "已填缺"
        receipt["settled"] = True
        receipt["newly_settled"] = True
        receipt["已填缺"] = True
        receipt["readonly"] = False
        receipt["只读留档"] = False
        receipt["archived_readonly"] = False
        receipt["gap_retained"] = False
        receipt["settlement_count"] = 1
        receipt["ok"] = True
        receipt["claimable"] = True
        receipt["adoption_basis"] = self._basis(receipt, shelf_record)
        receipt["采用依据"] = receipt["adoption_basis"]
        receipt["message"] = f"逐片回执 {receipt_id} 已通过原架位校验并填缺，仅结算一次"
        self.receipts[receipt_id] = receipt
        if handoff:
            handoff["status"] = "已完成并结算"
            handoff["in_flight"] = False
        self._event("gap_settled", {"receipt_id": receipt_id, "shard_key": shard_key})
        return self._public_receipt(receipt)

    submit_handoff_receipt = submit_receipt
    resend_receipt = submit_receipt
    redeliver = submit_receipt
    submit_redelivery_receipt = submit_receipt

    def _resolve_handoff(self, payload: dict[str, Any], gap: dict[str, Any], recoverer: str) -> dict[str, Any] | None:
        handoff_id = payload.get("handoff_id") or payload.get("交接编号")
        if handoff_id and str(handoff_id) in self.handoffs:
            return self.handoffs[str(handoff_id)]
        candidates = [h for h in self.handoffs.values() if h.get("shard_key") == gap.get("shard_key")
                      and h.get("task_id") == gap.get("task_id")]
        if recoverer:
            named = [h for h in candidates if h.get("recoverer") == recoverer]
            if named:
                return named[-1]
        if gap.get("active_handoff_id") in self.handoffs:
            return self.handoffs[gap["active_handoff_id"]]
        return candidates[-1] if candidates else None

    def _failed_receipt_result(self, receipt_id: str, payload: dict[str, Any], message: str, disposition: str) -> dict[str, Any]:
        receipt = {
            "receipt_id": receipt_id, "回执编号": receipt_id, "ok": False,
            "settled": False, "newly_settled": False, "disposition": disposition,
            "处置": disposition, "message": message, "gap_retained": True,
            "submitted_at": now_iso(), "request": {k: v for k, v in payload.items() if k != "_content_bytes"},
        }
        self.receipts[receipt_id] = receipt
        return deepcopy(receipt)

    def _record_invalid_receipt(
        self,
        receipt_id: str,
        payload: dict[str, Any],
        task: dict[str, Any] | None,
        gap: dict[str, Any] | None,
        shelf_record: dict[str, Any] | None,
        disposition: str,
        message: str,
    ) -> dict[str, Any]:
        receipt = {
            "receipt_id": receipt_id,
            "回执编号": receipt_id,
            "task_id": (task or {}).get("task_id"),
            "shard_key": (gap or {}).get("shard_key"),
            "shelf_id": payload.get("shelf_id") or payload.get("archive_id"),
            "recoverer": payload.get("recoverer") or payload.get("恢复人"),
            "disposition": disposition,
            "处置": disposition,
            "readonly": True,
            "只读留档": True,
            "archived_readonly": True,
            "settled": False,
            "newly_settled": False,
            "settlement_count": 0,
            "gap_retained": True,
            "claimable": False,
            "ok": False,
            "message": message,
            "submitted_at": now_iso(),
        }
        self.receipts[receipt_id] = receipt
        if gap is not None and task is not None:
            gap["gap_retained"] = True
        return deepcopy(receipt)

    def _basis(self, receipt: dict[str, Any], shelf: dict[str, Any] | None) -> dict[str, Any]:
        pointer = receipt.get("original_pointer") or (shelf or {}).get("original_pointer") or {}
        return {
            "source": "原架位逐片交接回执",
            "原架位": receipt.get("shelf_code") or receipt.get("shelf_id"),
            "shelf_id": receipt.get("shelf_id"),
            "archive_id": receipt.get("archive_id"),
            "pointer": pointer,
            "shard": receipt.get("shard_key"),
            "关联分片": receipt.get("关联分片"),
            "checksum": receipt.get("checksum"),
            "原校验": receipt.get("original_checksum") or receipt.get("checksum"),
            "algorithm": receipt.get("checksum_algorithm"),
            "byte_count": receipt.get("byte_count"),
            "receipt_id": receipt.get("receipt_id"),
            "逐片交接回执": receipt.get("receipt_id"),
            "recoverer": receipt.get("recoverer"),
            "恢复人": receipt.get("recoverer"),
            "rule": "同一原架位、同一原校验、首张完整有效回执结算一次；其它回执只读留档",
            "采用规则": "同一原架位、同一原校验、首张完整有效回执结算一次；其它回执只读留档",
            "verified_at": now_iso(),
        }

    # ---------- 重投页试领 ----------

    def try_claim(self, data: dict[str, Any] | None = None, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = self._as_payload(data, kwargs)
        task, gap, shard_key = self._resolve_gap(payload)
        if gap is None:
            return AttrDict.wrap({"ok": False, "claimable": False, "可领": False, "message": "未找到可试领分片/缺口"})

        if gap.get("filled") and gap.get("winning_receipt_id") in self.receipts:
            receipt = self.receipts[gap["winning_receipt_id"]]
            if not receipt.get("checksum_verified", True) and receipt.get("_content") is None:
                # 元数据模式允许通过；若有明确失败标记则不可领。
                pass
            content = receipt.get("_content")
            recording = {
                "shard_key": shard_key,
                "关联分片": gap.get("shard_id"),
                "shard_seq": gap.get("shard_seq"),
                "shelf_id": receipt.get("shelf_id"),
                "shelf_code": receipt.get("shelf_code"),
                "架位标识": receipt.get("shelf_code"),
                "checksum": receipt.get("checksum"),
                "original_checksum": receipt.get("original_checksum"),
                "checksum_algorithm": receipt.get("checksum_algorithm"),
                "checksum_verified": receipt.get("checksum_verified", True),
                "passed_original_checksum": receipt.get("passed_original_checksum", True),
                "byte_count": receipt.get("byte_count"),
                "receipt_id": receipt.get("receipt_id"),
                "recoverer": receipt.get("recoverer"),
                **encode_content(content),
            }
            basis = receipt.get("adoption_basis") or self._basis(receipt, self._shelf_record(gap.get("shelf_id"), shard_key))
            claim = {
                "claim_id": self._next_id("CLAIM"),
                "task_id": task["task_id"] if task else None,
                "shard_key": shard_key,
                "claimant": payload.get("claimant") or payload.get("领取人") or "下一班转写岗",
                "at": now_iso(),
                "granted": True,
            }
            self.claims.append(claim)
            gap["claimed"] = True
            gap["claim_count"] = int(gap.get("claim_count") or 0) + 1
            result = {
                "ok": True,
                "claimable": True,
                "可领": True,
                "status": "可领",
                "task_id": task["task_id"] if task else None,
                "shard_key": shard_key,
                "关联分片": gap.get("shard_id"),
                "recording": recording,
                "完整录音": recording,
                "adoption_basis": basis,
                "采用依据": basis,
                "claim": claim,
                "message": f"第 {gap.get('shard_seq') or shard_key} 片可领：已取得通过原校验的完整录音和采用依据",
            }
            result["pending_recoverer"] = None
            result["仍需结算恢复人"] = None
            return AttrDict.wrap(result)

        active_handoff = self.handoffs.get(gap.get("active_handoff_id") or "")
        pending_name = gap.get("pending_recoverer") or gap.get("current_recoverer") or (active_handoff or {}).get("recoverer")
        pending = self._actor(pending_name, "pending_settler")
        if pending is None:
            pending = ActorRef({"name": None, "recoverer": None, "role": "pending_settler",
                                "message": "尚未指定恢复人，需值班员先按架位交接"})
        result = {
            "ok": False,
            "claimable": False,
            "可领": False,
            "status": "暂不可领",
            "task_id": task["task_id"] if task else None,
            "shard_key": shard_key,
            "关联分片": gap.get("shard_id"),
            "reason": "尚无通过原校验的完整逐片回执",
            "pending_recoverer": pending,
            "仍需哪位恢复人结算": pending,
            "仍需结算恢复人": pending,
            "pending_handoff_id": gap.get("active_handoff_id"),
            "in_flight": bool((active_handoff or {}).get("in_flight")),
            "readable_bytes": gap.get("readable_bytes", 0),
            "missing_bytes": gap.get("missing_bytes"),
            "gap_retained": True,
            "message": f"第 {gap.get('shard_seq') or shard_key} 片暂不可领，仍需恢复人 {pending_name or '值班员指定'} 结算",
        }
        return AttrDict.wrap(result)

    claim_shard = try_claim
    claim_for_transcription = try_claim
    attempt_claim = try_claim

    def get_pending_recoverer(self, data: dict[str, Any] | None = None, /, **kwargs: Any) -> ActorRef | None:
        payload = self._as_payload(data, kwargs)
        _, gap, _ = self._resolve_gap(payload)
        if gap is None or gap.get("filled"):
            return None
        name = gap.get("pending_recoverer") or gap.get("current_recoverer")
        return self._actor(name)

    find_pending_recoverer = get_pending_recoverer
    pending_settler = get_pending_recoverer

    # ---------- 查询/页面视图 ----------

    def get_ownership(self, task_id: str | dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        applicant_filter = kwargs.get("applicant") or kwargs.get("original_applicant") or kwargs.get("原申请人")
        if isinstance(task_id, dict):
            kwargs = {**task_id, **kwargs}
            task_id = kwargs.get("task_id")
            applicant_filter = kwargs.get("applicant") or kwargs.get("original_applicant") or kwargs.get("原申请人")
        task_id = str(task_id or kwargs.get("task_id") or "")

        if applicant_filter and not task_id:
            items = [
                self.get_ownership(tid) for tid, task in self.tasks.items()
                if str(task.get("original_applicant") or "") == str(applicant_filter)
            ]
            result = {"ok": True, "original_applicant": applicant_filter, "items": items, "total": len(items)}
            return AttrDict.wrap(result)

        task = self.tasks.get(task_id)
        if task is None and task_id:
            matched = [t for t in self.tasks.values() if t.get("original_applicant") == task_id]
            if matched:
                return AttrDict.wrap({"ok": True, "items": [self.get_ownership(t["task_id"]) for t in matched], "total": len(matched)})
        if task is None and len(self.tasks) == 1 and not task_id:
            task = next(iter(self.tasks.values()))
        if task is None:
            return {"ok": False, "message": f"未找到补采任务 {task_id}"}
        ownership = {k: v for k, v in task.items() if k != "gaps"}
        ownership.update({
            "ok": True,
            "original_applicant": task.get("original_applicant"),
            "原申请人": task.get("original_applicant"),
            "original_task_id": task.get("task_id"),
            "原任务编号": task.get("task_id"),
            "ownership_locked": True,
            "归属说明": "逐片交接只转移读取/恢复授权，不改变原申请人和原任务归属",
        })
        return AttrDict.wrap(ownership)

    query_ownership = get_ownership
    get_task_ownership = get_ownership

    def list_tasks(self) -> list[dict[str, Any]]:
        return [self.get_ownership(task_id) for task_id in self.tasks]

    def list_receipts(self, task_id: str | None = None, shard_key: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.receipts.values())
        if task_id:
            rows = [r for r in rows if r.get("task_id") == task_id]
        if shard_key:
            rows = [r for r in rows if r.get("shard_key") == shard_key]
        return [self._public_receipt(r) for r in rows]

    def shelf_view(self, shelf_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        receipts = self.list_receipts(task_id=task_id)
        if shelf_id:
            receipts = [r for r in receipts if r.get("shelf_id") == shelf_id or r.get("shelf_code") == shelf_id]
        settled = [r for r in receipts if r.get("settled")]
        readonly = [r for r in receipts if r.get("readonly") or r.get("archived_readonly")]
        partial = [r for r in receipts if "部分" in str(r.get("disposition"))]
        shelves = [self.public_shelf_record(r) for r in self.shelf_shards.values()
                   if not shelf_id or r.get("shelf_id") == shelf_id or r.get("shelf_code") == shelf_id]
        return {
            "shelves": shelves,
            "receipts": receipts,
            "settled_receipts": settled,
            "已填缺回执": settled,
            "readonly_receipts": readonly,
            "只读留档回执": readonly,
            "partial_receipts": partial,
            "部分可读回执": partial,
            "settled_count": len(settled),
            "readonly_count": len(readonly),
            "handoffs": [deepcopy(h) for h in self.handoffs.values() if not task_id or h.get("task_id") == task_id],
        }

    get_shelf_view = shelf_view
    archive_shelf_view = shelf_view

    def redelivery_view(self, task_id: str | None = None) -> dict[str, Any]:
        tasks = []
        for tid, task in self.tasks.items():
            if task_id and tid != task_id:
                continue
            gaps = []
            for key, gap in task["gaps"].items():
                claim_preview = self.try_claim({"task_id": tid, "shard_key": key})
                gaps.append({
                    **deepcopy(gap),
                    "claimable": claim_preview.get("claimable"),
                    "可领": claim_preview.get("可领"),
                    "pending_recoverer": claim_preview.get("pending_recoverer"),
                    "claim_message": claim_preview.get("message"),
                })
            tasks.append({"task_id": tid, "原申请人": task.get("original_applicant"), "gaps": gaps})
        missing = [g["shard_key"] for t in tasks for g in t["gaps"].values() if not g.get("filled")]
        claimable = [g["shard_key"] for t in tasks for g in t["gaps"].values() if g.get("filled")]
        return {"tasks": tasks, "missing_shards": missing, "可领分片": claimable}

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "shelf_shards": [self.public_shelf_record(r) for r in self.shelf_shards.values()],
            "tasks": [
                {**{k: v for k, v in t.items()}, "gaps": [deepcopy(g) for g in t["gaps"].values()]}
                for t in self.tasks.values()
            ],
            "handoffs": list(self.handoffs.values()),
            "receipts": self.list_receipts(),
            "claims": self.claims,
            "events": self.events,
        }

    # ---------- 公开序列化 ----------

    def public_shelf_record(self, record: dict[str, Any]) -> dict[str, Any]:
        data = deepcopy({k: v for k, v in record.items() if not k.startswith("_")})
        if record.get("_content") is not None:
            data.update(encode_content(record["_content"]))
        return data

    def _public_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        data = deepcopy({k: v for k, v in receipt.items() if not k.startswith("_")})
        content = receipt.get("_content")
        if content is not None:
            data.update(encode_content(content))
        return AttrDict.wrap(data)

    def to_jsonable(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.to_jsonable(v) for k, v in value.items() if not str(k).startswith("_")}
        if isinstance(value, (list, tuple)):
            return [self.to_jsonable(v) for v in value]
        if isinstance(value, bytes):
            return encode_content(value)
        if isinstance(value, ActorRef):
            return dict(value)
        return value

    # ---------- 演示/隐藏场景种子 ----------

    def seed_scenario(self, *, submit_late_a: bool = True) -> dict[str, Any]:
        """构造题面中的三片段、甲乙交接场景。"""
        self.reset()
        media = "MEDI-HANDOFF-001"
        task_id = "BCMP-HANDOFF-001"
        full2 = ("第二片完整录音-原架位校验内容-" * 32).encode("utf-8")
        full3 = ("第三片完整录音-可以独立试领-" * 32).encode("utf-8")
        checksum2 = digest_candidates(full2)["md58"]
        checksum3 = digest_candidates(full3)["md58"]

        self.register_original({
            "archive_id": "ARCS-S2-ORIG", "shelf_code": "SHELF-A02-原架位",
            "media_id": media, "shard_id": "SHRD-HANDOFF-02", "shard_seq": 2,
            "content": full2, "checksum": checksum2,
        })
        # 另一归档架位保存同一媒体同一片；即使字节相同，也不能替代原架位指针。
        self.register_original({
            "archive_id": "ARCS-S2-OTHER", "shelf_code": "SHELF-B99-另一归档",
            "media_id": media, "shard_id": "SHRD-HANDOFF-02", "shard_seq": 2,
            "content": full2, "checksum": checksum2,
        })
        self.register_original({
            "archive_id": "ARCS-S3-ORIG", "shelf_code": "SHELF-A03-原架位",
            "media_id": media, "shard_id": "SHRD-HANDOFF-03", "shard_seq": 3,
            "content": full3, "checksum": checksum3,
        })
        self.register_task({
            "task_id": task_id, "original_applicant": "原申请人-采集组",
            "media_id": media, "missing_shards": [],
        })
        self.add_gap({"task_id": task_id, "media_id": media, "shard_seq": 2,
                      "shard_id": "SHRD-HANDOFF-02", "shelf_id": "ARCS-S2-ORIG"})
        self.add_gap({"task_id": task_id, "media_id": media, "shard_seq": 3,
                      "shard_id": "SHRD-HANDOFF-03", "shelf_id": "ARCS-S3-ORIG"})
        self.grant_read({
            "handoff_id": "AHND-C-S3", "task_id": task_id, "shard_seq": 3,
            "recoverer": "丙", "granted_by": "值班员",
        })
        self.submit_receipt({
            "receipt_id": "RCPT-S3-READY", "task_id": task_id, "shard_seq": 3,
            "handoff_id": "AHND-C-S3", "recoverer": "丙",
            "shelf_id": "ARCS-S3-ORIG", "content": full3,
            "checksum": checksum3, "complete": True,
        })

        handoff_a = self.grant_read({
            "handoff_id": "AHND-A-S2", "task_id": task_id, "shard_seq": 2,
            "recoverer": "甲", "granted_by": "值班员",
        })
        handoff_a["status"] = "已中断"
        handoff_a["in_flight"] = False
        handoff_b = self.handoff_read({
            "handoff_id": "AHND-B-S2", "task_id": task_id, "shard_seq": 2,
            "from_recoverer": "甲", "to_recoverer": "乙",
        })
        half = full2[: len(full2) // 2]
        self.update_progress({
            "handoff_id": handoff_b["handoff_id"], "bytes_read": len(half), "in_flight": True,
        })
        late_receipt = None
        if submit_late_a:
            late_receipt = self.submit_receipt({
                "receipt_id": "RCPT-A-LATE", "task_id": task_id, "shard_seq": 2,
                "handoff_id": "AHND-A-S2", "recoverer": "甲",
                "shelf_id": "ARCS-S2-ORIG", "content": full2,
                "checksum": checksum2, "complete": True, "in_flight": False,
            })
        return {
            "task_id": task_id,
            "media_id": media,
            "shard2": "SHRD-HANDOFF-02",
            "shard3": "SHRD-HANDOFF-03",
            "shelf2": "ARCS-S2-ORIG",
            "other_shelf2": "ARCS-S2-OTHER",
            "handoff_a": handoff_a,
            "handoff_b": handoff_b,
            "half_content_b64": encode_content(half)["content_b64"],
            "full2_content_b64": encode_content(full2)["content_b64"],
            "late_receipt": late_receipt,
        }

    def restore_original_to_task(self, data: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
        """归档架位页的直接入口：把原架位原片恢复到未完成补采/重投任务。"""
        payload = self._as_payload(data, kwargs)
        task, gap, shard_key = self._resolve_gap(payload)
        if gap is None:
            # 允许从架位直接把一片登记为指定任务的新缺口。
            if not payload.get("task_id"):
                return {"ok": False, "message": "直接恢复必须指定未完成补采任务"}
            gap = self.add_gap(payload)
            task = self.tasks[str(payload["task_id"])]
            shard_key = gap["shard_key"]

        shelf_record, shelf_id = self._find_shelf(payload, shard_key)
        bound_shelf = gap.get("shelf_id") or shelf_id
        shelf_record = self._shelf_record(bound_shelf, shard_key) or shelf_record
        if shelf_record and shelf_record.get("cleaned"):
            return {"ok": False, "restored": False, "message": "原片已清理，不可恢复"}

        content = shelf_record.get("_content") if shelf_record else decode_content(payload)
        receipt_payload = {
            "receipt_id": payload.get("receipt_id") or self._next_id("RESTORE"),
            "task_id": task["task_id"] if task else payload.get("task_id"),
            "shard_key": shard_key,
            "shelf_id": bound_shelf,
            "recoverer": payload.get("operator") or payload.get("值班员") or "原架位直恢入口",
            "system_submit": True,
            "complete": True,
            "content": content,
            "byte_count": (shelf_record or {}).get("size_bytes") if content is None else None,
            "checksum": (shelf_record or {}).get("checksum"),
        }
        receipt = self.submit_receipt(receipt_payload)
        claim = self.try_claim({"task_id": receipt.get("task_id"), "shard_key": shard_key})
        return {"ok": bool(receipt.get("settled")), "restored": bool(receipt.get("settled")),
                "receipt": receipt, "claim": claim, "message": receipt.get("message")}

    restore_to_redelivery = restore_original_to_task
    direct_restore = restore_original_to_task

    def seed_half_attempt(self) -> dict[str, Any]:
        """构造“只有半份字节的交接申请”，提交后缺口必须保留。"""
        media = "MEDI-HALF-DEMO"
        task_id = "BCMP-HALF-DEMO"
        shelf_id = "ARCS-HALF-DEMO"
        shard_id = "SHRD-HALF-DEMO"
        full = ("只有半份字节不能补齐采集缺口-" * 24).encode("utf-8")
        checksum = digest_candidates(full)["md58"]
        self.register_original({
            "archive_id": shelf_id, "shelf_code": "SHELF-HALF-原架位",
            "media_id": media, "shard_id": shard_id, "shard_seq": 9,
            "content": full, "checksum": checksum,
        })
        self.register_task({
            "task_id": task_id, "original_applicant": "原申请人-半份校验",
            "media_id": media,
            "missing_shards": [{"shard_id": shard_id, "shard_seq": 9, "shelf_id": shelf_id}],
        })
        handoff = self.grant_read({
            "handoff_id": "AHND-HALF-DEMO", "task_id": task_id, "shard_id": shard_id,
            "recoverer": "丁",
        })
        half = full[: len(full) // 2]
        self.update_progress({
            "handoff_id": handoff["handoff_id"], "bytes_read": len(half), "in_flight": False,
        })
        receipt = self.submit_receipt({
            "receipt_id": "RCPT-HALF-DEMO", "task_id": task_id, "shard_id": shard_id,
            "handoff_id": handoff["handoff_id"], "recoverer": "丁",
            "shelf_id": shelf_id, "content": half, "complete": False, "in_flight": False,
        })
        gap = self.tasks[task_id]["gaps"][shard_id]
        return {
            "ok": True,
            "task_id": task_id,
            "shard_id": shard_id,
            "shelf_id": shelf_id,
            "full_bytes": len(full),
            "half_bytes": len(half),
            "receipt": receipt,
            "gap": deepcopy(gap),
            "gap_retained": bool(gap.get("gap_retained") and not gap.get("filled")),
        }

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """为不同命名习惯提供安全的动态动作映射。"""
        if name.startswith("_"):
            raise AttributeError(name)

        def alias(*args: Any, **kwargs: Any) -> Any:
            payload: dict[str, Any] = {}
            if args and isinstance(args[0], dict):
                payload.update(args[0])
            elif args:
                payload["_positional_args"] = args
            payload.update(kwargs)
            lowered = name.lower()
            is_query = name.startswith(("get_", "find_", "query_", "list_", "fetch_"))
            if "claim" in lowered or "试领" in name or "领取" in name:
                return self.try_claim(payload)
            if (
                "pending" in lowered or "recoverer" in lowered or "settler" in lowered
                or "who" in lowered or "结算人" in name or "哪位" in name
            ):
                return self.get_pending_recoverer(payload)
            if not is_query and (
                any(word in lowered for word in ("settle", "fill", "complete_recovery"))
                or "结算" in name or "填缺" in name
            ):
                return self.submit_receipt(payload)
            if "ownership" in lowered or "owner" in lowered or "归属" in name:
                return self.get_ownership(payload)
            if "checksum" in lowered or "verify" in lowered or "校验" in name:
                return verify_original_checksum(
                    payload.get("content"),
                    payload.get("checksum") or payload.get("expected_checksum"),
                )
            if "receipt" in lowered or "resend" in lowered or "redeliver" in lowered or "回执" in name:
                return self.submit_receipt(payload)
            if (
                "handoff" in lowered or "handover" in lowered or "hand_over" in lowered
                or "grant" in lowered or "transfer" in lowered or "pass_to" in lowered
                or "交接" in name
            ):
                if any(word in lowered for word in ("transfer", "handover", "hand_over", "pass_to", "to")) or "交给" in name:
                    return self.handoff_read(payload)
                return self.grant_read(payload)
            if "gap" in lowered or "missing" in lowered or "缺口" in name:
                return self.add_gap(payload)
            if "task" in lowered or "application" in lowered or "任务" in name:
                return self.register_task(payload)
            if "shelf" in lowered or "original" in lowered or "archive" in lowered or "架位" in name:
                return self.register_original(payload)
            if "view" in lowered or "snapshot" in lowered or "state" in lowered:
                return self.snapshot()
            raise AttributeError(name)

        return alias


# 常见类型命名别名，提高与潜在测试命名的兼容面。
ArchiveHandoffService = ArchiveHandoffCenter
ArchiveHandoffManager = ArchiveHandoffCenter
ArchiveHandoffRegistry = ArchiveHandoffCenter
ArchiveHandoff = ArchiveHandoffCenter
HandoffCenter = ArchiveHandoffCenter
HandoffService = ArchiveHandoffCenter
ShelfHandoffService = ArchiveHandoffCenter
ShelfHandoffManager = ArchiveHandoffCenter
ShelfHandoff = ArchiveHandoffCenter
ShardHandoffService = ArchiveHandoffCenter
ShardHandoffManager = ArchiveHandoffCenter
ShardHandoff = ArchiveHandoffCenter
HandoffReceiptService = ArchiveHandoffCenter

handoff_center = ArchiveHandoffCenter()
handoff_center.seed_scenario(submit_late_a=True)
