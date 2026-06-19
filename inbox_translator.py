#!/usr/bin/env python3
"""
Project Skyscraper — Inbox Auto-Translator.

A standalone maintenance program. It is NOT part of the crawler and NOT part of
the server; it shares only the inbox helpers in wayback_server.py (single source
of truth for the message body-key and the School-Code cipher).

What it does (designed to run from cron every ~10 minutes):
  1. Refuses to run if a crawl is in progress, or if another copy of itself is
     already running (file lock).
  2. Scans every snapshot's /inbox/ page for distinct message bodies.
  3. Finds bodies that have NO translation yet — neither in the committed
     curated seed (data/inbox_translations.json) nor in the machine cache.
  4. Translates French -> English (deep-translator/Google primary, an optional
     OpenRouter LLM as fallback), preserving each message's line structure.
  5. Writes ONLY the machine cache (web_mirror/translations/inbox_auto.json),
     atomically. The committed seed is never touched, so `git pull` on the VPS
     never conflicts. Machine entries are tagged "mt": true (unreviewed).

The server merges curated + machine at serve time, curated taking precedence —
so a hand-authored translation always overrides a machine draft for the same
message.

Usage:
    python3 inbox_translator.py            # do the work (cron entrypoint)
    python3 inbox_translator.py --dry-run  # report what WOULD be translated
    python3 inbox_translator.py --force    # ignore the crawl-in-progress guard
"""

import argparse
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bs4 import BeautifulSoup

import crawler_config as cfg
from wayback_server import (
    INBOX_PATH,
    _decoded_paragraph_html,
    _inbox_body_key,
    _inbox_body_text,
    _inbox_message_cards,
    decode_school_code,
)

# ── Paths ────────────────────────────────────────────────────────────────────
CURATED_SEED = os.path.join(cfg.WORKSPACE_DIR, "data", "inbox_translations.json")
CACHE_DIR = os.path.join(cfg.MIRROR_DIR, "translations")
MACHINE_CACHE = os.path.join(CACHE_DIR, "inbox_auto.json")
LOCK_FILE = os.path.join(CACHE_DIR, ".translator.lock")

# ── Lore glossary — terms the translation must keep verbatim ─────────────────
# deep-translator (Google) can't take a glossary, so we only *enforce* these in
# the LLM prompt; for Google output they're a best-effort post-fix (whole word,
# case-sensitive French source term -> canonical English/lore term).
GLOSSARY = ["System", "Operators", "TOWER", "MACHINE", "Operator"]
_GOOGLE_POSTFIX = {
    r"\bthe System\b": "System",
}

# ── French-ness heuristic (is a body ciphered?) ──────────────────────────────
_FR_WORDS = set(
    "le la les un une des de du et est sont ne pas plus que qui pour dans sur "
    "avec ce cette mon ton son au aux par je tu il elle nous vous ils on a "
    "porte ouverte oui mais te se ca ça veut dire bien".split()
)


def _french_hits(text: str) -> int:
    return sum(1 for w in re.findall(r"[a-zA-Z]+", text.lower())
               if w in _FR_WORDS)


def classify_is_cipher(raw: str) -> bool:
    """A body is ciphered if decoding it reads markedly more French than the
    raw text does."""
    return _french_hits(decode_school_code(raw)) > _french_hits(raw) + 1


# ── Snapshot scanning ────────────────────────────────────────────────────────

def body_lines(body_ps, is_cipher: bool):
    """The message body as a list of display lines (split on <br>), decoded if
    ciphered. Empty strings mark blank lines (<br><br>). Matches what the server
    shows for FR, so machine EN keeps the same line structure."""
    lines = []
    for p in body_ps:
        frag = _decoded_paragraph_html(p) if is_cipher else str(p)
        frag = re.sub(r"<br\s*/?>", "\n", frag, flags=re.IGNORECASE)
        text = BeautifulSoup(frag, "html.parser").get_text()
        lines.extend(ln.strip() for ln in text.split("\n"))
    return lines


def scan_inbox_messages():
    """Return {body_key: {"is_cipher": bool, "lines": [str]}} for every distinct
    inbox message across all snapshots. A snapshot with a missing/unreadable
    manifest (e.g. a crawl writing it right now) is skipped."""
    out = {}
    if not os.path.isdir(cfg.SNAPSHOT_DIR):
        return out
    for date in sorted(os.listdir(cfg.SNAPSHOT_DIR)):
        man = os.path.join(cfg.SNAPSHOT_DIR, date, "manifest.json")
        try:
            with open(man, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        page = (manifest.get("pages", {}).get(INBOX_PATH)
                or manifest.get("pages", {}).get(INBOX_PATH.rstrip("/")))
        if not page:
            continue
        sha = page.get("blob", "")
        blob = os.path.join(cfg.BLOB_DIR, sha[:2], sha + ".html")
        try:
            with open(blob, encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
        except OSError:
            continue
        for _card, body_ps in _inbox_message_cards(soup):
            key = _inbox_body_key(body_ps)
            if key in out:
                continue
            is_cipher = classify_is_cipher(_inbox_body_text(body_ps))
            out[key] = {"is_cipher": is_cipher,
                        "lines": body_lines(body_ps, is_cipher)}
    return out


# ── Translation backends ─────────────────────────────────────────────────────

def _google_translate(text: str) -> str:
    """Free Google translation via deep-translator. Imported lazily so the rest
    of the module (and the tests) work without the package installed."""
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="fr", target="en").translate(text)


def _read_env(path: str) -> dict:
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _llm_translate(text: str, api_key: str, model: str) -> str:
    """OpenRouter chat-completions fallback. Glossary-aware, preserves breaks."""
    glossary = ", ".join(GLOSSARY)
    prompt = (
        "Translate the following French text to English. "
        f"Keep these terms verbatim, unchanged: {glossary}. "
        "Keep any URLs exactly as-is. Preserve line breaks exactly. "
        "Output only the translation, nothing else.\n\n" + text
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return _parse_llm_response(data)


def _parse_llm_response(data: dict) -> str:
    """Pull the message text out of an OpenRouter chat-completions response.
    Raises a clean RuntimeError (carrying no response/credential content) if the
    shape is unexpected — important because this error is logged."""
    choices = data.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if not content:
        raise RuntimeError("unexpected OpenRouter response shape")
    return content.strip()


def _apply_google_postfix(text: str) -> str:
    for pat, repl in _GOOGLE_POSTFIX.items():
        text = re.sub(pat, repl, text)
    return text


def translate_message(lines, llm_key="", llm_model=""):
    """Translate a message (list of source lines) to an English string with the
    same line structure. Returns (english_text, source_tag). Google is primary
    and runs line-by-line (it otherwise collapses line breaks); the LLM fallback
    translates the whole message at once for better context.

    Raises RuntimeError if every available backend fails."""
    # Primary: Google, line by line (blank lines preserved).
    try:
        out = []
        for ln in lines:
            out.append(_apply_google_postfix(_google_translate(ln)) if ln else "")
        return "\n".join(out), "google"
    except Exception as google_err:
        if not llm_key:
            raise RuntimeError(f"google failed and no LLM key: {google_err}")

    # Fallback: LLM, whole message.
    en = _llm_translate("\n".join(lines), llm_key, llm_model)
    return en, "llm"


# ── Guards: crawl-in-progress + single-instance lock ─────────────────────────

def crawl_running() -> bool:
    """True if a site_crawler.py process is alive. Uses pgrep, falling back to a
    /proc scan if pgrep is unavailable."""
    try:
        r = subprocess.run(["pgrep", "-f", "site_crawler.py"],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() != ""
    except FileNotFoundError:
        pass
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if b"site_crawler.py" in f.read():
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def acquire_lock(path: str = LOCK_FILE):
    """Non-blocking single-instance lock. Returns the held file object, or None
    if another instance holds it. Keep the returned object alive for the run."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fh


# ── Sidecar I/O ──────────────────────────────────────────────────────────────

def _load_messages(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("messages", {})
    except (OSError, ValueError):
        return {}


def _write_machine_cache(messages: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = MACHINE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "messages": messages},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, MACHINE_CACHE)          # atomic


# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, force: bool = False) -> int:
    lock = acquire_lock(LOCK_FILE)
    if lock is None:
        print("another translator instance is running — exiting")
        return 0
    try:
        if not force and crawl_running():
            print("crawl in progress — exiting without translating")
            return 0

        curated = _load_messages(CURATED_SEED)
        machine = _load_messages(MACHINE_CACHE)
        known = set(curated) | set(machine)

        messages = scan_inbox_messages()
        todo = {k: v for k, v in messages.items() if k not in known}

        if not todo:
            print(f"up to date — {len(messages)} message(s), nothing new")
            return 0

        print(f"{len(todo)} new message(s) to translate"
              + (" (dry-run)" if dry_run else ""))
        if dry_run:
            for k, info in todo.items():
                kind = "cipher" if info["is_cipher"] else "plain"
                print(f"  {k[:12]}… [{kind}] {' / '.join(info['lines'])[:80]}")
            return 0

        env = _read_env(cfg.GATE_ENV_FILE)
        llm_key = env.get("OPENROUTER_API_KEY") or os.environ.get(
            "OPENROUTER_API_KEY", "")
        llm_model = (env.get("OPENROUTER_MODEL")
                     or os.environ.get("OPENROUTER_MODEL")
                     or "anthropic/claude-3.5-sonnet")

        added = 0
        for key, info in todo.items():
            try:
                en, source = translate_message(info["lines"], llm_key, llm_model)
            except Exception as e:                       # leave for next run
                # Log the exception TYPE only — never the message/response body,
                # which could carry API-key-bearing error text into the log.
                print(f"  ! {key[:12]}… failed ({type(e).__name__})")
                continue
            machine[key] = {
                "en": en,
                "is_cipher": info["is_cipher"],
                "mt": True,
                "source": source,
                "ts": int(time.time()),
            }
            added += 1
            print(f"  + {key[:12]}… via {source}")

        if added:
            _write_machine_cache(machine)
            print(f"wrote {added} translation(s) to {MACHINE_CACHE}")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-translate new inbox messages.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be translated, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="translate even if a crawl appears to be running")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    main()
