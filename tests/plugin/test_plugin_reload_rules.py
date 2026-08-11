from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_normalize_plugin_module_name_accepts_common_forms():
    from nekro_agent.services.plugin.collector import PluginCollector

    normalize = PluginCollector.normalize_plugin_module_name
    assert normalize("demo") == "demo"
    assert normalize("demo.py") == "demo"
    assert normalize("demo.py.disabled") == "demo"
    assert normalize("mypkg/__init__.py") == "mypkg"
    assert normalize("mypkg/plugin.py") == "mypkg"
    assert normalize("mypkg/sub/mod.py") == "mypkg"
    assert normalize("mypkg\\plugin.py") == "mypkg"
    assert normalize("/mypkg/plugin.py") == "mypkg"

    for bad_name in ("", "   ", "..", "../evil.py", "./x.py", ".py"):
        with pytest.raises(ValueError, match="非法"):
            normalize(bad_name)


def _make_collector_with_dirs(tmp_path: Path):
    from nekro_agent.services.plugin.collector import PluginCollector

    collector = PluginCollector()
    collector.builtin_plugin_dir = tmp_path / "builtin"
    collector.workdir_plugin_dir = tmp_path / "workdir"
    collector.packages_dir = tmp_path / "packages"
    for base_dir in (collector.builtin_plugin_dir, collector.workdir_plugin_dir, collector.packages_dir):
        base_dir.mkdir()
    return collector


@pytest.mark.asyncio
async def test_reload_reports_disabled_and_missing_plugins(tmp_path: Path):
    collector = _make_collector_with_dirs(tmp_path)

    with pytest.raises(ValueError, match="不存在"):
        await collector.reload_plugin_by_module_name("ghost.py")

    (collector.workdir_plugin_dir / "demo.py.disabled").write_text("plugin = None\n", encoding="utf-8")
    with pytest.raises(ValueError, match="禁用"):
        await collector.reload_plugin_by_module_name("demo.py.disabled")

    # 包插件禁用形态：入口 __init__.py 被重命名为 __init__.py.disabled
    pkg_dir = collector.workdir_plugin_dir / "pkgd"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py.disabled").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")
    with pytest.raises(ValueError, match="禁用"):
        await collector.reload_plugin_by_module_name("pkgd/plugin.py")


@pytest.mark.asyncio
async def test_reload_resolves_package_inner_file_to_top_package(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    pkg_dir = collector.workdir_plugin_dir / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    (pkg_dir / "plugin.py").write_text("plugin = None\n", encoding="utf-8")

    loaded_paths: list[Path] = []

    async def fake_try_load(item_path: Path, is_builtin: bool = False, is_package: bool = False) -> bool:
        loaded_paths.append(item_path)
        return True

    monkeypatch.setattr(collector, "_try_load_plugin", fake_try_load)

    # 编辑器传入包内文件路径（旧实现会直接报"不在合法的加载目录中"）
    await collector.reload_plugin_by_module_name("mypkg/plugin.py")
    assert loaded_paths == [pkg_dir / "__init__.py"]

    loaded_paths.clear()
    await collector.reload_plugin_by_module_name("mypkg/__init__.py")
    assert loaded_paths == [pkg_dir / "__init__.py"]

    # 单文件插件传带 .py 后缀的文件名
    (collector.workdir_plugin_dir / "solo.py").write_text("plugin = None\n", encoding="utf-8")
    loaded_paths.clear()
    await collector.reload_plugin_by_module_name("solo.py")
    assert loaded_paths == [collector.workdir_plugin_dir / "solo.py"]


@pytest.mark.asyncio
async def test_reload_preserves_loaded_plugin_type(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    pkg_dir = collector.packages_dir / "cloud_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("plugin = None\n", encoding="utf-8")

    loaded_plugin = SimpleNamespace(
        module_name="cloud_pkg",
        is_builtin=False,
        is_package=True,
        _module=SimpleNamespace(__name__="packages.cloud_pkg"),
        _commands=[],
        cleanup_method=None,
        key="author.cloud_pkg",
    )
    collector.loaded_plugins = {loaded_plugin.key: loaded_plugin}
    collector.loaded_module_names = {"packages.cloud_pkg"}
    loaded_types: list[tuple[bool, bool]] = []

    async def fake_try_load(item_path: Path, is_builtin: bool = False, is_package: bool = False) -> bool:
        assert item_path == pkg_dir / "__init__.py"
        loaded_types.append((is_builtin, is_package))
        return True

    monkeypatch.setattr(collector, "_try_load_plugin", fake_try_load)

    await collector.reload_plugin_by_module_name("cloud_pkg")

    assert loaded_types == [(False, True)]


@pytest.mark.asyncio
async def test_reload_uses_actual_source_type_when_duplicate_source_exists(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    local_file = collector.workdir_plugin_dir / "duplicate.py"
    package_file = collector.packages_dir / "duplicate.py"
    local_file.write_text("plugin = None\n", encoding="utf-8")
    package_file.write_text("plugin = None\n", encoding="utf-8")
    loaded_plugin = SimpleNamespace(
        module_name="duplicate",
        is_builtin=False,
        is_package=True,
        _module=SimpleNamespace(__name__="packages.duplicate"),
        _commands=[],
        cleanup_method=None,
        key="author.duplicate",
    )
    collector.loaded_plugins = {loaded_plugin.key: loaded_plugin}
    collector.loaded_module_names = {"packages.duplicate"}
    loaded_types: list[tuple[Path, bool, bool]] = []

    async def fake_try_load(item_path: Path, is_builtin: bool = False, is_package: bool = False) -> bool:
        loaded_types.append((item_path, is_builtin, is_package))
        return True

    monkeypatch.setattr(collector, "_try_load_plugin", fake_try_load)

    await collector.reload_plugin_by_module_name("duplicate")

    assert loaded_types == [(local_file, False, False)]


@pytest.mark.asyncio
async def test_reload_infers_plugin_type_from_source_directory(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    builtin_file = collector.builtin_plugin_dir / "builtin_demo.py"
    package_file = collector.packages_dir / "cloud_demo.py"
    builtin_file.write_text("plugin = None\n", encoding="utf-8")
    package_file.write_text("plugin = None\n", encoding="utf-8")
    loaded_types: list[tuple[Path, bool, bool]] = []

    async def fake_try_load(item_path: Path, is_builtin: bool = False, is_package: bool = False) -> bool:
        loaded_types.append((item_path, is_builtin, is_package))
        return True

    monkeypatch.setattr(collector, "_try_load_plugin", fake_try_load)

    await collector.reload_plugin_by_module_name("builtin_demo")
    await collector.reload_plugin_by_module_name("cloud_demo")

    assert loaded_types == [
        (builtin_file, True, False),
        (package_file, False, True),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "expected_reload"),
    [("mypkg/helper.py", ["mypkg"]), ("mypkg/__init__.py", [])],
)
async def test_delete_package_file_reload_rules(
    tmp_path: Path,
    monkeypatch,
    file_path: str,
    expected_reload: list[str],
):
    from nekro_agent.routers import plugin_editor

    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from .plugin import plugin\n", encoding="utf-8")
    target_file = tmp_path / file_path
    if not target_file.exists():
        target_file.write_text("VALUE = 1\n", encoding="utf-8")
    unloaded: list[str] = []
    unload_scopes: list[str] = []
    reloaded: list[str] = []

    async def fake_unload(module_name: str, scope: str = "all") -> None:
        unloaded.append(module_name)
        unload_scopes.append(scope)

    async def fake_reload(module_name: str) -> None:
        reloaded.append(module_name)
        return True

    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_editor.plugin_collector, "unload_plugin_by_module_name", fake_unload)
    monkeypatch.setattr(plugin_editor.plugin_collector, "reload_plugin_by_module_name", fake_reload)

    response = await plugin_editor.delete_plugin_file.__wrapped__(file_path, _current_user=SimpleNamespace())

    assert response.ok is True
    assert not target_file.exists()
    assert unloaded == ["mypkg"]
    # 删除工作目录文件只允许卸载本地插件，防止误卸载同名内置/云端插件
    assert all(scope == "local" for scope in unload_scopes)
    assert reloaded == expected_reload


def test_plugin_editor_rejects_traversal_and_invalid_suffix(tmp_path: Path, monkeypatch):
    from nekro_agent.routers import plugin_editor
    from nekro_agent.schemas.errors import ValidationError

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("SECRET = True\n", encoding="utf-8")
    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(plugin_dir))

    for file_path in ("../outside.py", "notes.txt"):
        with pytest.raises(ValidationError):
            plugin_editor._resolve_plugin_file(file_path)



def test_plugin_editor_rejects_symlink_escape(tmp_path: Path, monkeypatch):
    from nekro_agent.routers import plugin_editor
    from nekro_agent.schemas.errors import ValidationError

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("SECRET = True\n", encoding="utf-8")
    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(plugin_dir))

    symlink = plugin_dir / "escape.py"
    try:
        symlink.symlink_to(outside_file)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(ValidationError):
        plugin_editor._resolve_plugin_file("escape.py", must_exist=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "expected_path", "expected_unload", "expected_reload"),
    [
        ("solo.py", "solo.py.disabled", ["solo"], []),
        ("solo.py.disabled", "solo.py", ["solo"], ["solo"]),
        ("pkg/__init__.py", "pkg/__init__.py.disabled", ["pkg"], []),
        ("pkg/__init__.py.disabled", "pkg/__init__.py", ["pkg"], ["pkg"]),
    ],
)
async def test_toggle_plugin_entry_file(
    tmp_path: Path,
    monkeypatch,
    file_path: str,
    expected_path: str,
    expected_unload: list[str],
    expected_reload: list[str],
):
    from nekro_agent.routers import plugin_editor

    target_file = tmp_path / file_path
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("plugin = None\n", encoding="utf-8")
    unloaded: list[str] = []
    unload_scopes: list[str] = []
    reloaded: list[str] = []

    async def fake_unload(module_name: str, scope: str = "all") -> None:
        unloaded.append(module_name)
        unload_scopes.append(scope)

    async def fake_reload(module_name: str) -> None:
        reloaded.append(module_name)
        return True

    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_editor.plugin_collector, "get_plugin_by_module_name", lambda _: SimpleNamespace())
    monkeypatch.setattr(plugin_editor.plugin_collector, "unload_plugin_by_module_name", fake_unload)
    monkeypatch.setattr(plugin_editor.plugin_collector, "reload_plugin_by_module_name", fake_reload)

    response = await plugin_editor.toggle_plugin_file.__wrapped__(file_path, _current_user=SimpleNamespace())

    assert response.model_dump() == {"ok": True, "file_path": expected_path}
    assert (tmp_path / expected_path).is_file()
    assert unloaded == expected_unload
    # 禁用工作目录文件只允许卸载本地插件，防止误卸载同名内置/云端插件
    assert all(scope == "local" for scope in unload_scopes)
    assert reloaded == expected_reload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "expected_path"),
    [
        # 包内子包入口：改名后顶层入口仍启用，必须重载顶层包，否则整个插件静默失效
        ("pkg/sub/__init__.py", "pkg/sub/__init__.py.disabled"),
        # 包内普通模块：允许启停，改名后同样需要重载所属插件
        ("pkg/util.py", "pkg/util.py.disabled"),
        ("pkg/util.py.disabled", "pkg/util.py"),
    ],
)
async def test_toggle_package_internal_file_reloads_top_level_plugin(
    tmp_path: Path,
    monkeypatch,
    file_path: str,
    expected_path: str,
):
    from nekro_agent.routers import plugin_editor

    entry_file = tmp_path / "pkg" / "__init__.py"
    entry_file.parent.mkdir(parents=True, exist_ok=True)
    entry_file.write_text("plugin = None\n", encoding="utf-8")
    target_file = tmp_path / file_path
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("value = 1\n", encoding="utf-8")
    unloaded: list[str] = []
    reloaded: list[str] = []

    async def fake_unload(module_name: str, scope: str = "all") -> None:
        unloaded.append(module_name)

    async def fake_reload(module_name: str) -> bool:
        reloaded.append(module_name)
        return True

    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_editor.plugin_collector, "get_plugin_by_module_name", lambda _: SimpleNamespace())
    monkeypatch.setattr(plugin_editor.plugin_collector, "unload_plugin_by_module_name", fake_unload)
    monkeypatch.setattr(plugin_editor.plugin_collector, "reload_plugin_by_module_name", fake_reload)

    response = await plugin_editor.toggle_plugin_file.__wrapped__(file_path, _current_user=SimpleNamespace())

    assert response.model_dump() == {"ok": True, "file_path": expected_path}
    assert (tmp_path / expected_path).is_file()
    assert unloaded == ["pkg"]
    assert reloaded == ["pkg"]


@pytest.mark.asyncio
async def test_toggle_wraps_reload_failure_and_rolls_back_rename(tmp_path: Path, monkeypatch):
    """启用入口后重载抛错时必须回滚改名并返回 PluginLoadError，而非未包装的 500。"""
    from nekro_agent.routers import plugin_editor
    from nekro_agent.schemas.errors import PluginLoadError

    disabled_entry = tmp_path / "pkg" / "__init__.py.disabled"
    disabled_entry.parent.mkdir(parents=True, exist_ok=True)
    disabled_entry.write_text("plugin = None\n", encoding="utf-8")

    async def fake_reload(module_name: str) -> bool:
        raise ValueError(f"插件 `{module_name}` 处于禁用状态")

    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_editor.plugin_collector, "get_plugin_by_module_name", lambda _: None)
    monkeypatch.setattr(plugin_editor.plugin_collector, "unload_plugin_by_module_name", AsyncMock())
    monkeypatch.setattr(plugin_editor.plugin_collector, "reload_plugin_by_module_name", fake_reload)

    with pytest.raises(PluginLoadError):
        await plugin_editor.toggle_plugin_file.__wrapped__(
            "pkg/__init__.py.disabled",
            _current_user=SimpleNamespace(),
        )

    assert disabled_entry.is_file()
    assert not (tmp_path / "pkg" / "__init__.py").exists()


@pytest.mark.asyncio
async def test_delete_restores_loaded_plugin_when_unlink_fails(tmp_path: Path, monkeypatch):
    from nekro_agent.routers import plugin_editor

    target_file = tmp_path / "demo.py"
    target_file.write_text("plugin = None\n", encoding="utf-8")
    original_unlink = Path.unlink
    reload_mock = AsyncMock(return_value=True)

    def fail_target_unlink(path: Path, *args, **kwargs):
        if path == target_file:
            raise PermissionError("read-only")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(plugin_editor, "WORKDIR_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_editor.plugin_collector, "get_plugin_by_module_name", lambda _: SimpleNamespace())
    monkeypatch.setattr(plugin_editor.plugin_collector, "unload_plugin_by_module_name", AsyncMock())
    monkeypatch.setattr(plugin_editor.plugin_collector, "reload_plugin_by_module_name", reload_mock)
    monkeypatch.setattr(Path, "unlink", fail_target_unlink)

    with pytest.raises(PermissionError, match="read-only"):
        await plugin_editor.delete_plugin_file.__wrapped__("demo.py", _current_user=SimpleNamespace())

    reload_mock.assert_awaited_once_with("demo.py")
    assert target_file.exists()


def test_get_plugin_by_module_name_falls_back_to_normalized_name(tmp_path: Path):
    collector = _make_collector_with_dirs(tmp_path)
    fake_plugin = SimpleNamespace(
        module_name="declared_name",
        _module=SimpleNamespace(__name__="workdir.mypkg"),
    )
    collector.loaded_plugins = {"author.demo": fake_plugin}

    assert collector.get_plugin_by_module_name("declared_name") is fake_plugin
    # 文件路径形态：按实际加载模块路径尾段匹配（声明名与文件名不一致的插件）
    assert collector.get_plugin_by_module_name("mypkg/plugin.py") is fake_plugin
    assert collector.get_plugin_by_module_name("mypkg") is fake_plugin
    assert collector.get_plugin_by_module_name("ghost.py") is None


def test_pop_stale_plugin_modules_removes_submodules(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    collector.loaded_module_names = {"workdir.mypkg"}
    monkeypatch.setitem(sys.modules, "workdir.mypkg", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "workdir.mypkg.utils", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "workdir.mypkg_other", SimpleNamespace())

    collector._pop_stale_plugin_modules("mypkg")

    # 包模块与其子模块一并弹出，避免重载后 `from .xxx import` 指向旧代码
    assert "workdir.mypkg" not in sys.modules
    assert "workdir.mypkg.utils" not in sys.modules
    # 前缀相似但不同的模块不受影响
    assert "workdir.mypkg_other" in sys.modules
    assert "workdir.mypkg" not in collector.loaded_module_names


@pytest.mark.asyncio
async def test_failed_package_import_purges_partial_modules(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin import collector as collector_module

    collector = _make_collector_with_dirs(tmp_path)
    package_dir = collector.workdir_plugin_dir / "broken"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("raise RuntimeError\n", encoding="utf-8")

    def fake_import(module_path: str):
        monkeypatch.setitem(sys.modules, module_path, SimpleNamespace())
        monkeypatch.setitem(sys.modules, f"{module_path}.helper", SimpleNamespace())
        raise RuntimeError("broken import")

    monkeypatch.setattr(collector_module, "import_module", fake_import)

    loaded = await collector._try_load_plugin(package_dir)

    assert loaded is False
    assert "workdir.broken" not in sys.modules
    assert "workdir.broken.helper" not in sys.modules


@pytest.mark.asyncio
async def test_init_plugins_uses_same_source_priority_as_reload(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    builtin_file = collector.builtin_plugin_dir / "duplicate.py"
    local_file = collector.workdir_plugin_dir / "duplicate.py"
    package_file = collector.packages_dir / "duplicate.py"
    for path in (builtin_file, local_file, package_file):
        path.write_text("plugin = None\n", encoding="utf-8")

    loaded_paths: list[tuple[Path, bool, bool]] = []

    async def fake_try_load(item_path: Path, is_builtin: bool = False, is_package: bool = False) -> bool:
        loaded_paths.append((item_path, is_builtin, is_package))
        return True

    monkeypatch.setattr(collector, "_try_load_plugin", fake_try_load)

    await collector.init_plugins()

    assert loaded_paths == [(builtin_file, True, False)]


@pytest.mark.asyncio
async def test_reload_returns_false_when_candidate_load_fails(tmp_path: Path, monkeypatch):
    collector = _make_collector_with_dirs(tmp_path)
    plugin_file = collector.workdir_plugin_dir / "broken.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr(collector, "_try_load_plugin", AsyncMock(return_value=False))

    assert await collector.reload_plugin_by_module_name("broken") is False


def test_plugin_router_without_custom_routes_is_reload_success():
    from fastapi import FastAPI

    from nekro_agent.services.plugin.base import NekroPlugin
    from nekro_agent.services.plugin.router_manager import PluginRouterManager

    plugin = NekroPlugin(
        name="No Routes",
        module_name="no_routes",
        description="router-less plugin",
        version="0.1.0",
        author="Tester",
        url="https://example.com",
    )
    plugin._is_enabled = True  # noqa: SLF001
    manager = PluginRouterManager()
    manager.set_app(FastAPI())

    assert manager.reload_plugin_router(plugin) is True


@pytest.mark.asyncio
async def test_duplicate_key_keeps_higher_priority_plugin(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin import collector as collector_module
    from nekro_agent.services.plugin.base import NekroPlugin

    collector = _make_collector_with_dirs(tmp_path)
    old_plugin = NekroPlugin(
        name="Builtin",
        module_name="builtin_name",
        description="higher priority",
        version="0.1.0",
        author="Tester",
        url="https://example.com",
    )
    old_plugin._update_plugin_type(True, False)  # noqa: SLF001
    old_plugin._set_module(SimpleNamespace(__name__="builtin.builtin_name"))  # noqa: SLF001
    new_plugin = NekroPlugin(
        name="Local",
        module_name="local_name",
        description="lower priority",
        version="0.1.0",
        author="Tester",
        url="https://example.com",
    )
    new_plugin._key = old_plugin.key  # noqa: SLF001
    collector.loaded_plugins[old_plugin.key] = old_plugin
    plugin_file = collector.workdir_plugin_dir / "local_name.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr(collector_module, "import_module", lambda _path: SimpleNamespace(plugin=new_plugin))

    loaded = await collector._load_plugin_module("workdir.local_name", plugin_file)

    assert loaded is False
    assert collector.loaded_plugins[old_plugin.key] is old_plugin


@pytest.mark.asyncio
async def test_duplicate_key_init_failure_preserves_old_plugin(tmp_path: Path, monkeypatch):
    from nekro_agent.services.plugin import collector as collector_module
    from nekro_agent.services.plugin.base import NekroPlugin

    collector = _make_collector_with_dirs(tmp_path)
    old_plugin = NekroPlugin(
        name="Cloud",
        module_name="cloud_name",
        description="existing plugin",
        version="0.1.0",
        author="Tester",
        url="https://example.com",
    )
    old_plugin._update_plugin_type(False, True)  # noqa: SLF001
    old_plugin._set_module(SimpleNamespace(__name__="packages.cloud_name"))  # noqa: SLF001
    new_plugin = NekroPlugin(
        name="Builtin",
        module_name="builtin_name",
        description="replacement plugin",
        version="0.1.0",
        author="Tester",
        url="https://example.com",
    )
    new_plugin._key = old_plugin.key  # noqa: SLF001

    async def fail_init() -> None:
        raise RuntimeError("init failed")

    new_plugin.init_method = fail_init
    collector.loaded_plugins[old_plugin.key] = old_plugin
    plugin_file = collector.builtin_plugin_dir / "builtin_name.py"
    plugin_file.write_text("plugin = None\n", encoding="utf-8")
    monkeypatch.setattr(collector_module, "import_module", lambda _path: SimpleNamespace(plugin=new_plugin))

    loaded = await collector._load_plugin_module("builtin.builtin_name", plugin_file, is_builtin=True)

    assert loaded is False
    assert collector.loaded_plugins[old_plugin.key] is old_plugin


def test_plugin_router_reload_replaces_actual_fastapi_routes(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from nekro_agent.services.plugin.base import NekroPlugin
    from nekro_agent.services.plugin.collector import plugin_collector
    from nekro_agent.services.plugin.router_manager import PluginRouterManager

    def build_plugin(label: str) -> NekroPlugin:
        plugin = NekroPlugin(
            name="Route Demo",
            module_name="route_demo",
            description="route reload test",
            version="0.1.0",
            author="Tester",
            url="https://example.com",
        )
        plugin._is_enabled = True  # noqa: SLF001

        @plugin.mount_router()
        def create_router() -> APIRouter:
            router = APIRouter()

            @router.get("/value")
            async def get_value() -> dict[str, str]:
                return {"value": label}

            return router

        return plugin

    old_plugin = build_plugin("old")
    new_plugin = build_plugin("new")
    active_plugin = {"value": old_plugin}
    monkeypatch.setattr(plugin_collector, "get_plugin", lambda _key: active_plugin["value"])

    app = FastAPI()
    manager = PluginRouterManager()
    manager.set_app(app)
    assert manager.mount_plugin_router(old_plugin) is True
    client = TestClient(app)
    route_path = f"/plugins/{old_plugin.key}/value"
    assert client.get(route_path).json() == {"value": "old"}

    active_plugin["value"] = new_plugin
    assert manager.reload_plugin_router(new_plugin) is True
    assert client.get(route_path).json() == {"value": "new"}
    assert [getattr(route, "path", None) for route in app.router.routes].count(route_path) == 1

    assert manager.unmount_plugin_router(new_plugin.key) is True
    assert client.get(route_path).status_code == 404
