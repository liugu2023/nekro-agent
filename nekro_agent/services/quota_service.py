"""每日回复配额管理服务

管理频道的临时配额提升（仅当天有效，重启失效）。
"""

import datetime
from typing import Dict

from nekro_agent.core import logger


class QuotaService:
    """配额管理服务"""

    def __init__(self):
        # {chat_key: {"date": "2024-01-15", "boost": 10}}
        self._daily_boosts: Dict[str, Dict] = {}

    def _today_str(self) -> str:
        return datetime.date.today().isoformat()

    def get_boost(self, chat_key: str) -> int:
        """获取当日临时提升额度，过期自动清零"""
        info = self._daily_boosts.get(chat_key)
        if not info or info["date"] != self._today_str():
            return 0
        return info["boost"]

    def set_boost(self, chat_key: str, amount: int):
        """设置当日临时提升额度"""
        self._daily_boosts[chat_key] = {
            "date": self._today_str(),
            "boost": amount,
        }
        logger.info(f"频道 {chat_key} 今日临时配额提升设为 {amount}")

    def add_boost(self, chat_key: str, amount: int) -> int:
        """追加当日临时提升额度，返回提升后的总额度"""
        current = self.get_boost(chat_key)
        new_total = current + amount
        self.set_boost(chat_key, new_total)
        return new_total

    def clear_boost(self, chat_key: str):
        """清除频道的临时提升"""
        self._daily_boosts.pop(chat_key, None)


quota_service = QuotaService()
