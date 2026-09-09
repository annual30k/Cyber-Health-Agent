"""Opt-in real host discovery probe; never uses the user's OpenClaw state."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    binary = shutil.which("openclaw")
    if not binary:
        raise SystemExit("OpenClaw is not installed")
    names = ["get_profile", "update_profile", "get_today", "log_meal", "get_audit_trail", "health_check", "get_schedule"]
    with tempfile.TemporaryDirectory(prefix="cyber-health-host-probe-") as directory:
        sandbox = Path(directory)
        env = dict(os.environ, OPENCLAW_STATE_DIR=str(sandbox / "state"),
                   OPENCLAW_CONFIG_PATH=str(sandbox / "openclaw.json"))
        commands = [
            ["mcp", "add", "cyber-health", "--command", str(root / ".venv/bin/python"),
             "--arg", "-m", "--arg", "cyber_health_mcp", "--cwd", str(root),
             "--env", "CYBER_HEALTH_DB=" + str(sandbox / "health.sqlite3"),
             "--include", ",".join("cyber_health_" + name for name in names)],
            ["mcp", "doctor", "cyber-health", "--probe", "--json"],
        ]
        for arguments in commands:
            result = subprocess.run([binary, *arguments], env=env, cwd=sandbox,
                                    capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            print(result.stderr, end="")
            if result.returncode:
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
