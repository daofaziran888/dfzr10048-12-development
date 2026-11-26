"""命名兼容入口：架位交接管线。"""
from app.pipeline.archive_handoff import *  # noqa: F401,F403
from app.pipeline.archive_handoff import ArchiveHandoffCenter as ShelfHandoffCenter

shelf_handoff_center = ArchiveHandoffCenter()
