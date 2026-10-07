"""End-to-end upgrade smoke test: the previous published Release upgrades to this build.

What runs is real: the previous Release's own installer and updater, the version
hand-off, and this build finishing the upgrade from a wheel (release mode, no source
tree). Only the GitHub "latest Release" lookup is replaced, through a sitecustomize
hook that activates solely when CYBER_HEALTH_SMOKE_RELEASE points at a local JSON
description of the wheel to serve. Everything happens inside a temporary directory.

    uv run python scripts/upgrade_smoke.py [--parent-wheel PATH] [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "annual30k/cyber-health-agent"
SMOKE_ENV = "CYBER_HEALTH_SMOKE_RELEASE"
SITECUSTOMIZE = f'''
"""Upgrade smoke test hook: serve a local wheel as the "latest Release" (inert unless {SMOKE_ENV} is set)."""
import json, os
from pathlib import Path

_spec = os.environ.get("{SMOKE_ENV}")
if _spec:
    import cyber_health.core_release as _core

    def _local_release(*_args, **_kwargs):
        data = json.loads(Path(_spec).read_text(encoding="utf-8"))
        wheel = Path(data["wheel_path"])
        return _core.CoreRelease(data["version"], wheel.name, wheel.as_uri(), data["sha256"], "file://smoke-test")

    _core.resolve_latest_core_release = _local_release
'''
IGNORED = shutil.ignore_patterns(".git", ".venv", "dist", "data", "__pycache__", ".ruff_cache", ".pytest_cache", "*.sqlite3*")


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, check=False)
    if result.returncode != 0 and "--json" not in cmd:
        raise SmokeFailure(f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def run_json(cmd: list[str], *, env: dict[str, str], cwd: Path) -> dict:
    result = run(cmd, env=env, cwd=cwd)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise SmokeFailure(f"non-JSON output from {' '.join(cmd)}:\n{result.stdout}\n{result.stderr}") from exc


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def venv_paths(python: Path) -> tuple[Path, Path]:
    out = run([str(python), "-c", "import sysconfig, json; p = sysconfig.get_paths(); print(json.dumps([p['scripts'], p['purelib']]))"])
    scripts, purelib = json.loads(out.stdout)
    return Path(scripts), Path(purelib)


def exe(scripts: Path, name: str) -> Path:
    return scripts / (f"{name}.exe" if os.name == "nt" else name)


def download_previous_release(work: Path) -> tuple[Path, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "cyber-health-upgrade-smoke"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    with urlopen(Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", headers=headers), timeout=30) as resp:
        release = json.loads(resp.read())
    asset = next(a for a in release["assets"] if a["name"].endswith("-py3-none-any.whl"))
    wheel = work / "parent" / asset["name"]
    wheel.parent.mkdir(parents=True)
    with urlopen(Request(asset["browser_download_url"], headers={"User-Agent": headers["User-Agent"]}), timeout=120) as resp:
        wheel.write_bytes(resp.read())
    digest = (asset.get("digest") or "").removeprefix("sha256:")
    if digest and sha256(wheel) != digest:
        raise SmokeFailure(f"downloaded {asset['name']} does not match its published digest")
    return wheel, release["tag_name"].removeprefix("v")


def wheel_version(wheel: Path) -> str:
    match = re.match(r"^cyber_health_agent-(.+?)-py3-none-any\.whl$", wheel.name)
    if not match:
        raise SmokeFailure(f"unexpected wheel name {wheel.name}")
    return match.group(1)


def build_candidate(work: Path) -> tuple[Path, str]:
    """Build this source tree as ``<version>+smoke`` so it always differs from the parent."""
    src = work / "candidate-src"
    shutil.copytree(REPO_ROOT, src, ignore=IGNORED)
    pyproject = src / "pyproject.toml"
    base = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    version = f"{base.split('+')[0]}+smoke"
    pyproject.write_text(re.sub(r'^version = "[^"]+"', f'version = "{version}"', pyproject.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE), encoding="utf-8")
    for init in (src / "cyber_health" / "__init__.py", src / "cyber_health_mcp" / "__init__.py"):
        init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    out = work / "candidate"
    run(["uv", "build", "--wheel", "-o", str(out), str(src)])
    return next(out.glob("*.whl")), version


def serve(python: Path, wheel: Path, version: str, spec: Path) -> None:
    """Install the hook into a venv and describe the wheel it should serve."""
    _, purelib = venv_paths(python)
    (purelib / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    spec.write_text(json.dumps({"version": version, "wheel_path": str(wheel), "sha256": sha256(wheel)}), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--parent-wheel", type=Path, help="Use this wheel instead of the latest published Release")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory for inspection")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="cyber-health-upgrade-smoke-")).resolve()
    neutral = work / "neutral-cwd"  # never run from a source checkout: it would shadow the installed code
    neutral.mkdir()
    target = work / "home" / ".cyber-health"
    env = dict(os.environ)
    env["CYBER_HEALTH_USER_BIN_DIR"] = str(work / "user-bin")
    env.pop("CYBER_HEALTH_NO_HANDOFF", None)
    try:
        if args.parent_wheel:
            parent_wheel, parent_version = args.parent_wheel.resolve(), wheel_version(args.parent_wheel)
        else:
            parent_wheel, parent_version = download_previous_release(work)
        candidate_wheel, candidate_version = build_candidate(work)
        print(f"parent {parent_version} ({parent_wheel.name}) -> candidate {candidate_version}")

        print("1. install the previous Release with its own installer")
        boot = work / "boot-venv"
        run(["uv", "venv", "--python", sys.executable, str(boot)])
        boot_python = exe(venv_paths_from(boot), "python")
        run(["uv", "pip", "install", "--python", str(boot_python), str(parent_wheel)])
        serve(boot_python, parent_wheel, parent_version, work / "parent.json")
        install = run_json(
            [str(exe(venv_paths(boot_python)[0], "cyber-health")), "install", "--target-dir", str(target),
             "--skip-openclaw", "--skip-codex", "--skip-hermes", "--json"],
            env={**env, SMOKE_ENV: str(work / "parent.json")}, cwd=neutral,
        )
        check(install.get("success") is True, f"parent install succeeded ({install.get('message')})")

        target_python = exe(venv_paths_from(target / "venv"), "python")
        target_scripts, _ = venv_paths(target_python)
        db = target / "data" / "cyber-health.sqlite3"
        run([str(target_python), "-P", "-c",
             "import sys; from cyber_health import CyberHealthService as S; s = S(sys.argv[1]); "
             "s.log_meal(occurred_at='2026-10-07T12:00:00+08:00', meal_type='lunch', foods=[{'name': 'rice'}], "
             "kcal_low=500, kcal_high=600, idempotency_key='smoke-lunch')", str(db)], cwd=neutral)

        print("2. upgrade with the previous Release's own updater")
        serve(target_python, candidate_wheel, candidate_version, work / "candidate.json")
        update_env = {**env, SMOKE_ENV: str(work / "candidate.json")}
        update_cmd = [str(exe(target_scripts, "cyber-health")), "update", "--target-dir", str(target),
                      "--openclaw-bin", "", "--codex-bin", "", "--hermes-bin", "", "--json"]
        report = run_json(update_cmd, env=update_env, cwd=neutral)
        check(report.get("success") is True, f"update succeeded ({report.get('message')})")
        check(report.get("old_version") == parent_version, f"old version reported as {parent_version}")
        check(report.get("new_version") == candidate_version, f"new version reported as {candidate_version}")
        if "handed_off" in report:  # parents from 0.5.1 on hand the finish step to the new build
            check(report["handed_off"] is True, "previous updater handed off to the new build")
            check(report.get("finished_by") == candidate_version, "the new build finished its own upgrade")
        check(report["backup"]["created"] and Path(report["backup"]["backup_file"]).is_file(), "verified backup written")

        meta = json.loads((target / "config" / "installation.json").read_text(encoding="utf-8"))
        check(meta.get("version") == candidate_version, f"installation metadata records {candidate_version}")
        installed = run([str(target_python), "-P", "-c", "import cyber_health; print(cyber_health.__version__)"], cwd=neutral)
        check(installed.stdout.strip() == candidate_version, "installed package is the new build")
        with closing(sqlite3.connect(db)) as conn:
            check(conn.execute("SELECT COUNT(*) FROM meal_log WHERE status = 'active'").fetchone()[0] == 1, "health data preserved")
            check(conn.execute("PRAGMA user_version").fetchone()[0] >= 1, "schema version recorded")
        shim = work / "user-bin" / ("cyber-health.cmd" if os.name == "nt" else "cyber-health")
        check(shim.exists() or shim.is_symlink(), "cyber-health command linked into the user bin dir")
        if os.name != "nt":
            check(target.stat().st_mode & 0o777 == 0o700, "installation directory is owner-only")

        print("3. a second update is a no-op on the latest Release")
        again = run_json(update_cmd, env=update_env, cwd=neutral)
        check(again.get("success") is True and again.get("up_to_date") is True, "second update reports already up to date")
        status = run_json([str(exe(target_scripts, "cyber-health")), "status", "--target-dir", str(target), "--json"],
                          env=update_env, cwd=neutral)
        check(status.get("version") == candidate_version, f"status shows {candidate_version}")
        print("upgrade smoke test passed")
        return 0
    except SmokeFailure as failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def venv_paths_from(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


if __name__ == "__main__":
    sys.exit(main())
