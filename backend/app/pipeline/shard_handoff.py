"""命名兼容入口：分片交接管线。"""
from app.pipeline.archive_handoff import *  # noqa: F401,F403
from app.pipeline.archive_handoff import ArchiveHandoffCenter as ShardHandoffCenter

shard_handoff_center = ArchiveHandoffCenter()
