from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cyber_health.core_release import CORE_RELEASE_API, CoreReleaseError, cache_core_release, resolve_latest_core_release


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
