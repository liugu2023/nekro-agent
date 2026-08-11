from __future__ import annotations

import asyncio
import json
import random
import secrets
import shlex
import shutil
import socket
import tomllib
from contextlib import suppress
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
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
from nekro_agent.services.plugin_dev.config import get_plugin_dev_config
from nekro_agent.services.plugin_dev.paths import (
    PLUGIN_DEV_NEKRO_SOURCE_DIR,
    PLUGIN_DEV_SANDBOX_STATE_PATH,
    PLUGIN_DEV_WORKSPACE_DIR,
)
from nekro_agent.services.plugin_dev.self_check import normalize_check_relative_path, stage_plugin_candidate
from nekro_agent.services.plugin_dev.versioning import get_version_info, update_source_lock_info
from nekro_agent.services.workspace.client import CCSandboxClient, CCSandboxError
from nekro_agent.services.workspace.container import (
    CONTAINER_WORKSPACE_PATH,
    SandboxContainerManager,
    _get_host_timezone,
    _resolve_nekro_network,
)

logger = get_sub_logger("plugin_dev_sandbox")

_PLUGIN_DEV_SKILL_NAME = "plugin-dev"
_PLUGIN_DEV_CONTAINER_PREFIX = "nekro-plugin-dev-cc"
_PLUGIN_DEV_SOURCE_CONTAINER_PATH = f"{CONTAINER_WORKSPACE_PATH}/nekro-agent-source"
_PLUGIN_DEV_SKILL_PATH = Path(__file__).with_name("plugin_dev_skill.md")
_RUNTIME_SOURCE_ENTRIES = (
    "nekro_agent",
    "plugins",
    "run_nekro_cli.py",
    "pyproject.toml",
    "README.md",
    "README_en.md",
    "LICENSE",
    "migrations",
)
_RUNTIME_SOURCE_IGNORE_PATTERNS = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".basedpyright",
    ".venv",
    "node_modules",
    "dist",
    "build",
    ".git",
)

_PLUGIN_DEV_CLAUDE_MD = """# NekroAgent 插件开发专用沙盒

这是 NekroAgent 内部托管的后台 Claude Code 沙盒，只用于生成、修复、迁移和审查 NekroAgent 插件代码。

## 工作边界

- 当前目录是插件开发工作副本，不是真实插件目录。
- 不要尝试直接访问或写入宿主机插件目录。
- 真实插件文件写入由 NekroAgent 后端的 proposal、版本记录和用户确认流程完成。
- 每次处理插件任务时必须使用 `plugin-dev` skill。
- 编写插件前先查看 `/workspace/nekro-agent-source` 的当前源码，优先参考已有插件 API、配置、事件和方法挂载写法；该目录是宿主 NekroAgent 当前运行环境的本地快照。
- 所有 import 路径和使用到的类、函数、装饰器、枚举必须能在 `/workspace/nekro-agent-source` 中找到对应定义。
- 不允许凭记忆编造内部包路径；无法在源码中确认的导入不要使用。
- `/workspace/nekro-agent-source` 是只读参考源码，不得修改。
- 不要自行联网拉取 GitHub main、latest 或最新 tag 作为参考；如果版本信息标记 `source_dirty`，仍以该本地快照为准。
- 任务会提供插件工作副本路径和插件自检命令。候选代码必须先写入工作副本，再由你在 CC 沙盒里运行该自检命令；只在回复里粘贴代码不算交付。
- 单文件任务只修改目标文件本身；包形式任务（目标文件在某个目录下）可以在该包目录内创建或修改多个 .py 模块文件，但不要在包目录之外创建文件。
- 包形式插件的入口 `__init__.py` 必须存在模块级 `plugin` 实例（可以 `from .plugin import plugin`）。
- 自检命令执行的是静态检查（语法、导入可用性、插件结构），不会运行插件代码；完整加载检查在用户确认应用提案时由后端执行。
- 只有 CC 沙盒自检通过后，才能调用内部网关创建 proposal；不要在自检前创建 proposal。如果无法调用网关，也必须确保工作副本里已经是最终候选代码。
- 包任务调用内部 `/check` 或 `/proposals` 时，`files` 只包含当前仍存在的完整 `.py` 文件；删除的包内文件必须放入 `deleted_files` 字符串数组，不能只从 `files` 中省略。
- 如需读取真实插件文件或提交写入提案，使用 `NEKRO_PLUGIN_DEV_INTERNAL_API_BASE`，请求头带 `X-Internal-API-Token: $INTERNAL_API_TOKEN`。
- 内部网关提供版本、文件列表、文件读取、静态自检和 proposal 创建能力，真实写入仍由 NekroAgent 后端和用户确认完成。

## 交付要求

- 单文件任务：最终交付完整可运行的单文件插件代码；包任务：交付包目录下完整一致的模块文件集。
- 保留用户已有逻辑，除非任务明确要求删除。
- 如果有风险、需要重载插件或涉及数据迁移，在代码块之外简要说明。
"""

_PLUGIN_DEV_CHECK_HELPER = r'''from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _candidate_bases() -> list[str]:
    raw_base = os.environ.get("NEKRO_PLUGIN_DEV_INTERNAL_API_BASE", "").rstrip("/")
    bases = [raw_base] if raw_base else []
    fallback = raw_base.replace("host.docker.internal", "172.17.0.1")
    if fallback and fallback not in bases:
        bases.append(fallback)
    return bases


def _fallback_report(candidate_path: str, detail: str) -> dict:
    return {
        "ok": False,
        "level": "static",
        "candidate_path": candidate_path,
        "checks": [
            {
                "id": "internal_gateway_error",
                "title": "调用插件自检网关",
                "ok": False,
                "detail": detail,
                "error": detail,
            }
        ],
        "warnings": [],
        "errors": [detail],
        "load_failures": [],
    }


def _collect_candidate_files(candidate_path: str, file_path: str) -> tuple[str, list[dict]]:
    """收集候选文件集。candidate_path 为目录时收集包下全部 .py，返回 (主文件内容, files)。"""
    if not os.path.isdir(candidate_path):
        with open(candidate_path, "r", encoding="utf-8") as file:
            content = file.read()
        return content, [{"file_path": file_path, "content": content}]

    top_dir = file_path.replace("\\", "/").split("/")[0]
    files = []
    for dirpath, dirnames, filenames in os.walk(candidate_path):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full_path = os.path.join(dirpath, filename)
            rel_in_pkg = os.path.relpath(full_path, candidate_path).replace(os.sep, "/")
            with open(full_path, "r", encoding="utf-8") as file:
                files.append({"file_path": f"{top_dir}/{rel_in_pkg}", "content": file.read()})
    if not files:
        raise OSError(f"候选目录中没有 .py 文件: {candidate_path}")
    primary = next((item for item in files if item["file_path"] == file_path), files[0])
    return primary["content"], files


def main() -> int:
    if len(sys.argv) < 4:
        print(json.dumps(_fallback_report("", "用法: plugin_dev_check.py <candidate_path> <file_path> <task_id> [level]"), ensure_ascii=False, indent=2))
        return 2

    candidate_path = sys.argv[1]
    file_path = sys.argv[2]
    task_id = sys.argv[3]
    level = sys.argv[4] if len(sys.argv) > 4 else "static"
    token = os.environ.get("INTERNAL_API_TOKEN", "")

    try:
        content, candidate_files = _collect_candidate_files(candidate_path, file_path)
    except OSError as exc:
        print(json.dumps(_fallback_report(candidate_path, f"读取候选文件失败: {exc}"), ensure_ascii=False, indent=2))
        return 1

    payload = json.dumps(
        {
            "task_id": task_id,
            "file_path": file_path,
            "content": content,
            "level": level,
            "files": candidate_files,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    last_error = ""
    for base in _candidate_bases():
        req = urllib.request.Request(
            base + "/check",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Internal-API-Token": token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
                print(body)
                try:
                    return 0 if json.loads(body).get("ok") else 1
                except json.JSONDecodeError:
                    return 1
        except urllib.error.HTTPError as exc:
            last_error = exc.read().decode("utf-8", errors="replace") or str(exc)
        except Exception as exc:
            last_error = str(exc)

    print(json.dumps(_fallback_report(candidate_path, f"插件自检网关调用失败: {last_error or 'missing NEKRO_PLUGIN_DEV_INTERNAL_API_BASE'}"), ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _load_plugin_dev_skill() -> str:
    return _PLUGIN_DEV_SKILL_PATH.read_text(encoding="utf-8")


class PluginDevSandboxState(BaseModel):
    status: Literal["active", "stopped", "failed"] = "stopped"
    container_name: str | None = None
    container_id: str | None = None
    host_port: int | None = None
    sandbox_api_token: str = ""
    last_error: str | None = None
    create_time: str = ""
    update_time: str = ""
    # 容器创建时是否挂载了参考源码；用于判断运行中容器的挂载是否过期，
    # 快照准备失败创建的无源码容器不会因此被反复重建
    reference_source_mounted: bool = False

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


class PluginDevSandboxRuntimeInfo(BaseModel):
    container_name: str = ""
    container_id: str = ""
    api_endpoint: str = ""
    healthy: bool = False
    preset_id: int | None = None
    preset_name: str = ""
    model_type: str = ""
    model_label: str = ""
    tools: list[str] = []


class PluginDevSandboxService:
    _lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _load_state() -> PluginDevSandboxState | None:
        if not PLUGIN_DEV_SANDBOX_STATE_PATH.exists():
            return None
        try:
            data = json.loads(PLUGIN_DEV_SANDBOX_STATE_PATH.read_text(encoding="utf-8"))
            return PluginDevSandboxState.model_validate(data)
        except Exception as e:
            # 状态文件损坏（写入中途被杀等）不能让状态查询、启动与内部网关鉴权全部 500；
            # 隔离损坏文件后按新状态重建，此时运行中的容器持有旧 token，需要重启沙盒
            logger.warning(
                f"插件开发沙盒状态文件损坏，将隔离并重建（运行中的沙盒需重启才能恢复内部网关鉴权）: "
                f"{PLUGIN_DEV_SANDBOX_STATE_PATH}: {e}",
            )
            with suppress(OSError):
                PLUGIN_DEV_SANDBOX_STATE_PATH.replace(PLUGIN_DEV_SANDBOX_STATE_PATH.with_suffix(".json.corrupted"))
            return None

    @staticmethod
    def _save_state(state: PluginDevSandboxState) -> PluginDevSandboxState:
        if not state.create_time:
            state.create_time = PluginDevSandboxService._utc_now()
        state.update_time = PluginDevSandboxService._utc_now()
        PLUGIN_DEV_SANDBOX_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：直接覆盖写时进程若在截断后被杀，状态文件会永久损坏
        temp_path = PLUGIN_DEV_SANDBOX_STATE_PATH.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(PLUGIN_DEV_SANDBOX_STATE_PATH)
        return state

    @staticmethod
    def _ensure_state() -> PluginDevSandboxState:
        state = PluginDevSandboxService._load_state()
        needs_save = False
        if state is None:
            state = PluginDevSandboxState(
                sandbox_api_token=secrets.token_urlsafe(32),
                create_time=PluginDevSandboxService._utc_now(),
            )
            needs_save = True
        if not state.sandbox_api_token:
            state.sandbox_api_token = secrets.token_urlsafe(32)
            needs_save = True
        # 内部网关每个请求都会经过这里取 token：状态未变化时不重写文件，避免纯粹的磁盘 churn
        if needs_save:
            return PluginDevSandboxService._save_state(state)
        return state

    @staticmethod
    def get_internal_api_token() -> str:
        return PluginDevSandboxService._ensure_state().sandbox_api_token

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
    def _describe_plugin_dev_preset() -> dict[str, Any]:
        preset = PluginDevSandboxService._resolve_plugin_dev_preset()
        if preset.model_type == "preset":
            model_label = preset.preset_model or "preset"
        else:
            model_label = preset.anthropic_model or "(未配置手动模型)"
        return {
            "preset_id": preset.id,
            "preset_name": preset.name,
            "model_type": preset.model_type,
            "model_label": model_label,
        }

    @staticmethod
    def _runtime_source_root() -> Path:
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _read_runtime_app_version(runtime_root: Path) -> str:
        pyproject_path = runtime_root / "pyproject.toml"
        if pyproject_path.exists():
            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                version = data.get("project", {}).get("version")
                if isinstance(version, str) and version:
                    return version
            except Exception:
                pass
        try:
            return importlib_metadata.version("nekro-agent")
        except importlib_metadata.PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _runtime_git_info(runtime_root: Path) -> tuple[str, str, bool, bool]:
        try:
            repo = git.Repo(runtime_root, search_parent_directories=True)
            commit = str(repo.head.commit.hexsha)
            dirty = repo.is_dirty(untracked_files=True)
            try:
                ref = str(repo.active_branch.name)
            except TypeError:
                ref = "detached"
            exact_tag = next((tag.name for tag in repo.tags if tag.commit == repo.head.commit), "")
            return commit, exact_tag or ref, dirty, bool(exact_tag)
        except Exception:
            return "", "", False, False

    @staticmethod
    def _copy_runtime_source_entry(runtime_root: Path, snapshot_root: Path, entry_name: str) -> bool:
        source = runtime_root / entry_name
        if not source.exists():
            return False

        target = snapshot_root / entry_name
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns(*_RUNTIME_SOURCE_IGNORE_PATTERNS),
                symlinks=True,
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return True

    @staticmethod
    def _copy_runtime_source_snapshot(runtime_root: Path, snapshot_root: Path) -> None:
        copied = False
        snapshot_root.mkdir(parents=True, exist_ok=True)
        for entry_name in _RUNTIME_SOURCE_ENTRIES:
            copied = PluginDevSandboxService._copy_runtime_source_entry(runtime_root, snapshot_root, entry_name) or copied
        if not copied:
            raise RuntimeError(f"未能从运行目录复制任何参考源码: {runtime_root}")
        if not (snapshot_root / "nekro_agent").exists():
            raise RuntimeError(f"参考源码快照缺少 nekro_agent 包: {runtime_root}")
        if not (snapshot_root / "run_nekro_cli.py").exists():
            raise RuntimeError(f"参考源码快照缺少 run_nekro_cli.py: {runtime_root}")

    @staticmethod
    def _replace_directory_contents_preserve_root(source_dir: Path, temp_dir: Path) -> None:
        if not source_dir.exists():
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_dir), str(source_dir))
            return

        source_dir.mkdir(parents=True, exist_ok=True)
        for child in source_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        for child in temp_dir.iterdir():
            shutil.move(str(child), str(source_dir / child.name))
        shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _prepare_runtime_source_snapshot() -> tuple[Path, str]:
        runtime_root = PluginDevSandboxService._runtime_source_root()
        source_dir = PLUGIN_DEV_NEKRO_SOURCE_DIR
        temp_dir = source_dir.with_name(f"{source_dir.name}.tmp-{secrets.token_hex(4)}")

        app_version = PluginDevSandboxService._read_runtime_app_version(runtime_root)
        commit, ref, dirty, exact_tag = PluginDevSandboxService._runtime_git_info(runtime_root)
        channel: Literal["stable", "preview"] = "stable" if app_version != "unknown" and not dirty and exact_tag else "preview"
        release = app_version if app_version != "unknown" else ref

        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            PluginDevSandboxService._copy_runtime_source_snapshot(runtime_root, temp_dir)
            PluginDevSandboxService._replace_directory_contents_preserve_root(source_dir, temp_dir)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

        notes = "参考源码来自当前运行环境的本地快照；无需访问 GitHub，且与宿主实际加载的插件 API 保持一致。"
        if dirty:
            notes += " 当前运行源码包含未提交修改。"
        update_source_lock_info(
            repo_url="",
            source_ref=ref or release or "runtime",
            resolved_commit=commit,
            channel=channel,
            release=release,
            source_origin="runtime_snapshot",
            source_path=str(source_dir.resolve()),
            source_dirty=dirty,
            notes=notes,
        )
        commit_label = commit[:12] if commit else "no-git"
        dirty_label = " dirty" if dirty else ""
        return source_dir, f"已生成 Nekro Agent 本地运行环境源码快照: {commit_label}{dirty_label}"

    @staticmethod
    async def _ensure_reference_source() -> tuple[Path | None, str]:
        plugin_dev_config = get_plugin_dev_config()
        if not plugin_dev_config.source_enabled:
            update_source_lock_info(
                repo_url="",
                source_ref="disabled",
                resolved_commit="",
                channel="preview",
                release="",
                source_origin="disabled",
                source_path="",
                source_dirty=False,
                notes="Nekro Agent 参考源码已在插件开发配置中禁用。",
            )
            return None, "Nekro Agent 参考源码未启用"

        try:
            # 快照包含 git 状态检查与整树拷贝，属同步重活，放线程避免阻塞事件循环
            return await asyncio.to_thread(PluginDevSandboxService._prepare_runtime_source_snapshot)
        except Exception as e:
            logger.warning(f"准备 Nekro Agent 参考源码失败: {e}")
            if PLUGIN_DEV_NEKRO_SOURCE_DIR.exists():
                update_source_lock_info(
                    repo_url="",
                    source_ref="cached-runtime",
                    resolved_commit="",
                    channel="preview",
                    release="cached-runtime",
                    source_origin="cached_runtime",
                    source_path=str(PLUGIN_DEV_NEKRO_SOURCE_DIR.resolve()),
                    source_dirty=True,
                    notes=f"本地运行环境源码快照刷新失败，继续使用已有缓存: {e}",
                )
                return PLUGIN_DEV_NEKRO_SOURCE_DIR, f"参考源码快照刷新失败，继续使用已有缓存: {e}"
            update_source_lock_info(
                repo_url="",
                source_ref="unavailable",
                resolved_commit="",
                channel="preview",
                release="",
                source_origin="unavailable",
                source_path="",
                source_dirty=False,
                notes=f"参考源码不可用: {e}",
            )
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
        check_helper_path = default_dir / "plugin_dev_check.py"
        check_helper_path.write_text(_PLUGIN_DEV_CHECK_HELPER, encoding="utf-8")
        check_helper_path.chmod(0o777)

        shared_dir = default_dir / "shared"
        shared_dir.mkdir(exist_ok=True)
        shared_dir.chmod(0o777)

        skills_dir = PLUGIN_DEV_WORKSPACE_DIR / ".claude_home" / "skills" / _PLUGIN_DEV_SKILL_NAME
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "SKILL.md").write_text(_load_plugin_dev_skill(), encoding="utf-8")
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
            if port in used_ports:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", port))
            except OSError:
                used_ports.add(port)
                continue
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
    async def _container_file_exists(container_name: str | None, file_path: str) -> bool:
        if not container_name:
            return False
        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(container_name)
            exec_inst = await container.exec(["test", "-f", file_path], stdout=True, stderr=True)
            # aiodocker 的 Stream 不支持 async for，只能通过 read_out() 读到 EOF
            async with exec_inst.start(detach=False) as stream:
                while await stream.read_out() is not None:
                    pass
            info = await exec_inst.inspect()
            return int(info.get("ExitCode", 1)) == 0
        except Exception as e:
            logger.warning(f"检查插件开发沙盒容器文件失败: {container_name}: {file_path}: {e}")
            return False
        finally:
            await docker.close()

    @staticmethod
    async def start() -> PluginDevSandboxState:
        async with PluginDevSandboxService._lifecycle_lock:
            return await PluginDevSandboxService._start_unlocked()

    @staticmethod
    async def _start_unlocked() -> PluginDevSandboxState:
        state = PluginDevSandboxService._ensure_state()
        PluginDevSandboxService._write_runtime_files()

        if state.status == "active" and await PluginDevSandboxService._container_running(state.container_name):
            source_stale = (
                get_plugin_dev_config().source_enabled
                and state.reference_source_mounted
                and not await PluginDevSandboxService._container_file_exists(
                    state.container_name,
                    f"{_PLUGIN_DEV_SOURCE_CONTAINER_PATH}/run_nekro_cli.py",
                )
            )
            # 容器 Running 不代表沙盒 API 可用（API 进程被 OOM 杀死而 PID1 存活时，
            # 容器层仍是 Running）。不做健康检查就早退会让坏沙盒永远不被重建，
            # 之后每个任务都在健康检查处失败，只能由管理员手动 stop 才能恢复。
            api_dead = not source_stale and not await CCSandboxClient(state, timeout=10.0).health_check()
            if not source_stale and not api_dead:
                return state
            logger.warning(
                "插件开发沙盒参考源码挂载已过期，正在重建容器"
                if source_stale
                else "插件开发沙盒容器仍在运行但 API 健康检查失败，正在重建容器",
            )
            await PluginDevSandboxService._remove_container(state.container_name)
            state.status = "stopped"
            PluginDevSandboxService._save_state(state)

        # 参考源码快照只在确实需要（重）建容器时刷新，避免每次 start() 都全量拷贝
        reference_source_dir, reference_source_message = await PluginDevSandboxService._ensure_reference_source()
        logger.info(reference_source_message)

        await PluginDevSandboxService._remove_container(state.container_name)

        # 每次新建容器时轮换内部网关 token，旧容器（及其可能泄漏的 token）随之失效
        state.sandbox_api_token = secrets.token_urlsafe(32)
        PluginDevSandboxService._save_state(state)

        image = f"{config.CC_SANDBOX_IMAGE}:{config.CC_SANDBOX_IMAGE_TAG}"
        if not await SandboxContainerManager.check_image_exists(image):
            raise ValidationError(reason=f"插件开发沙盒镜像 {image} 在本地不存在，请先拉取 CC 沙盒镜像")

        container_name = f"{_PLUGIN_DEV_CONTAINER_PREFIX}-{secrets.token_hex(4)}"
        host_port = await PluginDevSandboxService._find_free_port()
        workspace_host_dir = str(PLUGIN_DEV_WORKSPACE_DIR.resolve())
        claude_home_host_dir = str((PLUGIN_DEV_WORKSPACE_DIR / ".claude_home").resolve())
        host_tz = _get_host_timezone()
        internal_api_base = f"{config.SANDBOX_CHAT_API_URL.rstrip('/')}/internal/plugin-dev"

        binds = [
            f"{workspace_host_dir}:{CONTAINER_WORKSPACE_PATH}:rw",
            f"{claude_home_host_dir}:/home/appuser/.claude:rw",
        ]
        reference_source_mounted = bool(reference_source_dir and reference_source_dir.exists())
        if reference_source_dir and reference_source_mounted:
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
                f"NEKRO_PLUGIN_DEV_INTERNAL_API_BASE={internal_api_base}",
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

            try:
                container = await docker.containers.create_or_replace(name=container_name, config=container_config)
                await container.start()
                info = await container.show()
            except Exception:
                # state 尚未记录新容器名，启动失败必须就地回收，否则容器永久泄漏
                await PluginDevSandboxService._remove_container(container_name)
                raise
            state.container_name = container_name
            state.container_id = str(info["Id"])[:12]
            state.host_port = host_port
            state.reference_source_mounted = reference_source_mounted
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
        async with PluginDevSandboxService._lifecycle_lock:
            return await PluginDevSandboxService._stop_unlocked()

    @staticmethod
    async def _stop_unlocked() -> PluginDevSandboxState:
        state = PluginDevSandboxService._ensure_state()
        if state.container_name:
            docker = aiodocker.Docker()
            try:
                try:
                    container = await docker.containers.get(state.container_name)
                    await container.stop(t=10)
                except Exception as e:
                    if await PluginDevSandboxService._container_running(state.container_name):
                        logger.warning(f"停止插件开发沙盒容器失败: {state.container_name}: {e}")
                        state.status = "active"
                        state.last_error = f"停止容器失败: {e}"
                        return PluginDevSandboxService._save_state(state)
                    # 容器已不存在（如被外部清理），期望结果即已停止，继续走停止流程轮换 token
                    logger.info(f"插件开发沙盒容器已不存在，按已停止处理: {state.container_name}")
            finally:
                await docker.close()
        state.status = "stopped"
        state.last_error = None
        # 停止成功后立即轮换内部网关 token，使旧容器持有的凭据失效。
        state.sandbox_api_token = secrets.token_urlsafe(32)
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
    def build_self_check_command(candidate_path: str, file_path: str, task_id: str, level: str = "static") -> str:
        helper_path = f"{CONTAINER_WORKSPACE_PATH}/default/plugin_dev_check.py"
        args = [
            "python",
            helper_path,
            candidate_path,
            file_path,
            task_id,
            level,
        ]
        return " ".join(shlex.quote(arg) for arg in args)

    @staticmethod
    def workspace_current_container_root() -> str:
        return f"{CONTAINER_WORKSPACE_PATH}/default/current"

    @staticmethod
    def prepare_task_workspace(
        file_path: str,
        current_code: str,
        extra_files: "dict[str, str] | None" = None,
        deleted_files: "set[str] | None" = None,
    ) -> str:
        current_root = PLUGIN_DEV_WORKSPACE_DIR / "default" / "current"
        if current_root.exists():
            shutil.rmtree(current_root, ignore_errors=True)
        current_root.mkdir(parents=True, exist_ok=True)
        stage_plugin_candidate(
            file_path,
            current_code,
            current_root,
            extra_files=extra_files,
            deleted_files=deleted_files,
        )
        PluginDevSandboxService._make_tree_writable_for_sandbox(current_root)
        # 主文件容器路径始终返回文件本身（包任务时 staging 入口是目录，这里换算回主文件）
        relative_file = normalize_check_relative_path(file_path)
        return f"{CONTAINER_WORKSPACE_PATH}/default/current/{relative_file.as_posix()}"

    @staticmethod
    def _make_tree_writable_for_sandbox(path: Path) -> None:
        if not path.exists():
            return
        if path.is_dir():
            path.chmod(0o777)
            for child in path.rglob("*"):
                if child.is_dir():
                    child.chmod(0o777)
                else:
                    child.chmod(0o666)
            return
        path.chmod(0o666)

    @staticmethod
    def resolve_workspace_host_path(container_path: str) -> Path:
        workspace_prefix = f"{CONTAINER_WORKSPACE_PATH}/"
        normalized = container_path.strip()
        if not normalized.startswith(workspace_prefix):
            raise ValidationError(reason=f"不是插件开发沙盒工作区路径: {container_path}")
        relative_path = Path(normalized.removeprefix(workspace_prefix))
        host_path = PLUGIN_DEV_WORKSPACE_DIR / relative_path
        workspace_root = PLUGIN_DEV_WORKSPACE_DIR.resolve()
        resolved_path = host_path.resolve()
        try:
            resolved_path.relative_to(workspace_root)
        except ValueError as e:
            raise ValidationError(reason=f"插件工作副本路径越界: {container_path}") from e
        return resolved_path

    @staticmethod
    async def cancel_current_task() -> bool:
        sandbox_status, state = await PluginDevSandboxService.status()
        if sandbox_status != "running" or state is None:
            return False
        client = CCSandboxClient(state, timeout=30.0)
        return await client.force_cancel_current_task(workspace_id="default")

    @staticmethod
    async def inspect_runtime(*, refresh_tools: bool = False) -> PluginDevSandboxRuntimeInfo:
        state = await PluginDevSandboxService.start()
        client = CCSandboxClient(state, timeout=60.0)
        healthy = await client.health_check()
        tools = []
        if healthy:
            tools = await client.refresh_tools() if refresh_tools else await client.get_tools()
        preset_info = PluginDevSandboxService._describe_plugin_dev_preset()
        return PluginDevSandboxRuntimeInfo(
            container_name=state.container_name or "",
            container_id=state.container_id or "",
            api_endpoint=state.api_endpoint,
            healthy=healthy,
            preset_id=preset_info["preset_id"],
            preset_name=preset_info["preset_name"],
            model_type=preset_info["model_type"],
            model_label=preset_info["model_label"],
            tools=tools,
        )

    @staticmethod
    async def stream_generate(prompt: str) -> AsyncGenerator[str | dict, None]:
        state = await PluginDevSandboxService.start()
        client = CCSandboxClient(state, timeout=600.0)
        if not await client.health_check():
            raise OperationFailedError(operation="启动插件开发沙盒", detail="沙盒 API 健康检查失败")
        internal_api_base = f"{config.SANDBOX_CHAT_API_URL.rstrip('/')}/internal/plugin-dev"
        try:
            async for chunk in client.stream_message(
                prompt,
                workspace_id="default",
                source_chat_key="__plugin_dev__",
                env_vars={
                    "INTERNAL_API_TOKEN": state.sandbox_api_token,
                    "NEKRO_PLUGIN_DEV_INTERNAL_API_BASE": internal_api_base,
                    "NEKRO_PLUGIN_DEV_VERSION": get_version_info().model_dump_json(),
                    "NEKRO_PLUGIN_DEV_WORKSPACE": str(PLUGIN_DEV_WORKSPACE_DIR),
                },
            ):
                yield chunk
        except CCSandboxError as e:
            raise OperationFailedError(operation="执行插件开发任务", detail=str(e)) from e
