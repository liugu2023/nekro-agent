from __future__ import annotations

from .base import BaseAdapter
from .schemas.platform import PlatformChannel, PlatformMessage, PlatformUser

__all__ = ["BaseAdapter", "PlatformChannel", "PlatformMessage", "PlatformUser", "collect_message"]


async def collect_message(*args, **kwargs):
    from .collector import collect_message as _collect_message

    return await _collect_message(*args, **kwargs)
