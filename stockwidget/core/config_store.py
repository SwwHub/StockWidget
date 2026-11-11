# -*- coding: utf-8 -*-
"""固定程序数据路径、旧版本启动迁移与原子配置保存（纯 Python，无 Qt 依赖）。"""

from contextlib import closing
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

from stockwidget.constants import APP_NAME, CONFIG_FILE, CODE_LIST_FILES, CODES_STATUS_FILE


def _platform_config_paths() -> str:
    """系统固定数据目录；便携目录不可用时使用。"""
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    base = os.getenv("APPDATA") if sys.platform == "win32" else None
    return os.path.join(base or os.path.expanduser("~"), APP_NAME)


def _directory_is_writable(directory: str) -> bool:
    probe = os.path.join(directory, ".stockwidget-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as file:
            file.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def portable_directory() -> str | None:
    """打包运行时程序所在目录可写即视为便携版；源码运行与 macOS 始终返回 None。"""
    if sys.platform == "darwin" or not getattr(sys, "frozen", False):
        return None
    base = os.path.dirname(os.path.abspath(sys.executable))
    return base if _directory_is_writable(base) else None


def config_paths() -> str:
    """便携版把配置与缓存放在程序目录，其余情况使用平台固定目录。"""
    return portable_directory() or _platform_config_paths()


def data_cache_dir() -> str:
    return os.path.join(config_paths(), "data")


def history_cache_dir() -> str:
    return os.path.join(config_paths(), "cache")


def load_file(file_name: str, fallback: dict | None = None, *, directory: str | None = None) -> dict:
    fallback = {} if fallback is None else fallback
    try:
        with open(os.path.join(directory or config_paths(), file_name), encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else fallback
    except (OSError, ValueError):
        return fallback


def save_file(data: dict, file_name: str, *, directory: str | None = None) -> None:
    """使用独立临时文件原子保存，避免后台线程相互覆盖。"""
    directory = directory or config_paths()
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".stockwidget-", suffix=".tmp", delete=False) as file:
            temporary = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, os.path.join(directory, file_name))
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _read_configuration(directory):
    try:
        with open(os.path.join(directory, CONFIG_FILE), encoding="utf-8") as file:
            config = json.load(file)
        if not isinstance(config, dict):
            raise ValueError("配置不是 JSON 对象")
        return config
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise OSError("无法读取原配置，已保留原文件") from error


def _stage_history(source: Path, target: Path):
    """SQLite 用一致性快照复制，包括 WAL 中已提交的数据。"""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    snapshot_complete = False
    sidecars = {"history.sqlite3-journal", "history.sqlite3-wal", "history.sqlite3-shm"}
    # 先复制数据库：只有快照成功，才可省略已包含在快照中的 SQLite 辅助文件。
    for path in sorted(source.iterdir(), key=lambda item: item.name != "history.sqlite3"):
        destination = target / path.name
        if os.path.lexists(destination) or snapshot_complete and path.name in sidecars:
            continue
        if path.name == "history.sqlite3":
            deadline = time.monotonic() + 2

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise OSError("历史缓存正在使用，请稍后重试")

            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db, \
                        closing(sqlite3.connect(destination)) as copy:
                    db.backup(copy, pages=128, progress=progress, sleep=.01)
            except sqlite3.Error as error:
                if getattr(error, "sqlite_errorcode", None) not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                    raise OSError("无法复制历史缓存") from error
                shutil.copy2(path, destination)
            else:
                snapshot_complete = True
        elif path.is_symlink() or path.is_file():
            shutil.copy2(path, destination, follow_symlinks=False)
        elif path.is_dir():
            shutil.copytree(path, destination, symlinks=True)


def _stage_directory(source, target):
    def ignore(directory, names):
        return {"cache"} if Path(directory) == source and (source / "cache").is_dir() else set()
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True, ignore=ignore)
    _stage_history(source / "cache", target / "cache")
    # 旧 home 中的根目录代码文件在本次迁移内归一到 data/，保留原件。
    data = target / "data"
    if not data.is_symlink():
        for name in (*CODE_LIST_FILES, CODES_STATUS_FILE):
            if (source / name).is_file() and not os.path.lexists(data / name):
                if data.exists() and not data.is_dir():
                    raise OSError("无法保存迁移后的市场代码目录")
                data.mkdir(exist_ok=True)
                shutil.copy2(source / name, data / name)
    _rewrite_legacy_links(source, target)


def _rewrite_legacy_links(source, stage):
    """内部链接映射到迁移副本，外部链接保留旧位置下的原目标。"""
    resolved_source = source.resolve()
    for path in stage.rglob("*"):
        if path.is_symlink():
            link = Path(os.readlink(path))
            original = source / path.relative_to(stage)
            target = Path(os.path.abspath(original.parent.resolve() / link))
            if target.is_relative_to(source):
                replacement = os.path.relpath(stage / target.relative_to(source), path.parent)
            elif target.is_relative_to(resolved_source):
                replacement = os.path.relpath(stage / target.relative_to(resolved_source), path.parent)
            elif not link.is_absolute():
                replacement = str(target)
            else:
                continue
            path.unlink()
            path.symlink_to(replacement)


def _install_migration(stage, root, backups):
    """完成所有文件准备后安装；落盘失败时恢复原目标文件。"""
    changed = []
    try:
        boundary = root.resolve()
        for path in sorted(stage.rglob("*")):
            destination = root / path.relative_to(stage)
            if not destination.parent.resolve().is_relative_to(boundary):
                raise OSError("迁移目标超出程序数据目录，已保留旧目录")
            if path.is_dir() and not path.is_symlink():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.name == "history.sqlite3" and any(
                    Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise OSError("目标历史缓存正在使用，请稍后重试")
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup = backups / path.relative_to(stage)
            existed = os.path.lexists(destination)
            if existed:
                if destination.is_dir() and not destination.is_symlink():
                    raise OSError("目标目录有同名文件夹")
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup, follow_symlinks=False)
            changed.append((destination, backup if existed else None))
            os.replace(path, destination)
    except OSError:
        for destination, backup in reversed(changed):
            if backup is not None:
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        raise


def _adopt_portable_data() -> None:
    """首次便携运行时，把平台目录里的配置与市场代码表复制到程序目录。"""
    portable = portable_directory()
    if not portable:
        return
    platform_dir = _platform_config_paths()
    if os.path.abspath(portable) == os.path.abspath(platform_dir):
        return
    if os.path.exists(os.path.join(portable, CONFIG_FILE)):
        return
    try:
        config = os.path.join(platform_dir, CONFIG_FILE)
        if os.path.isfile(config):
            shutil.copy2(config, os.path.join(portable, CONFIG_FILE))
        source = os.path.join(platform_dir, "data")
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(portable, "data"), dirs_exist_ok=True)
    except OSError:
        # 迁移只是便利，失败时按空配置启动，不阻塞程序运行。
        pass


def initialize_data_directory() -> dict:
    """便携版接管平台目录中的配置；仅 darwin 迁移 ~/StockWidget 后删除旧目录。"""
    _adopt_portable_data()
    root = Path(config_paths())
    if sys.platform != "darwin" or portable_directory():
        return load_file(CONFIG_FILE)
    legacy = Path(os.path.expanduser("~")) / APP_NAME
    if legacy == root or not os.path.lexists(legacy):
        return load_file(CONFIG_FILE)
    if legacy.is_symlink() and legacy.resolve() == root.resolve():
        config = _read_configuration(root)
        legacy.unlink()
        return config
    if legacy.resolve() in root.resolve().parents or root.resolve() in legacy.resolve().parents:
        raise OSError("旧目录与默认目录互相包含")
    source = root if (root / CONFIG_FILE).is_file() else legacy
    config = _read_configuration(source)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stockwidget-migrate-", dir=root) as temporary:
        stage = Path(temporary) / "files"
        stage.mkdir()
        if source == legacy:
            _stage_directory(legacy, stage)
            migrated = root
        else:
            # 新配置优先；完整保留旧目录，即使有额外文件也不会在清理时丢失。
            backup = "legacy_backup"
            suffix = 1
            while os.path.lexists(root / backup):
                backup = f"legacy_backup_{suffix}"
                suffix += 1
            _stage_directory(legacy, stage / backup)
            migrated = root / backup
        icon = config.get("custom_icon_path")
        if isinstance(icon, str) and icon.startswith(str(legacy) + os.sep):
            config["custom_icon_path"] = str(migrated / Path(icon).relative_to(legacy))
        save_file(config, CONFIG_FILE, directory=str(stage))
        _install_migration(stage, root, Path(temporary) / "backups")
        if _read_configuration(root) != config:
            raise OSError("迁移配置校验失败")
    if legacy.is_symlink():
        legacy.unlink()
    else:
        shutil.rmtree(legacy)
    return config
