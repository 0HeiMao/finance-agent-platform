"""兼容启动入口：python -m backend.app 或 uvicorn backend.app:app。"""

from .main import app

__all__ = ["app"]
