"""写时备份（COW）：开跑只建清单，真正改文件前才拷贝原文。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Iterable

SKIP_DIRS = {"_sessions"}
SKIP_NAMES = {"temp_sandbox_script.py", "_cow_created.json", "_cow_meta.json"}


class CowSession:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.normpath(workspace_dir)
        self.backup_root = tempfile.mkdtemp(prefix="cyber_lobster_cow_")
        self.initial_files = self._scan_existing()
        self._write_meta()

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.workspace_dir).replace("\\", "/")

    def _scan_existing(self) -> set[str]:
        found: set[str] = set()
        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith(".") or name in SKIP_NAMES:
                    continue
                found.add(self._rel(os.path.join(root, name)))
        return found

    def _write_meta(self) -> None:
        meta = {
            "workspace": self.workspace_dir,
            "initial_files": sorted(self.initial_files),
        }
        with open(os.path.join(self.backup_root, "_cow_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    def cleanup(self) -> None:
        shutil.rmtree(self.backup_root, ignore_errors=True)

    def rollback(self) -> tuple[int, int]:
        """
        回退本轮改动。
        返回 (恢复/写回数, 删除本轮新建数)
        """
        created_path = os.path.join(self.backup_root, "_cow_created.json")
        created: set[str] = set()
        if os.path.isfile(created_path):
            try:
                with open(created_path, "r", encoding="utf-8") as f:
                    created = set(json.load(f))
            except (OSError, json.JSONDecodeError):
                created = set()

        restored = 0
        deleted_new = 0

        # 1) 恢复备份里的原文（修改 / 误删）
        for root, _dirs, files in os.walk(self.backup_root):
            for name in files:
                if name in {"_cow_meta.json", "_cow_created.json"}:
                    continue
                bpath = os.path.join(root, name)
                rel = os.path.relpath(bpath, self.backup_root).replace("\\", "/")
                dest = os.path.normpath(os.path.join(self.workspace_dir, *rel.split("/")))
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(bpath, dest)
                    restored += 1
                except OSError:
                    pass

        # 2) 删除本轮新建
        for rel in list(created):
            dest = os.path.normpath(os.path.join(self.workspace_dir, *rel.split("/")))
            if os.path.isfile(dest):
                try:
                    os.remove(dest)
                    deleted_new += 1
                except OSError:
                    pass

        # 3) 兜底：当前存在、但不在开跑清单、也不在 created 记录里的新文件
        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith(".") or name in SKIP_NAMES:
                    continue
                path = os.path.join(root, name)
                rel = self._rel(path)
                if rel in self.initial_files:
                    continue
                # 已有备份的按恢复处理；无备份的当新建删掉
                bpath = os.path.join(self.backup_root, *rel.split("/"))
                if os.path.isfile(bpath):
                    continue
                try:
                    os.remove(path)
                    deleted_new += 1
                except OSError:
                    pass

        return restored, deleted_new


def build_sandbox_cow_preamble(workspace_dir: str, backup_root: str, initial_files: Iterable[str]) -> str:
    """注入到沙箱脚本最前面：拦截写/删/改名，写前备份。"""
    initial = sorted(set(initial_files))
    ws = workspace_dir.replace("\\", "\\\\")
    br = backup_root.replace("\\", "\\\\")
    initial_json = json.dumps(initial, ensure_ascii=False)
    return f'''# --- COW preamble (auto-injected) ---
import builtins as _cow_builtins
import json as _cow_json
import os as _cow_os
import shutil as _cow_shutil

_COW_WS = _cow_os.path.normpath(r"{ws}")
_COW_BACKUP = _cow_os.path.normpath(r"{br}")
_COW_INITIAL = set(_cow_json.loads({repr(initial_json)}))
_COW_BACKED = set()
_COW_CREATED = set()
_COW_CREATED_FILE = _cow_os.path.join(_COW_BACKUP, "_cow_created.json")
_COW_SKIP_DIRS = {{"_sessions"}}
_COW_SKIP_NAMES = {{"temp_sandbox_script.py", "_cow_created.json", "_cow_meta.json"}}
_cow_orig_open = _cow_builtins.open
_cow_orig_remove = _cow_os.remove
_cow_orig_unlink = getattr(_cow_os, "unlink", _cow_os.remove)
_cow_orig_replace = getattr(_cow_os, "replace", None)
_cow_orig_rename = _cow_os.rename
_cow_orig_makedirs = _cow_os.makedirs

def _cow_rel(path):
    path = _cow_os.path.abspath(path)
    try:
        rel = _cow_os.path.relpath(path, _COW_WS).replace("\\\\", "/")
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    parts = rel.split("/")
    if any(p in _COW_SKIP_DIRS or p.startswith(".") for p in parts[:-1] if p):
        return None
    name = parts[-1] if parts else ""
    if not name or name.startswith(".") or name in _COW_SKIP_NAMES:
        return None
    return rel

def _cow_persist_created():
    with _cow_orig_open(_COW_CREATED_FILE, "w", encoding="utf-8") as f:
        _cow_json.dump(sorted(_COW_CREATED), f, ensure_ascii=False)

def _cow_backup_existing(rel):
    if rel in _COW_BACKED or rel in _COW_CREATED:
        return
    src = _cow_os.path.join(_COW_WS, *rel.split("/"))
    if not _cow_os.path.isfile(src):
        return
    if rel not in _COW_INITIAL:
        return
    dst = _cow_os.path.join(_COW_BACKUP, *rel.split("/"))
    _cow_orig_makedirs(_cow_os.path.dirname(dst), exist_ok=True)
    _cow_shutil.copy2(src, dst)
    _COW_BACKED.add(rel)

def _cow_mark_created(rel):
    if rel in _COW_INITIAL or rel in _COW_CREATED:
        return
    _COW_CREATED.add(rel)
    _cow_persist_created()

def _cow_before_write(path):
    rel = _cow_rel(path)
    if not rel:
        return
    abs_path = _cow_os.path.abspath(path)
    if _cow_os.path.isfile(abs_path) and rel in _COW_INITIAL:
        _cow_backup_existing(rel)
    else:
        _cow_mark_created(rel)

def _cow_before_delete(path):
    rel = _cow_rel(path)
    if not rel:
        return
    if rel in _COW_CREATED:
        _COW_CREATED.discard(rel)
        _cow_persist_created()
        return
    if rel in _COW_INITIAL:
        _cow_backup_existing(rel)

def _cow_open(file, mode="r", *args, **kwargs):
    mode_s = str(mode)
    if any(ch in mode_s for ch in "wax+"):
        _cow_before_write(file)
    return _cow_orig_open(file, mode, *args, **kwargs)

def _cow_remove(path, *args, **kwargs):
    _cow_before_delete(path)
    return _cow_orig_remove(path, *args, **kwargs)

def _cow_unlink(path, *args, **kwargs):
    _cow_before_delete(path)
    return _cow_orig_unlink(path, *args, **kwargs)

def _cow_rename(src, dst, *args, **kwargs):
    _cow_before_delete(src)
    _cow_before_write(dst)
    return _cow_orig_rename(src, dst, *args, **kwargs)

def _cow_replace(src, dst, *args, **kwargs):
    _cow_before_delete(src)
    _cow_before_write(dst)
    return _cow_orig_replace(src, dst, *args, **kwargs)

_cow_builtins.open = _cow_open
_cow_os.remove = _cow_remove
_cow_os.unlink = _cow_unlink
_cow_os.rename = _cow_rename
if _cow_orig_replace is not None:
    _cow_os.replace = _cow_replace

try:
    import pathlib as _cow_pathlib
    _CowPath = _cow_pathlib.Path
    _cow_path_write_text = _CowPath.write_text
    _cow_path_write_bytes = _CowPath.write_bytes
    _cow_path_open = _CowPath.open
    _cow_path_unlink = _CowPath.unlink
    _cow_path_rename = _CowPath.rename
    _cow_path_replace = getattr(_CowPath, "replace", None)

    def _cow_p_write_text(self, *a, **k):
        _cow_before_write(str(self))
        return _cow_path_write_text(self, *a, **k)

    def _cow_p_write_bytes(self, *a, **k):
        _cow_before_write(str(self))
        return _cow_path_write_bytes(self, *a, **k)

    def _cow_p_open(self, mode="r", *a, **k):
        mode_s = str(mode)
        if any(ch in mode_s for ch in "wax+"):
            _cow_before_write(str(self))
        return _cow_path_open(self, mode, *a, **k)

    def _cow_p_unlink(self, *a, **k):
        _cow_before_delete(str(self))
        return _cow_path_unlink(self, *a, **k)

    def _cow_p_rename(self, target, *a, **k):
        _cow_before_delete(str(self))
        _cow_before_write(str(target))
        return _cow_path_rename(self, target, *a, **k)

    _CowPath.write_text = _cow_p_write_text
    _CowPath.write_bytes = _cow_p_write_bytes
    _CowPath.open = _cow_p_open
    _CowPath.unlink = _cow_p_unlink
    _CowPath.rename = _cow_p_rename
    if _cow_path_replace is not None:
        def _cow_p_replace(self, target, *a, **k):
            _cow_before_delete(str(self))
            _cow_before_write(str(target))
            return _cow_path_replace(self, target, *a, **k)
        _CowPath.replace = _cow_p_replace
except Exception:
    pass
# --- end COW preamble ---
'''
