"""Process-lifetime instance lock (P-1.8 D2, design v1.10 §12).

One ``STATE_DIR`` may host exactly one application process.  The lock is an
advisory ``flock`` on ``<STATE_DIR>/migration.lock``; the file itself carries no
state.  The kernel releases the lock when the last descriptor for the open file
description is closed, including on SIGKILL, so a leftover file is not evidence
that the lock is held.

``O_CLOEXEC`` closes the descriptor in the child at ``exec``.  A plain
``fork()`` child inherits it between fork and exec, so the project treats any
child as short-lived launch-only (all actual child processes are ``exec``'d);
it must not become a long-lived unexec'd worker that keeps the parent's lock.
"""
from __future__ import annotations

import errno
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend import config


class AlreadyRunning(RuntimeError):
    """Another process already holds the instance lock."""


class LockUnsupported(RuntimeError):
    """The filesystem does not support advisory flock."""


@dataclass
class InstanceLock:
    path: Path
    fd: int | None = None

    @property
    def owned(self) -> bool:
        return self.fd is not None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise AlreadyRunning(str(self.path)) from exc
            if exc.errno in (errno.ENOLCK, errno.EOPNOTSUPP):
                raise LockUnsupported(str(self.path)) from exc
            raise
        self.fd = fd
        return self

    def assert_owned(self) -> None:
        """Verify this token still refers to the held descriptor.

        This is a token check, not a second ``flock``: re-locking a path from the
        same process on a *new* descriptor would conflict with the existing lock.
        """
        if self.fd is None:
            raise AlreadyRunning("instance lock token is not held")
        try:
            os.fstat(self.fd)
        except OSError as exc:
            raise AlreadyRunning("instance lock descriptor is closed") from exc

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "InstanceLock":
        self.assert_owned()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def lock_path(state_dir: str | Path | None = None) -> Path:
    return Path(state_dir or config.STATE_DIR) / "migration.lock"


def acquire(state_dir: str | Path | None = None) -> InstanceLock:
    return InstanceLock(lock_path(state_dir)).acquire()


def assert_owned(token: Optional[InstanceLock]) -> InstanceLock:
    if token is None:
        raise AlreadyRunning("instance_lock_required")
    token.assert_owned()
    return token


__all__ = ["AlreadyRunning", "InstanceLock", "LockUnsupported", "acquire",
           "assert_owned", "lock_path"]
