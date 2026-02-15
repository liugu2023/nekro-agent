"""
人设配置迁移模块
处理从旧的 AI_CHAT_PRESET_SETTING 配置升级到新的人设管理系统
"""

from nekro_agent.core.config import config
from nekro_agent.core.logger import logger
from nekro_agent.models.db_preset import DBPreset


async def migrate_preset_config():
    """
    迁移人设配置（仅执行一次）

    流程:
    1. 检查是否存在系统默认人设 (author="__system__")
    2. 如果存在且已完成迁移，跳过
    3. 如果存在但未标记迁移完成，添加标记（兼容旧版本）
    4. 如果不存在，从旧配置创建系统默认人设
    """
    try:
        # 使用 filter().first() 避免 MultipleObjectsReturned 异常
        system_preset = await DBPreset.filter(author="__system__").first()

        if system_preset:
            # 检查是否已完成迁移
            ext_data = system_preset.ext_data or {}
            if ext_data.get("migration_completed"):
                logger.debug("人设迁移已完成，跳过")
                return

            # 兼容：已有系统人设但无迁移标记（可能是之前版本创建的）
            # 只添加标记，不修改内容（保护用户在前端的修改）
            ext_data["migration_completed"] = True
            ext_data["is_system_default"] = True
            system_preset.ext_data = ext_data
            await system_preset.save()
            logger.info("已为现有系统人设添加迁移标记")
            return

        # 首次迁移：从旧配置创建系统人设
        logger.info("首次初始化系统默认人设...")
        system_preset = await DBPreset.create(
            name=config.AI_CHAT_PRESET_NAME,
            title=config.AI_CHAT_PRESET_NAME,
            content=config.AI_CHAT_PRESET_SETTING,
            description="系统默认人设",
            tags="",
            author="__system__",
            avatar="",
            ext_data={
                "is_system_default": True,
                "migration_completed": True,
                "migrated_from_config": True,
            },
        )
        logger.success(f"系统默认人设已创建 (ID: {system_preset.id})")

    except Exception as e:
        logger.error(f"人设配置迁移失败: {e}")
        raise

