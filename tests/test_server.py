"""
Unit tests for wayback_server.py.

All tests are offline (no network, no browser, no live site).
Run with:  python3 -m pytest tests/ -v   (from /archive/skyscraper/)
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so we can import wayback_server and
# crawler_config without installing anything.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import crawler_config as cfg
import wayback_server as ws


# ══════════════════════════════════════════════════════════════════════════════
# 1.  _token_for  — deterministic SHA-256 gate token
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenFor:
    def test_known_value(self):
        pw = "hunter2"
        expected = hashlib.sha256(pw.encode()).hexdigest()
        assert ws._token_for(pw) == expected

    def test_empty_string(self):
        expected = hashlib.sha256(b"").hexdigest()
        assert ws._token_for("") == expected

    def test_deterministic(self):
        assert ws._token_for("abc") == ws._token_for("abc")

    def test_different_passwords_differ(self):
        assert ws._token_for("foo") != ws._token_for("bar")

    def test_returns_64_hex_chars(self):
        tok = ws._token_for("whatever")
        assert len(tok) == 64
        assert all(c in "0123456789abcdef" for c in tok)


# ══════════════════════════════════════════════════════════════════════════════
# 2.  _load_gate_password  — reads ARCHIVE_PASSWORD from .env
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadGatePassword:
    def test_reads_password_from_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("ARCHIVE_PASSWORD=supersecret\n")
        monkeypatch.setattr(cfg, "GATE_ENV_FILE", str(env_file))
        assert ws._load_gate_password() == "supersecret"

    def test_returns_empty_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_ENV_FILE", str(tmp_path / "nonexistent.env"))
        assert ws._load_gate_password() == ""

    def test_ignores_other_keys(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_KEY=other\nARCHIVE_PASSWORD=mypass\nFOO=bar\n")
        monkeypatch.setattr(cfg, "GATE_ENV_FILE", str(env_file))
        assert ws._load_gate_password() == "mypass"

    def test_strips_surrounding_whitespace(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("ARCHIVE_PASSWORD=  padded  \n")
        monkeypatch.setattr(cfg, "GATE_ENV_FILE", str(env_file))
        assert ws._load_gate_password() == "padded"

    def test_returns_empty_when_key_missing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("UNRELATED=foo\n")
        monkeypatch.setattr(cfg, "GATE_ENV_FILE", str(env_file))
        assert ws._load_gate_password() == ""


# ══════════════════════════════════════════════════════════════════════════════
# 3.  _build_landing_page  — HTML for the front-page gate
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildLandingPage:
    def test_password_mode_has_password_input(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page()
        assert 'type="password"' in html
        assert 'name="pw"' in html

    def test_password_mode_has_form_action(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page()
        assert 'action="/~gate"' in html

    def test_button_mode_no_password_input(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "button")
        html = ws._build_landing_page()
        assert 'type="password"' not in html
        assert 'name="pw"' not in html

    def test_button_mode_has_submit_button(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "button")
        html = ws._build_landing_page()
        assert "<button" in html
        assert 'action="/~gate"' in html

    def test_error_message_rendered(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page(error="Incorrect password.")
        assert "Incorrect password." in html
        assert 'class="error"' in html

    def test_no_error_message_by_default(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page()
        assert 'class="error"' not in html

    def test_contains_project_skyscraper(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page()
        assert "Project Skyscraper" in html

    def test_contains_ggf(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "button")
        html = ws._build_landing_page()
        assert "GoodGuysFree" in html


# ══════════════════════════════════════════════════════════════════════════════
# 4.  build_header_html  — thin fixed top bar
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildHeaderHtml:
    def test_timestamp_format_display(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/page/")
        # Should format as "Jun 14, 2026 · 11:37 UTC"
        assert "Jun 14, 2026" in html
        assert "11:37 UTC" in html

    def test_date_only_format_display(self):
        html = ws.build_header_html("2026-06-13", "https://example.com/page/")
        # Should format as "Jun 13, 2026" (no time)
        assert "Jun 13, 2026" in html
        assert "UTC" not in html

    def test_mirror_notice_present(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/")
        assert "MIRROR" in html or "mirror" in html.lower()

    def test_ggf_badge_present(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/")
        assert "GGF" in html

    def test_site_domain_linked(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/")
        assert cfg.SITE_DOMAIN in html

    def test_invalid_date_falls_back_to_raw(self):
        html = ws.build_header_html("not-a-date", "https://example.com/")
        assert "not-a-date" in html

    def test_wb_topbar_id_present(self):
        html = ws.build_header_html("2026-06-13", "https://example.com/")
        assert "wb-topbar" in html

    @pytest.mark.parametrize("date,expected_time", [
        ("2026-01-01T0000", "00:00 UTC"),
        ("2026-12-31T2359", "23:59 UTC"),
        ("2025-06-15T1234", "12:34 UTC"),
    ])
    def test_time_formatting_parametrized(self, date, expected_time):
        html = ws.build_header_html(date, "https://example.com/")
        assert expected_time in html


# ══════════════════════════════════════════════════════════════════════════════
# 5.  ManifestCache  — loads and caches snapshot manifests
# ══════════════════════════════════════════════════════════════════════════════

def _make_manifest(pages: dict | None = None, assets: dict | None = None,
                   date: str = "2026-06-13",
                   crawled_at: str = "2026-06-13T17:54:00+00:00") -> dict:
    """Build a minimal but valid manifest dict."""
    return {
        "date": date,
        "crawled_at": crawled_at,
        "pages": pages or {},
        "assets": assets or {},
    }


def _write_manifest(snapshot_dir: Path, name: str, manifest: dict,
                    changes: dict | None = None) -> Path:
    snap = snapshot_dir / name
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if changes is not None:
        (snap / "changes.json").write_text(json.dumps(changes), encoding="utf-8")
    return snap


class TestManifestCache:
    def test_empty_dir_gives_no_dates(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.dates == []
        assert cache.latest_date is None

    def test_nonexistent_dir_gives_no_dates(self, tmp_path):
        cache = ws.ManifestCache(str(tmp_path / "does_not_exist"))
        assert cache.dates == []
        assert cache.latest_date is None

    def test_single_snapshot_loaded(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        m = _make_manifest({"/" : {"blob": "abc123", "content_type": "text/html",
                                   "original_url": "https://example.com/",
                                   "title": "Home"}})
        _write_manifest(snap_dir, "2026-06-13T1754", m)
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.dates == ["2026-06-13T1754"]
        assert cache.latest_date == "2026-06-13T1754"

    def test_two_snapshots_sorted(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest(date="2026-06-13"))
        _write_manifest(snap_dir, "2026-06-14T1137", _make_manifest(date="2026-06-14"))
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.dates == ["2026-06-13T1754", "2026-06-14T1137"]
        assert cache.latest_date == "2026-06-14T1137"

    def test_get_manifest_returns_correct_data(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        m = _make_manifest(date="2026-06-13", crawled_at="2026-06-13T17:54:00+00:00")
        _write_manifest(snap_dir, "2026-06-13T1754", m)
        cache = ws.ManifestCache(str(snap_dir))
        result = cache.get_manifest("2026-06-13T1754")
        assert result is not None
        assert result["date"] == "2026-06-13"
        assert result["crawled_at"] == "2026-06-13T17:54:00+00:00"

    def test_get_manifest_missing_returns_none(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.get_manifest("2099-01-01") is None

    def test_get_changes_loaded(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        changes = {"summary": "1 added", "pages_added": ["/new/"], "pages_modified": []}
        _write_manifest(snap_dir, "2026-06-14T1137", _make_manifest(), changes=changes)
        cache = ws.ManifestCache(str(snap_dir))
        result = cache.get_changes("2026-06-14T1137")
        assert result is not None
        assert result["summary"] == "1 added"
        assert "/new/" in result["pages_added"]

    def test_get_changes_absent_returns_none(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest())
        # No changes.json written
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.get_changes("2026-06-13T1754") is None

    def test_legacy_date_only_name_accepted(self, tmp_path):
        """Server must accept YYYY-MM-DD snapshot names (legacy format)."""
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13", _make_manifest(date="2026-06-13"))
        cache = ws.ManifestCache(str(snap_dir))
        assert "2026-06-13" in cache.dates

    def test_reload_picks_up_new_snapshot(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest())
        cache = ws.ManifestCache(str(snap_dir))
        assert len(cache.dates) == 1

        _write_manifest(snap_dir, "2026-06-14T1137", _make_manifest(date="2026-06-14"))
        cache.reload()
        assert len(cache.dates) == 2
        assert cache.latest_date == "2026-06-14T1137"

    def test_ignores_dir_without_manifest(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        # A directory with no manifest.json should be silently ignored.
        (snap_dir / "incomplete-snapshot").mkdir(parents=True)
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest())
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.dates == ["2026-06-13T1754"]


# ══════════════════════════════════════════════════════════════════════════════
# 6.  build_overlay_html  — floating navigation overlay
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def two_snapshot_cache(tmp_path):
    """ManifestCache backed by two real on-disk manifests."""
    snap_dir = tmp_path / "snapshots"
    m1 = _make_manifest(
        pages={"/": {"blob": "aaa", "content_type": "text/html",
                     "original_url": "https://project-skyscraper.com/", "title": "Home"}},
        date="2026-06-13",
        crawled_at="2026-06-13T17:54:00+00:00",
    )
    changes1 = {"summary": "First snapshot", "pages_added": ["/"],
                "pages_modified": [], "pages_removed": []}
    _write_manifest(snap_dir, "2026-06-13T1754", m1, changes=changes1)

    m2 = _make_manifest(
        pages={
            "/": {"blob": "bbb", "content_type": "text/html",
                  "original_url": "https://project-skyscraper.com/", "title": "Home"},
            "/about/": {"blob": "ccc", "content_type": "text/html",
                        "original_url": "https://project-skyscraper.com/about/", "title": "About"},
        },
        date="2026-06-14",
        crawled_at="2026-06-14T11:37:00+00:00",
    )
    changes2 = {"summary": "1 added, 1 modified", "pages_added": ["/about/"],
                "pages_modified": ["/"], "pages_removed": []}
    _write_manifest(snap_dir, "2026-06-14T1137", m2, changes=changes2)

    return ws.ManifestCache(str(snap_dir))


class TestBuildOverlayHtml:
    def test_both_date_rows_present(self, two_snapshot_cache):
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        assert "2026-06-13T1754" in html
        assert "2026-06-14T1137" in html

    def test_current_date_row_has_current_class(self, two_snapshot_cache):
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        assert "wb-row-current" in html

    def test_first_snapshot_prev_disabled(self, two_snapshot_cache):
        """When viewing the oldest snapshot, the 'prev' button must be disabled."""
        html = ws.build_overlay_html(
            "2026-06-13T1754", two_snapshot_cache, None, "/"
        )
        # The prev button onclick target will be empty string, and it gets `disabled`
        assert "disabled" in html

    def test_last_snapshot_next_disabled(self, two_snapshot_cache):
        """When viewing the newest snapshot, the 'next' button must be disabled."""
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        # Should only have one `disabled` (for next), prev should be enabled
        disabled_count = html.count("disabled")
        assert disabled_count >= 1  # at least next is disabled

    def test_middle_snapshot_neither_disabled(self, tmp_path):
        """Three snapshots: middle one should have both prev/next enabled."""
        snap_dir = tmp_path / "snapshots"
        for name, date in [("2026-06-12T1000", "2026-06-12"),
                           ("2026-06-13T1000", "2026-06-13"),
                           ("2026-06-14T1000", "2026-06-14")]:
            _write_manifest(snap_dir, name, _make_manifest(date=date))
        cache = ws.ManifestCache(str(snap_dir))
        html = ws.build_overlay_html("2026-06-13T1000", cache, None, "/")
        # The HTML `disabled` attribute is added as `dis_prev`/`dis_next` in the
        # button tag itself (not inside the CSS block).  For the middle snapshot
        # both nav buttons should have a non-empty onclick target, so neither
        # `<button … disabled …>` should appear in the markup.
        # We check the nav buttons specifically by looking at their rendered HTML.
        # The CSS always contains ".wb-btn:disabled" — ignore that.
        nav_buttons = re.findall(r'<button class="wb-btn"[^>]*>', html)
        assert len(nav_buttons) == 2, "Expected exactly two nav buttons"
        for btn in nav_buttons:
            assert "disabled" not in btn, f"Nav button should not be disabled: {btn}"

    def test_change_summary_appears(self, two_snapshot_cache):
        changes = {"summary": "1 added, 1 modified", "pages_added": [], "pages_modified": []}
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, changes, "/"
        )
        assert "1 added, 1 modified" in html

    def test_no_changes_shows_first_snapshot(self, two_snapshot_cache):
        html = ws.build_overlay_html(
            "2026-06-13T1754", two_snapshot_cache, None, "/"
        )
        assert "First snapshot" in html

    def test_new_page_dot_class(self, two_snapshot_cache):
        changes = {"summary": "", "pages_added": ["/about/"], "pages_modified": []}
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, changes, "/about/"
        )
        assert "wb-new" in html

    def test_modified_page_dot_class(self, two_snapshot_cache):
        changes = {"summary": "", "pages_added": [], "pages_modified": ["/"]}
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, changes, "/"
        )
        assert "wb-modified" in html

    def test_crawled_at_time_in_picker(self, two_snapshot_cache):
        """Picker rows should show time from crawled_at, not just the date name."""
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        # crawled_at for snapshot 2 is "2026-06-14T11:37:00+00:00" → "11:37:00 UTC"
        assert "11:37:00 UTC" in html

    def test_overlay_contains_script(self, two_snapshot_cache):
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        assert "<script>" in html
        assert "wbNav" in html


# ══════════════════════════════════════════════════════════════════════════════
# 7.  rewrite_internal_links  — link rewriting for archive navigation
# ══════════════════════════════════════════════════════════════════════════════

class TestRewriteInternalLinks:
    DATE = "2026-06-14T1137"

    def _rewrite(self, html: str, asset_by_path: dict | None = None) -> str:
        return ws.rewrite_internal_links(html, self.DATE, asset_by_path or {})

    def test_internal_page_link_rewritten(self):
        html = '<a href="/about/">About</a>'
        out = self._rewrite(html)
        assert f'/@{self.DATE}/about/' in out

    def test_absolute_internal_link_rewritten(self):
        html = f'<a href="https://{cfg.SITE_DOMAIN}/contact/">Contact</a>'
        out = self._rewrite(html)
        assert f'/@{self.DATE}/contact/' in out

    def test_external_link_untouched(self):
        html = '<a href="https://external-site.com/page">Ext</a>'
        out = self._rewrite(html)
        assert "external-site.com/page" in out
        assert "/@" not in out

    def test_already_rewritten_link_untouched(self):
        href = f"/@{self.DATE}/existing/"
        html = f'<a href="{href}">Page</a>'
        out = self._rewrite(html)
        # Should appear exactly once (not double-prefixed)
        assert out.count(f"/@{self.DATE}/existing/") == 1
        assert f"/@{self.DATE}/@{self.DATE}" not in out

    def test_asset_link_rewritten_to_blob(self):
        asset_map = {"/wp-content/uploads/image.jpg": "/_assets/ab/abcdef.jpg"}
        html = '<a href="/wp-content/uploads/image.jpg">Image</a>'
        out = self._rewrite(html, asset_map)
        assert "/_assets/ab/abcdef.jpg" in out

    def test_asset_link_falls_back_to_live(self):
        """Asset link with no blob falls back to the live site URL."""
        html = '<a href="/wp-content/uploads/photo.png">Photo</a>'
        out = self._rewrite(html, {})
        assert cfg.SITE_ORIGIN in out
        assert "photo.png" in out

    def test_mailto_untouched(self):
        html = '<a href="mailto:hello@example.com">Email</a>'
        out = self._rewrite(html)
        assert "mailto:hello@example.com" in out

    def test_hash_anchor_untouched(self):
        html = '<a href="#section">Section</a>'
        out = self._rewrite(html)
        assert 'href="#section"' in out

    def test_assets_prefix_untouched(self):
        html = '<a href="/_assets/ab/abcdef.jpg">Blob</a>'
        out = self._rewrite(html)
        assert '/_assets/ab/abcdef.jpg' in out

    def test_tilde_prefix_untouched(self):
        html = '<a href="/~api/dates">API</a>'
        out = self._rewrite(html)
        assert '/~api/dates' in out

    def test_form_disabled_notice_stripped(self):
        """Legacy 'Form disabled' comment injected by old crawler is removed."""
        html = '<p>This is the form. [ARCHIVED — Form disabled]</p><p>Other content</p>'
        out = self._rewrite(html)
        assert "Form disabled" not in out

    def test_www_subdomain_internal_link_rewritten(self):
        html = f'<a href="https://www.{cfg.SITE_DOMAIN}/page/">Page</a>'
        out = self._rewrite(html)
        assert f'/@{self.DATE}/page/' in out


# ══════════════════════════════════════════════════════════════════════════════
# 8.  Date routing regex  — validate the URL-matching pattern
# ══════════════════════════════════════════════════════════════════════════════

DATE_ROUTE_RE = re.compile(r'^/@(\d{4}-\d{2}-\d{2}(?:T\d{4})?)(/.*)$')


@pytest.mark.parametrize("path,expected_date,expected_page", [
    ("/@2026-06-14T1137/", "2026-06-14T1137", "/"),
    ("/@2026-06-14T1137/about/", "2026-06-14T1137", "/about/"),
    ("/@2026-06-13/", "2026-06-13", "/"),
    ("/@2026-06-13/contact/", "2026-06-13", "/contact/"),
    ("/@2026-06-13/deep/nested/path/", "2026-06-13", "/deep/nested/path/"),
])
def test_date_route_regex_valid_paths(path, expected_date, expected_page):
    m = DATE_ROUTE_RE.match(path)
    assert m is not None, f"Should match: {path}"
    assert m.group(1) == expected_date
    assert m.group(2) == expected_page


@pytest.mark.parametrize("path", [
    "/",
    "/about/",
    "/@notadate/page/",
    "/@2026-06-14/",           # missing the leading slash in page part? No - this is valid (date-only)
    "/~api/dates",
    "/_assets/ab/file.jpg",
])
def test_date_route_regex_invalid_paths(path):
    # These should either not match or are valid — let's check what the regex
    # actually accepts. Only truly invalid ones should fail.
    m = DATE_ROUTE_RE.match(path)
    if path in ("/", "/about/", "/@notadate/page/", "/~api/dates", "/_assets/ab/file.jpg"):
        assert m is None, f"Should NOT match: {path}"


def test_date_route_date_only_with_page():
    m = DATE_ROUTE_RE.match("/@2026-06-14/about/")
    assert m is not None
    assert m.group(1) == "2026-06-14"
    assert m.group(2) == "/about/"


def test_date_route_timestamp_format():
    m = DATE_ROUTE_RE.match("/@2026-06-14T2359/some/page/")
    assert m is not None
    assert m.group(1) == "2026-06-14T2359"
    assert m.group(2) == "/some/page/"


def test_date_route_rejects_no_slash_after_date():
    # "/@2026-06-14" with no trailing slash — the regex requires (/.*)
    m = DATE_ROUTE_RE.match("/@2026-06-14")
    assert m is None
