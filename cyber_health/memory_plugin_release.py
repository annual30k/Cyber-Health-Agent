"""Fetch and verify published Obsidian Memory plugin release artifacts.

Cyber Health deliberately does not bundle the public plugin.  When OpenClaw
needs it, this module obtains the newest *published* GitHub Release artifact,
checks its SHA-256 digest, and stores it below the user's Cyber Health install.
It never treats a repository branch as an installable release.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from urllib.request import Request, urlopen


MEMORY_PLUGIN_REPOSITORY = "annual30k/obsidian-memory-plugin"
MEMORY_PLUGIN_RELEASE_API = (
    f"https://api.github.com/repos/{MEMORY_PLUGIN_REPOSITORY}/releases/latest"
)
ARCHIVE_NAME = re.compile(r"^obsidian-memory-plugin-[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\.tgz$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class MemoryPluginReleaseError(RuntimeError):
    """A release is missing, malformed, untrusted, or could not be fetched."""


@dataclass(frozen=True)
class MemoryPluginRelease:
    version: str
    archive_name: str
    archive_url: str
    sha256: str
    release_url: str


@dataclass
class MemoryPluginReleaseStatus:
    action: str = "skipped"  # skipped, planned, downloaded, reused, error
    version: str = ""
    archive_path: str = ""
    sha256: str = ""
    reason: str = ""
    executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FetchBytes = Callable[[str], bytes]


def _fetch_bytes(url: str) -> bytes:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "cyber-health-agent"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:  # nosec B310: fixed HTTPS GitHub URLs
            return response.read()
    except Exception as exc:  # pragma: no cover - exact networking errors vary by platform
        raise MemoryPluginReleaseError(f"Could not fetch the Obsidian Memory plugin release: {exc}") from exc


def _asset_sha256(asset: dict[str, Any], assets: list[dict[str, Any]], fetch: FetchBytes) -> str:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        candidate = digest.split(":", 1)[1].lower()
        if SHA256.fullmatch(candidate):
            return candidate

    checksum_asset = next(
        (
            item for item in assets
            if isinstance(item.get("name"), str) and item["name"] in {"SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"}
        ),
        None,
    )
    if not checksum_asset or not isinstance(checksum_asset.get("browser_download_url"), str):
        raise MemoryPluginReleaseError(
            "Latest Obsidian Memory plugin Release has no SHA-256 digest. "
            "Publish the tgz asset with GitHub's digest field or a SHA256SUMS/checksums.txt asset."
        )
    lines = fetch(checksum_asset["browser_download_url"]).decode("utf-8", errors="strict").splitlines()
    archive_name = asset["name"]
    for line in lines:
        match = re.fullmatch(r"\s*([a-fA-F0-9]{64})\s+\*?(.+?)\s*", line)
        if match and match.group(2) == archive_name:
            return match.group(1).lower()
    raise MemoryPluginReleaseError(f"Checksum manifest has no SHA-256 entry for {archive_name}.")


def resolve_latest_memory_plugin_release(fetch: FetchBytes = _fetch_bytes) -> MemoryPluginRelease:
    """Resolve the newest stable release and its independently published hash."""
    try:
        raw = json.loads(fetch(MEMORY_PLUGIN_RELEASE_API).decode("utf-8"))
    except MemoryPluginReleaseError:
        raise
    except Exception as exc:
        raise MemoryPluginReleaseError(f"GitHub returned invalid release metadata: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("draft") or raw.get("prerelease"):
        raise MemoryPluginReleaseError("No stable Obsidian Memory plugin Release is available.")
    tag = raw.get("tag_name")
    release_url = raw.get("html_url")
    assets = raw.get("assets")
    if not isinstance(tag, str) or not isinstance(release_url, str) or not isinstance(assets, list):
        raise MemoryPluginReleaseError("Latest Obsidian Memory plugin Release metadata is incomplete.")
    safe_assets = [item for item in assets if isinstance(item, dict)]
    archive = next(
        (
            item for item in safe_assets
            if isinstance(item.get("name"), str)
            and ARCHIVE_NAME.fullmatch(item["name"])
            and isinstance(item.get("browser_download_url"), str)
            and item["browser_download_url"].startswith("https://")
        ),
        None,
    )
    if archive is None:
        raise MemoryPluginReleaseError("Latest Obsidian Memory plugin Release has no valid obsidian-memory-plugin-*.tgz asset.")
    version = tag.removeprefix("v")
    archive_version = archive["name"][len("obsidian-memory-plugin-"):-len(".tgz")]
    if version != archive_version:
        raise MemoryPluginReleaseError(
            f"Latest Release tag ({tag}) does not match its plugin archive ({archive['name']})."
        )
    return MemoryPluginRelease(
        version=version,
        archive_name=archive["name"],
        archive_url=archive["browser_download_url"],
        sha256=_asset_sha256(archive, safe_assets, fetch),
        release_url=release_url,
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def cache_memory_plugin_release(
    release: MemoryPluginRelease,
    cache_dir: Path,
    *,
    fetch: FetchBytes = _fetch_bytes,
) -> MemoryPluginReleaseStatus:
    """Download and atomically cache a verified release artifact.

    A matching regular cached file is reused.  Never replace a cache entry
    until the candidate bytes have passed the release's published digest.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / release.archive_name
    if archive_path.exists() and archive_path.is_file() and not archive_path.is_symlink():
        if _sha256_bytes(archive_path.read_bytes()) == release.sha256:
            return MemoryPluginReleaseStatus(
                action="reused", version=release.version, archive_path=str(archive_path),
                sha256=release.sha256, reason="Reused a SHA-256-verified cached plugin Release.", executed=True,
            )
    content = fetch(release.archive_url)
    actual = _sha256_bytes(content)
    if actual != release.sha256:
        raise MemoryPluginReleaseError(
            f"Downloaded Obsidian Memory plugin checksum mismatch (expected {release.sha256}, got {actual})."
        )
    fd, temporary = tempfile.mkstemp(prefix=f".{release.archive_name}.", suffix=".tmp", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, archive_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return MemoryPluginReleaseStatus(
        action="downloaded", version=release.version, archive_path=str(archive_path),
        sha256=release.sha256, reason="Downloaded and SHA-256-verified the latest stable plugin Release.", executed=True,
    )
