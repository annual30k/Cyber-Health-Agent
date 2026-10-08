from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from cyber_health.core_release import (
    CORE_RELEASE_API,
    CoreReleaseError,
    CoreReleaseFetchError,
    _latest_tag_via_web,
    _request,
    cache_core_release,
    resolve_latest_core_release,
)


class TestCoreRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-core-release-")
        self.cache = Path(self.temp_dir.name) / "releases"
        self.wheel = b"verified core wheel"
        self.sha256 = hashlib.sha256(self.wheel).hexdigest()
        self.wheel_url = "https://github.com/annual30k/cyber-health-agent/releases/download/v0.3.0/cyber_health_agent-0.3.0-py3-none-any.whl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def fetch(self, url: str) -> bytes:
        if url == CORE_RELEASE_API:
            return json.dumps({"tag_name": "v0.3.0", "html_url": "https://github.com/annual30k/cyber-health-agent/releases/tag/v0.3.0", "draft": False, "prerelease": False, "assets": [{"name": "cyber_health_agent-0.3.0-py3-none-any.whl", "browser_download_url": self.wheel_url, "digest": f"sha256:{self.sha256}"}]}).encode()
        if url == self.wheel_url:
            return self.wheel
        raise AssertionError(url)

    def test_resolves_and_caches_verified_wheel(self) -> None:
        release = resolve_latest_core_release(self.fetch)
        first = cache_core_release(release, self.cache, fetch=self.fetch)
        second = cache_core_release(release, self.cache, fetch=self.fetch)
        self.assertEqual(release.version, "0.3.0")
        self.assertEqual(first.action, "downloaded")
        self.assertEqual(second.action, "reused")

    def test_rejects_missing_checksum(self) -> None:
        def fetch(_url: str) -> bytes:
            return json.dumps({"tag_name": "v0.3.0", "html_url": "https://github.com/annual30k/cyber-health-agent/releases/tag/v0.3.0", "draft": False, "prerelease": False, "assets": [{"name": "cyber_health_agent-0.3.0-py3-none-any.whl", "browser_download_url": self.wheel_url}]}).encode()
        with self.assertRaisesRegex(CoreReleaseError, "SHA-256"):
            resolve_latest_core_release(fetch)

    def test_rejects_tag_wheel_version_mismatch(self) -> None:
        def fetch(_url: str) -> bytes:
            return json.dumps({"tag_name": "v0.3.0", "html_url": "https://github.com/annual30k/cyber-health-agent/releases/tag/v0.3.0", "draft": False, "prerelease": False, "assets": [{"name": "cyber_health_agent-0.2.9-py3-none-any.whl", "browser_download_url": self.wheel_url, "digest": f"sha256:{self.sha256}"}]}).encode()
        with self.assertRaisesRegex(CoreReleaseError, "does not match"):
            resolve_latest_core_release(fetch)


class WebFallbackTests(unittest.TestCase):
    """When the API is rate-limited or down, the latest Release resolves through github.com."""

    TAG_URL = "https://github.com/annual30k/cyber-health-agent/releases/download/v0.7.0"
    WHEEL = "cyber_health_agent-0.7.0-py3-none-any.whl"
    SHA = "b" * 64

    def fetch(self, manifest: str):
        calls = []

        def fetch(url: str) -> bytes:
            calls.append(url)
            if url == CORE_RELEASE_API:
                raise CoreReleaseFetchError("HTTP Error 403: rate limit exceeded")
            if url == f"{self.TAG_URL}/SHA256SUMS":
                return manifest.encode()
            raise AssertionError(url)

        return fetch, calls

    def test_rate_limited_api_falls_back_to_tag_redirect_and_checksums(self) -> None:
        fetch, calls = self.fetch(f"{self.SHA}  dist/{self.WHEEL}\n{'c' * 64}  dist/cyber_health_agent-0.7.0.tar.gz\n")
        release = resolve_latest_core_release(fetch, latest_tag=lambda: "v0.7.0")
        self.assertEqual(release.version, "0.7.0")
        self.assertEqual(release.sha256, self.SHA)
        self.assertEqual(release.wheel_url, f"{self.TAG_URL}/{self.WHEEL}")
        self.assertEqual(release.release_url, "https://github.com/annual30k/cyber-health-agent/releases/tag/v0.7.0")
        self.assertEqual(calls[0], CORE_RELEASE_API)

    def test_fallback_refuses_a_wheel_without_checksum(self) -> None:
        fetch, _ = self.fetch(f"{'c' * 64}  dist/cyber_health_agent-0.7.0.tar.gz\n")
        with self.assertRaisesRegex(CoreReleaseError, "unverified"):
            resolve_latest_core_release(fetch, latest_tag=lambda: "v0.7.0")

    def test_invalid_api_metadata_is_not_masked_by_the_fallback(self) -> None:
        def broken_metadata(_url: str) -> bytes:
            return json.dumps({"tag_name": "v0.7.0", "html_url": "x", "assets": []}).encode()

        def must_not_run() -> str:
            raise AssertionError("fallback must not run when the API answered")

        with self.assertRaisesRegex(CoreReleaseError, "no valid"):
            resolve_latest_core_release(broken_metadata, latest_tag=must_not_run)

    def test_error_names_both_failures(self) -> None:
        fetch, _ = self.fetch("")

        def offline() -> str:
            raise CoreReleaseFetchError("could not reach github.com")

        with self.assertRaises(CoreReleaseError) as caught:
            resolve_latest_core_release(fetch, latest_tag=offline)
        self.assertIn("rate limit exceeded", str(caught.exception))
        self.assertIn("could not reach github.com", str(caught.exception))

    def test_api_manifest_entries_with_a_directory_prefix_match(self) -> None:
        wheel_url = f"{self.TAG_URL}/{self.WHEEL}"

        def fetch(url: str) -> bytes:
            if url == CORE_RELEASE_API:
                return json.dumps({"tag_name": "v0.7.0", "html_url": "x", "draft": False, "prerelease": False, "assets": [
                    {"name": self.WHEEL, "browser_download_url": wheel_url},
                    {"name": "SHA256SUMS", "browser_download_url": f"{self.TAG_URL}/SHA256SUMS"},
                ]}).encode()
            return f"{self.SHA}  dist/{self.WHEEL}\n".encode()

        self.assertEqual(resolve_latest_core_release(fetch).sha256, self.SHA)

    def test_token_is_sent_only_to_the_api(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret-token"}):
            api = _request(CORE_RELEASE_API)
            download = _request(f"{self.TAG_URL}/{self.WHEEL}")
        self.assertEqual(api.get_header("Authorization"), "Bearer secret-token")
        self.assertIsNone(download.get_header("Authorization"))

    def test_latest_tag_is_read_from_the_redirect_without_following_it(self) -> None:
        def opener_raising(error):
            opener = mock.MagicMock()
            opener.open.side_effect = error
            return opener

        redirect = HTTPError("u", 302, "Found", {"Location": "https://github.com/a/b/releases/tag/v0.7.0"}, None)
        with mock.patch("cyber_health.core_release.build_opener", return_value=opener_raising(redirect)):
            self.assertEqual(_latest_tag_via_web(), "v0.7.0")
        missing = HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("cyber_health.core_release.build_opener", return_value=opener_raising(missing)), \
                self.assertRaises(CoreReleaseFetchError):
            _latest_tag_via_web()
        elsewhere = HTTPError("u", 302, "Found", {"Location": "https://github.com/a/b/releases"}, None)
        with mock.patch("cyber_health.core_release.build_opener", return_value=opener_raising(elsewhere)), \
                self.assertRaisesRegex(CoreReleaseError, "did not redirect"):
            _latest_tag_via_web()
