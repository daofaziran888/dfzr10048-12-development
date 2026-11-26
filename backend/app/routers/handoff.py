"""归档架位逐片交接 HTTP 接口。

路由同时保留连字符/下划线和 archive_shelf/fail_redeliver 资源前缀别名，
方便不同调用约定接入；所有业务判断仍在 ArchiveHandoffCenter/领域服务中。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.pipeline.archive_handoff import ArchiveHandoffCenter, handoff_center

router = APIRouter(prefix="/api", tags=["归档架位逐片交接"])


async def _body(request: Request) -> dict[str, Any]:
    if request.method == "GET":
        return dict(request.query_params)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {"payload": data}
    # 允许调用方把参数包在 values/payload 中，也允许顶层直接传字段。
    merged: dict[str, Any] = {}
    for key in ("payload", "values"):
        inner = data.get(key)
        if isinstance(inner, dict):
            merged.update(inner)
    merged.update(data)
    return merged


def _json(result: Any, status_code: int = 200) -> JSONResponse:
    if isinstance(result, tuple):
        result = {"ok": bool(result[0]), "message": str(result[1]) if len(result) > 1 else ""}
    return JSONResponse(handoff_center.to_jsonable(result), status_code=status_code)


def _service() -> ArchiveHandoffCenter:
    # 测试可能替换模块级单例；始终动态读取。
    from app.services import archive_handoff as service_module

    return getattr(service_module, "handoff_center", handoff_center)


# ---------- 状态/演示数据 ----------

@router.get("/archive-handoff/state")
@router.get("/archive_handoff/state")
@router.get("/shelf-handoff/state")
@router.get("/shelf_handoff/state")
async def state(request: Request):
    return _json(_service().snapshot())


@router.post("/archive-handoff/reset")
@router.post("/archive_handoff/reset")
async def reset(request: Request):
    body = await _body(request)
    svc = _service().reset()
    if body.get("seed", True):
        svc.seed_scenario()
    return _json({"ok": True, "message": "交接状态已重置", "state": svc.snapshot()})


@router.post("/archive-handoff/seed")
@router.post("/archive_handoff/seed")
@router.post("/archive-handoff/scenario")
async def seed(request: Request):
    body = await _body(request)
    result = _service().seed_scenario(submit_late_a=bool(body.get("submit_late_a", True)))
    return _json({"ok": True, "message": "已构造甲乙逐片交接场景", "seed": result})


# ---------- 原架位/任务/归属 ----------

@router.post("/archive-handoff/shelf-shards")
@router.post("/archive_handoff/shelf-shards")
@router.post("/archive-handoff/originals")
@router.post("/archive_shelf/handoff-originals")
async def register_original(request: Request):
    return _json(_service().register_original(await _body(request)))


@router.post("/archive-handoff/tasks")
@router.post("/archive_handoff/tasks")
@router.post("/archive-handoff/applications")
async def register_task(request: Request):
    return _json(_service().register_task(await _body(request)))


@router.get("/archive-handoff/tasks")
@router.get("/archive_handoff/tasks")
async def list_tasks():
    return _json({"items": _service().list_tasks(), "total": len(_service().tasks)})


@router.get("/archive-handoff/ownership")
@router.get("/archive_handoff/ownership")
@router.get("/archive-handoff/tasks/{task_id}/ownership")
@router.get("/archive_handoff/tasks/{task_id}/ownership")
async def ownership(request: Request, task_id: str | None = None):
    body = await _body(request)
    return _json(_service().get_ownership(task_id or body))


# ---------- 交接授权/在途进度 ----------

@router.post("/archive-handoff/restore-to-task")
@router.post("/archive_handoff/restore-to-task")
@router.post("/archive_shelf/restore-to-task")
@router.post("/fail_redeliver/restore-from-shelf")
async def restore_to_task(request: Request):
    return _json(_service().restore_original_to_task(await _body(request)))


@router.post("/archive-handoff/handoffs")
@router.post("/archive_handoff/handoffs")
@router.post("/handoffs")
@router.post("/archive-handoff/grant")
@router.post("/archive_shelf/handoffs")
@router.post("/archive_shelf/handoff")
async def grant(request: Request):
    try:
        return _json(_service().grant_read(await _body(request)))
    except (KeyError, PermissionError, ValueError) as exc:
        return _json({"ok": False, "message": str(exc), "claimable": False}, status_code=200)


@router.post("/archive-handoff/handoffs/transfer")
@router.post("/archive_handoff/handoffs/transfer")
@router.post("/archive-handoff/transfer")
@router.post("/archive_shelf/handoffs/transfer")
async def transfer(request: Request):
    try:
        return _json(_service().handoff_read(await _body(request)))
    except (KeyError, PermissionError, ValueError) as exc:
        return _json({"ok": False, "message": str(exc)}, status_code=200)


@router.post("/archive-handoff/progress")
@router.post("/archive_handoff/progress")
@router.post("/archive-handoff/read-progress")
async def progress(request: Request):
    try:
        return _json(_service().update_progress(await _body(request)))
    except KeyError as exc:
        return _json({"ok": False, "message": str(exc)}, status_code=200)


# ---------- 逐片回执 ----------

@router.post("/archive-handoff/receipts")
@router.post("/archive_handoff/receipts")
@router.post("/handoff-receipts")
@router.post("/archive-handoff/receipt")
@router.post("/archive-handoff/redeliver")
@router.post("/archive_shelf/handoff-receipts")
@router.post("/fail_redeliver/handoff-receipts")
async def receipts(request: Request):
    body = await _body(request)
    idem = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    if idem and not body.get("receipt_id"):
        body["receipt_id"] = idem
    result = _service().submit_receipt(body)
    # 同步通用失败重投服务中同编号分片的种子行。
    try:
        from app.services.fail_redeliver import FailRedeliverService

        FailRedeliverService()._sync_redeliver_row(
            str(result.get("关联分片") or result.get("shard_key") or ""), result,
        )
    except Exception:
        pass
    return _json(result)


@router.get("/archive-handoff/receipts")
@router.get("/archive_handoff/receipts")
async def list_receipts(request: Request):
    q = dict(request.query_params)
    items = _service().list_receipts(task_id=q.get("task_id"), shard_key=q.get("shard_key"))
    return _json({"items": items, "total": len(items)})


# ---------- 重投页试领/待结算人 ----------

@router.post("/archive-handoff/claims")
@router.post("/archive_handoff/claims")
@router.post("/claims")
@router.post("/archive-handoff/claim")
@router.post("/archive-handoff/try-claim")
@router.post("/fail_redeliver/claims")
@router.post("/fail_redeliver/claim")
@router.post("/fail_redeliver/try-claim")
async def create_claim(request: Request):
    body = await _body(request)
    result = _service().try_claim(body)
    try:
        from app.services.fail_redeliver import FailRedeliverService

        shard = str(result.get("关联分片") or body.get("shard_id") or "")
        if shard:
            FailRedeliverService()._sync_redeliver_row(shard, result)
    except Exception:
        pass
    return _json(result)


@router.get("/archive-handoff/claim")
@router.get("/archive_handoff/claim")
@router.get("/fail_redeliver/claim")
async def get_claim(request: Request):
    return _json(_service().try_claim(dict(request.query_params)))


@router.get("/archive-handoff/pending-recoverer")
@router.get("/archive_handoff/pending-recoverer")
@router.get("/pending-recoverer")
@router.get("/fail_redeliver/pending-recoverer")
async def pending(request: Request):
    result = _service().get_pending_recoverer(dict(request.query_params))
    return _json({"ok": result is not None, "pending_recoverer": result, "仍需结算恢复人": result})


# ---------- 页面聚合视图 ----------

@router.get("/archive-handoff/shelf-view")
@router.get("/archive_handoff/shelf-view")
@router.get("/archive_shelf/handoff-view")
async def shelf_view(request: Request):
    q = dict(request.query_params)
    return _json(_service().shelf_view(shelf_id=q.get("shelf_id"), task_id=q.get("task_id")))


@router.get("/archive-handoff/redelivery-view")
@router.get("/archive_handoff/redelivery-view")
@router.get("/fail_redeliver/handoff-view")
async def redelivery_view(request: Request):
    q = dict(request.query_params)
    return _json(_service().redelivery_view(task_id=q.get("task_id")))


# ---------- 通用 action 分派，兼容不同动作命名 ----------

@router.post("/archive-handoff/demo-half")
@router.post("/archive_handoff/demo-half")
async def demo_half():
    return _json(_service().seed_half_attempt())


@router.post("/archive-handoff/dispatch")
@router.post("/archive_handoff/dispatch")
@router.post("/archive_shelf/handoff-action")
@router.post("/fail_redeliver/handoff-action")
async def dispatch(request: Request):
    body = await _body(request)
    action = str(body.get("action") or body.get("动作") or "")
    svc = _service()
    mapping = {
        "交接读取": svc.grant_read,
        "按架位交接": svc.handoff_read,
        "交给乙恢复": svc.handoff_read,
        "读取进度": svc.update_progress,
        "重送回执": svc.submit_receipt,
        "提交回执": svc.submit_receipt,
        "试领": svc.try_claim,
        "试领第2片": svc.try_claim,
        "查询待结算人": svc.get_pending_recoverer,
        "归属查询": svc.get_ownership,
        "架位核对": svc.shelf_view,
        "重投核对": svc.redelivery_view,
    }
    func = mapping.get(action) or getattr(svc, action, None)
    if func is None or not callable(func):
        return _json({"ok": False, "message": f"未知交接动作：{action}"})
    try:
        result = func(body.get("payload") if isinstance(body.get("payload"), dict) else body)
        return _json(result)
    except (KeyError, PermissionError, ValueError) as exc:
        return _json({"ok": False, "message": str(exc)})
