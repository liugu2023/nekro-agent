"""ContextToken 内存缓存

每条消息的 context_token 需要在回复时携带，用于 iLink 会话绑定。
重启后缓存丢失，需等待用户再次发消息。
"""


class ContextTokenStore:
    """context_token 内存缓存"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def set(self, account_id: str, user_id: str, token: str) -> None:
        """缓存 context_token"""
        key = f"{account_id}:{user_id}"
        self._store[key] = token

    def get(self, account_id: str, user_id: str) -> str:
        """获取 context_token，不存在返回空字符串"""
        key = f"{account_id}:{user_id}"
        return self._store.get(key, "")
