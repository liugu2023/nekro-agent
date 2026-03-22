"""SessionGuard 会话暂停管理

检测 errcode=-14 后暂停 account 1 小时，防止频繁请求被封。
"""

import time


class SessionGuard:
    """会话暂停管理器"""

    def __init__(self) -> None:
        self._paused_until: dict[str, float] = {}

    def pause(self, account_id: str, duration_seconds: int = 3600) -> None:
        """暂停指定账号

        Args:
            account_id: 账号 ID
            duration_seconds: 暂停时长（秒），默认 1 小时
        """
        self._paused_until[account_id] = time.time() + duration_seconds

    def is_paused(self, account_id: str) -> bool:
        """检查账号是否处于暂停状态"""
        until = self._paused_until.get(account_id)
        if until is None:
            return False
        if time.time() >= until:
            del self._paused_until[account_id]
            return False
        return True

    def remaining_seconds(self, account_id: str) -> int:
        """获取剩余暂停秒数"""
        until = self._paused_until.get(account_id)
        if until is None:
            return 0
        remaining = until - time.time()
        if remaining <= 0:
            self._paused_until.pop(account_id, None)
            return 0
        return int(remaining)

    def resume(self, account_id: str) -> None:
        """手动恢复账号"""
        self._paused_until.pop(account_id, None)
