from pathlib import Path

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.user.auth import get_hashed_password

MIGRATION_FLAG = Path(OsEnv.DATA_DIR) / ".password_migration_done"


async def migrate_empty_passwords():
    """将所有非 admin 用户的密码重置为 用户名@123（仅执行一次）"""
    if MIGRATION_FLAG.exists():
        return

    users = await DBUser.all()
    migrated = 0
    for user in users:
        if user.username == "admin":
            continue
        user.password = get_hashed_password(f"{user.platform_userid}@123")
        await user.save()
        migrated += 1

    MIGRATION_FLAG.touch()
    if migrated:
        logger.info(f"已将 {migrated} 个用户的密码重置为 平台ID@123")
    else:
        logger.info("无需迁移用户密码")
