#!/usr/bin/env python3
"""Small helpers for locked, atomic JSON state files.

The bots keep a few JSON baselines beside the browser profiles. Those files are
updated by long-running workers, so writes must be both serialized and atomic.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


@contextmanager
def locked_state_file(path: Path, timeout: float = 5.0):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"state file lock timeout: {lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_json_unlocked(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{int(time.time() * 1000000)}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def read_json_file(path: Path, default: Any) -> Any:
    with locked_state_file(path):
        return _read_json_unlocked(path, default)


def write_json_file(path: Path, data: Any) -> None:
    with locked_state_file(path):
        _write_json_unlocked(path, data)


def update_json_file(path: Path, mutator: Callable[[Any], Any], default: Any) -> Any:
    """Read, mutate, and write a JSON file under one exclusive lock."""
    with locked_state_file(path):
        current = _read_json_unlocked(path, default)
        updated = mutator(current)
        if updated is None:
            updated = current
        _write_json_unlocked(path, updated)
        return updated
