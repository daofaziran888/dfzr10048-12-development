"""命名兼容入口：架位交接服务。"""
from app.services.archive_handoff import *  # noqa: F401,F403
from app.services.archive_handoff import ArchiveHandoffDomainService as ShelfHandoffDomainService
from app.pipeline.archive_handoff import ShelfHandoffService, handoff_center as shelf_handoff_center

shelf_handoff_service = shelf_handoff_center
