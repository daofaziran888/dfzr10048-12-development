"""业务路由汇总：交接专用路由先注册，通用资源路由后注册。"""
from __future__ import annotations

from app.routers import handoff, resource

ROUTERS = [handoff, resource]
