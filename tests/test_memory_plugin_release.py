from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cyber_health.memory_plugin_release import (
    MEMORY_PLUGIN_RELEASE_API,
    MemoryPluginReleaseError,
    cache_memory_plugin_release,
    resolve_latest_memory_plugin_release,
)


class TestMemoryPluginRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-plugin-release-")
        self.cache = Path(self.temp_dir.name) / "plugins"
        self.archive = b"verified plugin archive"
        self.sha256 = hashlib.sha256(self.archive).hexdigest()
        self.archive_url = "https://github.com/annual30k/obsidian-memory-plugin/releases/download/v0.4.0/obsidian-memory-plugin-0.4.0.tgz"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def fetch(self, url: str) -> bytes:
        if url == MEMORY_PLUGIN_RELEASE_API:
            return json.dumps({
                "tag_name": "v0.4.0", "html_url": "https://github.com/annual30k/obsidian-memory-plugin/releases/tag/v0.4.0",
                "draft": False, "prerelease": False,
                "assets": [{"name": "obsidian-memory-plugin-0.4.0.tgz", "browser_download_url": self.archive_url, "digest": f"sha256:{self.sha256}"}],
            }).encode()
        if url == self.archive_url:
            return self.archive
        raise AssertionError(url)

    def test_resolves_and_verifies_published_release(self) -> None:
        release = resolve_latest_memory_plugin_release(self.fetch)
        self.assertEqual(release.version, "0.4.0")
        first = cache_memory_plugin_release(release, self.cache, fetch=self.fetch)
        second = cache_memory_plugin_release(release, self.cache, fetch=self.fetch)
        self.assertEqual(first.action, "downloaded")
        self.assertEqual(second.action, "reused")
        self.assertEqual(Path(first.archive_path).read_bytes(), self.archive)

    def test_rejects_release_without_checksum(self) -> None:
        def fetch(url: str) -> bytes:
            return json.dumps({
                "tag_name": "v0.4.0", "html_url": "https://github.com/annual30k/obsidian-memory-plugin/releases/tag/v0.4.0",
                "draft": False, "prerelease": False,
                "assets": [{"name": "obsidian-memory-plugin-0.4.0.tgz", "browser_download_url": self.archive_url}],
            }).encode()
        with self.assertRaisesRegex(MemoryPluginReleaseError, "SHA-256"):
            resolve_latest_memory_plugin_release(fetch)

    def test_rejects_tag_and_artifact_version_mismatch(self) -> None:
        def fetch(_url: str) -> bytes:
            return json.dumps({
                "tag_name": "v0.4.0", "html_url": "https://github.com/annual30k/obsidian-memory-plugin/releases/tag/v0.4.0",
                "draft": False, "prerelease": False,
                "assets": [{"name": "obsidian-memory-plugin-0.3.9.tgz", "browser_download_url": self.archive_url, "digest": f"sha256:{self.sha256}"}],
            }).encode()
        with self.assertRaisesRegex(MemoryPluginReleaseError, "does not match"):
            resolve_latest_memory_plugin_release(fetch)

    def test_rejects_checksum_mismatch_without_writing_archive(self) -> None:
        release = resolve_latest_memory_plugin_release(self.fetch)
        with self.assertRaisesRegex(MemoryPluginReleaseError, "checksum mismatch"):
            cache_memory_plugin_release(release, self.cache, fetch=lambda _url: b"tampered")
        self.assertFalse((self.cache / "obsidian-memory-plugin-0.4.0.tgz").exists())
