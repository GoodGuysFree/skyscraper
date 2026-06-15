"""
Project Skyscraper — Local Wayback Machine Server.

Serves the archived site with date-based routing and a floating
navigation overlay for time-travel.

Usage:
    uv run wayback_server.py                  # default port 8070
    uv run wayback_server.py --port 9090      # custom port
"""

import os
import sys
import json
import argparse
import re
import hmac
import hashlib
import threading
import subprocess
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import unquote, urlparse, parse_qs
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup

import crawler_config as cfg

sys.stdout.reconfigure(encoding="utf-8")

INTERNAL_HOSTS = {cfg.SITE_DOMAIN, f"www.{cfg.SITE_DOMAIN}"}

# ─── Gate ────────────────────────────────────────────────────────────────────

_GATE_COOKIE = "wb_token"


def _load_gate_password() -> str:
    """Read ARCHIVE_PASSWORD from the .env file (never committed)."""
    env_path = cfg.GATE_ENV_FILE
    if not os.path.isfile(env_path):
        return ""
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("ARCHIVE_PASSWORD="):
                return line.partition("=")[2].strip()
    return ""


def _token_for(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _load_trigger_token() -> str:
    """Read TRIGGER_TOKEN from .env (never committed)."""
    env_path = cfg.GATE_ENV_FILE
    if not os.path.isfile(env_path):
        return ""
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("TRIGGER_TOKEN="):
                return line.partition("=")[2].strip()
    return ""


def verify_trigger_signature(secret: str, ts_str: str, body: str,
                              sig_header: str, now: int | None = None) -> bool:
    """Return True if HMAC-SHA256 signature is valid and timestamp is fresh."""
    try:
        ts = int(ts_str)
    except (ValueError, TypeError):
        return False
    now = now if now is not None else int(time.time())
    if abs(now - ts) > 300:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), f"{ts_str}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ─── Crawl Scheduler ─────────────────────────────────────────────────────────

class CrawlScheduler:
    """Debounce + coalesce external crawl triggers.

    States (implicit):
      idle        no timer, not running
      debouncing  timer active, not running
      running     crawl subprocess + cooldown active
      running+queued  same, plus one re-crawl pending
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._debounce_timer: threading.Timer | None = None
        self._running = False   # True from crawl-start through end of cooldown
        self._queued = False    # re-crawl requested while running/cooling

    def trigger(self):
        """Called by the webhook handler. Returns immediately."""
        with self._lock:
            if self._running:
                self._queued = True
                self._log("trigger received — crawl running, queued")
                return
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                cfg.TRIGGER_DEBOUNCE_SECONDS, self._debounce_fired
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()
            self._log(f"trigger received — debounce reset ({cfg.TRIGGER_DEBOUNCE_SECONDS}s)")

    def _debounce_fired(self):
        with self._lock:
            self._debounce_timer = None
            self._running = True
        self._log("debounce elapsed — spawning crawl")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        log_path = os.path.join(cfg.WORKSPACE_DIR, "crawl.log")
        self._log("crawl started")
        try:
            with open(log_path, "a") as log:
                subprocess.run(
                    [sys.executable, os.path.join(cfg.WORKSPACE_DIR, "site_crawler.py")],
                    stdout=log, stderr=log,
                    cwd=cfg.WORKSPACE_DIR,
                    env=os.environ,
                )
        except Exception as e:
            self._log(f"crawl error: {e}")
        self._log(f"crawl done — cooling down ({cfg.TRIGGER_COOLDOWN_SECONDS}s)")
        time.sleep(cfg.TRIGGER_COOLDOWN_SECONDS)
        with self._lock:
            self._running = False
            if self._queued:
                self._queued = False
                self._log("queued trigger — restarting debounce")
                self._debounce_timer = threading.Timer(
                    cfg.TRIGGER_DEBOUNCE_SECONDS, self._debounce_fired
                )
                self._debounce_timer.daemon = True
                self._debounce_timer.start()
            else:
                self._log("idle")

    @staticmethod
    def _log(msg: str):
        sys.stdout.write(f"  [scheduler] {msg}\n")
        sys.stdout.flush()


def _build_landing_page(error: str = "") -> str:
    """Return the full HTML for the front-page gate / splash."""
    if cfg.GATE_MODE == "password":
        error_html = f'<p class="error">{error}</p>' if error else ""
        gate_html = f"""\
      {error_html}
      <form method="POST" action="/~gate">
        <input type="password" name="pw" placeholder="Password" autofocus>
        <button type="submit">Enter Archive</button>
      </form>"""
    else:
        gate_html = """\
      <form method="POST" action="/~gate">
        <button type="submit">Enter Archive</button>
      </form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project Skyscraper — Archive</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d0d0d;
      color: #c0c0c0;
      font-family: 'IBM Plex Mono', 'Courier New', monospace;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem;
    }}
    .card {{
      max-width: 660px;
      width: 100%;
      border: 1px solid #222;
      background: #111;
      padding: 3rem 3.5rem;
    }}
    .logo {{ font-size: 0.7rem; color: #444; letter-spacing: 0.25em; text-transform: uppercase; margin-bottom: 0.4rem; }}
    h1 {{ font-size: 1.5rem; color: #e8e8e8; letter-spacing: 0.06em; margin-bottom: 0.25rem; }}
    .tagline {{ font-size: 0.75rem; color: #444; margin-bottom: 3rem; }}
    .section {{ margin-bottom: 1.8rem; }}
    .section-label {{
      font-size: 0.65rem; color: #3a3a3a; text-transform: uppercase;
      letter-spacing: 0.2em; margin-bottom: 0.5rem;
    }}
    .section p {{ font-size: 0.82rem; color: #777; line-height: 1.75; }}
    hr {{ border: none; border-top: 1px solid #1c1c1c; margin: 2.2rem 0; }}
    .gate {{ text-align: center; }}
    input[type=password] {{
      display: block; width: 100%;
      background: #0d0d0d; border: 1px solid #222; color: #c0c0c0;
      padding: 0.65rem 1rem; font-family: inherit; font-size: 0.88rem;
      margin-bottom: 0.75rem; outline: none;
    }}
    input[type=password]:focus {{ border-color: #3a3a3a; }}
    button {{
      display: block; width: 100%;
      background: #181818; border: 1px solid #2e2e2e; color: #bbb;
      padding: 0.65rem 2rem; font-family: inherit; font-size: 0.82rem;
      letter-spacing: 0.1em; cursor: pointer; text-transform: uppercase;
    }}
    button:hover {{ background: #1e1e1e; border-color: #444; color: #e0e0e0; }}
    .error {{ color: #c0504d; font-size: 0.78rem; margin-bottom: 0.75rem; }}
    .footer {{ margin-top: 2rem; font-size: 0.65rem; color: #2a2a2a; text-align: center; }}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">GoodGuysFree Community</div>
  <h1>Project Skyscraper</h1>
  <div class="tagline">Wayback Archive — community-maintained preservation mirror</div>

  <div class="section">
    <div class="section-label">About</div>
    <p>
      This archive preserves snapshots of project-skyscraper.com, an ARG
      (alternate reality game) created for the No Man's Sky community.
      Pages are captured periodically so the record is never lost.
    </p>
  </div>

  <div class="section">
    <div class="section-label">Disclaimer</div>
    <p>
      This is an unofficial fan archive, not affiliated with Hello Games or
      the site's original creators. All original content remains the property
      of its creators. Provided for research and preservation only.
    </p>
  </div>

  <div class="section">
    <div class="section-label">Credits &amp; Thanks</div>
    <p>
      <!-- Add credits and thanks here -->
      Built and maintained by the GoodGuysFree community.
      Thanks to everyone who contributed to solving the ARG.
    </p>
  </div>

  <hr>

  <div class="gate">
{gate_html}
  </div>
</div>
<div class="footer">Project Skyscraper Wayback Machine</div>
</body>
</html>"""
_ABS_URL_RE = re.compile(r"^https?://([^/]+)(/.*)?$", re.IGNORECASE)
# Extensions that mark an href as a media/file link (download), not a page.
_ASSET_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|css|js|json|xml|rss|txt|csv|"
    r"pdf|zip|gz|tar|rar|7z|mp[34]|m4a|webm|mov|avi|wav|ogg|"
    r"woff2?|ttf|otf|eot|doc[xm]?|xls[xm]?|ppt[xm]?)$",
    re.IGNORECASE,
)


def rewrite_internal_links(html: str, date: str, asset_by_path: dict) -> str:
    """Rewrite on-site <a> links so navigation stays inside the archive.

    - Links to on-site *pages* become /@<date>/<path>.
    - Links to on-site *media files* (e.g. an <a> to a .jpg in wp-content) become
      the local /_assets/... blob if we downloaded it, else fall back to the live
      URL so they at least resolve online instead of 404-ing as a phantom page.
    - External links and already-rewritten links are left untouched.

    Working without JS means middle-click / open-in-new-tab also stay local.
    """
    soup = BeautifulSoup(html, "html.parser")
    prefix = f"/@{date}"

    # Strip the legacy "[ARCHIVED — Form disabled]" notice baked into older
    # snapshots (the crawler no longer injects it).
    for s in soup.find_all(string=lambda t: t and "Form disabled" in t):
        parent = s.find_parent()
        if parent is not None:
            parent.decompose()
        else:
            s.extract()

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if href.startswith(("/@", "/_assets", "/~")):
            continue

        # Resolve to an on-site path, or skip if external.
        m = _ABS_URL_RE.match(href)
        if m:
            if m.group(1).lower() not in INTERNAL_HOSTS:
                continue  # external link — leave untouched
            path = m.group(2) or "/"
        elif href.startswith("/"):
            path = href
        else:
            continue  # other relative link — leave untouched

        bare = path.split("?", 1)[0].split("#", 1)[0]
        if _ASSET_EXT_RE.search(bare):
            # On-site media file link → local blob, or live URL as fallback.
            local = asset_by_path.get(bare)
            a["href"] = local if local else (cfg.SITE_ORIGIN + path)
        else:
            a["href"] = prefix + path

    return str(soup)


# ─── Manifest Cache ──────────────────────────────────────────────────────────

class ManifestCache:
    """Loads and caches snapshot manifests."""

    def __init__(self, snapshot_dir: str):
        self.snapshot_dir = snapshot_dir
        self._manifests: dict[str, dict] = {}
        self._dates: list[str] = []
        self._changes: dict[str, dict] = {}
        self._asset_by_path: dict[str, dict[str, str]] = {}
        self._signature = None
        self.reload()

    def _compute_signature(self):
        """Cheap fingerprint of the snapshots on disk (dates + manifest mtimes)."""
        if not os.path.isdir(self.snapshot_dir):
            return ()
        sig = []
        for name in sorted(os.listdir(self.snapshot_dir)):
            mp = os.path.join(self.snapshot_dir, name, "manifest.json")
            if os.path.isfile(mp):
                sig.append((name, os.path.getmtime(mp)))
        return tuple(sig)

    def maybe_reload(self):
        """Reload if a crawl has added/updated snapshots since we last loaded.
        Keeps a long-running server in sync without a restart."""
        if self._compute_signature() != self._signature:
            self.reload()

    @staticmethod
    def _asset_path_key(original_url: str) -> str | None:
        """Canonical on-site path for an asset's original URL, so that an <a>
        href to that file can be matched regardless of Photon CDN / query."""
        try:
            p = urlparse(original_url)
        except Exception:
            return None
        path = p.path or ""
        # Jetpack Photon wraps URLs as i0.wp.com/<host>/<path> — strip the host.
        if path.startswith(f"/{cfg.SITE_DOMAIN}/"):
            path = path[len(f"/{cfg.SITE_DOMAIN}"):]
        return path or None

    def reload(self):
        # Build into local structures, then swap references in atomically. Under
        # ThreadingHTTPServer this lets readers run lock-free: they always see a
        # fully-consistent previous-or-next snapshot set, never a half-cleared one.
        manifests: dict[str, dict] = {}
        dates: list[str] = []
        changes: dict[str, dict] = {}
        asset_by_path: dict[str, dict[str, str]] = {}

        if os.path.isdir(self.snapshot_dir):
            for name in sorted(os.listdir(self.snapshot_dir)):
                manifest_path = os.path.join(self.snapshot_dir, name, "manifest.json")
                if os.path.isfile(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as f:
                            manifest = json.load(f)
                        manifests[name] = manifest
                        dates.append(name)
                        # Build reverse map: on-site file path → local blob URL
                        by_path = {}
                        for local_url, meta in manifest.get("assets", {}).items():
                            key = self._asset_path_key(meta.get("original_url", ""))
                            if key:
                                by_path[key] = local_url
                        asset_by_path[name] = by_path
                    except Exception:
                        pass

                changes_path = os.path.join(self.snapshot_dir, name, "changes.json")
                if os.path.isfile(changes_path):
                    try:
                        with open(changes_path, "r", encoding="utf-8") as f:
                            changes[name] = json.load(f)
                    except Exception:
                        pass

        self._manifests = manifests
        self._dates = dates
        self._changes = changes
        self._asset_by_path = asset_by_path
        self._signature = self._compute_signature()

    def get_asset_by_path(self, date: str) -> dict:
        return self._asset_by_path.get(date, {})

    @property
    def dates(self) -> list[str]:
        return self._dates

    @property
    def latest_date(self) -> str | None:
        return self._dates[-1] if self._dates else None

    def get_manifest(self, date: str) -> dict | None:
        return self._manifests.get(date)

    def get_changes(self, date: str) -> dict | None:
        return self._changes.get(date)

    def get_page_history(self, path: str) -> list[dict]:
        """Get all dates where this page existed, with its hash per date."""
        history = []
        for date in self._dates:
            m = self._manifests.get(date)
            if m and path in m.get("pages", {}):
                entry = m["pages"][path].copy()
                entry["date"] = date
                history.append(entry)
        return history


# ─── Navigation Overlay ─────────────────────────────────────────────────────

def build_header_html(date: str, original_url: str) -> str:
    """Thin fixed top bar: mirror notice, link to live page, snapshot date, GGF badge."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%dT%H%M")
        date_display = dt.strftime("%b %d, %Y · %H:%M UTC")
    except ValueError:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_display = dt.strftime("%b %d, %Y")
        except ValueError:
            date_display = date

    live_domain = cfg.SITE_DOMAIN
    return f"""<!-- ═══ WB HEADER ═══ -->
<style>
#wb-topbar {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 99998;
  background: rgba(8,8,8,0.93); border-bottom: 1px solid #1e1e1e;
  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  font-family: 'IBM Plex Mono', 'Courier New', monospace;
  font-size: 11px; color: #666;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 14px; height: 26px; gap: 10px; box-sizing: border-box;
}}
#wb-topbar a {{ color: #7dd3fc; text-decoration: none; }}
#wb-topbar a:hover {{ color: #bae6fd; }}
#wb-topbar .wb-tb-left {{ display: flex; align-items: center; gap: 8px; }}
#wb-topbar .wb-tb-mirror {{
  background: #1a1a1a; border: 1px solid #2a2a2a; color: #888;
  padding: 1px 6px; font-size: 10px; letter-spacing: 0.08em;
}}
#wb-topbar .wb-tb-right {{ display: flex; align-items: center; gap: 10px; white-space: nowrap; }}
#wb-topbar .wb-tb-sep {{ color: #2e2e2e; }}
#wb-topbar strong {{ color: #9ca3af; font-weight: normal; }}
#wb-topbar .wb-tb-ggf {{
  color: #333; font-size: 10px; letter-spacing: 0.15em;
  border-left: 1px solid #1e1e1e; padding-left: 10px;
}}
</style>
<div id="wb-topbar">
  <div class="wb-tb-left">
    <span>This is a mirror of <a href="{cfg.SITE_ORIGIN}" target="_blank" rel="noopener noreferrer">https://{live_domain}</a></span>
    <span class="wb-tb-sep">·</span>
    <span>Now viewing snapshot: <strong>{date_display}</strong></span>
  </div>
  <div class="wb-tb-right">
    <span class="wb-tb-ggf">GGF</span>
  </div>
</div>
<!-- ═══ END WB HEADER ═══ -->"""


def build_overlay_html(current_date: str, manifests,
                       changes: dict | None, current_path: str) -> str:
    """Build the floating navigation overlay injected into every HTML page."""
    dates = manifests.dates

    def get_display(d):
        m = manifests.get_manifest(d)
        if m and "crawled_at" in m:
            return f"{d} {m['crawled_at'][11:19]} UTC"
        return d

    date_idx = dates.index(current_date) if current_date in dates else -1
    prev_date = dates[date_idx - 1] if date_idx > 0 else ""
    next_date = dates[date_idx + 1] if date_idx < len(dates) - 1 else ""

    # Change indicator dot for current page on current snapshot
    dot_class = ""
    dot_title = ""
    if changes:
        if current_path in changes.get("pages_added", []):
            dot_class = "wb-new"
            dot_title = "New on this snapshot"
        elif current_path in changes.get("pages_modified", []):
            dot_class = "wb-modified"
            dot_title = "Modified on this snapshot"
    dot_html = f'<span class="wb-dot {dot_class}" title="{dot_title}"></span>' if dot_class else ""

    summary = changes.get("summary", "") if changes else "First snapshot"
    dates_json = json.dumps(dates)

    # Picker rows — newest first so current is near the top
    rows = []
    for d in reversed(dates):
        is_cur = d == current_date
        cls = "wb-row wb-row-current" if is_cur else "wb-row"
        now_badge = '<span class="wb-now">now</span>' if is_cur else ""
        rows.append(f'<div class="{cls}" onclick="wbNav(\'{d}\')">'
                    f'<span>{get_display(d)}</span>{now_badge}</div>')
    rows_html = "\n        ".join(rows)

    dis_prev = "disabled" if not prev_date else ""
    dis_next = "disabled" if not next_date else ""

    return f"""
<!-- ═══ WAYBACK MACHINE OVERLAY ═══ -->
<div id="wb-overlay">
<style>
#wb-overlay {{
  position: fixed; top: 38px; right: 12px; z-index: 999999;
  font-family: 'IBM Plex Mono', 'Courier New', monospace;
  font-size: 12px; color: #e0e0e0; user-select: none;
}}
.wb-panel {{
  background: rgba(10,10,10,0.93); border: 1px solid #333; border-radius: 6px;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  box-shadow: 0 4px 24px rgba(0,0,0,0.65); padding: 8px 10px; min-width: 300px;
}}
.wb-nav {{ display: flex; align-items: center; gap: 6px; }}
.wb-btn {{
  background: #1e1e1e; border: 1px solid #3a3a3a; color: #999;
  width: 28px; height: 28px; border-radius: 4px; cursor: pointer;
  font-size: 13px; display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: background 0.12s, color 0.12s, border-color 0.12s;
  font-family: inherit;
}}
.wb-btn:hover:not(:disabled) {{ background: #2a2a2a; color: #fff; border-color: #555; }}
.wb-btn:disabled {{ opacity: 0.2; cursor: default; }}
.wb-picker {{ position: relative; flex: 1; }}
.wb-picker-btn {{
  width: 100%; background: #181818; border: 1px solid #3a3a3a; color: #ddd;
  padding: 5px 10px; border-radius: 4px; cursor: pointer;
  font-family: inherit; font-size: 11px; text-align: left;
  display: flex; align-items: center; gap: 6px;
  transition: background 0.12s, border-color 0.12s;
}}
.wb-picker-btn:hover {{ background: #222; border-color: #555; }}
.wb-picker-btn.wb-open {{ background: #222; border-color: #555; border-bottom-left-radius: 0; border-bottom-right-radius: 0; }}
.wb-caret {{ margin-left: auto; opacity: 0.4; font-size: 9px; padding-left: 4px; }}
.wb-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}
.wb-dot.wb-new {{ background: #4ade80; box-shadow: 0 0 5px #4ade80; }}
.wb-dot.wb-modified {{ background: #fbbf24; box-shadow: 0 0 5px #fbbf24; }}
.wb-list {{
  display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 2;
  background: #0e0e0e; border: 1px solid #333; border-top: none;
  border-bottom-left-radius: 4px; border-bottom-right-radius: 4px;
  box-shadow: 0 10px 28px rgba(0,0,0,0.75);
  max-height: 230px; overflow-y: auto;
}}
.wb-list.wb-open {{ display: block; }}
.wb-row {{
  padding: 6px 10px; cursor: pointer; font-size: 11px;
  border-bottom: 1px solid #191919;
  display: flex; align-items: center; justify-content: space-between;
  color: #999; transition: background 0.1s, color 0.1s;
}}
.wb-row:last-child {{ border-bottom: none; }}
.wb-row:hover {{ background: #1c1c1c; color: #eee; }}
.wb-row-current {{ background: #0b1a0b; color: #c8e6c8; }}
.wb-row-current:hover {{ background: #0f200f; color: #e0f0e0; }}
.wb-now {{
  font-size: 9px; color: #4ade80; border: 1px solid #2d6a2d;
  padding: 1px 5px; border-radius: 3px; letter-spacing: 0.05em;
}}
.wb-summary {{
  margin-top: 6px; padding-top: 5px; border-top: 1px solid #1c1c1c;
  color: #4a4a4a; font-size: 10px;
}}
</style>
<div class="wb-panel">
  <div class="wb-nav">
    <button class="wb-btn" {dis_prev} onclick="wbNav('{prev_date}')">◀</button>
    <div class="wb-picker" id="wb-picker">
      <button class="wb-picker-btn" id="wb-picker-btn" onclick="wbToggle(event)">
        {dot_html}<span>{get_display(current_date)}</span><span class="wb-caret">▾</span>
      </button>
      <div class="wb-list" id="wb-list">
        {rows_html}
      </div>
    </div>
    <button class="wb-btn" {dis_next} onclick="wbNav('{next_date}')">▶</button>
  </div>
  <div class="wb-summary">{summary}</div>
</div>
<script>
(function() {{
  var currentDate = "{current_date}";
  var currentPath = "{current_path}";

  window.wbNav = function(date) {{
    if (date) window.location.href = "/@" + date + currentPath;
  }};

  window.wbToggle = function(e) {{
    e.stopPropagation();
    var btn = document.getElementById('wb-picker-btn');
    var list = document.getElementById('wb-list');
    var open = list.classList.contains('wb-open');
    list.classList.toggle('wb-open', !open);
    btn.classList.toggle('wb-open', !open);
    if (!open) {{
      var cur = list.querySelector('.wb-row-current');
      if (cur) cur.scrollIntoView({{block: 'nearest'}});
    }}
  }};

  document.addEventListener('click', function(e) {{
    var picker = document.getElementById('wb-picker');
    if (picker && !picker.contains(e.target)) {{
      document.getElementById('wb-list').classList.remove('wb-open');
      document.getElementById('wb-picker-btn').classList.remove('wb-open');
    }}
  }});

  // Intercept internal page links — maintain date context
  document.addEventListener('click', function(e) {{
    var link = e.target.closest('a');
    if (!link) return;
    var href = link.getAttribute('href');
    if (!href) return;
    if (href.startsWith('/@') || href.startsWith('/_assets') ||
        href.startsWith('#') || href.startsWith('mailto:') ||
        href.startsWith('javascript:')) return;
    if (/^https?:\\/\\//.test(href)) {{
      try {{
        var u = new URL(href);
        if (u.hostname !== '{cfg.SITE_DOMAIN}' &&
            u.hostname !== 'www.{cfg.SITE_DOMAIN}') return;
        href = u.pathname;
      }} catch(e) {{ return; }}
    }}
    if (href.startsWith('/')) {{
      e.preventDefault();
      window.location.href = '/@' + currentDate + href;
    }}
  }});
}})();
</script>
</div>
<!-- ═══ END WAYBACK OVERLAY ═══ -->
"""


# ─── Request Handler ─────────────────────────────────────────────────────────

class WaybackHandler(BaseHTTPRequestHandler):
    manifests: ManifestCache = None   # set by main()
    gate_password: str = ""           # set by main()
    trigger_token: str = ""           # set by main()
    scheduler: CrawlScheduler = None  # set by main()

    def log_message(self, format, *args):
        sys.stdout.write(f"  {args[0]}\n")

    # ── Gate helpers ─────────────────────────────────────────────────────────

    def _is_authenticated(self) -> bool:
        if cfg.GATE_MODE != "password":
            return True
        c = SimpleCookie()
        c.load(self.headers.get("Cookie", ""))
        token = c.get(_GATE_COOKIE)
        return bool(token and token.value == _token_for(WaybackHandler.gate_password))

    def _auth_cookie_header(self) -> str:
        token = _token_for(WaybackHandler.gate_password)
        return f"{_GATE_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"

    def do_HEAD(self):
        self._head_only = True
        self.do_GET()

    def _handle_trigger(self):
        ts_str = self.headers.get("X-Timestamp", "")
        sig_header = self.headers.get("X-Signature", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

        token = WaybackHandler.trigger_token
        if not token:
            self._respond(503, b'{"error":"trigger not configured"}',
                          "application/json")
            return

        if not ts_str or not sig_header:
            self._respond(400, b'{"error":"missing X-Timestamp or X-Signature"}',
                          "application/json")
            return

        if not verify_trigger_signature(token, ts_str, body, sig_header):
            self._respond(401, b'{"error":"invalid signature or stale timestamp"}',
                          "application/json")
            return

        WaybackHandler.scheduler.trigger()
        self._respond(202, b'{"status":"accepted"}', "application/json")

    def do_POST(self):
        path = unquote(self.path)

        # Trigger-crawl webhook — HMAC-authenticated, no gate cookie required
        if path == "/~api/trigger-crawl":
            self._handle_trigger()
            return

        if path != "/~gate":
            self._error(405, "Method not allowed")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)

        if cfg.GATE_MODE == "button":
            # Splash only — no password required; redirect straight in.
            latest = self.manifests.latest_date or ""
            self.send_response(302)
            self.send_header("Location", f"/@{latest}/")
            self.end_headers()
            return

        # Password mode
        pw = params.get("pw", [""])[0]
        if pw == WaybackHandler.gate_password:
            latest = self.manifests.latest_date or ""
            self.send_response(302)
            self.send_header("Set-Cookie", self._auth_cookie_header())
            self.send_header("Location", f"/@{latest}/")
            self.end_headers()
        else:
            body_html = _build_landing_page(error="Incorrect password.")
            self._respond(403, body_html.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):
        path = unquote(self.path)

        # Keep in sync with the crawler — pick up newly-added snapshots/pages.
        self.manifests.maybe_reload()

        # ── Root → landing page (gate or splash) ──
        if path == "/" or path == "":
            if self._is_authenticated():
                latest = self.manifests.latest_date
                if latest:
                    self._redirect(f"/@{latest}/")
                else:
                    self._error(503, "No snapshots available. Run site_crawler.py first.")
            else:
                body = _build_landing_page()
                self._respond(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return

        # ── Logout ──
        if path == "/~gate/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", f"{_GATE_COOKIE}=; Max-Age=0; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return

        # ── Blob/asset serving — always open (content-addressed, no page info) ──
        if path.startswith("/_assets/"):
            self._serve_asset(path)
            return

        # ── Everything else requires auth ──
        if not self._is_authenticated():
            self._redirect("/")
            return

        # ── API endpoints ──
        if path.startswith("/~api/"):
            self._handle_api(path)
            return

        # ── Date-routed page serving ──
        match = re.match(r'^/@(\d{4}-\d{2}-\d{2}(?:T\d{4})?)(/.*)$', path)
        if match:
            date = match.group(1)
            page_path = match.group(2)
            self._serve_page(date, page_path)
            return

        # ── Fallback: treat as latest-date page ──
        latest = self.manifests.latest_date
        if latest and path.startswith("/"):
            self._redirect(f"/@{latest}{path}")
            return

        self._error(404, "Not found")

    def _serve_page(self, date: str, page_path: str):
        manifest = self.manifests.get_manifest(date)
        if not manifest:
            self._error(404, f"No snapshot for date: {date}")
            return

        # Normalize path
        if not page_path.endswith("/"):
            page_path += "/"

        pages = manifest.get("pages", {})
        page_data = pages.get(page_path)

        if not page_data:
            # Try without trailing slash
            alt_path = page_path.rstrip("/")
            page_data = pages.get(alt_path)
            if not page_data and alt_path:
                page_data = pages.get(alt_path + "/")

        if not page_data:
            self._error(404, f"Page not found: {page_path} (date: {date})")
            return

        sha = page_data["blob"]
        blob_path = os.path.join(cfg.BLOB_DIR, sha[:2], sha + ".html")

        if not os.path.isfile(blob_path):
            self._error(500, f"Blob missing: {sha}")
            return

        with open(blob_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Rewrite on-site links to local date-scoped paths so navigation stays
        # inside the archive (works without JS — middle-click / new-tab too).
        html = rewrite_internal_links(
            html, date, self.manifests.get_asset_by_path(date)
        )

        # Inject top header bar after <body>
        original_url = page_data.get("original_url", cfg.SITE_ORIGIN + page_path)
        header = build_header_html(date, original_url)
        body_tag = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_tag:
            end = body_tag.end()
            html = html[:end] + "\n" + header + html[end:]
        else:
            html = header + html

        # Inject navigation overlay before </body>
        changes = self.manifests.get_changes(date)
        overlay = build_overlay_html(
            date, self.manifests, changes, page_path
        )
        if "</body>" in html:
            html = html.replace("</body>", overlay + "\n</body>")
        elif "</html>" in html:
            html = html.replace("</html>", overlay + "\n</html>")
        else:
            html += overlay

        self._respond(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_asset(self, path: str):
        # path: /_assets/ab/abcdef1234.jpg
        rel = path[len("/_assets/"):]
        fs_path = os.path.join(cfg.BLOB_DIR, rel.replace("/", os.sep))

        if not os.path.isfile(fs_path):
            self._error(404, f"Asset not found: {path}")
            return

        _, ext = os.path.splitext(fs_path)
        content_type = cfg.MIME_TYPES.get(ext.lower(), "application/octet-stream")

        with open(fs_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Content-addressed = immutable = cache forever
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _handle_api(self, path: str):
        if path == "/~api/dates":
            data = {
                "dates": self.manifests.dates,
                "latest": self.manifests.latest_date,
                "count": len(self.manifests.dates),
            }
            self._json_response(data)

        elif path.startswith("/~api/manifest/"):
            date = path.split("/")[-1]
            m = self.manifests.get_manifest(date)
            if m:
                self._json_response(m)
            else:
                self._error(404, f"No manifest for {date}")

        elif path.startswith("/~api/changes/"):
            date = path.split("/")[-1]
            c = self.manifests.get_changes(date)
            if c:
                self._json_response(c)
            else:
                self._error(404, f"No changes for {date}")

        elif path.startswith("/~api/page-history/"):
            page_path = "/" + "/".join(path.split("/")[3:])
            history = self.manifests.get_page_history(page_path)
            self._json_response({"path": page_path, "snapshots": history})

        elif path == "/~api/reload":
            self.manifests.reload()
            self._json_response({"status": "ok", "dates": len(self.manifests.dates)})

        else:
            self._error(404, f"Unknown API endpoint: {path}")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _error(self, code: int, message: str):
        body = f"""<!DOCTYPE html>
<html><head><title>{code}</title>
<style>
body {{ background: #111; color: #e0e0e0; font-family: 'IBM Plex Mono', monospace;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; }}
.box {{ text-align: center; max-width: 500px; }}
h1 {{ font-size: 48px; margin: 0 0 16px; opacity: 0.6; }}
p {{ color: #888; }}
a {{ color: #7dd3fc; }}
</style></head><body><div class="box">
<h1>{code}</h1><p>{message}</p>
<p><a href="/">← Back to latest snapshot</a></p>
</div></body></html>"""
        self._respond(code, body.encode("utf-8"), "text/html; charset=utf-8")

    def _json_response(self, data):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self._respond(200, body, "application/json; charset=utf-8")

    def _respond(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Project Skyscraper Wayback Server")
    parser.add_argument("--port", type=int, default=cfg.SERVER_PORT)
    parser.add_argument("--host", default=cfg.SERVER_HOST)
    args = parser.parse_args()

    print(f"═══ Project Skyscraper Wayback Machine ═══")
    print()

    # Load gate password from .env
    gate_pw = _load_gate_password()
    WaybackHandler.gate_password = gate_pw
    if cfg.GATE_MODE == "password":
        if gate_pw:
            print(f"  Gate:      password mode (ARCHIVE_PASSWORD set)")
        else:
            print(f"  Gate:      password mode — WARNING: no ARCHIVE_PASSWORD in {cfg.GATE_ENV_FILE}")
    else:
        print(f"  Gate:      button mode (no password)")

    # Load trigger token and start scheduler
    trigger_tok = _load_trigger_token()
    WaybackHandler.trigger_token = trigger_tok
    WaybackHandler.scheduler = CrawlScheduler()
    if trigger_tok:
        print(f"  Trigger:   webhook enabled (TRIGGER_TOKEN set, "
              f"debounce {cfg.TRIGGER_DEBOUNCE_SECONDS}s, "
              f"cooldown {cfg.TRIGGER_COOLDOWN_SECONDS}s)")
    else:
        print(f"  Trigger:   WARNING — no TRIGGER_TOKEN in {cfg.GATE_ENV_FILE}, webhook disabled")

    # Load manifests
    cache = ManifestCache(cfg.SNAPSHOT_DIR)
    WaybackHandler.manifests = cache

    if not cache.dates:
        print("⚠ No snapshots found. Run 'uv run site_crawler.py' first.")
        print(f"  Looking in: {cfg.SNAPSHOT_DIR}")
        print()

    print(f"  Snapshots: {len(cache.dates)} dates loaded")
    if cache.dates:
        print(f"  Range:     {cache.dates[0]} → {cache.dates[-1]}")
        latest = cache.get_manifest(cache.latest_date)
        if latest:
            print(f"  Latest:    {len(latest.get('pages', {}))} pages, "
                  f"{len(latest.get('assets', {}))} assets")
    print()
    print(f"  🌐 http://{args.host}:{args.port}/")
    print(f"     Press Ctrl+C to stop")
    print()

    server = ThreadingHTTPServer((args.host, args.port), WaybackHandler)
    server.daemon_threads = True  # don't let in-flight requests block shutdown
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
