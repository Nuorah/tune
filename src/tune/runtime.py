"""Private, ephemeral filesystem locations used by tune."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class RuntimeSecurityError(RuntimeError):
    """Raised when the runtime directory is not safe to use."""


def runtime_dir() -> Path:
    """Return tune's private runtime directory, creating it if necessary."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        path = Path(base) / "tune"
    else:
        path = Path(tempfile.gettempdir()) / f"tune-{os.getuid()}"

    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    except FileNotFoundError as error:
        raise RuntimeSecurityError(
            f"runtime parent does not exist: {path.parent}"
        ) from error

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeSecurityError(f"runtime path is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeSecurityError(f"runtime directory is owned by another user: {path}")

    path.chmod(0o700)
    return path


def control_socket_path() -> Path:
    """Return the daemon's Unix-domain control socket path."""
    return runtime_dir() / "control.sock"


def player_socket_path() -> Path:
    """Return mpv's Unix-domain control socket path."""
    return runtime_dir() / "player.sock"
