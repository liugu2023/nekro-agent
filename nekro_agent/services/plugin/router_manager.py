
"""插件路由管理器

负责插件路由的动态挂载、卸载和热重载功能。
使用正确的 FastAPI 动态路由机制。
"""

import inspect
from functools import wraps
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.routing import BaseRoute

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.plugin.base import NekroPlugin

logger = get_sub_logger("plugin_system")
class PluginRouteMiddleware:
    """插件路由中间件

    用于在路由调用时检查插件是否仍然启用，
    如果插件被禁用，则返回404错误。
    """

    def __init__(self, plugin_key: str, plugin_name: str):
        self.plugin_key = plugin_key
        self.plugin_name = plugin_name

    async def __call__(self, request: Request, call_next):
        """中间件调用逻辑"""
        # 检查插件是否仍然启用
        from nekro_agent.services.plugin.collector import plugin_collector

        plugin = plugin_collector.get_plugin(self.plugin_key)
        if not plugin or not plugin.is_enabled:
            raise HTTPException(status_code=404, detail="Plugin not found or disabled")

        # 插件启用，继续处理请求
        return await call_next(request)


class PluginRouterManager:
    """插件路由管理器

    负责管理插件路由的动态挂载、卸载和热重载。
    使用正确的 FastAPI 动态路由机制，支持运行时添加/移除路由。
    """

    def __init__(self):
        self._app: Optional[FastAPI] = None
        self._mounted_plugins: Set[str] = set()  # 已挂载路由的插件键
        self._plugin_routers: Dict[str, APIRouter] = {}  # 插件路由缓存
        self._mounted_routes: Dict[str, List[BaseRoute]] = {}  # include_router 复制到主应用的实际路由

    def set_app(self, app: FastAPI) -> None:
        """设置FastAPI应用实例"""
        self._app = app
        logger.info("✅ 插件路由管理器已绑定到 FastAPI 应用")

    def mount_plugin_router(self, plugin: NekroPlugin) -> bool:
        """动态挂载插件路由到主应用

        注意：APIRouter 应该使用 include_router 而不是 Mount！
        Mount 是用于挂载整个 FastAPI 子应用的，不是 APIRouter！

        Args:
            plugin: 插件实例

        Returns:
            bool: 是否成功挂载
        """
        if not self._app:
            logger.error("❌ FastAPI应用实例未设置，无法挂载插件路由")
            return False

        if not plugin.is_enabled:
            return False

        plugin_router = plugin.get_plugin_router()
        if not plugin_router:
            return True

        existing_route_ids: Optional[Set[int]] = None
        try:
            # 如果已经挂载，先卸载
            if plugin.key in self._mounted_plugins:
                if not self.unmount_plugin_router(plugin.key):
                    return False

            mount_path = f"/plugins/{plugin.key}"
            self._add_plugin_middleware(plugin_router, plugin.key, plugin.name)
            existing_route_ids = {id(route) for route in self._app.router.routes}
            self._app.include_router(plugin_router, prefix=mount_path, tags=[f"Plugin:{plugin.name}"])
            mounted_routes = [route for route in self._app.router.routes if id(route) not in existing_route_ids]

            # 记录挂载状态
            self._mounted_plugins.add(plugin.key)
            self._plugin_routers[plugin.key] = plugin_router
            self._mounted_routes[plugin.key] = mounted_routes

            logger.info(f"✅ 插件 {plugin.name} 的路由已动态挂载到 {mount_path}")

            self._update_openapi_schema()

        except Exception as e:
            if existing_route_ids is not None:
                self._app.router.routes[:] = [
                    route for route in self._app.router.routes if id(route) in existing_route_ids
                ]
                self._update_openapi_schema()
            logger.exception(f"❌ 挂载插件 {plugin.name} 的路由失败: {e}")
            return False
        else:
            return True

    def _add_plugin_middleware(self, router: APIRouter, plugin_key: str, plugin_name: str) -> None:
        """为插件路由添加中间件，用于检查插件是否启用"""
        for route in router.routes:
            if hasattr(route, "endpoint") and callable(route.endpoint):
                # 保存原始的端点函数
                original_endpoint = getattr(route.endpoint, "__nekro_original_endpoint__", route.endpoint)

                # 使用闭包创建新的端点函数，包含中间件逻辑
                def create_wrapped_endpoint(orig_func: Any, key: str, _name: str):

                    # 保持原始函数的签名
                    @wraps(orig_func)
                    async def wrapped_endpoint(*args, **kwargs):
                        # 检查插件是否仍然启用
                        from nekro_agent.services.plugin.collector import (
                            plugin_collector,
                        )

                        plugin = plugin_collector.get_plugin(key)
                        if not plugin or not plugin.is_enabled:
                            raise HTTPException(status_code=404, detail="Plugin not found or disabled")

                        # 插件启用，调用原始端点
                        if inspect.iscoroutinefunction(orig_func):
                            return await orig_func(*args, **kwargs)
                        return orig_func(*args, **kwargs)

                    setattr(wrapped_endpoint, "__nekro_original_endpoint__", orig_func)
                    return wrapped_endpoint

                # 替换路由的端点函数
                route.endpoint = create_wrapped_endpoint(original_endpoint, plugin_key, plugin_name)

    def unmount_plugin_router(self, plugin_key: str) -> bool:
        """动态卸载插件路由

        🚨 注意：include_router 添加的路由无法简单地移除，
        因为它们被直接合并到主应用的路由表中。
        这是 FastAPI 的设计限制。

        Args:
            plugin_key: 插件键

        Returns:
            bool: 是否成功卸载
        """
        if not self._app:
            logger.error("❌ FastAPI应用实例未设置，无法卸载插件路由")
            return False

        if plugin_key not in self._mounted_plugins and plugin_key not in self._mounted_routes:
            return True

        try:
            mounted_routes = self._mounted_routes.pop(plugin_key, [])
            mounted_route_ids = {id(route) for route in mounted_routes}
            self._app.router.routes[:] = [
                route for route in self._app.router.routes if id(route) not in mounted_route_ids
            ]

            self._mounted_plugins.discard(plugin_key)
            self._plugin_routers.pop(plugin_key, None)

            logger.info(f"插件 {plugin_key} 的 {len(mounted_routes)} 条路由已卸载")
            self._update_openapi_schema()

        except Exception as e:
            logger.exception(f"❌ 卸载插件 {plugin_key} 的路由失败: {e}")
            return False
        else:
            return True

    def _update_openapi_schema(self) -> None:
        """更新OpenAPI文档架构

        清除缓存，让 FastAPI 在下次请求时自动重新生成文档。
        """
        if not self._app:
            return

        # 清除缓存的 OpenAPI schema
        self._app.openapi_schema = None

    def reload_plugin_router(self, plugin: NekroPlugin) -> bool:
        """重载插件路由

        Args:
            plugin: 插件实例

        Returns:
            bool: 是否成功重载
        """
        if not self.unmount_plugin_router(plugin.key):
            return False

        # 清除插件的路由缓存
        plugin._router = None  # noqa: SLF001

        # 重新挂载
        return self.mount_plugin_router(plugin)

    def get_mounted_plugins(self) -> Set[str]:
        """获取已挂载路由的插件键列表"""
        return self._mounted_plugins.copy()

    def is_plugin_mounted(self, plugin_key: str) -> bool:
        """检查插件路由是否已挂载"""
        return plugin_key in self._mounted_plugins

    def refresh_all_plugin_routes(self) -> None:
        """刷新所有插件路由
        """
        if not self._app:
            logger.error("❌ FastAPI应用实例未设置，无法刷新插件路由")
            return

        logger.info("🔄 开始刷新所有插件路由...")

        # 导入插件收集器
        from nekro_agent.services.plugin.collector import plugin_collector

        # 获取所有有路由的插件
        plugins_with_router = plugin_collector.get_plugins_with_router()

        # 卸载所有已挂载的插件路由
        for plugin_key in list(self._mounted_plugins):
            self.unmount_plugin_router(plugin_key)

        # 重新挂载所有启用的插件路由
        success_count = 0
        for plugin in plugins_with_router:
            if self.mount_plugin_router(plugin):
                success_count += 1

        logger.info(f"✅ 插件路由刷新完成，成功挂载 {success_count} 个插件的路由")

    def get_plugins_router_info(self) -> Dict:
        """获取插件路由信息

        Returns:
            Dict: 插件路由信息
        """
        from nekro_agent.services.plugin.collector import plugin_collector

        all_plugins = plugin_collector.get_all_plugins()
        plugins_with_router = []
        detailed_routes = {}

        for plugin in all_plugins:
            if not plugin.is_enabled:
                continue

            plugin_router = plugin.get_plugin_router()
            if plugin_router:
                mount_path = f"/plugins/{plugin.key}"

                # 基本信息
                plugin_info = {
                    "plugin_key": plugin.key,
                    "plugin_name": plugin.name,
                    "mount_path": mount_path,
                    "enabled": plugin.is_enabled,
                    "mounted": plugin.key in self._mounted_plugins,  # 添加挂载状态
                }
                plugins_with_router.append(plugin_info)

                # 详细路由信息
                routes = []
                for route in plugin_router.routes:
                    # 安全地获取路由属性
                    route_info = {
                        "name": getattr(route, "name", "unnamed"),
                        "path": getattr(route, "path", "unknown"),
                        "methods": list(getattr(route, "methods", [])),
                    }
                    routes.append(route_info)

                detailed_routes[plugin.key] = {
                    "plugin_name": plugin.name,
                    "plugin_description": plugin.description,
                    "mount_path": mount_path,
                    "routes_count": len(routes),
                    "routes": routes,
                    "mounted": plugin.key in self._mounted_plugins,  # 添加挂载状态
                }

        return {
            "total_plugins": len(all_plugins),
            "plugins_with_router": len(plugins_with_router),
            "mounted_count": len(self._mounted_plugins),  # 添加已挂载数量
            "router_summary": plugins_with_router,
            "detailed_routes": detailed_routes,
        }

    def debug_routes(self) -> List[str]:
        """调试当前应用的所有路由信息"""
        if not self._app:
            logger.error("❌ FastAPI应用实例未设置，无法调试路由")
            return []

        logger.info("=== 开始调试应用路由信息 ===")
        logger.info(f"应用路由总数: {len(self._app.router.routes)}")

        plugin_routes = []
        for i, route in enumerate(self._app.router.routes):
            route_path = getattr(route, "path", "unknown")
            route_info = f"{i}: {route_path} - {type(route).__name__}"

            if hasattr(route, "methods"):
                methods = getattr(route, "methods", set())
                route_info += f" [{', '.join(methods)}]"

            # 检查是否是插件路由
            if hasattr(route, "path") and "plugins" in str(getattr(route, "path", "")):
                plugin_routes.append(str(route_path))
                logger.info(f"    🔍 发现插件路由: {route_path}")

        logger.info(f"=== 调试完成，发现 {len(plugin_routes)} 个插件路由 ===")
        return plugin_routes

    def verify_plugin_routes(self, plugin_key: str) -> List[str]:
        """验证指定插件的路由是否正确挂载

        Args:
            plugin_key: 插件键

        Returns:
            List[str]: 找到的插件路由路径列表
        """
        if not self._app:
            logger.error("❌ FastAPI应用实例未设置，无法验证路由")
            return []

        target_prefix = f"/plugins/{plugin_key}"
        found_routes = []

        logger.info(f"🔍 验证插件 {plugin_key} 的路由...")
        logger.info(f"目标路径前缀: {target_prefix}")

        for route in self._app.router.routes:
            if hasattr(route, "path"):
                route_path = str(getattr(route, "path", ""))
                if route_path.startswith(target_prefix):
                    found_routes.append(route_path)
                    methods = getattr(route, "methods", set())
                    logger.info(f"    ✅ 找到路由: {route_path} [{', '.join(methods)}]")

        if found_routes:
            logger.success(f"验证完成，插件 {plugin_key} 共有 {len(found_routes)} 个路由已挂载")
        else:
            logger.warning(f"⚠️  插件 {plugin_key} 没有找到任何挂载的路由！")

        return found_routes


# 全局插件路由管理器实例
plugin_router_manager = PluginRouterManager()
