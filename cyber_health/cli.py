"""Unified CLI management tool for Cyber Health Agent.

Usage:
  cyber-health install [--target-dir DIR] [--db DB] [--dry-run] [--json]
  cyber-health update [--target-dir DIR] [--dry-run] [--json]
  cyber-health uninstall [--purge-data] [--confirm-purge TOKEN] [--dry-run] [--json]
  cyber-health status [--target-dir DIR] [--json]
  cyber-health mcp [--db DB] [--allow-all]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .install import (
    FIXED_OPENCLAW_SERVER_NAME,
    DEFAULT_INSTALL_DIR_NAME,
    get_executable_name,
    get_venv_bin_dir,
    verify_sqlite_integrity,
)
from .uninstall import verify_cyber_health_command_signature
from .health_memory import HealthManagerMemoryStatus, inspect_health_manager_memory
from .codex_integration import find_codex_cli, inspect_codex_registration


def run_status(target_dir_str: str | None, as_json: bool = False) -> int:
    target_dir = Path(target_dir_str) if target_dir_str else Path.home() / DEFAULT_INSTALL_DIR_NAME
    config_file = target_dir / "config" / "installation.json"
    db_file = target_dir / "data" / "cyber-health.sqlite3"
    backups_dir = target_dir / "data" / "backups"
    venv_python = get_venv_bin_dir(target_dir / "venv") / get_executable_name("python")
    mcp_bin = get_venv_bin_dir(target_dir / "venv") / get_executable_name("cyber-health-mcp")

    status_data: dict[str, Any] = {
        "installed": target_dir.exists() and config_file.exists(),
        "target_dir": str(target_dir),
        "version": "not installed",
        "installed_at": None,
        "database": {
            "exists": db_file.exists(),
            "path": str(db_file),
            "size_bytes": db_file.stat().st_size if db_file.exists() else 0,
            "integrity": "not checked",
        },
        "backups_count": 0,
        "recent_backups": [],
        "venv_ready": venv_python.exists() and mcp_bin.exists(),
        "openclaw_registered": False,
        "openclaw_details": {},
        "codex_registered": False,
        "codex_details": {},
        "health_manager_memory": HealthManagerMemoryStatus().to_dict(),
    }

    if config_file.exists():
        try:
            meta = json.loads(config_file.read_text(encoding="utf-8"))
            status_data["version"] = meta.get("version", "unknown")
            status_data["installed_at"] = meta.get("installed_at")
        except Exception:
            status_data["version"] = "error reading metadata"

    if db_file.exists():
        ok, msg = verify_sqlite_integrity(db_file)
        status_data["database"]["integrity"] = "ok" if ok else f"failed ({msg})"

    if backups_dir.exists():
        backups = sorted([b for b in backups_dir.iterdir() if b.name.endswith(".sqlite3")], key=lambda p: p.name)
        status_data["backups_count"] = len(backups)
        status_data["recent_backups"] = [b.name for b in backups[-5:]]

    # Check OpenClaw registration
    openclaw_bin = shutil.which("openclaw")
    if openclaw_bin:
        status_data["health_manager_memory"] = inspect_health_manager_memory(openclaw_bin).to_dict()
        try:
            res = subprocess.run(
                [openclaw_bin, "mcp", "show", FIXED_OPENCLAW_SERVER_NAME, "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0:
                try:
                    raw_data = json.loads(res.stdout)
                    if isinstance(raw_data, dict) and verify_cyber_health_command_signature(
                        raw_data.get("command"), raw_data.get("args")
                    ):
                        status_data["openclaw_registered"] = True
                        status_data["openclaw_details"] = {
                            "command": raw_data.get("command"),
                            "cwd": raw_data.get("cwd"),
                            "ownership_verified": True,
                        }
                    else:
                        status_data["openclaw_details"] = {
                            "ownership_verified": False,
                            "reason": "Registration exists but is not a recognized Cyber Health command",
                        }
                except Exception:
                    status_data["openclaw_details"] = {
                        "ownership_verified": False,
                        "reason": "OpenClaw returned malformed JSON",
                    }
        except Exception:
            pass

    codex_bin = find_codex_cli()
    if codex_bin:
        codex_status = inspect_codex_registration(codex_bin, target_dir, db_file)
        status_data["codex_registered"] = (
            codex_status.detected and codex_status.ownership_proven
        )
        status_data["codex_details"] = {
            "command": codex_status.command,
            "ownership_verified": codex_status.ownership_proven,
            "reason": codex_status.reason,
        }

    if as_json:
        print(json.dumps(status_data, indent=2))
    else:
        print("============================================================")
        print("Cyber Health Agent Status")
        print("============================================================")
        print(f"Installed           : {'YES' if status_data['installed'] else 'NO'}")
        print(f"Target Directory    : {status_data['target_dir']}")
        print(f"Version             : {status_data['version']}")
        print(f"Installed At        : {status_data['installed_at'] or 'N/A'}")
        print(f"Python Runtime      : {'Ready' if status_data['venv_ready'] else 'Missing'}")
        print(f"Database Exists     : {status_data['database']['exists']}")
        print(f"Database Path       : {status_data['database']['path']}")
        print(f"Database Size       : {status_data['database']['size_bytes'] / (1024*1024):.2f} MB")
        print(f"Database Integrity  : {status_data['database']['integrity']}")
        print(f"Backups Available   : {status_data['backups_count']} snapshots")
        if status_data["recent_backups"]:
            print(f"Recent Snapshot     : {status_data['recent_backups'][-1]}")
        print(f"OpenClaw Registered : {'YES' if status_data['openclaw_registered'] else 'NO'}")
        if status_data["openclaw_registered"]:
            print(f"OpenClaw Command    : {status_data['openclaw_details'].get('command')}")
        print(f"Codex Registered    : {'YES' if status_data['codex_registered'] else 'NO'}")
        if status_data["codex_registered"]:
            print(f"Codex Command       : {status_data['codex_details'].get('command')}")
        memory = status_data["health_manager_memory"]
        print(f"Health Memory       : {memory.get('state', 'unavailable')}")
        if memory.get("reason"):
            print(f"Memory Reason       : {memory['reason']}")
        print("============================================================")

    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="cyber-health",
        description="Unified management CLI for Cyber Health Agent.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to run")

    # install
    install_parser = subparsers.add_parser("install", help="Install Cyber Health Agent into ~/.cyber-health")
    install_parser.add_argument("--target-dir", type=str, default=None)
    install_parser.add_argument("--db", type=str, default=None)
    install_parser.add_argument("--project-root", type=str, default=None)
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--json", action="store_true")
    install_parser.add_argument("--no-uv", action="store_true")
    install_parser.add_argument("--editable", action="store_true")
    install_parser.add_argument("--skip-openclaw", action="store_true")
    install_parser.add_argument("--skip-codex", action="store_true")
    install_parser.add_argument("--codex-bin", type=str, default=None)
    install_parser.add_argument("--codex-home", type=str, default=None)

    # update
    update_parser = subparsers.add_parser("update", help="Update Cyber Health Agent with automatic database backup")
    update_parser.add_argument("--target-dir", type=str, default=None)
    update_parser.add_argument("--project-root", type=str, default=None)
    update_parser.add_argument("--dry-run", action="store_true")
    update_parser.add_argument("--json", action="store_true")
    update_parser.add_argument("--no-uv", action="store_true")
    update_parser.add_argument("--codex-bin", type=str, default=None)
    update_parser.add_argument("--codex-home", type=str, default=None)

    # uninstall
    uninstall_parser = subparsers.add_parser("uninstall", help="Safely uninstall Cyber Health Agent host integrations")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true")
    uninstall_parser.add_argument("--purge-data", action="store_true")
    uninstall_parser.add_argument("--confirm-purge", type=str, default=None)
    uninstall_parser.add_argument("--project-root", type=str, default=None)
    uninstall_parser.add_argument("--db", type=str, default=None)
    uninstall_parser.add_argument("--codex-bin", type=str, default=None)
    uninstall_parser.add_argument("--codex-home", type=str, default=None)

    # status
    status_parser = subparsers.add_parser("status", help="Show Cyber Health Agent installation and database status")
    status_parser.add_argument("--target-dir", type=str, default=None)
    status_parser.add_argument("--json", action="store_true")

    # mcp
    mcp_parser = subparsers.add_parser("mcp", help="Run the Cyber Health stdio MCP server directly")
    mcp_parser.add_argument("--db", dest="db_path", type=str, default=None)
    mcp_parser.add_argument("--allow-all", dest="allow_all", action="store_true", default=False)

    if not argv or argv in (["--help"], ["-h"], ["help"]):
        parser.print_help()
        return 0

    args, unknown = parser.parse_known_args(argv)

    if args.command == "install":
        from .install import main as install_main
        return install_main(argv[1:])
    elif args.command == "update":
        from .update import main as update_main
        return update_main(argv[1:])
    elif args.command == "uninstall":
        from .uninstall import main as uninstall_main
        return uninstall_main(argv[1:])
    elif args.command == "status":
        return run_status(args.target_dir, as_json=args.json)
    elif args.command == "mcp":
        from cyber_health_mcp.server import main as mcp_main
        try:
            mcp_idx = argv.index("mcp")
            mcp_args = argv[:mcp_idx] + argv[mcp_idx + 1:]
        except ValueError:
            mcp_args = list(argv)
        res = mcp_main(mcp_args)
        return res if isinstance(res, int) else 0
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
