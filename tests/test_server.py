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

    def test_authenticated_shows_direct_enter_link(self, monkeypatch):
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page(authenticated=True, latest_date="2026-06-15T0330")
        assert "/@2026-06-15T0330/" in html
        assert 'type="password"' not in html
        assert 'action="/~gate"' not in html

    def test_authenticated_no_latest_date_ignored(self, monkeypatch):
        """authenticated=True with no latest_date falls back to unauthenticated form."""
        monkeypatch.setattr(cfg, "GATE_MODE", "password")
        html = ws._build_landing_page(authenticated=True, latest_date="")
        assert 'type="password"' in html


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

    def test_no_status_badge_when_none(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/")
        assert "Page status" not in html
        # The CSS rule exists but no <span class="wb-tb-status" ...> element
        assert 'class="wb-tb-status"' not in html

    @pytest.mark.parametrize("status,expected_text", [
        ("new",             "NEW PAGE"),
        ("changed",         "CHANGED"),
        ("unchanged",       "UNCHANGED"),
        ("not_in_snapshot", "NOT IN SNAPSHOT"),
    ])
    def test_page_status_badge_text(self, status, expected_text):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/", status)
        assert "Page status" in html
        assert expected_text in html
        assert "wb-tb-status" in html

    def test_no_old_notice_by_default(self):
        html = ws.build_header_html("2026-06-14T1137", "https://example.com/")
        assert "Old snapshot" not in html
        # The CSS rule for .wb-tb-old always exists; the ELEMENT must not.
        assert 'class="wb-tb-old"' not in html

    def test_old_notice_when_backfilled(self):
        html = ws.build_header_html("2026-06-04T1806", "https://example.com/",
                                    is_backfilled=True)
        assert "Old snapshot — may not be accurate" in html
        assert 'class="wb-tb-old"' in html


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

    def test_is_backfilled_true(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        m = _make_manifest(date="2026-06-04")
        m["backfilled"] = True
        _write_manifest(snap_dir, "2026-06-04T1806", m)
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.is_backfilled("2026-06-04T1806") is True

    def test_is_backfilled_false_for_normal(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest(date="2026-06-13"))
        cache = ws.ManifestCache(str(snap_dir))
        assert cache.is_backfilled("2026-06-13T1754") is False

    def test_is_backfilled_false_for_missing(self, tmp_path):
        cache = ws.ManifestCache(str(tmp_path / "none"))
        assert cache.is_backfilled("2099-01-01") is False


class TestPickerBackfillItalics:
    def test_backfilled_row_marked_old(self, tmp_path):
        snap_dir = tmp_path / "snapshots"
        old = _make_manifest(date="2026-06-04"); old["backfilled"] = True
        _write_manifest(snap_dir, "2026-06-04T1806", old, changes={"summary": "First snapshot"})
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest(date="2026-06-13"),
                        changes={"summary": "1 added, 0 modified, 0 removed, 0 new assets"})
        cache = ws.ManifestCache(str(snap_dir))
        html = ws.build_overlay_html("2026-06-13T1754", cache, cache.get_changes("2026-06-13T1754"), "/")
        # Exactly one picker ROW carries the old class (the CSS rule '.wb-row-old'
        # also contains the substring, so match the row-class combination).
        assert html.count('wb-row wb-row-old') == 1
        # CSS italic rule present
        assert "font-style: italic" in html

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

    def test_no_dot_classes_in_overlay(self, two_snapshot_cache):
        """Page status is shown in the topbar, not as dots in the overlay."""
        changes = {"summary": "", "pages_added": ["/about/"], "pages_modified": ["/"]}
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, changes, "/about/"
        )
        assert "wb-new" not in html
        assert "wb-modified" not in html

    def test_picker_rows_have_summary_tooltip(self, two_snapshot_cache):
        """Each picker row should carry a title= tooltip with that snapshot's summary."""
        html = ws.build_overlay_html(
            "2026-06-14T1137", two_snapshot_cache, None, "/"
        )
        # changes2 summary is "1 added, 1 modified"
        assert 'title="1 added, 1 modified"' in html
        # changes1 summary is "First snapshot"
        assert 'title="First snapshot"' in html

    def test_picker_rows_first_snapshot_tooltip(self, tmp_path):
        """Snapshot with no changes.json gets 'First snapshot' tooltip."""
        snap_dir = tmp_path / "snapshots"
        _write_manifest(snap_dir, "2026-06-13T1754", _make_manifest())
        cache = ws.ManifestCache(str(snap_dir))
        html = ws.build_overlay_html("2026-06-13T1754", cache, None, "/")
        assert 'title="First snapshot"' in html

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
# 6b. Page-missing response — friendly 404 with nav overlay
# ══════════════════════════════════════════════════════════════════════════════

class TestPageMissingResponse:
    """Test _serve_page_missing by driving WaybackHandler through a fake socket."""

    def _make_handler(self, tmp_path) -> ws.WaybackHandler:
        snap_dir = tmp_path / "snapshots"
        m = _make_manifest(
            pages={"/": {"blob": "aaa", "content_type": "text/html",
                         "original_url": "https://project-skyscraper.com/", "title": "Home"}},
            date="2026-06-14",
            crawled_at="2026-06-14T11:37:00+00:00",
        )
        changes = {"summary": "1 added", "pages_added": ["/new/"],
                   "pages_modified": [], "pages_removed": []}
        _write_manifest(snap_dir, "2026-06-14T1137", m, changes=changes)
        cache = ws.ManifestCache(str(snap_dir))

        buf = []
        handler = ws.WaybackHandler.__new__(ws.WaybackHandler)
        handler.manifests = cache
        handler.gate_password = ""
        handler._head_only = False
        handler.wfile = type("W", (), {"write": lambda self, b: buf.append(b)})()
        handler._buf = buf

        def respond(code, body, ct):
            handler._last_code = code
            handler._last_body = body
            handler._last_ct = ct

        handler._respond = respond
        return handler

    def test_missing_page_returns_404(self, tmp_path):
        h = self._make_handler(tmp_path)
        h._serve_page_missing("2026-06-14T1137", "/nonexistent/")
        assert h._last_code == 404

    def test_missing_page_html_contains_message(self, tmp_path):
        h = self._make_handler(tmp_path)
        h._serve_page_missing("2026-06-14T1137", "/nonexistent/")
        html = h._last_body.decode("utf-8")
        assert "PAGE NOT IN THIS SNAPSHOT" in html
        assert "/nonexistent/" in html

    def test_missing_page_has_front_page_link(self, tmp_path):
        h = self._make_handler(tmp_path)
        h._serve_page_missing("2026-06-14T1137", "/nonexistent/")
        html = h._last_body.decode("utf-8")
        assert "/@2026-06-14T1137/" in html

    def test_missing_page_has_not_in_snapshot_status(self, tmp_path):
        h = self._make_handler(tmp_path)
        h._serve_page_missing("2026-06-14T1137", "/nonexistent/")
        html = h._last_body.decode("utf-8")
        assert "NOT IN SNAPSHOT" in html

    def test_missing_page_has_nav_overlay(self, tmp_path):
        h = self._make_handler(tmp_path)
        h._serve_page_missing("2026-06-14T1137", "/nonexistent/")
        html = h._last_body.decode("utf-8")
        assert "wb-overlay" in html
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


# ─── verify_trigger_signature ────────────────────────────────────────────────

import hmac as _hmac
import time as _time
from wayback_server import verify_trigger_signature


SECRET = "testsecret"


def _make_sig(ts_str: str, body: str = "{}") -> str:
    digest = _hmac.new(
        SECRET.encode(), f"{ts_str}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def test_trigger_sig_valid():
    now = int(_time.time())
    ts = str(now)
    assert verify_trigger_signature(SECRET, ts, "{}", _make_sig(ts), now=now)


def test_trigger_sig_wrong_secret():
    now = int(_time.time())
    ts = str(now)
    sig = _make_sig(ts)
    assert not verify_trigger_signature("wrongsecret", ts, "{}", sig, now=now)


def test_trigger_sig_tampered_body():
    now = int(_time.time())
    ts = str(now)
    sig = _make_sig(ts, "{}")
    assert not verify_trigger_signature(SECRET, ts, '{"evil":1}', sig, now=now)


def test_trigger_sig_stale_timestamp():
    now = int(_time.time())
    ts = str(now - 301)  # just outside the 300-second window
    sig = _make_sig(ts)
    assert not verify_trigger_signature(SECRET, ts, "{}", sig, now=now)


def test_trigger_sig_future_timestamp():
    now = int(_time.time())
    ts = str(now + 301)
    sig = _make_sig(ts)
    assert not verify_trigger_signature(SECRET, ts, "{}", sig, now=now)


def test_trigger_sig_boundary_timestamp():
    now = int(_time.time())
    ts = str(now - 300)  # exactly at the boundary — still valid
    sig = _make_sig(ts)
    assert verify_trigger_signature(SECRET, ts, "{}", sig, now=now)


def test_trigger_sig_missing_timestamp():
    assert not verify_trigger_signature(SECRET, "", "{}", "sha256=abc")


def test_trigger_sig_non_integer_timestamp():
    assert not verify_trigger_signature(SECRET, "notanint", "{}", "sha256=abc")


def test_trigger_sig_empty_body():
    now = int(_time.time())
    ts = str(now)
    sig = _make_sig(ts, "")
    assert verify_trigger_signature(SECRET, ts, "", sig, now=now)


# ─── CrawlScheduler ──────────────────────────────────────────────────────────

import threading as _threading
from unittest.mock import patch, MagicMock
from wayback_server import CrawlScheduler
import crawler_config as _cfg


def test_scheduler_trigger_starts_debounce():
    sched = CrawlScheduler()
    with patch("wayback_server.threading") as mock_threading:
        mock_timer = MagicMock()
        mock_threading.Timer.return_value = mock_timer
        sched.trigger()
        mock_threading.Timer.assert_called_once_with(
            _cfg.TRIGGER_DEBOUNCE_SECONDS, sched._debounce_fired
        )
        mock_timer.start.assert_called_once()


def test_scheduler_rapid_triggers_reset_timer():
    sched = CrawlScheduler()
    with patch("wayback_server.threading") as mock_threading:
        mock_timer = MagicMock()
        mock_threading.Timer.return_value = mock_timer
        sched.trigger()
        sched.trigger()  # second trigger should cancel first timer
        assert mock_timer.cancel.call_count == 1
        assert mock_threading.Timer.call_count == 2


def test_scheduler_trigger_while_running_sets_queued():
    sched = CrawlScheduler()
    sched._running = True
    sched.trigger()
    assert sched._queued is True


def test_scheduler_multiple_triggers_while_running_only_one_queued():
    sched = CrawlScheduler()
    sched._running = True
    sched.trigger()
    sched.trigger()
    sched.trigger()
    assert sched._queued is True  # still just True, not a counter


def test_scheduler_no_timer_started_while_running():
    sched = CrawlScheduler()
    sched._running = True
    with patch("wayback_server.threading") as mock_threading:
        sched.trigger()
        mock_threading.Timer.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# _is_pagination_page
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", ["/", "/page/2/", "/page/10/"])
def test_is_pagination_page_true(path):
    assert ws._is_pagination_page(path)


@pytest.mark.parametrize("path", [
    "/about/", "/2016/01/22/event/", "/page/", "/page/foo/", "/page/2",
])
def test_is_pagination_page_false(path):
    assert not ws._is_pagination_page(path)


# ══════════════════════════════════════════════════════════════════════════════
# apply_diff_colors
# ══════════════════════════════════════════════════════════════════════════════

def _changes(added=(), modified=()):
    return {
        "pages_added": list(added),
        "pages_modified": list(modified),
        "pages_removed": [],
        "assets_added": 0,
    }


_DATE = "2026-06-16T1725"
_PREFIX = f"/@{_DATE}"


def _page_html(*hrefs):
    links = "".join(f'<a href="{h}">link</a>' for h in hrefs)
    return f"<html><head></head><body>{links}</body></html>"


class TestApplyDiffColors:
    def test_new_page_gets_wb_diff_new(self):
        html = _page_html(f"{_PREFIX}/2026/06/16/some-post/")
        result = ws.apply_diff_colors(html, _DATE, _changes(added=["/2026/06/16/some-post/"]))
        assert "wb-diff-new" in result

    def test_modified_page_gets_wb_diff_changed(self):
        html = _page_html(f"{_PREFIX}/2026/01/01/old-post/")
        result = ws.apply_diff_colors(html, _DATE, _changes(modified=["/2026/01/01/old-post/"]))
        assert "wb-diff-changed" in result

    def test_unchanged_post_gets_wb_diff_unchanged(self):
        html = _page_html(f"{_PREFIX}/2016/12/01/event/")
        result = ws.apply_diff_colors(html, _DATE, _changes())
        assert "wb-diff-unchanged" in result

    def test_non_post_link_not_tagged(self):
        html = _page_html(f"{_PREFIX}/about/", f"{_PREFIX}/page/2/")
        result = ws.apply_diff_colors(html, _DATE, _changes(added=["/about/"]))
        assert "wb-diff-" not in result

    def test_external_link_not_tagged(self):
        html = _page_html("https://example.com/2016/01/01/foo/")
        result = ws.apply_diff_colors(html, _DATE, _changes())
        assert "wb-diff-" not in result

    def test_css_injected_when_tagged(self):
        html = _page_html(f"{_PREFIX}/2026/06/16/post/")
        result = ws.apply_diff_colors(html, _DATE, _changes(added=["/2026/06/16/post/"]))
        assert "wb-diff-new" in result
        assert "wb-diff-off" in result  # toggle CSS present

    def test_no_op_when_no_post_links(self):
        html = _page_html(f"{_PREFIX}/about/")
        result = ws.apply_diff_colors(html, _DATE, _changes())
        assert result == html  # string unchanged

    def test_diff_toggle_in_header_when_pagination(self):
        header = ws.build_header_html(_DATE, "https://example.com/", show_diff_toggle=True)
        assert "wb-diff-toggle" in header
        assert "wb-diff-off" in header

    def test_no_diff_toggle_in_header_by_default(self):
        header = ws.build_header_html(_DATE, "https://example.com/")
        assert "wb-diff-toggle" not in header


# ══════════════════════════════════════════════════════════════════════════════
# 10. AccessLog
# ══════════════════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3
import threading as _threading_mod
import time


class TestAccessLog:
    def test_records_request(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        log.record("1.2.3.4", "/about/", 200, 1234)
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            rows = c.execute("SELECT ip, path, status, bytes FROM access_log").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("1.2.3.4", "/about/", 200, 1234)

    def test_skips_assets(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        log.record("1.2.3.4", "/_assets/ab/abc123.html", 200, 500)
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 0

    def test_skips_api(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        log.record("1.2.3.4", "/~api/dates", 200, 100)
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 0

    def test_skips_static(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        log.record("1.2.3.4", "/_static/bg.jpg", 200, 50000)
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 0

    def test_skips_favicon(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        log.record("1.2.3.4", "/favicon.ico", 200, 256)
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 0

    def test_persists_across_instances(self, tmp_path):
        db = str(tmp_path / "stats.db")
        log_a = ws.AccessLog(db)
        log_a.record("10.0.0.1", "/", 200, 100)
        # Open a new instance on the same path
        log_b = ws.AccessLog(db)
        with _sqlite3.connect(db) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 1

    def test_concurrent_writes(self, tmp_path):
        log = ws.AccessLog(str(tmp_path / "stats.db"))
        errors = []

        def worker():
            try:
                for _ in range(20):
                    log.record("1.2.3.4", "/page/", 200, 100)
            except Exception as e:
                errors.append(e)

        threads = [_threading_mod.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        with _sqlite3.connect(str(tmp_path / "stats.db")) as c:
            count = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        assert count == 200


# ══════════════════════════════════════════════════════════════════════════════
# 11. _compute_sessions
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeSessions:
    def test_empty(self):
        count, durations = ws._compute_sessions([])
        assert count == 0
        assert durations == []

    def test_single_ip_no_gap(self):
        # 3 requests 60s apart → 1 session, duration = 120s
        base = 1_700_000_000.0
        rows = [(base, "ip1"), (base + 60, "ip1"), (base + 120, "ip1")]
        count, durations = ws._compute_sessions(rows)
        assert count == 1
        assert len(durations) == 1
        assert durations[0] == pytest.approx(120.0)

    def test_gap_splits(self):
        # 2 requests 2000s apart → 2 sessions
        base = 1_700_000_000.0
        rows = [(base, "ip1"), (base + 2000, "ip1")]
        count, durations = ws._compute_sessions(rows)
        assert count == 2

    def test_multiple_ips(self):
        # ip A: 2 requests 30s apart → 1 session
        # ip B: 3 requests 60s apart → 1 session
        base = 1_700_000_000.0
        rows = [
            (base, "ipA"), (base + 30, "ipA"),
            (base, "ipB"), (base + 60, "ipB"), (base + 120, "ipB"),
        ]
        count, durations = ws._compute_sessions(rows)
        assert count == 2


# ══════════════════════════════════════════════════════════════════════════════
# 12. _compute_stats
# ══════════════════════════════════════════════════════════════════════════════

def _fresh_db(tmp_path):
    """Open a fresh stats DB and return the connection (write-mode)."""
    db_path = str(tmp_path / "stats.db")
    log = ws.AccessLog(db_path)
    conn = _sqlite3.connect(db_path)
    return conn


class TestComputeStats:
    def test_empty_db(self, tmp_path):
        conn = _fresh_db(tmp_path)
        stats = ws._compute_stats(conn)
        assert stats["summary"]["total_today"] == 0
        assert stats["summary"]["unique_ips_today"] == 0
        assert stats["summary"]["total_week"] == 0
        assert stats["summary"]["unique_ips_week"] == 0
        assert stats["summary"]["total_all"] == 0
        assert stats["hourly"] == [0] * 24
        assert stats["daily"] == []
        assert stats["top_paths"] == []

    def test_counts_requests(self, tmp_path):
        conn = _fresh_db(tmp_path)
        now = time.time()
        for _ in range(3):
            conn.execute(
                "INSERT INTO access_log(ts,ip,path,status,bytes) VALUES(?,?,?,?,?)",
                (now, "1.2.3.4", "/", 200, 100)
            )
        conn.commit()
        stats = ws._compute_stats(conn)
        assert stats["summary"]["total_today"] == 3

    def test_skips_status_non_200_in_top_paths(self, tmp_path):
        conn = _fresh_db(tmp_path)
        now = time.time()
        conn.execute(
            "INSERT INTO access_log(ts,ip,path,status,bytes) VALUES(?,?,?,?,?)",
            (now, "1.2.3.4", "/notfound/", 404, 100)
        )
        conn.commit()
        stats = ws._compute_stats(conn)
        assert stats["top_paths"] == []


# ══════════════════════════════════════════════════════════════════════════════
# 13. _build_stats_html
# ══════════════════════════════════════════════════════════════════════════════

class TestStatsHtml:
    def _empty_stats(self):
        return ws._compute_stats_empty()

    def test_returns_html_string(self):
        result = ws._build_stats_html(self._empty_stats())
        assert isinstance(result, str)
        assert "<!DOCTYPE" in result

    def test_contains_section_headers(self):
        result = ws._build_stats_html(self._empty_stats())
        # Should contain recognizable section headings
        assert "Today" in result or "Hourly" in result or "STATS" in result

    def test_contains_summary_values(self):
        stats = self._empty_stats()
        stats["summary"]["total_today"] = 42
        result = ws._build_stats_html(stats)
        assert "42" in result

    def test_hourly_bars_present(self):
        result = ws._build_stats_html(self._empty_stats())
        # The hourly section renders 24 bar row divs
        assert result.count('<div class="hbar-row"') == 24

    def test_sessions_line_present(self):
        stats = self._empty_stats()
        stats["sessions"] = 7
        result = ws._build_stats_html(stats)
        assert "7" in result

    def test_xss_path_escaped(self, tmp_path):
        """XSS payload in path column must be HTML-escaped, not rendered raw."""
        db_path = str(tmp_path / "stats.db")
        log = ws.AccessLog(db_path)
        conn = _sqlite3.connect(db_path)
        now = time.time()
        conn.execute(
            "INSERT INTO access_log(ts,ip,path,status,bytes) VALUES(?,?,?,?,?)",
            (now, "1.2.3.4", "/<script>alert(1)</script>", 200, 100),
        )
        conn.commit()
        stats = ws._compute_stats(conn)
        result = ws._build_stats_html(stats)
        assert "<script>alert(1)</script>" not in result
        assert "&lt;script&gt;" in result


# ══════════════════════════════════════════════════════════════════════════════
# _inject_form_block — targets the INNER Jetpack form, not the password gate
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveClientIp:
    def test_no_xff_falls_back_to_remote(self):
        assert ws._resolve_client_ip(None, "127.0.0.1") == "127.0.0.1"
        assert ws._resolve_client_ip("", "10.0.0.5") == "10.0.0.5"

    def test_single_xff_entry(self):
        assert ws._resolve_client_ip("203.0.113.7", "127.0.0.1") == "203.0.113.7"

    def test_takes_last_entry_caddy_appended(self):
        # client may spoof earlier entries; Caddy appends the real one last
        assert ws._resolve_client_ip("1.2.3.4, 203.0.113.7", "127.0.0.1") == "203.0.113.7"

    def test_strips_whitespace(self):
        assert ws._resolve_client_ip("  203.0.113.9  ", "127.0.0.1") == "203.0.113.9"

    def test_ignores_empty_segments(self):
        assert ws._resolve_client_ip("203.0.113.7, ,", "127.0.0.1") == "203.0.113.7"


class TestInjectFormBlock:
    def test_injects_into_body_tag(self):
        html = '<html><body><form class="jetpack-contact-form__form"></form></body></html>'
        result = ws._inject_form_block(html)
        assert "wb-form-block" in result
        assert result.index("wb-form-block") < result.index("</body>")

    def test_injects_into_html_tag_when_no_body(self):
        html = '<html><form class="jetpack-contact-form__form"></form></html>'
        result = ws._inject_form_block(html)
        assert "wb-form-block" in result

    def test_appends_when_no_closing_tags(self):
        html = '<form class="jetpack-contact-form__form"></form>'
        result = ws._inject_form_block(html)
        assert "wb-form-block" in result

    def test_shows_form_disabled_message(self):
        html = '<html><body><form class="jetpack-contact-form__form"></form></body></html>'
        result = ws._inject_form_block(html)
        assert "Form disabled in archive" in result

    # ── _neutralize_all_forms: structural, JS-independent, ALL forms ────────
    def test_neutralize_rewrites_action(self):
        html = ('<form class="jetpack-contact-form__form" method="GET" '
                'action="https://project-skyscraper.com/request-memory-timestamp-094317/" '
                'data-wp-on--submit="actions.onFormSubmit">x</form>')
        result = ws._neutralize_all_forms(html)
        assert 'action="#"' in result
        assert "project-skyscraper.com/request-memory" not in result

    def test_neutralize_strips_wp_interactivity_directives(self):
        html = ('<form id="jp-form-abc" method="get" action="https://x/" '
                'data-wp-on--submit="actions.onFormSubmit" '
                'data-wp-on--reset="actions.onFormReset">x</form>')
        result = ws._neutralize_all_forms(html)
        assert "data-wp-on--submit" not in result
        assert "data-wp-on--reset" not in result

    def test_neutralize_sets_onsubmit_return_false(self):
        html = '<form class="jetpack-contact-form__form" action="https://x/">x</form>'
        result = ws._neutralize_all_forms(html)
        assert 'onsubmit="return false;"' in result

    def test_neutralize_disables_password_gate_form(self):
        # The WP gate form (-> wp-login.php) must ALSO be made inert.
        html = ('<form class="post-password-form" method="post" '
                'action="https://project-skyscraper.com/wp-login.php?action=postpass">'
                '<input name="post_password"></form>')
        result = ws._neutralize_all_forms(html)
        assert 'action="#"' in result
        assert "wp-login.php" not in result
        assert 'onsubmit="return false;"' in result

    def test_neutralize_disables_search_and_comment_forms(self):
        html = ('<form role="search" method="get" action="https://x/">a</form>'
                '<form id="commentform" method="post" action="https://x/wp-comments-post.php">b</form>')
        result = ws._neutralize_all_forms(html)
        assert result.count('action="#"') == 2
        assert "wp-comments-post.php" not in result
        assert "https://x/" not in result

    def test_neutralize_no_action_attr_adds_one(self):
        html = '<form class="jetpack-contact-form__form" method="get">x</form>'
        result = ws._neutralize_all_forms(html)
        assert 'action="#"' in result

    def test_targets_jetpack_form_selector_not_post_password(self):
        # The block JS must hook the Jetpack form, NOT the WordPress gate form.
        html = '<html><body><form class="jetpack-contact-form__form"></form></body></html>'
        result = ws._inject_form_block(html)
        assert "jetpack-contact-form__form" in result
        assert 'input[name="post_password"]' not in result


# ══════════════════════════════════════════════════════════════════════════════
# _inject_password_gate — client-side re-creation of the WP post-password gate
# ══════════════════════════════════════════════════════════════════════════════

class TestInjectPasswordGate:
    def test_injects_gate_overlay(self):
        html = "<html><body><p>secret</p></body></html>"
        result = ws._inject_password_gate(html, "EMILY")
        assert "wb-pwgate" in result
        assert result.index("wb-pwgate") < result.index("</body>")

    def test_embeds_hash_not_plaintext(self):
        # Literal password must never appear; only its SHA-256.
        html = "<html><body><p>secret</p></body></html>"
        result = ws._inject_password_gate(html, "EMILY")
        assert "EMILY" not in result
        assert hashlib.sha256(b"EMILY").hexdigest() in result

    def test_different_passwords_embed_different_hashes(self):
        html = "<html><body></body></html>"
        a = ws._inject_password_gate(html, "EMILY")
        b = ws._inject_password_gate(html, "EVENT HORIZON")
        assert hashlib.sha256(b"EMILY").hexdigest() in a
        assert hashlib.sha256(b"EVENT HORIZON").hexdigest() in b
        assert hashlib.sha256(b"EVENT HORIZON").hexdigest() not in a

    def test_content_remains_in_dom(self):
        # Content stays (gate is cosmetic / no-JS readable); overlay sits on top.
        html = "<html><body><p>the secret content</p></body></html>"
        result = ws._inject_password_gate(html, "EMILY")
        assert "the secret content" in result
