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

    def _current_hour(self) -> int:
        """获取当前小时 (0-23)"""
        return datetime.datetime.now().hour

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

    def calculate_hourly_quota(self, daily_limit: int) -> int:
        """根据每日限额计算每小时限额（向上取整）"""
        if daily_limit <= 0:
            return 0
        # 确保每小时至少1条，除非daily_limit为0
        return max(1, (daily_limit + 23) // 24)  # 向上取整

    def get_hourly_quota_progress(self, chat_key: str, current_hour_count: int, hourly_limit: int) -> Dict:
        """获取当前小时的配额进度"""
        return {
            "hour": self._current_hour(),
            "current_count": current_hour_count,
            "hourly_limit": hourly_limit,
            "remaining": max(0, hourly_limit - current_hour_count),
            "exceeded": current_hour_count >= hourly_limit,
        }


quota_service = QuotaService()
