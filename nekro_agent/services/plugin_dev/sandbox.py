from __future__ import annotations

import json
import random
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

import aiodocker
import git
from pydantic import BaseModel

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.errors import OperationFailedError, ValidationError
from nekro_agent.services.network.proxy_manager import SystemProxyFeature, build_subprocess_proxy_env, mask_proxy_url
from nekro_agent.services.plugin_dev.config import get_plugin_dev_config
from nekro_agent.services.plugin_dev.paths import (
    PLUGIN_DEV_NEKRO_SOURCE_DIR,
    PLUGIN_DEV_SANDBOX_STATE_PATH,
    PLUGIN_DEV_WORKSPACE_DIR,
)
from nekro_agent.services.plugin_dev.self_check import stage_plugin_candidate
from nekro_agent.services.plugin_dev.versioning import get_version_info, update_source_lock_info
from nekro_agent.services.workspace.client import CCSandboxClient, CCSandboxError
from nekro_agent.services.workspace.container import (
    CONTAINER_WORKSPACE_PATH,
    SandboxContainerManager,
    _get_host_timezone,
    _resolve_nekro_network,
)
from nekro_agent.tools.docker_util import get_self_container

logger = get_sub_logger("plugin_dev_sandbox")

_PLUGIN_DEV_SKILL_NAME = "plugin-dev"
_PLUGIN_DEV_CONTAINER_PREFIX = "nekro-plugin-dev-cc"
_PLUGIN_DEV_SOURCE_CONTAINER_PATH = f"{CONTAINER_WORKSPACE_PATH}/nekro-agent-source"

_PLUGIN_DEV_SKILL = """---
name: plugin-dev
description: NekroAgent 插件开发、修复、迁移与审查规范。处理插件代码时必须使用。
---

# NekroAgent 插件开发规范

你是 NekroAgent 的插件开发专用 Claude Code 沙盒。

## 必须遵守

- 每次处理插件代码前，先读取任务中的版本信息，确认 stable/preview API 对齐。
- 输出完整单文件插件代码，不要输出省略片段。
- 保留用户已有逻辑，除非任务明确要求删除。
- 如果涉及配置，使用 `ConfigBase`。
- 如果涉及 Agent 可调用能力，使用 `NekroPlugin.mount_sandbox_method`。
- 不要假设 GitHub main 等于当前稳定版；以任务注入的版本文件为准。
- 编写插件前必须先参考 `/workspace/nekro-agent-source` 中的插件 API、配置、事件、方法挂载和已有插件示例。
- 所有 import 路径、类名、函数名、装饰器和枚举必须以 `/workspace/nekro-agent-source` 中真实存在的源码为准。
- 使用任何 NekroAgent 包前，必须先在源码中确认该模块和符号存在；不允许凭记忆编造 `nekro_agent.*`、`plugins.*` 或其他内部导入路径。
- 如果找不到可导入的模块或符号，必须改用源码中已存在的等价 API，或在说明中标明无法确认，不能输出会导入失败的代码。
- `/workspace/nekro-agent-source` 是只读参考源码，不要修改它，也不要把它当作输出目录。
- 任务会提供一个可写的插件工作副本路径和插件自检命令。交付最终代码前，先把候选代码写入该工作副本并运行自检命令。
- 若插件自检失败，必须继续修复直到通过；若因环境缺少依赖无法执行自检，必须在最终说明中明确指出。
- 不要直接要求宿主机文件权限；真实文件写入由 NekroAgent 后端 proposal/版本系统完成。

## 输出要求

最终回复必须包含一个 Python 代码块，代码块内容是完整的新插件文件。
代码块之外可以简要说明风险、是否需要重载插件、是否涉及数据迁移。
"""

_PLUGIN_DEV_CLAUDE_MD = """# NekroAgent 插件开发专用沙盒

这是 NekroAgent 内部托管的后台 Claude Code 沙盒，只用于生成、修复、迁移和审查 NekroAgent 插件代码。

## 工作边界

- 当前目录是插件开发工作副本，不是真实插件目录。
- 不要尝试直接访问或写入宿主机插件目录。
- 真实插件文件写入由 NekroAgent 后端的 proposal、版本记录和用户确认流程完成。
- 每次处理插件任务时必须使用 `plugin-dev` skill。
- 编写插件前先查看 `/workspace/nekro-agent-source` 的当前源码，优先参考已有插件 API、配置、事件和方法挂载写法。
- 所有 import 路径和使用到的类、函数、装饰器、枚举必须能在 `/workspace/nekro-agent-source` 中找到对应定义。
- 不允许凭记忆编造内部包路径；无法在源码中确认的导入不要使用。
- `/workspace/nekro-agent-source` 是只读参考源码，不得修改。
- 任务会提供插件工作副本路径和插件自检命令。请先把候选代码写入工作副本，再运行自检命令，确认通过后再输出最终代码。

## 交付要求

- 最终回复输出完整单文件 Python 插件代码。
- 保留用户已有逻辑，除非任务明确要求删除。
- 如果有风险、需要重载插件或涉及数据迁移，在代码块之外简要说明。
"""


class PluginDevSandboxState(BaseModel):
    status: Literal["active", "stopped", "failed"] = "stopped"
    container_name: str | None = None
    container_id: str | None = None
    host_port: int | None = None
    sandbox_api_token: str = ""
    last_error: str | None = None
    create_time: str = ""
    update_time: str = ""

    @property
    def metadata(self) -> dict[str, str]:
        return {"sandbox_api_token": self.sandbox_api_token} if self.sandbox_api_token else {}

    @property
    def api_endpoint(self) -> str:
        if OsEnv.RUN_IN_DOCKER and self.container_name:
            return f"http://{self.container_name}:{config.CC_SANDBOX_INTERNAL_PORT}"
        if self.host_port:
            return f"http://127.0.0.1:{self.host_port}"
        raise ValueError("插件开发沙盒尚无可用的 API 地址")


class PluginDevSandboxService:
    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _load_state() -> PluginDevSandboxState | None:
        if not PLUGIN_DEV_SANDBOX_STATE_PATH.exists():
            return None
        data = json.loads(PLUGIN_DEV_SANDBOX_STATE_PATH.read_text(encoding="utf-8"))
        return PluginDevSandboxState.model_validate(data)

    @staticmethod
    def _save_state(state: PluginDevSandboxState) -> PluginDevSandboxState:
        if not state.create_time:
            state.create_time = PluginDevSandboxService._utc_now()
        state.update_time = PluginDevSandboxService._utc_now()
        PLUGIN_DEV_SANDBOX_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLUGIN_DEV_SANDBOX_STATE_PATH.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return state

    @staticmethod
    def _ensure_state() -> PluginDevSandboxState:
        state = PluginDevSandboxService._load_state()
        if state is None:
            state = PluginDevSandboxState(
                sandbox_api_token=secrets.token_urlsafe(32),
                create_time=PluginDevSandboxService._utc_now(),
            )
        if not state.sandbox_api_token:
            state.sandbox_api_token = secrets.token_urlsafe(32)
        return PluginDevSandboxService._save_state(state)

    @staticmethod
    def _resolve_plugin_dev_preset():
        from nekro_agent.core.cc_model_presets import cc_presets_store

        preset_id = get_plugin_dev_config().cc_model_preset_id
        if preset_id is not None:
            preset = cc_presets_store.get_by_id(int(preset_id))
            if preset:
                return preset
        return cc_presets_store.ensure_default()

    @staticmethod
    def _parse_image_tag(image: str) -> str | None:
        if "@" in image:
            image = image.split("@", 1)[0]
        last_segment = image.rsplit("/", 1)[-1]
        if ":" not in last_segment:
            return None
        return last_segment.rsplit(":", 1)[-1] or None

    @staticmethod
    async def _detect_release_channel() -> Literal["stable", "preview"]:
        try:
            container = await get_self_container()
            info = await container.show()
            name = str(info.get("Name") or "").strip("/")
            image = str(info.get("Config", {}).get("Image") or "")
            if "nekro_agent" not in name and "nekro-agent" not in image:
                return "preview"
            return "stable" if PluginDevSandboxService._parse_image_tag(image) == "latest" else "preview"
        except Exception as e:
            logger.info(f"无法通过 Docker 容器识别 Nekro Agent 版本通道，按预览版处理: {e}")
            return "preview"

    @staticmethod
    def _resolve_ref_commit(repo: git.Repo, source_ref: str, env: dict[str, str]) -> str:
        repo.git.fetch("origin", source_ref, "--tags", env=env)
        candidates = [
            source_ref,
            f"origin/{source_ref}",
            f"refs/tags/{source_ref}",
            f"refs/remotes/origin/{source_ref}",
            "FETCH_HEAD",
        ]
        for candidate in candidates:
            try:
                return str(repo.commit(candidate).hexsha)
            except Exception:
                continue
        return str(repo.git.rev_parse(source_ref))

    @staticmethod
    def _latest_semver_tag(repo: git.Repo, env: dict[str, str]) -> str:
        repo.git.fetch("origin", "--tags", env=env)
        tags = [tag.name for tag in repo.tags]
        semver_tags: list[tuple[tuple[int, int, int], str]] = []
        for tag in tags:
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
            if match:
                semver_tags.append(((int(match.group(1)), int(match.group(2)), int(match.group(3))), tag))
        if not semver_tags:
            raise RuntimeError("未找到可用的正式版 tag")
        semver_tags.sort(key=lambda item: item[0])
        return semver_tags[-1][1]

    @staticmethod
    def _resolve_source_target(
        repo: git.Repo, channel: Literal["stable", "preview"], env: dict[str, str]
    ) -> tuple[str, str, str]:
        if channel == "stable":
            source_ref = PluginDevSandboxService._latest_semver_tag(repo, env)
            return source_ref, source_ref, PluginDevSandboxService._resolve_ref_commit(repo, source_ref, env)
        source_ref = "main"
        return source_ref, "main", PluginDevSandboxService._resolve_ref_commit(repo, source_ref, env)

    @staticmethod
    async def _ensure_reference_source() -> tuple[Path | None, str]:
        plugin_dev_config = get_plugin_dev_config()
        if not plugin_dev_config.source_enabled:
            return None, "Nekro Agent 参考源码未启用"

        source_dir = PLUGIN_DEV_NEKRO_SOURCE_DIR
        channel = await PluginDevSandboxService._detect_release_channel()
        env = build_subprocess_proxy_env(SystemProxyFeature.PLUGIN_UPDATE)
        if env:
            logger.info(f"使用代理 {mask_proxy_url(env.get('HTTPS_PROXY'))} 准备 Nekro Agent 参考源码")

        try:
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            if not source_dir.exists():
                git.Repo.clone_from(
                    plugin_dev_config.source_repo_url,
                    source_dir,
                    no_checkout=True,
                    env=env,
                )

            repo = git.Repo(source_dir)
            source_ref, release, resolved_commit = PluginDevSandboxService._resolve_source_target(repo, channel, env)
            repo.git.checkout(resolved_commit)
            update_source_lock_info(
                repo_url=plugin_dev_config.source_repo_url,
                source_ref=source_ref,
                resolved_commit=resolved_commit,
                channel=channel,
                release=release,
            )
            return source_dir, f"已按 {channel} 锁定 Nekro Agent 参考源码: {source_ref} -> {resolved_commit[:12]}"
        except Exception as e:
            logger.warning(f"准备 Nekro Agent 参考源码失败: {e}")
            if source_dir.exists():
                try:
                    repo = git.Repo(source_dir)
                    cached_commit = str(repo.head.commit.hexsha)
                    fallback_ref = "main" if channel == "preview" else "latest"
                    update_source_lock_info(
                        repo_url=plugin_dev_config.source_repo_url,
                        source_ref=fallback_ref,
                        resolved_commit=cached_commit,
                        channel=channel,
                        release=fallback_ref,
                    )
                    return source_dir, f"参考源码更新失败，继续使用现有缓存: {cached_commit[:12]} ({e})"
                except Exception:
                    return source_dir, f"参考源码更新失败，继续使用现有缓存: {e}"
            return None, f"参考源码准备失败，继续生成: {e}"

    @staticmethod
    def _write_runtime_files() -> None:
        cc_preset = PluginDevSandboxService._resolve_plugin_dev_preset()
        PLUGIN_DEV_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        api_key = cc_preset.auth_token if cc_preset else ""
        base_url = cc_preset.base_url if cc_preset else ""
        model = cc_preset.anthropic_model if cc_preset and cc_preset.model_type == "manual" else ""
        timeout_ms = int(cc_preset.api_timeout_ms) if cc_preset and cc_preset.api_timeout_ms else 300000

        settings: dict[str, Any] = {
            "provider": "anthropic",
            "providers": {
                "anthropic": {
                    "name": "Anthropic",
                    "base_url": base_url,
                    "auth_token": api_key,
                    "model": model,
                }
            },
            "active_provider": "anthropic",
            "timeout_ms": timeout_ms,
        }
        (PLUGIN_DEV_WORKSPACE_DIR / "settings.json").write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if cc_preset:
            claude_dir = PLUGIN_DEV_WORKSPACE_DIR / ".claude"
            claude_dir.mkdir(exist_ok=True)
            (claude_dir / "settings.json").write_text(
                json.dumps(cc_preset.to_config_json(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        default_dir = PLUGIN_DEV_WORKSPACE_DIR / "default"
        default_dir.mkdir(exist_ok=True)
        (default_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
        (default_dir / "CLAUDE.md").write_text(_PLUGIN_DEV_CLAUDE_MD, encoding="utf-8")

        shared_dir = default_dir / "shared"
        shared_dir.mkdir(exist_ok=True)
        shared_dir.chmod(0o777)

        skills_dir = PLUGIN_DEV_WORKSPACE_DIR / ".claude_home" / "skills" / _PLUGIN_DEV_SKILL_NAME
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "SKILL.md").write_text(_PLUGIN_DEV_SKILL, encoding="utf-8")
        (PLUGIN_DEV_WORKSPACE_DIR / ".claude_home").chmod(0o777)

    @staticmethod
    async def _find_free_port() -> int:
        used_ports_raw = await DBWorkspace.filter(host_port__isnull=False).values_list("host_port", flat=True)
        used_ports = {int(p) for p in used_ports_raw if p is not None}
        state = PluginDevSandboxService._load_state()
        if state and state.host_port:
            used_ports.add(state.host_port)

        for _ in range(100):
            port = random.randint(config.CC_SANDBOX_PORT_RANGE_START, config.CC_SANDBOX_PORT_RANGE_END)
            if port not in used_ports:
                return port
        raise RuntimeError("无法在端口段内找到空闲端口")

    @staticmethod
    async def _container_running(container_name: str | None) -> bool:
        if not container_name:
            return False
        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(container_name)
            info = await container.show()
            return bool(info.get("State", {}).get("Running"))
        except Exception:
            return False
        finally:
            await docker.close()

    @staticmethod
    async def _remove_container(container_name: str | None) -> None:
        if not container_name:
            return
        docker = aiodocker.Docker()
        try:
            try:
                container = await docker.containers.get(container_name)
            except Exception:
                return
            try:
                await container.stop(t=10)
            except Exception:
                pass
            try:
                await container.delete(force=True)
            except Exception as e:
                logger.warning(f"删除插件开发沙盒容器失败: {container_name}: {e}")
        finally:
            await docker.close()

    @staticmethod
    async def start() -> PluginDevSandboxState:
        state = PluginDevSandboxService._ensure_state()
        PluginDevSandboxService._write_runtime_files()
        reference_source_dir, reference_source_message = await PluginDevSandboxService._ensure_reference_source()
        logger.info(reference_source_message)

        if state.status == "active" and await PluginDevSandboxService._container_running(state.container_name):
            return state

        await PluginDevSandboxService._remove_container(state.container_name)

        image = f"{config.CC_SANDBOX_IMAGE}:{config.CC_SANDBOX_IMAGE_TAG}"
        if not await SandboxContainerManager.check_image_exists(image):
            raise ValidationError(reason=f"插件开发沙盒镜像 {image} 在本地不存在，请先拉取 CC 沙盒镜像")

        container_name = f"{_PLUGIN_DEV_CONTAINER_PREFIX}-{secrets.token_hex(4)}"
        host_port = await PluginDevSandboxService._find_free_port()
        workspace_host_dir = str(PLUGIN_DEV_WORKSPACE_DIR.resolve())
        claude_home_host_dir = str((PLUGIN_DEV_WORKSPACE_DIR / ".claude_home").resolve())
        host_tz = _get_host_timezone()

        binds = [
            f"{workspace_host_dir}:{CONTAINER_WORKSPACE_PATH}:rw",
            f"{claude_home_host_dir}:/home/appuser/.claude:rw",
        ]
        if reference_source_dir and reference_source_dir.exists():
            binds.append(f"{reference_source_dir.resolve()}:{_PLUGIN_DEV_SOURCE_CONTAINER_PATH}:ro")
        if Path("/etc/localtime").exists():
            binds.append("/etc/localtime:/etc/localtime:ro")

        container_config: dict[str, Any] = {
            "Image": image,
            "HostConfig": {
                "Binds": binds,
                "PortBindings": {
                    f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": str(host_port)}
                    ]
                },
                "RestartPolicy": {"Name": "no"},
            },
            "Env": [
                f"WORKSPACE_ROOT={CONTAINER_WORKSPACE_PATH}",
                f"SETTINGS_PATH={CONTAINER_WORKSPACE_PATH}/settings.json",
                "RUNTIME_POLICY=agent",
                "SKIP_PERMISSIONS=true",
                f"INTERNAL_API_TOKEN={state.sandbox_api_token}",
                f"PORT={config.CC_SANDBOX_INTERNAL_PORT}",
                "HOST=0.0.0.0",
                f"TZ={host_tz}",
            ],
            "ExposedPorts": {f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": {}},
        }

        docker = aiodocker.Docker()
        try:
            docker_network = await _resolve_nekro_network(docker) if OsEnv.RUN_IN_DOCKER else ""
            if docker_network:
                container_config["HostConfig"]["NetworkMode"] = docker_network

            container = await docker.containers.create_or_replace(name=container_name, config=container_config)
            await container.start()
            info = await container.show()
            state.container_name = container_name
            state.container_id = str(info["Id"])[:12]
            state.host_port = host_port
            state.status = "active"
            state.last_error = None
            PluginDevSandboxService._save_state(state)
            logger.info(f"插件开发沙盒容器已启动: {container_name} (port={host_port})")
        finally:
            await docker.close()

        timeout = config.CC_SANDBOX_STARTUP_TIMEOUT
        if not await SandboxContainerManager._wait_healthy(state.api_endpoint, timeout):
            state.status = "failed"
            state.last_error = f"容器启动超时（{timeout}s）"
            PluginDevSandboxService._save_state(state)
            raise RuntimeError(f"插件开发 cc-sandbox 容器健康检查超时: {state.container_name}")
        return state

    @staticmethod
    async def stop() -> PluginDevSandboxState:
        state = PluginDevSandboxService._ensure_state()
        if state.container_name:
            docker = aiodocker.Docker()
            try:
                try:
                    container = await docker.containers.get(state.container_name)
                    await container.stop(t=10)
                except Exception as e:
                    logger.warning(f"停止插件开发沙盒容器失败（忽略）: {state.container_name}: {e}")
            finally:
                await docker.close()
        state.status = "stopped"
        return PluginDevSandboxService._save_state(state)

    @staticmethod
    async def status() -> tuple[str, PluginDevSandboxState | None]:
        state = PluginDevSandboxService._load_state()
        if state is None:
            return "stopped", None
        if state.status == "active":
            if await PluginDevSandboxService._container_running(state.container_name):
                return "running", state
            state.status = "stopped"
            PluginDevSandboxService._save_state(state)
            return "stopped", state
        if state.status == "failed":
            return "failed", state
        return "stopped", state

    @staticmethod
    def sync_settings() -> None:
        PluginDevSandboxService._write_runtime_files()

    @staticmethod
    def prepare_task_workspace(file_path: str, current_code: str) -> str:
        current_root = PLUGIN_DEV_WORKSPACE_DIR / "default" / "current"
        if current_root.exists():
            shutil.rmtree(current_root, ignore_errors=True)
        current_root.mkdir(parents=True, exist_ok=True)
        staged_path = stage_plugin_candidate(file_path, current_code, current_root)
        relative_path = staged_path.relative_to(PLUGIN_DEV_WORKSPACE_DIR / "default")
        return f"{CONTAINER_WORKSPACE_PATH}/default/{relative_path.as_posix()}"

    @staticmethod
    async def cancel_current_task() -> bool:
        sandbox_status, state = await PluginDevSandboxService.status()
        if sandbox_status != "running" or state is None:
            return False
        client = CCSandboxClient(state, timeout=30.0)
        return await client.force_cancel_current_task(workspace_id="default")

    @staticmethod
    async def stream_generate(prompt: str) -> AsyncGenerator[str | dict, None]:
        state = await PluginDevSandboxService.start()
        client = CCSandboxClient(state, timeout=600.0)
        if not await client.health_check():
            raise OperationFailedError(operation="启动插件开发沙盒", detail="沙盒 API 健康检查失败")
        try:
            async for chunk in client.stream_message(
                prompt,
                workspace_id="default",
                source_chat_key="__plugin_dev__",
                env_vars={
                    "NEKRO_PLUGIN_DEV_VERSION": get_version_info().model_dump_json(),
                    "NEKRO_PLUGIN_DEV_WORKSPACE": str(PLUGIN_DEV_WORKSPACE_DIR),
                },
            ):
                yield chunk
        except CCSandboxError as e:
            raise OperationFailedError(operation="执行插件开发任务", detail=str(e)) from e
