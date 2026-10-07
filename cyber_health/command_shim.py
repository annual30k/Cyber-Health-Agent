"""Expose the ``cyber-health`` management command on PATH through a user bin directory.

The installer, updater and uninstaller own exactly one entry point: a symlink (or a
marked ``.cmd`` shim on Windows) named ``cyber-health`` in ``~/.local/bin``, the same
per-user directory uv and pipx use. An existing file there that Cyber Health did not
create is never replaced or removed, and shell startup files are never edited: when the
directory is not on PATH the status carries the exact line the user can add.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

USER_BIN_ENV = "CYBER_HEALTH_USER_BIN_DIR"
COMMAND_NAME = "cyber-health"
_WINDOWS_MARKER = "rem managed-by: cyber-health"


@dataclass
class CommandShimStatus:
    action: str = "none"  # none, planned, linked, unchanged, removed, refused, skipped, error
    path: str = ""
    target: str = ""
    on_path: bool = False
    reason: str = ""
    path_hint: str = ""
    executed: bool = False


def default_user_bin_dir() -> Path:
    override = os.environ.get(USER_BIN_ENV)
    return Path(override).expanduser() if override else Path.home() / ".local" / "bin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _shim_path(bin_dir: Path) -> Path:
    return bin_dir / (f"{COMMAND_NAME}.cmd" if _is_windows() else COMMAND_NAME)


def _target_path(venv_bin_dir: Path) -> Path:
    return venv_bin_dir / (f"{COMMAND_NAME}.exe" if _is_windows() else COMMAND_NAME)


def _windows_shim_bytes(target: Path) -> bytes:
    # Bytes, not text: text mode would translate the CRLF line endings on Windows.
    return f'@echo off\r\n{_WINDOWS_MARKER}\r\n"{target}" %*\r\n'.encode()


def _exists(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def _points_to(shim: Path, target: Path) -> bool:
    if _is_windows():
        try:
            return shim.is_file() and shim.read_bytes() == _windows_shim_bytes(target)
        except (OSError, ValueError):
            return False
    return shim.is_symlink() and os.readlink(shim) == str(target)


def _is_inside(path: Path, directory: Path) -> bool:
    candidates = {Path(os.path.abspath(directory))}
    with contextlib.suppress(OSError, RuntimeError):
        candidates.add(directory.resolve())
    return any(path == base or base in path.parents for base in candidates)


def _owned(shim: Path, install_dir: Path) -> bool:
    """A shim is ours only if it is the marked Windows wrapper or a symlink into our install."""
    if _is_windows():
        try:
            return shim.is_file() and _WINDOWS_MARKER.encode() in shim.read_bytes()
        except (OSError, ValueError):
            return False
    if not shim.is_symlink():
        return False
    link_target = Path(os.path.abspath(os.path.join(shim.parent, os.readlink(shim))))
    return _is_inside(link_target, install_dir)


def _on_path(bin_dir: Path) -> bool:
    wanted = os.path.normcase(os.path.abspath(bin_dir))
    return any(
        os.path.normcase(os.path.abspath(os.path.expanduser(entry))) == wanted
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    )


def _path_hint(bin_dir: Path) -> str:
    if _is_windows():
        return (
            "[Environment]::SetEnvironmentVariable('Path', "
            f"[Environment]::GetEnvironmentVariable('Path', 'User') + ';{bin_dir}', 'User')"
        )
    return f'export PATH="{bin_dir}:$PATH"'


def _base_status(shim: Path, target: Path, bin_dir: Path) -> CommandShimStatus:
    on_path = _on_path(bin_dir)
    return CommandShimStatus(
        path=str(shim), target=str(target), on_path=on_path, path_hint="" if on_path else _path_hint(bin_dir)
    )


def ensure_command_shim(
    install_dir: Path,
    venv_bin_dir: Path,
    *,
    bin_dir: Path | None = None,
    dry_run: bool = False,
) -> CommandShimStatus:
    """Create or repair the ``cyber-health`` entry point; never touch a foreign file."""
    bin_dir = Path(bin_dir) if bin_dir is not None else default_user_bin_dir()
    shim, target = _shim_path(bin_dir), _target_path(venv_bin_dir)
    status = _base_status(shim, target, bin_dir)
    exists = _exists(shim)
    if exists and _points_to(shim, target):
        status.action, status.reason = "unchanged", "The command already points at this installation."
        return status
    if exists and not _owned(shim, install_dir):
        status.action = "refused"
        status.reason = f"{shim} exists and was not created by Cyber Health; it was left untouched."
        return status
    if dry_run:
        status.action = "planned"
        status.reason = "Will repair the Cyber Health command link." if exists else "Will create the command link."
        return status
    if not target.exists():
        status.action, status.reason = "error", f"Installed command {target} is missing."
        return status
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if exists:
            shim.unlink()
        if _is_windows():
            shim.write_bytes(_windows_shim_bytes(target))
        else:
            shim.symlink_to(target)
    except OSError as exc:
        status.action, status.reason = "error", f"Could not create {shim}: {exc}"
        return status
    status.action, status.executed = "linked", True
    status.reason = "Repaired the Cyber Health command link." if exists else "Created the command link."
    return status


def remove_command_shim(
    install_dir: Path,
    *,
    bin_dir: Path | None = None,
    dry_run: bool = False,
) -> CommandShimStatus:
    """Remove the entry point only when it belongs to this installation."""
    bin_dir = Path(bin_dir) if bin_dir is not None else default_user_bin_dir()
    shim = _shim_path(bin_dir)
    status = CommandShimStatus(path=str(shim), on_path=_on_path(bin_dir))
    if not _exists(shim):
        status.reason = "No Cyber Health command link to remove."
        return status
    if not _owned(shim, install_dir):
        status.action = "refused"
        status.reason = f"{shim} was not created by this Cyber Health installation; it was left untouched."
        return status
    if dry_run:
        status.action, status.reason = "planned", "Will remove the Cyber Health command link."
        return status
    try:
        shim.unlink()
    except OSError as exc:
        status.action, status.reason = "error", f"Could not remove {shim}: {exc}"
        return status
    status.action, status.executed, status.reason = "removed", True, "Removed the Cyber Health command link."
    return status


def command_shim_note(status: CommandShimStatus) -> str:
    """One sentence for the report message when the user has to act, else empty."""
    if status.action in ("refused", "error"):
        return f"The cyber-health command was not linked: {status.reason}"
    if status.action in ("linked", "unchanged", "planned") and not status.on_path:
        return f"Add {Path(status.path).parent} to PATH to run cyber-health directly: {status.path_hint}"
    return ""


def command_shim_report_lines(status: CommandShimStatus) -> list[str]:
    lines = [
        "--- cyber-health Command ---",
        f"Action       : {status.action}",
        f"Link         : {status.path or 'N/A'}",
        f"On PATH      : {status.on_path}",
        f"Reason       : {status.reason or 'N/A'}",
    ]
    if status.path_hint:
        lines.append(f"Add to PATH  : {status.path_hint}")
    return [*lines, ""]
