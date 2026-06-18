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
import html
import sqlite3
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


# ─── Access Log ──────────────────────────────────────────────────────────────

class AccessLog:
    """SQLite-backed access log. Thread-safe: one write-connection + Lock;
    stats reads open a separate read-only connection so they never block writers."""

    _SKIP = ("/_assets/", "/~api/", "/_static/", "/favicon.")

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS access_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL    NOT NULL,
                ip      TEXT    NOT NULL,
                path    TEXT    NOT NULL,
                status  INTEGER NOT NULL,
                bytes   INTEGER NOT NULL,
                ua      TEXT,
                referer TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ts     ON access_log(ts);
            CREATE INDEX IF NOT EXISTS idx_ip_ts  ON access_log(ip, ts);
            CREATE INDEX IF NOT EXISTS idx_path   ON access_log(path);
        """)
        conn.commit()
        return conn

    def record(self, ip: str, path: str, status: int, bytes_sent: int,
               ua: str | None = None, referer: str | None = None) -> None:
        if any(path.startswith(p) for p in self._SKIP):
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO access_log(ts,ip,path,status,bytes,ua,referer) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), ip, path, status, bytes_sent, ua, referer),
            )
            self._conn.commit()

    def stats(self) -> dict:
        with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as c:
            return _compute_stats(c)


def _compute_sessions(rows: list[tuple]) -> tuple[int, list[float]]:
    """Split per-IP request stream into sessions on >30min idle gap.

    rows: list of (ts: float, ip: str), already sorted by ip then ts.
    Returns (session_count, list_of_durations_in_seconds).
    """
    state: dict[str, tuple[float, float]] = {}  # ip -> (session_start, last_ts)
    completed: list[float] = []

    for ts, ip in rows:
        if ip in state:
            start, last = state[ip]
            if ts - last > 1800:
                completed.append(last - start)
                state[ip] = (ts, ts)
            else:
                state[ip] = (start, ts)
        else:
            state[ip] = (ts, ts)

    for start, last in state.values():
        completed.append(last - start)

    return len(completed), completed


def _compute_stats(conn: sqlite3.Connection) -> dict:
    """Compute stats dict from a SQLite connection (read-only safe)."""
    now = time.time()
    DAY, WEEK = 86_400, 7 * 86_400

    def scalar(sql, *args):
        r = conn.execute(sql, args).fetchone()
        return (r[0] or 0) if r else 0

    summary = {
        "total_today":       scalar("SELECT COUNT(*) FROM access_log WHERE ts>?", now - DAY),
        "unique_ips_today":  scalar("SELECT COUNT(DISTINCT ip) FROM access_log WHERE ts>?", now - DAY),
        "total_week":        scalar("SELECT COUNT(*) FROM access_log WHERE ts>?", now - WEEK),
        "unique_ips_week":   scalar("SELECT COUNT(DISTINCT ip) FROM access_log WHERE ts>?", now - WEEK),
        "total_all":         scalar("SELECT COUNT(*) FROM access_log"),
    }

    # 24-bucket hourly histogram (last 7 days)
    hourly = [0] * 24
    for h, cnt in conn.execute(
        "SELECT CAST(strftime('%H', ts, 'unixepoch') AS INTEGER), COUNT(*) "
        "FROM access_log WHERE ts>? GROUP BY 1", (now - WEEK,)
    ):
        hourly[h] = cnt

    # Daily trend — last 14 days
    daily = conn.execute(
        "SELECT date(ts,'unixepoch'), COUNT(*), COUNT(DISTINCT ip) "
        "FROM access_log WHERE ts>? GROUP BY 1 ORDER BY 1",
        (now - 14 * DAY,)
    ).fetchall()

    # Top 20 paths by hits (last 30 days, status=200 only)
    top_paths = conn.execute(
        "SELECT path, COUNT(*) FROM access_log "
        "WHERE ts>? AND status=200 GROUP BY path ORDER BY 2 DESC LIMIT 20",
        (now - 30 * DAY,)
    ).fetchall()

    # Sessions (last 30 days)
    rows = conn.execute(
        "SELECT ts, ip FROM access_log WHERE ts>? ORDER BY ip, ts",
        (now - 30 * DAY,)
    ).fetchall()
    session_count, durations = _compute_sessions(rows)
    avg_dur = sum(durations) / len(durations) if durations else 0

    return {
        "summary":      summary,
        "hourly":       hourly,
        "daily":        [(d, r, u) for d, r, u in daily],
        "top_paths":    [(p, h) for p, h in top_paths],
        "sessions":     session_count,
        "avg_duration": avg_dur,
    }


def _compute_stats_empty() -> dict:
    """Return a zero-filled stats dict for when no AccessLog is initialized."""
    return {
        "summary": {
            "total_today":      0,
            "unique_ips_today": 0,
            "total_week":       0,
            "unique_ips_week":  0,
            "total_all":        0,
        },
        "hourly":       [0] * 24,
        "daily":        [],
        "top_paths":    [],
        "sessions":     0,
        "avg_duration": 0,
    }


def _build_stats_html(stats: dict) -> str:
    """Build the /~api/stats HTML page."""
    s = stats["summary"]
    hourly = stats["hourly"]
    daily = stats["daily"]
    top_paths = stats["top_paths"]
    sessions = stats["sessions"]
    avg_dur = stats["avg_duration"]
    avg_min = round(avg_dur / 60, 1)

    # Hourly histogram bars
    max_h = max(hourly) if any(hourly) else 1
    hour_bars = []
    for i, cnt in enumerate(hourly):
        pct = round(cnt / max_h * 100) if max_h else 0
        hour_bars.append(
            f'<div class="hbar-row">'
            f'<span class="hbar-label">{i:02d}</span>'
            f'<div class="hbar-track">'
            f'<div class="hbar-fill" style="width:{pct}%"></div>'
            f'</div>'
            f'<span class="hbar-cnt">{cnt}</span>'
            f'</div>'
        )
    hour_bars_html = "\n".join(hour_bars)

    # Daily trend table rows
    daily_rows = []
    for day, req, ips in daily:
        daily_rows.append(
            f"<tr><td>{day}</td><td>{req}</td><td>{ips}</td></tr>"
        )
    daily_html = "\n".join(daily_rows) if daily_rows else "<tr><td colspan='3'>No data</td></tr>"

    # Top paths table rows
    path_rows = []
    for path, hits in top_paths:
        path_rows.append(f"<tr><td>{html.escape(path)}</td><td>{hits}</td></tr>")
    paths_html = "\n".join(path_rows) if path_rows else "<tr><td colspan='2'>No data</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Access Stats — Project Skyscraper</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d0d0d; color: #e0e0e0;
      font-family: 'IBM Plex Mono', 'Courier New', monospace;
      font-size: 13px; line-height: 1.6;
      padding: 2rem;
    }}
    h1 {{ font-size: 1.2rem; color: #9ca3af; letter-spacing: 0.1em;
          margin-bottom: 2rem; font-weight: normal; }}
    h2 {{ font-size: 0.8rem; color: #555; text-transform: uppercase;
          letter-spacing: 0.2em; margin: 2rem 0 0.75rem; font-weight: normal; }}
    .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.5rem; }}
    .card {{
      background: #111; border: 1px solid #1e1e1e;
      padding: 1rem 1.25rem; min-width: 160px; flex: 1;
    }}
    .card-label {{ font-size: 0.7rem; color: #555; text-transform: uppercase;
                   letter-spacing: 0.15em; margin-bottom: 0.3rem; }}
    .card-value {{ font-size: 1.6rem; color: #c8c8c8; }}
    .total-all {{ color: #3a3a3a; font-size: 0.8rem; margin-top: 0.5rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
    th {{ text-align: left; color: #555; font-size: 0.7rem; text-transform: uppercase;
           letter-spacing: 0.12em; padding: 0.4rem 0.6rem;
           border-bottom: 1px solid #1e1e1e; font-weight: normal; }}
    td {{ padding: 0.35rem 0.6rem; border-bottom: 1px solid #141414;
           color: #aaa; }}
    td:first-child {{ color: #c8c8c8; }}
    tr:last-child td {{ border-bottom: none; }}
    .hbar-row {{ display: flex; align-items: center; gap: 0.5rem;
                 margin-bottom: 3px; }}
    .hbar-label {{ width: 2rem; color: #555; font-size: 0.75rem; text-align: right; }}
    .hbar-track {{ flex: 1; background: #1a1a1a; height: 14px; }}
    .hbar-fill {{ background: #2563eb; height: 100%; }}
    .hbar-cnt {{ width: 3rem; color: #555; font-size: 0.75rem; }}
    .sessions-line {{ color: #888; margin-top: 0.5rem; }}
    .utc-note {{ color: #3a3a3a; font-size: 0.7rem; margin-top: 0.3rem; }}
  </style>
</head>
<body>
  <h1>ACCESS STATS — Project Skyscraper Wayback</h1>

  <h2>Today / This Week</h2>
  <div class="cards">
    <div class="card">
      <div class="card-label">Requests Today</div>
      <div class="card-value">{s['total_today']}</div>
    </div>
    <div class="card">
      <div class="card-label">Unique IPs Today</div>
      <div class="card-value">{s['unique_ips_today']}</div>
    </div>
    <div class="card">
      <div class="card-label">Requests This Week</div>
      <div class="card-value">{s['total_week']}</div>
    </div>
    <div class="card">
      <div class="card-label">Unique IPs This Week</div>
      <div class="card-value">{s['unique_ips_week']}</div>
    </div>
  </div>
  <div class="total-all">All-time total: {s['total_all']} requests</div>

  <h2>Hourly Distribution (Last 7 Days, UTC)</h2>
  <div class="utc-note">Hours in UTC</div>
  {hour_bars_html}

  <h2>Daily Trend (Last 14 Days)</h2>
  <table>
    <tr><th>Date</th><th>Requests</th><th>Unique IPs</th></tr>
    {daily_html}
  </table>

  <h2>Sessions (Last 30 Days)</h2>
  <div class="sessions-line">~{sessions} sessions, avg duration {avg_min} min</div>

  <h2>Top 20 Pages (Last 30 Days, Status 200)</h2>
  <table>
    <tr><th>Path</th><th>Hits</th></tr>
    {paths_html}
  </table>
</body>
</html>"""


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
                    [sys.executable, os.path.join(cfg.WORKSPACE_DIR, "site_crawler.py"),
                     "--trigger", "api"],
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


def _build_landing_page(error: str = "", latest_date: str = "",
                        authenticated: bool = False) -> str:
    """Return the full HTML for the front-page gate / splash."""
    if authenticated and latest_date:
        gate_html = f'<a class="enter-btn" href="/@{latest_date}/">Enter Archive →</a>'
    elif cfg.GATE_MODE == "password":
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
      background: #0d0d0d url('/_static/bg.jpg') center center / cover no-repeat fixed;
      color: #c8c8c8;
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
      border: 1px solid #2a2a2a;
      background: #111;
      padding: 3rem 3.5rem;
    }}
    .logo {{ font-size: 0.75rem; color: #666; letter-spacing: 0.25em; text-transform: uppercase; margin-bottom: 0.4rem; }}
    h1 {{ font-size: 1.75rem; color: #f0f0f0; letter-spacing: 0.06em; margin-bottom: 0.25rem; }}
    .tagline {{ font-size: 0.82rem; color: #555; margin-bottom: 3rem; }}
    .section {{ margin-bottom: 1.8rem; }}
    .section-label {{
      font-size: 0.7rem; color: #555; text-transform: uppercase;
      letter-spacing: 0.2em; margin-bottom: 0.5rem;
    }}
    .section p {{ font-size: 0.9rem; color: #888; line-height: 1.75; }}
    hr {{ border: none; border-top: 1px solid #252525; margin: 2.2rem 0; }}
    .gate {{ text-align: center; }}
    input[type=password] {{
      display: block; width: 100%;
      background: #0d0d0d; border: 1px solid #2e2e2e; color: #c8c8c8;
      padding: 0.7rem 1rem; font-family: inherit; font-size: 0.9rem;
      margin-bottom: 0.75rem; outline: none;
    }}
    input[type=password]:focus {{ border-color: #484848; }}
    button {{
      display: block; width: 100%;
      background: #1a1a1a; border: 1px solid #383838; color: #ccc;
      padding: 0.7rem 2rem; font-family: inherit; font-size: 0.85rem;
      letter-spacing: 0.1em; cursor: pointer; text-transform: uppercase;
    }}
    button:hover {{ background: #222; border-color: #555; color: #eee; }}
    .enter-btn {{
      display: block; width: 100%;
      background: #1a1a1a; border: 1px solid #383838; color: #ccc;
      padding: 0.7rem 2rem; font-family: inherit; font-size: 0.85rem;
      letter-spacing: 0.1em; cursor: pointer; text-transform: uppercase;
      text-decoration: none; text-align: center;
    }}
    .enter-btn:hover {{ background: #222; border-color: #555; color: #eee; }}
    .error {{ color: #e05c5c; font-size: 0.82rem; margin-bottom: 0.75rem; }}
    .footer {{ margin-top: 2rem; font-size: 0.68rem; color: #333; text-align: center; }}
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
      Built and maintained by the GoodGuysFree community.
      Thanks to everyone who contributed to solving the ARG.<br>
      Thanks to Ekimo1920 of Voyagers Haven for his work to trigger this site's crawler when his bot finds changes.
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


_POST_URL_RE = re.compile(r"^/\d{4}/\d{2}/\d{2}/")
_PAGINATION_RE = re.compile(r"^/(?:page/\d+/)?$")


def _is_pagination_page(page_path: str) -> bool:
    """True for the WordPress blog index and its /page/N/ siblings."""
    return bool(_PAGINATION_RE.match(page_path))


def apply_diff_colors(html: str, date: str, changes: dict) -> str:
    """Add wb-diff-* classes to post links on index pages.

    Only touches <a> tags whose rewritten href matches /@date/YYYY/MM/DD/…
    (i.e. individual post links). All other links are left alone.
    Colors are on by default; adding class wb-diff-off to <html> hides them.
    Returns the original string unchanged if nothing was tagged.
    """
    added = set(changes.get("pages_added", []))
    modified = set(changes.get("pages_modified", []))
    prefix = f"/@{date}"

    soup = BeautifulSoup(html, "html.parser")
    tagged = False

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href.startswith(prefix):
            continue
        path = href[len(prefix):]
        if not _POST_URL_RE.match(path):
            continue
        if not path.endswith("/"):
            path += "/"
        if path in added:
            cls = "wb-diff-new"
        elif path in modified:
            cls = "wb-diff-changed"
        else:
            cls = "wb-diff-unchanged"
        existing = a.get("class") or []
        a["class"] = existing + [cls]
        tagged = True

    if not tagged:
        return html

    style_tag = soup.new_tag("style")
    style_tag.string = (
        "a.wb-diff-new{color:#4ade80!important}"
        "a.wb-diff-changed{color:#fbbf24!important}"
        "html.wb-diff-off a.wb-diff-new,"
        "html.wb-diff-off a.wb-diff-changed,"
        "html.wb-diff-off a.wb-diff-unchanged{color:inherit!important}"
    )
    head = soup.find("head")
    if head:
        head.append(style_tag)
    else:
        soup.insert(0, style_tag)

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

    def is_backfilled(self, date: str) -> bool:
        """True for snapshots synthesized from an external mirror (not crawled
        by us). They carry "backfilled": true and may not be fully accurate."""
        m = self._manifests.get(date)
        return bool(m and m.get("backfilled"))

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

_PAGE_STATUS_BADGES: dict[str, tuple[str, str]] = {
    "new":             ("NEW PAGE",         "#4ade80", "#0a1a0a", "#1a4a2a"),
    "changed":         ("CHANGED",          "#fbbf24", "#1a1200", "#4a3a0a"),
    "unchanged":       ("UNCHANGED",        "#3a3a3a", "transparent", "#2a2a2a"),
    "not_in_snapshot": ("NOT IN SNAPSHOT",  "#f87171", "#1a0a0a", "#4a1a1a"),
}


def build_header_html(date: str, original_url: str,
                      page_status: str | None = None,
                      show_diff_toggle: bool = False,
                      inbox_status: str | None = None,
                      is_backfilled: bool = False) -> str:
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

    status_html = ""
    if page_status and page_status in _PAGE_STATUS_BADGES:
        label, color, bg, border = _PAGE_STATUS_BADGES[page_status]
        status_html = (
            f'<span class="wb-tb-sep">·</span>'
            f'<span>Page status: <span class="wb-tb-status" '
            f'style="color:{color};background:{bg};border-color:{border}">'
            f'{label}</span></span>'
        )

    _INBOX_COLORS = {"new": "#4ade80", "changed": "#fbbf24", "unchanged": "#e0e0e0"}
    inbox_html = ""
    if inbox_status is not None:
        ic = _INBOX_COLORS.get(inbox_status, "#e0e0e0")
        inbox_html = (
            f'<a href="/@{date}/inbox/" class="wb-inbox-link" '
            f'style="color:{ic}" title="Inbox ({inbox_status})">ARCHITECT INBOX</a>'
        )

    # Notice for backfilled (externally-sourced) snapshots — white text.
    old_html = ""
    if is_backfilled:
        old_html = (
            '<span class="wb-tb-old" title="This snapshot was reconstructed from '
            'an external mirror and may not be fully accurate">'
            'Old snapshot — may not be accurate</span>'
        )

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
#wb-topbar .wb-tb-center {{ flex: 1; display: flex; align-items: center; justify-content: center; }}
#wb-topbar .wb-tb-sep {{ color: #2e2e2e; }}
#wb-topbar strong {{ color: #9ca3af; font-weight: normal; }}
#wb-topbar .wb-tb-ggf {{
  color: #333; font-size: 10px; letter-spacing: 0.15em;
  border-left: 1px solid #1e1e1e; padding-left: 10px;
}}
#wb-topbar .wb-tb-status {{
  font-size: 10px; padding: 1px 6px; border-radius: 3px;
  letter-spacing: 0.05em; border: 1px solid;
}}
{'#wb-topbar .wb-diff-toggle{background:none;border:1px solid #2a2a2a;color:#555;font-family:inherit;font-size:10px;letter-spacing:.08em;padding:1px 7px;cursor:pointer;border-radius:2px}#wb-topbar .wb-diff-toggle:hover{border-color:#444;color:#999}' if show_diff_toggle else ''}
#wb-topbar .wb-inbox-link {{ font-size: 10px; letter-spacing: 0.12em; text-decoration: none; }}
#wb-topbar .wb-inbox-link:hover {{ opacity: 0.75; }}
#wb-topbar .wb-tb-old {{
  color: #ffffff; font-size: 10px; letter-spacing: 0.08em;
  background: #3a2a00; border: 1px solid #6a5200; border-radius: 3px;
  padding: 1px 8px; margin-right: 10px;
}}
</style>
<div id="wb-topbar">
  <div class="wb-tb-left">
    <span>This is a mirror of <a href="{cfg.SITE_ORIGIN}" target="_blank" rel="noopener noreferrer">https://{live_domain}</a></span>
    <span class="wb-tb-sep">·</span>
    <span>Now viewing snapshot: <strong>{date_display}</strong></span>
    {status_html}
  </div>
  <div class="wb-tb-center">{old_html}{inbox_html}</div>
  <div class="wb-tb-right">
    {f'<button class="wb-diff-toggle" onclick="document.documentElement.classList.toggle(\'wb-diff-off\')" title="Toggle diff coloring on post links">diff</button>' if show_diff_toggle else ''}
    <a href="/~api/stats" style="color:#7dd3fc;text-decoration:none;font-size:0.85em;font-family:inherit;">SITE STATS</a>
    <span class="wb-tb-ggf">GGF</span>
  </div>
</div>
<!-- ═══ END WB HEADER ═══ -->"""


# Marker that identifies UNLOCKED protected-page content: the inner Jetpack
# contact form (the ARG form behind the password). Its presence means the blob
# holds the real content, not the WordPress password prompt.
INNER_FORM_MARKER = "jetpack-contact-form__form"

_FORM_BLOCK_SNIPPET = """\
<div id="wb-form-block" style="display:none;position:fixed;top:50%;left:50%;
transform:translate(-50%,-50%);background:#1a1a1a;border:1px solid #555;
color:#e0e0e0;font-family:'IBM Plex Mono','Courier New',monospace;
padding:1.5rem 2rem;z-index:2147483647;text-align:center;border-radius:4px;
box-shadow:0 4px 24px rgba(0,0,0,0.85);">
<p style="margin:0 0 1rem;font-size:0.95rem;letter-spacing:0.04em;">
Form disabled in archive</p>
<button onclick="document.getElementById('wb-form-block').style.display='none'"
style="background:#333;color:#e0e0e0;border:1px solid #666;padding:0.35rem 1.2rem;
font-family:inherit;cursor:pointer;border-radius:2px;font-size:0.85rem;">OK</button>
</div>
<script>
(function(){
  function block(e){
    e.preventDefault();
    e.stopPropagation();
    document.getElementById('wb-form-block').style.display='block';
  }
  document.addEventListener('DOMContentLoaded',function(){
    document.querySelectorAll(
      'form.jetpack-contact-form__form, form[id^="jp-form-"]'
    ).forEach(function(f){
      // The form already has onsubmit="return false" + AJAX, so intercept the
      // submit button click (capture phase) as well as the submit event.
      f.addEventListener('submit', block, true);
      f.querySelectorAll('button[type="submit"], input[type="submit"]')
       .forEach(function(b){ b.addEventListener('click', block, true); });
    });
  });
})();
</script>"""


# Every form opening tag. We never let an archived form reach the live site.
# (Our own injected forms — the synthetic gate — are added AFTER this runs, so
# they are never matched here.)
_FORM_TAG_RE = re.compile(r'<form\b[^>]*>', re.IGNORECASE)


def _neutralize_all_forms(html: str) -> str:
    """Make EVERY archived form structurally non-functional so none can submit to
    the live site — independent of JavaScript or which snapshot served it. Covers
    the WordPress password gate (-> wp-login.php), the inner ARG/Jetpack form, the
    search form, comment forms, etc.

    Rewrites each form's opening tag: action -> "#", method -> get, onsubmit ->
    return false, and strips the WordPress Interactivity directives
    (data-wp-on--submit / --reset) that would otherwise fire an AJAX request to
    the real endpoint. Appearance is unchanged. The 'Form disabled in archive'
    modal is layered on top of the inner ARG form for UX.
    """
    def fix(m: re.Match) -> str:
        tag = m.group(0)
        if re.search(r'\baction\s*=\s*"[^"]*"', tag, re.IGNORECASE):
            tag = re.sub(r'\baction\s*=\s*"[^"]*"', 'action="#"', tag,
                         flags=re.IGNORECASE)
        else:
            tag = tag[:-1] + ' action="#">'
        if re.search(r'\bmethod\s*=\s*"[^"]*"', tag, re.IGNORECASE):
            tag = re.sub(r'\bmethod\s*=\s*"[^"]*"', 'method="get"', tag,
                         flags=re.IGNORECASE)
        tag = re.sub(r'\sdata-wp-on--(?:submit|reset)\s*=\s*"[^"]*"', '', tag,
                     flags=re.IGNORECASE)
        if re.search(r'\bonsubmit\s*=', tag, re.IGNORECASE):
            tag = re.sub(r'\bonsubmit\s*=\s*"[^"]*"', 'onsubmit="return false;"',
                         tag, flags=re.IGNORECASE)
        else:
            tag = tag[:-1] + ' onsubmit="return false;">'
        return tag
    return _FORM_TAG_RE.sub(fix, html)


def _inject_form_block(html: str) -> str:
    """Inject a 'Form disabled in archive' notice that intercepts submission of
    the inner Jetpack contact form (the ARG form behind the password gate)."""
    if "</body>" in html:
        return html.replace("</body>", _FORM_BLOCK_SNIPPET + "\n</body>", 1)
    if "</html>" in html:
        return html.replace("</html>", _FORM_BLOCK_SNIPPET + "\n</html>", 1)
    return html + _FORM_BLOCK_SNIPPET


def _inject_password_gate(html: str, password: str) -> str:
    """Re-create the WordPress post-password gate client-side over unlocked
    content. A full-screen overlay (shown only with JS) asks for the password;
    the typed value's SHA-256 is compared to the embedded hash in-browser.

    The literal password is never sent to the client (only its hash), honoring
    the 'PAGE_PASSWORD must not appear in responses' rule. No-JS visitors see the
    content directly (the overlay stays hidden), keeping the archive readable
    without JavaScript. crypto.subtle requires a secure context (https or
    localhost) — production is behind TLS, so validation works there.
    """
    pw_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    snippet = """\
<div id="wb-pwgate" style="display:none;position:fixed;inset:0;
z-index:2147483646;background:#0d0d0d;color:#e0e0e0;
font-family:'IBM Plex Mono','Courier New',monospace;
flex-direction:column;align-items:center;justify-content:center;padding:2rem;">
<div style="max-width:440px;width:100%;text-align:center;">
<p style="margin:0 0 1.25rem;letter-spacing:0.05em;line-height:1.5;">
This content is password protected. To view it, enter the password below.</p>
<form id="wb-pwgate-form" autocomplete="off">
<input id="wb-pwgate-input" type="password" placeholder="Password"
style="background:#000;color:#e0e0e0;border:1px solid #555;padding:0.5rem 0.75rem;
font-family:inherit;font-size:0.95rem;width:60%;border-radius:2px;" autofocus>
<button type="submit" style="background:#333;color:#e0e0e0;border:1px solid #666;
padding:0.5rem 1.25rem;margin-left:0.5rem;font-family:inherit;cursor:pointer;
border-radius:2px;font-size:0.9rem;">Enter</button>
</form>
<p id="wb-pwgate-err" style="display:none;color:#f87171;margin-top:0.9rem;
font-size:0.85rem;">Incorrect password.</p>
</div></div>
<script>
(function(){
  var H="__HASH__";
  var gate=document.getElementById('wb-pwgate');
  // JS shows the gate; without JS the content is simply visible.
  gate.style.display='flex';
  document.documentElement.style.overflow='hidden';
  function hex(buf){return Array.from(new Uint8Array(buf))
    .map(function(b){return b.toString(16).padStart(2,'0');}).join('');}
  document.getElementById('wb-pwgate-form').addEventListener('submit',
    async function(e){
      e.preventDefault();
      var v=document.getElementById('wb-pwgate-input').value;
      try{
        var d=await crypto.subtle.digest('SHA-256',
          new TextEncoder().encode(v));
        if(hex(d)===H){
          gate.style.display='none';
          document.documentElement.style.overflow='';
          return;
        }
      }catch(err){}
      document.getElementById('wb-pwgate-err').style.display='block';
    });
})();
</script>""".replace("__HASH__", pw_hash)
    if "</body>" in html:
        return html.replace("</body>", snippet + "\n</body>", 1)
    if "</html>" in html:
        return html.replace("</html>", snippet + "\n</html>", 1)
    return html + snippet


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

    summary = changes.get("summary", "") if changes else "First snapshot"
    dates_json = json.dumps(dates)

    # Picker rows — newest first so current is near the top
    rows = []
    for d in reversed(dates):
        is_cur = d == current_date
        is_old = manifests.is_backfilled(d)
        cls = "wb-row"
        if is_cur:
            cls += " wb-row-current"
        if is_old:
            cls += " wb-row-old"          # rendered in italics (see CSS)
        now_badge = '<span class="wb-now">now</span>' if is_cur else ""
        c = manifests.get_changes(d)
        base_tip = c.get("summary", "") if c else "First snapshot"
        tooltip = (f"Old (backfilled) snapshot — may not be accurate. {base_tip}"
                   if is_old else base_tip)
        rows.append(f'<div class="{cls}" title="{tooltip}" onclick="wbNav(\'{d}\')">'
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
.wb-row-old {{ font-style: italic; color: #9a9a9a; }}
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
        <span>{get_display(current_date)}</span><span class="wb-caret">▾</span>
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
        href.startsWith('/~') ||
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
    manifests: ManifestCache = None        # set by main()
    gate_password: str = ""                # set by main()
    trigger_token: str = ""                # set by main()
    scheduler: CrawlScheduler = None       # set by main()
    access_log: "AccessLog | None" = None  # set by main()

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

        # ── Root → always show landing page ──
        if path == "/" or path == "":
            latest = self.manifests.latest_date or ""
            if self._is_authenticated():
                if not latest:
                    self._error(503, "No snapshots available. Run site_crawler.py first.")
                    return
                body = _build_landing_page(authenticated=True, latest_date=latest)
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

        # ── Static UI files (bg images etc.) — always open, no auth ──
        if path.startswith("/_static/"):
            self._serve_static(path)
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
            self._serve_page_missing(date, page_path)
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

        # Compute page status from changes for the header badge.
        changes = self.manifests.get_changes(date)
        if changes:
            if page_path in changes.get("pages_added", []):
                page_status = "new"
            elif page_path in changes.get("pages_modified", []):
                page_status = "changed"
            else:
                page_status = "unchanged"
        else:
            page_status = None

        # Inbox link status for header.
        manifest_pages = manifest.get("pages", {})
        if "/inbox/" in manifest_pages:
            if changes:
                if "/inbox/" in changes.get("pages_added", []):
                    inbox_status = "new"
                elif "/inbox/" in changes.get("pages_modified", []):
                    inbox_status = "changed"
                else:
                    inbox_status = "unchanged"
            else:
                inbox_status = "unchanged"
        else:
            inbox_status = None

        # On index/pagination pages, colour post links by diff status.
        is_pagination = _is_pagination_page(page_path)
        if is_pagination and changes:
            html = apply_diff_colors(html, date, changes)

        # Inject top header bar after <body>
        original_url = page_data.get("original_url", cfg.SITE_ORIGIN + page_path)
        header = build_header_html(date, original_url, page_status,
                                   show_diff_toggle=is_pagination,
                                   inbox_status=inbox_status,
                                   is_backfilled=self.manifests.is_backfilled(date))
        body_tag = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_tag:
            end = body_tag.end()
            html = html[:end] + "\n" + header + html[end:]
        else:
            html = header + html

        # Neutralize EVERY archived form so none can submit to the live site —
        # the password gate (-> wp-login.php), the inner ARG form, search, etc.
        # Runs before any injection below, so our own synthetic forms are safe.
        if "<form" in html.lower():
            html = _neutralize_all_forms(html)

        # Add the 'Form disabled in archive' modal over the inner ARG form (UX on
        # top of the structural neutralization above).
        if INNER_FORM_MARKER in html:
            html = _inject_form_block(html)

        # Protected pages: re-create the password gate client-side over the
        # unlocked content. "Unlocked" = the blob no longer carries the WordPress
        # post_password prompt (some protected pages, e.g. Brussels, have no inner
        # form, so presence of the WP prompt — not the inner form — is the signal).
        # Older snapshots that stored the raw prompt are served as-is.
        known_pw = cfg.PROTECTED_PAGES.get(page_path)
        if known_pw and 'name="post_password"' not in html:
            html = _inject_password_gate(html, known_pw)

        # Inject navigation overlay before </body>
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

    def _serve_page_missing(self, date: str, page_path: str):
        """Friendly 404 page when a path doesn't exist in the requested snapshot."""
        front_url = f"/@{date}/"
        header = build_header_html(date, cfg.SITE_ORIGIN + page_path, "not_in_snapshot")
        changes = self.manifests.get_changes(date)
        overlay = build_overlay_html(date, self.manifests, changes, page_path)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Page not in snapshot</title>
<style>
body {{ background: #0d0d0d; color: #e0e0e0;
       font-family: 'IBM Plex Mono', 'Courier New', monospace;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; padding-top: 26px; box-sizing: border-box; }}
.box {{ text-align: center; max-width: 480px; padding: 40px 20px; }}
h1 {{ font-size: 20px; color: #9ca3af; font-weight: normal; margin: 0 0 20px;
      letter-spacing: 0.1em; }}
p {{ color: #555; font-size: 14px; line-height: 1.9; margin: 0 0 16px; }}
a {{ color: #7dd3fc; text-decoration: none; }}
a:hover {{ color: #bae6fd; }}
.path {{ color: #2e2e2e; font-size: 11px; margin-top: 24px; word-break: break-all; }}
</style>
</head><body>
{header}
<div class="box">
  <h1>PAGE NOT IN THIS SNAPSHOT</h1>
  <p>This page didn't exist when this snapshot was taken.<br>
     Use the date picker to find a snapshot that includes it,<br>
     or <a href="{front_url}">go to the front page of this snapshot</a>.</p>
  <p class="path">{page_path}</p>
</div>
{overlay}
</body></html>"""
        self._respond(404, body.encode("utf-8"), "text/html; charset=utf-8")

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

    def _serve_static(self, path: str):
        # path: /_static/<filename> — serves from static/ dir next to this script
        filename = path[len("/_static/"):]
        # Guard against path traversal
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            self._error(404, "Not found")
            return
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        fs_path = os.path.join(static_dir, filename)
        if not os.path.isfile(fs_path):
            self._error(404, f"Static file not found: {filename}")
            return
        _, ext = os.path.splitext(fs_path)
        content_type = cfg.MIME_TYPES.get(ext.lower(), "application/octet-stream")
        with open(fs_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
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

        elif path == "/~api/stats":
            stats = (self.__class__.access_log.stats()
                     if self.__class__.access_log is not None
                     else _compute_stats_empty())
            html = _build_stats_html(stats)
            self._respond(200, html.encode("utf-8"), "text/html; charset=utf-8")

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
        # Log after sending — no latency impact
        if self.__class__.access_log is not None:
            path = self.path.split("?")[0]
            self.__class__.access_log.record(
                ip=self.client_address[0],
                path=path,
                status=code,
                bytes_sent=len(body),
                ua=self.headers.get("User-Agent"),
                referer=self.headers.get("Referer"),
            )


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

    # Initialize access log
    db_path = os.path.join(cfg.WORKSPACE_DIR, "stats.db")
    WaybackHandler.access_log = AccessLog(db_path)
    print(f"  Stats:     {db_path}")

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
