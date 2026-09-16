"""Resolve, verify, and cache published Cyber Health Core wheels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from urllib.request import Request, urlopen


CORE_REPOSITORY = "annual30k/cyber-health-agent"
CORE_RELEASE_API = f"https://api.github.com/repos/{CORE_REPOSITORY}/releases/latest"
WHEEL_NAME = re.compile(r"^cyber_health_agent-([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?)-py3-none-any\.whl$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CoreReleaseError(RuntimeError):
    """A Core release is unavailable, malformed, or cannot be verified."""


@dataclass(frozen=True)
class CoreRelease:
    version: str
    wheel_name: str
    wheel_url: str
    sha256: str
    release_url: str


@dataclass
class CoreReleaseStatus:
    action: str = "skipped"  # local, planned, downloaded, reused, error
    version: str = ""
    wheel_path: str = ""
    sha256: str = ""
    reason: str = ""
    executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FetchBytes = Callable[[str], bytes]


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "cyber-health-agent"})
    try:
        with urlopen(request, timeout=20) as response:  # nosec B310: fixed GitHub HTTPS endpoint
            return response.read()
    except Exception as exc:  # pragma: no cover - platform-dependent network details
        raise CoreReleaseError(f"Could not fetch Cyber Health Core Release: {exc}") from exc


def _checksum(asset: dict[str, Any], assets: list[dict[str, Any]], fetch: FetchBytes) -> str:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if SHA256.fullmatch(value):
            return value
    manifest = next((item for item in assets if item.get("name") in {"SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"}), None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("browser_download_url"), str):
        raise CoreReleaseError("Latest Core Release needs a GitHub SHA-256 digest or SHA256SUMS asset.")
    for line in fetch(manifest["browser_download_url"]).decode("utf-8", errors="strict").splitlines():
        match = re.fullmatch(r"\s*([a-fA-F0-9]{64})\s+\*?(.+?)\s*", line)
        if match and match.group(2) == asset["name"]:
            return match.group(1).lower()
    raise CoreReleaseError(f"Checksum manifest has no entry for {asset['name']}.")


def resolve_latest_core_release(fetch: FetchBytes = _fetch) -> CoreRelease:
    try:
        payload = json.loads(fetch(CORE_RELEASE_API).decode("utf-8"))
    except CoreReleaseError:
        raise
    except Exception as exc:
        raise CoreReleaseError(f"GitHub returned invalid Core Release metadata: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise CoreReleaseError("No stable Cyber Health Core Release is available.")
    tag, release_url, assets = payload.get("tag_name"), payload.get("html_url"), payload.get("assets")
    if not isinstance(tag, str) or not isinstance(release_url, str) or not isinstance(assets, list):
        raise CoreReleaseError("Latest Core Release metadata is incomplete.")
    safe_assets = [item for item in assets if isinstance(item, dict)]
    wheel = next((item for item in safe_assets if isinstance(item.get("name"), str) and WHEEL_NAME.fullmatch(item["name"]) and isinstance(item.get("browser_download_url"), str) and item["browser_download_url"].startswith("https://")), None)
    if wheel is None:
        raise CoreReleaseError("Latest Core Release has no valid cyber_health_agent-*-py3-none-any.whl asset.")
    version = tag.removeprefix("v")
    if WHEEL_NAME.fullmatch(wheel["name"]).group(1) != version:
        raise CoreReleaseError(f"Core Release tag ({tag}) does not match wheel ({wheel['name']}).")
    return CoreRelease(version, wheel["name"], wheel["browser_download_url"], _checksum(wheel, safe_assets, fetch), release_url)


def cache_core_release(release: CoreRelease, cache_dir: Path, *, fetch: FetchBytes = _fetch) -> CoreReleaseStatus:
    cache_dir.mkdir(parents=True, exist_ok=True)
    wheel = cache_dir / release.wheel_name
    if wheel.is_file() and not wheel.is_symlink() and hashlib.sha256(wheel.read_bytes()).hexdigest() == release.sha256:
        return CoreReleaseStatus("reused", release.version, str(wheel), release.sha256, "Reused a SHA-256-verified Core wheel.", True)
    content = fetch(release.wheel_url)
    actual = hashlib.sha256(content).hexdigest()
    if actual != release.sha256:
        raise CoreReleaseError(f"Downloaded Core wheel checksum mismatch (expected {release.sha256}, got {actual}).")
    fd, temporary = tempfile.mkstemp(prefix=f".{release.wheel_name}.", suffix=".tmp", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, wheel)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return CoreReleaseStatus("downloaded", release.version, str(wheel), release.sha256, "Downloaded and SHA-256-verified the latest stable Core wheel.", True)
