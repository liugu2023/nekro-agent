"""ConfigCacheManager - typing_ticket 缓存

per-user getConfig 缓存，24h 随机 TTL + 指数退避重试。
参考: OpenClaw src/api/config-cache.ts
"""

import random
import time

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("adapter.wechat_openclaw.config_cache")


class _CacheEntry:
    """缓存条目"""

    __slots__ = ("typing_ticket", "expire_at", "fail_count", "next_retry_at")

    def __init__(self, typing_ticket: str, ttl: float) -> None:
        self.typing_ticket = typing_ticket
        self.expire_at = time.time() + ttl
        self.fail_count = 0
        self.next_retry_at: float = 0

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expire_at

    @property
    def can_retry(self) -> bool:
        return time.time() >= self.next_retry_at


class ConfigCacheManager:
    """typing_ticket 配置缓存管理器"""

    # TTL 范围: 20-24 小时（随机化避免同时刷新）
    MIN_TTL = 20 * 3600
    MAX_TTL = 24 * 3600

    # 退避参数
    INITIAL_BACKOFF = 2.0  # 初始退避 2 秒
    MAX_BACKOFF = 3600.0  # 最大退避 1 小时
    BACKOFF_MULTIPLIER = 2.0

    def __init__(self) -> None:
        self._cache: dict[str, _CacheEntry] = {}

    def _make_key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def _random_ttl(self) -> float:
        return random.uniform(self.MIN_TTL, self.MAX_TTL)

    def get(self, account_id: str, user_id: str) -> str | None:
        """获取缓存的 typing_ticket，过期或不存在返回 None"""
        key = self._make_key(account_id, user_id)
        entry = self._cache.get(key)
        if entry is None or entry.is_expired:
            return None
        return entry.typing_ticket

    def set(self, account_id: str, user_id: str, typing_ticket: str) -> None:
        """设置 typing_ticket 缓存"""
        key = self._make_key(account_id, user_id)
        self._cache[key] = _CacheEntry(typing_ticket, self._random_ttl())

    def should_retry(self, account_id: str, user_id: str) -> bool:
        """检查是否可以重试获取 config"""
        key = self._make_key(account_id, user_id)
        entry = self._cache.get(key)
        if entry is None:
            return True
        return entry.can_retry

    def record_failure(self, account_id: str, user_id: str) -> None:
        """记录获取失败，计算下次重试时间"""
        key = self._make_key(account_id, user_id)
        entry = self._cache.get(key)
        if entry is None:
            entry = _CacheEntry("", 0)
            self._cache[key] = entry

        entry.fail_count += 1
        backoff = min(
            self.INITIAL_BACKOFF * (self.BACKOFF_MULTIPLIER ** (entry.fail_count - 1)),
            self.MAX_BACKOFF,
        )
        entry.next_retry_at = time.time() + backoff
        logger.debug(f"Config 获取失败第 {entry.fail_count} 次，{backoff:.0f}s 后重试")

    def invalidate(self, account_id: str, user_id: str) -> None:
        """使缓存失效"""
        key = self._make_key(account_id, user_id)
        self._cache.pop(key, None)
