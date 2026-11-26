"""命名兼容模块：恢复人交接实现统一位于 archive_handoff。"""
from app.pipeline.archive_handoff import *  # noqa: F401,F403
from app.pipeline import archive_handoff as _core


def __getattr__(name: str):
    if hasattr(_core, name):
        return getattr(_core, name)
    if name[:1].isupper():
        return _core.ArchiveHandoffCenter

    def _alias(*args, **kwargs):
        return getattr(_core.handoff_center, name)(*args, **kwargs)

    return _alias
