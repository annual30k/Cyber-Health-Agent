"""Resolve, verify, and cache published Cyber Health Core wheels."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

CORE_REPOSITORY = "annual30k/cyber-health-agent"
CORE_RELEASE_API = f"https://api.github.com/repos/{CORE_REPOSITORY}/releases/latest"
# Web fallback when the API is rate-limited or unreachable; these URLs use no API quota.
CORE_RELEASES_WEB = f"https://github.com/{CORE_REPOSITORY}/releases"
CHECKSUM_MANIFEST = "SHA256SUMS"
WHEEL_NAME = re.compile(r"^cyber_health_agent-([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?)-py3-none-any\.whl$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CoreReleaseError(RuntimeError):
    """A Core release is unavailable, malformed, or cannot be verified."""


class CoreReleaseFetchError(CoreReleaseError):
    """GitHub could not be reached or refused the request (network, rate limit, 5xx)."""


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


def _manifest_sha256(manifest: str, file_name: str) -> str | None:
    """Look ``file_name`` up in a sha256sum manifest; entries may carry a path such as ``dist/``."""
    for line in manifest.splitlines():
        match = re.fullmatch(r"\s*([a-fA-F0-9]{64})\s+\*?(.+?)\s*", line)
        if match and re.split(r"[\\/]", match.group(2))[-1] == file_name:
            return match.group(1).lower()
    return None


def _request(url: str) -> Request:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "cyber-health-agent"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    # Only the API gets the token; downloads redirect to a CDN that must not receive it.
    if token and urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token.strip()}"
    return Request(url, headers=headers)


def _fetch(url: str) -> bytes:
    try:
        with urlopen(_request(url), timeout=20) as response:  # nosec B310: fixed GitHub HTTPS endpoint
            return response.read()
    except Exception as exc:
        raise CoreReleaseFetchError(f"Could not fetch Cyber Health Core Release: {exc}") from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _latest_tag_via_web() -> str:
    """Read the tag that github.com/<repo>/releases/latest redirects to (stable releases only)."""
    request = Request(f"{CORE_RELEASES_WEB}/latest", method="HEAD", headers={"User-Agent": "cyber-health-agent"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            location = response.geturl()
    except HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise CoreReleaseFetchError(f"GitHub releases page returned HTTP {exc.code}") from exc
        location = exc.headers.get("Location", "")
    except (URLError, OSError) as exc:
        raise CoreReleaseFetchError(f"Could not reach the GitHub releases page: {exc}") from exc
    match = re.search(r"/releases/tag/([^/?#]+)$", location)
    if not match:
        raise CoreReleaseError("GitHub did not redirect to a published Core Release.")
    return match.group(1)


def resolve_latest_core_release_via_web(fetch: FetchBytes = _fetch, latest_tag: Callable[[], str] = _latest_tag_via_web) -> CoreRelease:
    """Resolve the latest stable Release without the API: tag redirect plus the SHA256SUMS asset."""
    tag = latest_tag()
    version = tag.removeprefix("v")
    wheel_name = f"cyber_health_agent-{version}-py3-none-any.whl"
    match = WHEEL_NAME.fullmatch(wheel_name)
    if not match or match.group(1) != version:
        raise CoreReleaseError(f"Latest Core Release tag {tag!r} is not a valid version.")
    download = f"{CORE_RELEASES_WEB}/download/{tag}"
    sha256 = _manifest_sha256(fetch(f"{download}/{CHECKSUM_MANIFEST}").decode("utf-8", errors="strict"), wheel_name)
    if sha256:
        return CoreRelease(version, wheel_name, f"{download}/{wheel_name}", sha256, f"{CORE_RELEASES_WEB}/tag/{tag}")
    raise CoreReleaseError(f"{CHECKSUM_MANIFEST} of {tag} has no entry for {wheel_name}; refusing an unverified wheel.")


def _checksum(asset: dict[str, Any], assets: list[dict[str, Any]], fetch: FetchBytes) -> str:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if SHA256.fullmatch(value):
            return value
    manifest = next((item for item in assets if item.get("name") in {"SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"}), None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("browser_download_url"), str):
        raise CoreReleaseError("Latest Core Release needs a GitHub SHA-256 digest or SHA256SUMS asset.")
    found = _manifest_sha256(fetch(manifest["browser_download_url"]).decode("utf-8", errors="strict"), asset["name"])
    if found:
        return found
    raise CoreReleaseError(f"Checksum manifest has no entry for {asset['name']}.")


def resolve_latest_core_release(
    fetch: FetchBytes = _fetch, latest_tag: Callable[[], str] = _latest_tag_via_web
) -> CoreRelease:
    try:
        raw = fetch(CORE_RELEASE_API)
    except CoreReleaseFetchError as api_error:
        # Rate limit or outage: fall back to the web endpoints (same SHA-256 requirement).
        try:
            return resolve_latest_core_release_via_web(fetch, latest_tag)
        except CoreReleaseError as web_error:
            raise CoreReleaseError(f"{api_error}; web fallback also failed: {web_error}") from web_error
    try:
        payload = json.loads(raw.decode("utf-8"))
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
