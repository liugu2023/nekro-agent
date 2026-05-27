from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from nonebot import get_app, get_driver
from nonebot.adapters.onebot.v11 import Bot

from nekro_agent.adapters import cleanup_adapters, init_adapters
from nekro_agent.core.database import init_db
from nekro_agent.core.db_migration import run_db_migrations
from nekro_agent.core.logger import logger
from nekro_agent.routers import mount_api_routes, mount_middlewares
from nekro_agent.services.mail.mail_service import send_bot_status_email
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.scheduler import memory_scheduler
from nekro_agent.services.plugin.collector import init_plugins
from nekro_agent.services.runtime_state import mark_shutting_down, mark_started
from nekro_agent.services.timer.recurring_timer_service import recurring_timer_service
from nekro_agent.services.timer.timer_service import timer_service
from nekro_agent.systems.cloud.scheduler import start_telemetry_task, stop_telemetry_task

logging.getLogger("passlib").setLevel(logging.ERROR)

_BOOTSTRAPPED = False


async def _init_memory_scheduler() -> None:
    """初始化记忆调度器"""
    if not is_memory_system_enabled():
        logger.info("记忆系统总开关关闭，跳过记忆调度器初始化")
        return

    from nekro_agent.services.memory.consolidator import consolidate_workspace
    from nekro_agent.services.memory.embedding_service import get_memory_embedding_dimension
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    try:
        await memory_qdrant_manager.ensure_collection(get_memory_embedding_dimension())
    except Exception as e:
        logger.warning(f"记忆系统 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")

    async def consolidation_handler(workspace_id: int, chat_key: str) -> None:
        try:
            result = await consolidate_workspace(workspace_id, chat_key)
            logger.debug(
                f"记忆沉淀完成: workspace={workspace_id}, "
                f"paragraphs={result.paragraphs_created}, "
                f"entities={result.entities_created}",
            )
        except Exception as e:
            logger.warning(f"记忆沉淀失败: workspace={workspace_id}, error={e}")

    memory_scheduler.set_consolidation_handler(consolidation_handler)
    await memory_scheduler.start()


async def _init_kb_collection() -> None:
    """初始化知识库向量 Collection（工作区 + 全局）。"""
    try:
        from nekro_agent.services.kb.index_service import ensure_kb_collection

        await ensure_kb_collection()
    except Exception as e:
        logger.warning(f"知识库 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")

    try:
        from nekro_agent.services.kb.library_index_service import ensure_kb_library_collection

        await ensure_kb_library_collection()
    except Exception as e:
        logger.warning(f"全局知识库 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")


async def _recover_stale_kb_tasks() -> None:
    """恢复因服务重启而卡在非终态的知识库文档/资产，重新调度索引。"""
    try:
        from nekro_agent.models.db_kb_asset import DBKBAsset
        from nekro_agent.models.db_kb_document import DBKBDocument
        from nekro_agent.services.kb.index_service import schedule_rebuild_document
        from nekro_agent.services.kb.library_index_service import schedule_rebuild_asset

        stale_statuses = ["extracting", "indexing", "pending"]

        stale_docs = await DBKBDocument.filter(sync_status__in=stale_statuses).exclude(extract_status="failed").all()
        for doc in stale_docs:
            logger.info(f"恢复非终态知识库文档索引: document_id={doc.id}, status={doc.sync_status}")
            await schedule_rebuild_document(doc)

        stale_assets = await DBKBAsset.filter(sync_status__in=stale_statuses).exclude(extract_status="failed").all()
        for asset in stale_assets:
            logger.info(f"恢复非终态全局知识库资产索引: asset_id={asset.id}, status={asset.sync_status}")
            await schedule_rebuild_asset(asset)

        total = len(stale_docs) + len(stale_assets)
        if total:
            logger.info(f"共恢复 {total} 个非终态知识库索引任务（文档 {len(stale_docs)}，资产 {len(stale_assets)}）")
    except Exception as e:
        logger.warning(f"恢复非终态知识库索引任务失败: {e}")


def _try_get_driver() -> Any:
    try:
        return get_driver()
    except ValueError:
        return None


def bootstrap_nonebot_plugin() -> Any:
    global _BOOTSTRAPPED

    driver = _try_get_driver()
    if driver is None:
        return None
    if _BOOTSTRAPPED:
        return driver.config

    app = get_app()
    mount_middlewares(app)
    mount_api_routes(app)

    @driver.on_startup
    async def on_startup() -> None:
        mark_started()
        app = get_app()

        await init_db()
        await run_db_migrations()
        await init_adapters(app)

        from nekro_agent.services.command.built_in import register_built_in_commands

        register_built_in_commands()
        logger.info("Built-in commands registered")

        await init_plugins()

        from nekro_agent.services.preset_service import init_default_preset

        await init_default_preset()
        logger.info("Default preset initialized")

        try:
            from nekro_agent.services.plugin.collector import plugin_collector
            from nekro_agent.services.plugin.router_manager import plugin_router_manager

            plugin_router_manager.set_app(app)

            plugins_with_router = plugin_collector.get_plugins_with_router()
            success_count = 0
            for plugin in plugins_with_router:
                if plugin_router_manager.mount_plugin_router(plugin):
                    success_count += 1

            logger.info(f"插件路由热挂载完成，成功挂载 {success_count} 个插件的路由")

        except Exception as e:
            logger.exception(f"初始化插件路由管理器失败: {e}")

        await timer_service.start()
        logger.info("Timer service initialized")

        await recurring_timer_service.start()
        logger.info("Recurring timer service initialized")

        await _init_memory_scheduler()
        logger.info("Memory scheduler initialized")

        await _init_kb_collection()
        logger.info("Knowledge base collection initialized")

        await _recover_stale_kb_tasks()
        start_telemetry_task()

        async def _recover_cc_pending() -> None:
            await asyncio.sleep(5)

            try:
                from nekro_agent.services.workspace.container import SandboxContainerManager

                await SandboxContainerManager.recover_on_startup()
                logger.info("[cc_workspace] 工作区容器状态同步完成")
            except Exception as e:
                logger.warning(f"[cc_workspace] 工作区容器状态同步失败（非致命）: {e}")

            try:
                from builtin.cc_workspace.main import recover_pending_cc_results

                await recover_pending_cc_results()
            except ImportError:
                pass
            except Exception as e:
                logger.error(f"[cc_workspace] CC 待投递结果恢复任务失败: {e}")

            if is_memory_system_enabled():
                try:
                    from nekro_agent.services.memory.rebuild import recover_pending_memory_rebuilds

                    await recover_pending_memory_rebuilds()
                except Exception as e:
                    logger.error(f"[memory] 记忆重建恢复任务失败: {e}")

        asyncio.create_task(_recover_cc_pending())

    @driver.on_shutdown
    async def on_shutdown() -> None:
        shutdown_started_at = time.perf_counter()
        mark_shutting_down()
        logger.debug("[shutdown] begin")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping telemetry task")
        await stop_telemetry_task()
        logger.debug(f"[shutdown] telemetry task stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping memory scheduler")
        await memory_scheduler.stop()
        logger.debug(f"[shutdown] memory scheduler stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping recurring timer service")
        await recurring_timer_service.stop()
        logger.debug(f"[shutdown] recurring timer service stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping timer service")
        await timer_service.stop()
        logger.debug(f"[shutdown] timer service stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] cleaning up adapters")
        await cleanup_adapters(get_app())
        logger.debug(f"[shutdown] adapters cleaned up in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] stopping cc result watcher")
            from builtin.cc_workspace.main import shutdown_cc_result_watcher

            await shutdown_cc_result_watcher()
            logger.debug(f"[shutdown] cc result watcher stopped in {time.perf_counter() - step_started_at:.3f}s")
        except ImportError:
            logger.debug("[shutdown] cc result watcher not loaded, skipped")
        except Exception as e:
            logger.warning(f"[cc_workspace] 停止后台结果监听器失败: {e}")

        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] cleaning up plugins")
            from nekro_agent.services.plugin.collector import plugin_collector

            await plugin_collector.cleanup_all_plugins()
            logger.debug(f"[shutdown] plugins cleaned up in {time.perf_counter() - step_started_at:.3f}s")
        except Exception as e:
            logger.exception(f"清理插件时发生错误: {e}")

        logger.debug(f"[shutdown] finished in {time.perf_counter() - shutdown_started_at:.3f}s")
        logger.info("Timer service stopped")

    @driver.on_bot_connect
    async def on_bot_connect(bot: Bot) -> None:
        adapter = bot.adapter.get_name()
        bot_id = bot.self_id
        await send_bot_status_email(adapter, bot_id, True)

    @driver.on_bot_disconnect
    async def on_bot_disconnect(bot: Bot) -> None:
        adapter = bot.adapter.get_name()
        bot_id = bot.self_id
        await send_bot_status_email(adapter, bot_id, False)

    _BOOTSTRAPPED = True
    return driver.config
