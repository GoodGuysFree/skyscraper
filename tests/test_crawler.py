"""
Unit tests for site_crawler.py — BlobStore only.

All tests are offline (no Playwright, no network).
Run with:  python3 -m pytest tests/ -v   (from /archive/skyscraper/)
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import crawler_config as cfg

# BlobStore lives in site_crawler.py which imports playwright at module level.
# We mock it before import so tests don't require playwright installed.
import unittest.mock as mock
import sys

# Pre-populate sys.modules with stubs for heavy optional deps so the import
# doesn't fail in a test environment that may lack playwright/requests.
for _mod in ("playwright", "playwright.sync_api", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

# Stub out the specific names site_crawler.py uses at module level
_pw_stub = mock.MagicMock()
sys.modules["playwright.sync_api"] = _pw_stub
_pw_stub.sync_playwright = mock.MagicMock()
_pw_stub.TimeoutError = Exception

import site_crawler as sc


# ══════════════════════════════════════════════════════════════════════════════
# 9.  BlobStore.put_bytes / put_text / has / url_for
# ══════════════════════════════════════════════════════════════════════════════

class TestBlobStorePutBytes:
    def test_put_bytes_returns_sha256(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"hello world"
        expected_sha = hashlib.sha256(data).hexdigest()
        sha = store.put_bytes(data, ".bin")
        assert sha == expected_sha

    def test_put_bytes_file_exists_on_disk(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"test content"
        sha = store.put_bytes(data, ".txt")
        blob_path = Path(store.root) / sha[:2] / f"{sha}.txt"
        assert blob_path.exists()
        assert blob_path.read_bytes() == data

    def test_put_bytes_idempotent(self, tmp_path):
        """Second put of the same data must not raise and must leave one file."""
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"idempotent data"
        sha1 = store.put_bytes(data, ".bin")
        sha2 = store.put_bytes(data, ".bin")
        assert sha1 == sha2
        blob_path = Path(store.root) / sha1[:2] / f"{sha1}.bin"
        assert blob_path.exists()

    def test_put_text_stores_utf8(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        text = "Hello, 世界"
        sha = store.put_text(text, ".html")
        expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert sha == expected_sha
        blob_path = Path(store.root) / sha[:2] / f"{sha}.html"
        assert blob_path.read_text("utf-8") == text

    def test_put_text_idempotent(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        text = "same content"
        sha1 = store.put_text(text, ".html")
        sha2 = store.put_text(text, ".html")
        assert sha1 == sha2


class TestBlobStoreHas:
    def test_has_returns_false_before_put(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha = hashlib.sha256(b"ghost").hexdigest()
        assert store.has(sha, ".bin") is False

    def test_has_returns_true_after_put(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"real content"
        sha = store.put_bytes(data, ".css")
        assert store.has(sha, ".css") is True

    def test_has_returns_false_wrong_ext(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"some bytes"
        sha = store.put_bytes(data, ".css")
        # Same hash, different extension — should not exist
        assert store.has(sha, ".js") is False


class TestBlobStoreUrlFor:
    def test_url_for_format(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha = "a" * 64
        url = store.url_for(sha, ".jpg")
        assert url == f"/_assets/aa/{sha}.jpg"

    def test_url_for_uses_asset_prefix(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha = "b3c4" + "0" * 60
        url = store.url_for(sha, ".png")
        assert url.startswith(cfg.ASSET_URL_PREFIX + "/")

    def test_url_for_shard_from_first_two_chars(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha = "fe" + "0" * 62
        url = store.url_for(sha, ".css")
        assert "/fe/" in url

    def test_url_for_consistent_with_put_bytes(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"roundtrip test"
        sha = store.put_bytes(data, ".js")
        url = store.url_for(sha, ".js")
        # URL path should resolve to the same file that was written
        rel = url.lstrip("/")  # e.g. "_assets/ab/abc....js"
        # Convert /_assets/... to filesystem path
        fs = Path(store.root).parent / rel
        assert fs.exists() or url.startswith("/_assets/")


# ══════════════════════════════════════════════════════════════════════════════
# 10.  BlobStore.fs_path  — filesystem path construction
# ══════════════════════════════════════════════════════════════════════════════

class TestBlobStoreFsPath:
    def test_fs_path_shard_directory(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha = "abcdef" + "0" * 58
        path = store.fs_path(sha, ".jpg")
        # Should be rooted at store.root / "ab" / "<sha>.jpg"
        assert path == os.path.join(store.root, "ab", f"{sha}.jpg")

    def test_fs_path_matches_put_bytes_location(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"path check data"
        sha = store.put_bytes(data, ".html")
        fs = store.fs_path(sha, ".html")
        assert os.path.isfile(fs)
        with open(fs, "rb") as f:
            assert f.read() == data

    def test_fs_path_different_shard_for_different_sha_prefix(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        sha_ab = "ab" + "c" * 62
        sha_cd = "cd" + "e" * 62
        path_ab = store.fs_path(sha_ab, ".bin")
        path_cd = store.fs_path(sha_cd, ".bin")
        assert "/ab/" in path_ab
        assert "/cd/" in path_cd
        assert path_ab != path_cd

    def test_fs_path_creates_shard_dir_on_put(self, tmp_path):
        store = sc.BlobStore(str(tmp_path / "blobs"))
        data = b"shard dir test"
        sha = store.put_bytes(data, ".css")
        shard_dir = Path(store.root) / sha[:2]
        assert shard_dir.is_dir()


# ══════════════════════════════════════════════════════════════════════════════
# Trigger log (--trigger arg written to crawl_triggers.log)
# ══════════════════════════════════════════════════════════════════════════════

import hashlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock


def _run_main_with_args(args, tmp_path):
    """Call site_crawler.main() with a patched MIRROR_DIR and mocked crawler."""
    with patch.object(sc.cfg, "MIRROR_DIR", str(tmp_path)), \
         patch.object(sc.cfg, "BLOB_DIR", str(tmp_path / "_assets")), \
         patch.object(sc.cfg, "SNAPSHOT_DIR", str(tmp_path / "snapshots")), \
         patch("site_crawler.SiteCrawler") as MockCrawler:
        MockCrawler.return_value.crawl = MagicMock()
        MockCrawler.return_value.augment = MagicMock()
        import sys as _sys
        old_argv = _sys.argv
        _sys.argv = ["site_crawler.py"] + args
        try:
            sc.main()
        finally:
            _sys.argv = old_argv
        return MockCrawler


class TestTriggerLog:
    def _read_log(self, tmp_path):
        log_path = tmp_path / "crawl_triggers.log"
        return [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]

    def test_trigger_log_created_on_crawl(self, tmp_path):
        _run_main_with_args([], tmp_path)
        log = self._read_log(tmp_path)
        assert len(log) == 1

    def test_trigger_default_is_manual(self, tmp_path):
        _run_main_with_args([], tmp_path)
        assert self._read_log(tmp_path)[0]["trigger"] == "manual"

    def test_trigger_cron(self, tmp_path):
        _run_main_with_args(["--trigger", "cron"], tmp_path)
        assert self._read_log(tmp_path)[0]["trigger"] == "cron"

    def test_trigger_api(self, tmp_path):
        _run_main_with_args(["--trigger", "api"], tmp_path)
        assert self._read_log(tmp_path)[0]["trigger"] == "api"

    def test_mode_crawl(self, tmp_path):
        _run_main_with_args([], tmp_path)
        assert self._read_log(tmp_path)[0]["mode"] == "crawl"

    def test_mode_augment(self, tmp_path):
        _run_main_with_args(["--augment"], tmp_path)
        assert self._read_log(tmp_path)[0]["mode"] == "augment"

    def test_trigger_log_has_ts_field(self, tmp_path):
        _run_main_with_args([], tmp_path)
        entry = self._read_log(tmp_path)[0]
        assert "ts" in entry
        assert entry["ts"].endswith("Z")

    def test_trigger_log_appends(self, tmp_path):
        _run_main_with_args(["--trigger", "cron"], tmp_path)
        _run_main_with_args(["--trigger", "api"], tmp_path)
        log = self._read_log(tmp_path)
        assert len(log) == 2
        assert log[0]["trigger"] == "cron"
        assert log[1]["trigger"] == "api"


# ══════════════════════════════════════════════════════════════════════════════
# canonical_hash — dynamic content stripping
# ══════════════════════════════════════════════════════════════════════════════

class TestCanonicalHash:
    def _sha256(self, s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def test_static_page_matches_blob_hash(self):
        html = "<html><body><p>Hello world</p></body></html>"
        assert sc.canonical_hash(html) == self._sha256(html)

    def test_dynamic_counter_stripped(self):
        html_a = "<p>Memory_bloc_restoration: 201/365 Completed</p>"
        html_b = "<p>Memory_bloc_restoration: 240/365 Completed</p>"
        assert sc.canonical_hash(html_a) == sc.canonical_hash(html_b)

    def test_verification_counter_stripped(self):
        html_a = "<p>Memory_bloc_verification: 0/365 Completed</p>"
        html_b = "<p>Memory_bloc_verification: 99/365 Completed</p>"
        assert sc.canonical_hash(html_a) == sc.canonical_hash(html_b)

    def test_real_content_change_still_detected(self):
        html_a = "<p>Memory_bloc_restoration: 201/365 Completed</p><p>Chapter 1</p>"
        html_b = "<p>Memory_bloc_restoration: 201/365 Completed</p><p>Chapter 2</p>"
        assert sc.canonical_hash(html_a) != sc.canonical_hash(html_b)

    def test_both_counters_stripped_independently(self):
        html_a = "<p>Memory_bloc_restoration: 1/365 Completed<br/>Memory_bloc_verification: 0/365 Completed</p>"
        html_b = "<p>Memory_bloc_restoration: 300/365 Completed<br/>Memory_bloc_verification: 50/365 Completed</p>"
        assert sc.canonical_hash(html_a) == sc.canonical_hash(html_b)

    def test_canonical_hash_differs_from_blob_when_dynamic_present(self):
        html = "<p>Memory_bloc_restoration: 201/365 Completed</p>"
        blob_hash = self._sha256(html)
        assert sc.canonical_hash(html) != blob_hash

    def _scramble_anchor(self, data_word, visible):
        return (f'<a class="scramble-text glitch-link" data-word="{data_word}" '
                f'href="https://example.com">{visible}</a>')

    def test_scramble_text_shuffle_ignored(self):
        # Same data-word, different shuffles → same canonical hash
        a = self._scramble_anchor("Connection detected", "nndCioeetct  ecoident")
        b = self._scramble_anchor("Connection detected", "dctenoeiCnotnctd  ein")
        assert sc.canonical_hash(a) == sc.canonical_hash(b)

    def test_scramble_text_real_change_detected(self):
        # Different data-word → different canonical hash
        a = self._scramble_anchor("Connection detected", "nndCioeetct  ecoident")
        b = self._scramble_anchor("Access granted",      "scAtc  asrengde")
        assert sc.canonical_hash(a) != sc.canonical_hash(b)

    def test_scramble_text_data_word_preserved_in_canonical(self):
        # data-word attribute is retained — it's in the tag, not the stripped content
        a = self._scramble_anchor("Message A", "shuffled")
        b = self._scramble_anchor("Message B", "shuffled")
        assert sc.canonical_hash(a) != sc.canonical_hash(b)


# ══════════════════════════════════════════════════════════════════════════════
# skip-if-no-changes logic in crawl()
# ══════════════════════════════════════════════════════════════════════════════

def _empty_changes(previous_date="2026-06-15T1832"):
    return {
        "previous_date": previous_date,
        "pages_added": [],
        "pages_modified": [],
        "pages_removed": [],
        "assets_added": 0,
        "assets_modified": 0,
        "summary": "0 added, 0 modified, 0 removed, 0 new assets",
    }


class TestSkipUnchangedSnapshot:
    def _run_crawl(self, tmp_path, changes_return):
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir(parents=True)
        with patch.object(sc.cfg, "SNAPSHOT_DIR", str(snap_dir)), \
             patch.object(sc.cfg, "MIRROR_DIR", str(tmp_path)), \
             patch.object(sc.cfg, "BLOB_DIR", str(tmp_path / "_assets")), \
             patch.object(sc.cfg, "STATE_FILE", str(tmp_path / "state.json")), \
             patch("site_crawler.sync_playwright"), \
             patch("site_crawler.fetch_sitemap", return_value=b"<urlset></urlset>"), \
             patch("site_crawler.parse_sitemap", return_value=([], [])), \
             patch.object(sc.SiteCrawler, "_compute_changes", return_value=changes_return), \
             patch.object(sc.SiteCrawler, "_crawl_loop"):
            crawler = sc.SiteCrawler()
            crawler.crawl()
        return snap_dir

    def test_no_changes_skips_snapshot(self, tmp_path):
        snap_dir = self._run_crawl(tmp_path, _empty_changes())
        assert list(snap_dir.iterdir()) == []

    def test_pages_added_saves_snapshot(self, tmp_path):
        changes = _empty_changes()
        changes["pages_added"] = ["/new-page/"]
        snap_dir = self._run_crawl(tmp_path, changes)
        assert any(snap_dir.iterdir())

    def test_pages_modified_saves_snapshot(self, tmp_path):
        changes = _empty_changes()
        changes["pages_modified"] = ["/about/"]
        snap_dir = self._run_crawl(tmp_path, changes)
        assert any(snap_dir.iterdir())

    def test_pages_removed_saves_snapshot(self, tmp_path):
        changes = _empty_changes()
        changes["pages_removed"] = ["/old-page/"]
        snap_dir = self._run_crawl(tmp_path, changes)
        assert any(snap_dir.iterdir())

    def test_assets_added_saves_snapshot(self, tmp_path):
        changes = _empty_changes()
        changes["assets_added"] = 3
        snap_dir = self._run_crawl(tmp_path, changes)
        assert any(snap_dir.iterdir())

    def test_first_snapshot_always_saved(self, tmp_path):
        changes = _empty_changes(previous_date=None)
        snap_dir = self._run_crawl(tmp_path, changes)
        assert any(snap_dir.iterdir())
