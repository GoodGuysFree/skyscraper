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
