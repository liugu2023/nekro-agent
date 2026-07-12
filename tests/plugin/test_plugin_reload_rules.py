from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
