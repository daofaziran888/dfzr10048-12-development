"""逐片架位交接、回执幂等结算与试领链路测试。"""
from __future__ import annotations

import hashlib

from app.pipeline.archive_handoff import ArchiveHandoffCenter


def make_center() -> ArchiveHandoffCenter:
    center = ArchiveHandoffCenter()
    center.seed_scenario(submit_late_a=True)
    return center


def test_late_verified_receipt_fills_and_claim_returns_basis_and_content():
    center = make_center()
    claim = center.try_claim(task_id="BCMP-HANDOFF-001", shard_seq=2)

    assert claim["claimable"] is True
    recording = claim["recording"]
    assert recording["receipt_id"] == "RCPT-A-LATE"
    assert recording["shelf_id"] == "ARCS-S2-ORIG"
    assert recording["checksum_verified"] is True
    assert recording["content_b64"]
    basis = claim["adoption_basis"]
    assert basis["shelf_id"] == "ARCS-S2-ORIG"
    assert basis["receipt_id"] == "RCPT-A-LATE"
    assert "原校验" in basis


def test_other_shelf_same_media_shard_cannot_replace_original_pointer():
    center = make_center()
    seed = center.seed_scenario(submit_late_a=False)
    wrong = center.submit_receipt(
        receipt_id="RCPT-WRONG-SHELF",
        task_id=seed["task_id"], shard_seq=2, recoverer="乙",
        shelf_id=seed["other_shelf2"],
        content_b64=seed["full2_content_b64"], complete=True,
    )
    assert wrong["ok"] is False
    assert wrong["disposition"] == "来源不符"
    claim = center.try_claim(task_id=seed["task_id"], shard_seq=2)
    assert claim["claimable"] is False
    assert str(claim["pending_recoverer"]) == "乙"


def test_pending_claim_names_active_recoverer_while_read_in_flight():
    center = ArchiveHandoffCenter()
    seed = center.seed_scenario(submit_late_a=False)

    claim = center.try_claim(task_id=seed["task_id"], shard_seq=2)
    assert claim["claimable"] is False
    assert claim["in_flight"] is True
    assert claim["pending_recoverer"]["recoverer"] == "乙"
    assert "乙" in claim["message"]


def test_second_complete_receipt_is_readonly_and_replay_settles_once():
    center = make_center()
    seed = center.seed_scenario(submit_late_a=True)

    second = center.submit_receipt(
        receipt_id="RCPT-B-DONE",
        task_id=seed["task_id"], shard_seq=2,
        handoff_id=seed["handoff_b"]["handoff_id"], recoverer="乙",
        shelf_id=seed["shelf2"], content_b64=seed["full2_content_b64"], complete=True,
    )
    assert second["settled"] is False
    assert second["readonly"] is True
    assert second["disposition"] == "只读留档"

    replay = center.submit_receipt(
        receipt_id="RCPT-B-DONE",
        task_id=seed["task_id"], shard_seq=2,
        content_b64=seed["full2_content_b64"],
    )
    assert replay["duplicate"] is True
    assert replay["newly_settled"] is False
    assert replay["settlement_count"] == 1

    shard2 = [
        r for r in center.list_receipts(task_id=seed["task_id"])
        if str(r.get("shard_seq")) == "2"
    ]
    assert sum(1 for r in shard2 if r["settled"]) == 1
    assert sum(1 for r in shard2 if r.get("readonly")) == 1


def test_shard_three_remains_claimable():
    center = make_center()
    claim = center.try_claim(task_id="BCMP-HANDOFF-001", shard_seq=3)
    assert claim["claimable"] is True
    assert claim["recording"]["checksum_verified"] is True


def test_half_bytes_application_keeps_collection_gap():
    center = ArchiveHandoffCenter()
    full = b"complete-original-bytes" * 8
    checksum = hashlib.md5(full).hexdigest()[:8]
    center.register_original(
        archive_id="ARCS-HALF", shelf_code="SHELF-HALF",
        media_id="MEDI-HALF", shard_id="SHRD-HALF",
        content=full, checksum=checksum,
    )
    center.register_task(
        task_id="T-HALF", original_applicant="申请人", media_id="MEDI-HALF",
        missing_shards=[{"shard_id": "SHRD-HALF", "shelf_id": "ARCS-HALF"}],
    )
    center.grant_read(task_id="T-HALF", shard_id="SHRD-HALF", recoverer="丙")
    receipt = center.submit_receipt(
        receipt_id="R-HALF", task_id="T-HALF", shard_id="SHRD-HALF",
        recoverer="丙", shelf_id="ARCS-HALF", content=full[: len(full) // 2],
    )

    assert receipt["settled"] is False
    assert receipt["gap_retained"] is True
    gap = center.tasks["T-HALF"]["gaps"]["SHRD-HALF"]
    assert gap["filled"] is False
    assert gap["gap_retained"] is True
    assert gap["missing_bytes"] > 0
    assert center.try_claim(task_id="T-HALF", shard_id="SHRD-HALF")["claimable"] is False


def test_cleaned_shelf_cannot_restore():
    center = ArchiveHandoffCenter()
    content = b"cleaned"
    center.register_original(
        archive_id="ARCS-CLEAN", shelf_code="SHELF-CLEAN",
        media_id="M-CLEAN", shard_id="S-CLEAN", content=content, cleaned=True,
    )
    center.register_task(
        task_id="T-CLEAN", original_applicant="O", media_id="M-CLEAN",
        missing_shards=[{"shard_id": "S-CLEAN", "shelf_id": "ARCS-CLEAN"}],
    )
    try:
        center.grant_read(task_id="T-CLEAN", shard_id="S-CLEAN", recoverer="甲")
    except PermissionError as exc:
        assert "已清理" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("已清理架位不应允许交接读取")


def test_ownership_remains_with_original_applicant():
    center = make_center()
    ownership = center.get_ownership(task_id="BCMP-HANDOFF-001")
    assert ownership["original_applicant"] == "原申请人-采集组"
    assert ownership["ownership_locked"] is True
    by_applicant = center.get_ownership(applicant="原申请人-采集组")
    assert by_applicant["total"] == 1
