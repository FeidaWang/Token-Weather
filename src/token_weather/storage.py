"""Bounded reads, same-filesystem atomic writes, and exclusive ownership locks."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator

MAX_JSON = 256 * 1024


class WeatherError(Exception):
    pass


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise WeatherError("Invalid identifier; use 1–96 letters, digits, dots, underscores or hyphens")
    return value


def owned_path(root: Path, *parts: str) -> Path:
    """Reject symlinks at every component, including internal symlinks."""
    target = root.joinpath(*parts)
    if root.is_symlink():
        raise WeatherError("Owned root must not be a symlink")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise WeatherError("Path escaped owned root") from exc
    if any(p in ("..", "") for p in relative.parts):
        raise WeatherError("Path escaped owned root")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise WeatherError(f"Refusing symlink: {cursor}")
    return target


def read_bytes(path: Path, limit: int = MAX_JSON) -> bytes:
    if path.is_symlink():
        raise WeatherError(f"Refusing symlink: {path}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise WeatherError(f"File exceeds {limit} byte limit: {path.name}")
    return data


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(read_bytes(path))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise WeatherError(f"Invalid JSON: {path.name}") from exc


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise WeatherError("Refusing a symlink write")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tw-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical_json(value))


@contextlib.contextmanager
def lock(path: Path, timeout: float = 2.0) -> Iterator[None]:
    if path.is_symlink():
        raise WeatherError("Lock must not be a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WeatherError("Workflow is busy; retry after the current writer finishes")
                time.sleep(0.005)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append_json(path: Path, value: Any) -> None:
    """Caller holds the workflow lock. A torn tail is an explicit recovery error."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        data = canonical_json(value)
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count == 0:
                raise WeatherError("Incomplete append")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
