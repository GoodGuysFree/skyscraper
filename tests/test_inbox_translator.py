"""
Unit tests for inbox_translator.py.

All offline: the translation backends are monkeypatched, no network, no browser,
no real crawl. Snapshot scanning is exercised via injected fakes.
"""

import fcntl
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import inbox_translator as it
import wayback_server as ws

CIPHER = "Lf pwkyh hry wuohkyh."          # -> "La porte est ouverte."
PLAIN = "Bonjour le monde."


def _body(body_html):
    """Build body_ps for one synthetic card."""
    html = ('<div class="wp-block-group has-primary-background-color">'
            f'<p class="wp-block-paragraph">{body_html}</p></div>')
    soup = ws.BeautifulSoup(html, "html.parser")
    (_, body_ps), = ws._inbox_message_cards(soup)
    return body_ps


def _boom(*_a, **_k):
    raise RuntimeError("backend down")


class TestClassifyIsCipher:
    def test_cipher_detected(self):
        assert it.classify_is_cipher(CIPHER) is True

    def test_plain_not_cipher(self):
        assert it.classify_is_cipher(PLAIN) is False


class TestBodyLines:
    def test_splits_br_and_blank_lines(self):
        assert it.body_lines(_body("a<br/>b<br/><br/>c"), False) == \
            ["a", "b", "", "c"]

    def test_decodes_cipher_per_line(self):
        assert it.body_lines(_body("Lf<br/>pwkyh"), True) == ["La", "porte"]

    def test_plain_not_decoded(self):
        assert it.body_lines(_body("Bonjour"), False) == ["Bonjour"]


class TestTranslateMessage:
    def test_google_per_line_preserves_blank_lines(self, monkeypatch):
        monkeypatch.setattr(it, "_google_translate", lambda s: s.upper())
        en, src = it.translate_message(["aa", "", "bb"])
        assert en == "AA\n\nBB"
        assert src == "google"

    def test_llm_fallback_when_google_fails(self, monkeypatch):
        monkeypatch.setattr(it, "_google_translate", _boom)
        monkeypatch.setattr(it, "_llm_translate", lambda t, k, m: "X\n\nY")
        en, src = it.translate_message(["a", "", "b"], llm_key="k", llm_model="m")
        assert en == "X\n\nY"
        assert src == "llm"

    def test_raises_when_google_fails_and_no_key(self, monkeypatch):
        monkeypatch.setattr(it, "_google_translate", _boom)
        with pytest.raises(RuntimeError):
            it.translate_message(["a"], llm_key="")


class TestParseLlmResponse:
    def test_valid(self):
        assert it._parse_llm_response(
            {"choices": [{"message": {"content": "  hi  "}}]}) == "hi"

    def test_empty_choices_raises_clean(self):
        with pytest.raises(RuntimeError):
            it._parse_llm_response({"choices": []})

    def test_missing_content_raises_clean(self):
        with pytest.raises(RuntimeError):
            it._parse_llm_response({"choices": [{"message": {}}]})

    def test_error_payload_raises_clean(self):
        # OpenRouter 200-with-error body must not KeyError/IndexError.
        with pytest.raises(RuntimeError):
            it._parse_llm_response({"error": {"message": "bad key"}})


class TestLock:
    def test_second_instance_blocked(self, tmp_path):
        p = str(tmp_path / "t.lock")
        first = it.acquire_lock(p)
        assert first is not None
        assert it.acquire_lock(p) is None       # already held
        fcntl.flock(first, fcntl.LOCK_UN)
        first.close()


class TestRun:
    def _patch_common(self, it_mod, tmp_path, monkeypatch, crawl=False):
        monkeypatch.setattr(it_mod, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(it_mod, "MACHINE_CACHE",
                            str(tmp_path / "inbox_auto.json"))
        monkeypatch.setattr(it_mod, "CURATED_SEED", str(tmp_path / "seed.json"))
        monkeypatch.setattr(it_mod, "acquire_lock",
                            lambda *a, **k: open(tmp_path / "lk", "w"))
        monkeypatch.setattr(it_mod, "crawl_running", lambda: crawl)

    def test_skips_when_crawl_running(self, tmp_path, monkeypatch):
        self._patch_common(it, tmp_path, monkeypatch, crawl=True)
        seen = {"scanned": False}
        monkeypatch.setattr(it, "scan_inbox_messages",
                            lambda: seen.__setitem__("scanned", True) or {})
        assert it.run() == 0
        assert seen["scanned"] is False          # bailed before scanning

    def test_translates_new_message_and_writes_cache(self, tmp_path, monkeypatch):
        self._patch_common(it, tmp_path, monkeypatch)
        monkeypatch.setattr(it, "_google_translate", lambda s: s.upper())
        monkeypatch.setattr(it, "scan_inbox_messages", lambda: {
            "key1": {"is_cipher": True, "lines": ["la", "", "porte"]}})
        assert it.run() == 0
        entry = json.loads((tmp_path / "inbox_auto.json").read_text())[
            "messages"]["key1"]
        assert entry["en"] == "LA\n\nPORTE"      # line structure preserved
        assert entry["is_cipher"] is True
        assert entry["mt"] is True
        assert entry["source"] == "google"
        assert isinstance(entry["ts"], int)

    def test_skips_already_known(self, tmp_path, monkeypatch):
        cache = tmp_path / "inbox_auto.json"
        cache.write_text(json.dumps({"messages": {
            "key1": {"en": "x", "is_cipher": False}}}))
        self._patch_common(it, tmp_path, monkeypatch)
        called = {"translate": False}
        monkeypatch.setattr(it, "_google_translate",
                            lambda s: called.__setitem__("translate", True) or s)
        monkeypatch.setattr(it, "scan_inbox_messages", lambda: {
            "key1": {"is_cipher": False, "lines": ["x"]}})
        it.run()
        assert called["translate"] is False      # nothing new -> no backend call

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        self._patch_common(it, tmp_path, monkeypatch)
        monkeypatch.setattr(it, "_google_translate", _boom)  # must not be called
        monkeypatch.setattr(it, "scan_inbox_messages", lambda: {
            "key1": {"is_cipher": False, "lines": ["hello"]}})
        assert it.run(dry_run=True) == 0
        assert not (tmp_path / "inbox_auto.json").exists()
