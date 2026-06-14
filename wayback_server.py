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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse
from pathlib import Path
from datetime import datetime

from bs4 import BeautifulSoup

import crawler_config as cfg

sys.stdout.reconfigure(encoding="utf-8")

INTERNAL_HOSTS = {cfg.SITE_DOMAIN, f"www.{cfg.SITE_DOMAIN}"}
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

    # Change indicator
    change_class = ""
    change_tooltip = ""
    if changes:
        added = changes.get("pages_added", [])
        modified = changes.get("pages_modified", [])
        if current_path in added:
            change_class = "wb-new"
            change_tooltip = "New page on this date"
        elif current_path in modified:
            change_class = "wb-modified"
            change_tooltip = "Modified on this date"

    dates_json = json.dumps(dates)
    summary = changes.get("summary", "") if changes else "First snapshot"

    return f"""
<!-- ═══ WAYBACK MACHINE OVERLAY ═══ -->
<div id="wb-overlay" class="wb-collapsed">
<style>
#wb-overlay {{
  position: fixed; top: 12px; right: 12px; z-index: 999999;
  font-family: 'IBM Plex Mono', 'Courier New', monospace;
  font-size: 12px; line-height: 1.4;
  color: #e0e0e0; background: rgba(10, 10, 10, 0.92);
  border: 1px solid #333; border-radius: 6px;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  padding: 10px 14px;
  max-width: 360px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.6);
  transition: all 0.25s ease;
  user-select: none;
}}
#wb-overlay.wb-collapsed {{ padding: 6px 10px; max-width: 240px; }}
#wb-overlay.wb-collapsed .wb-body {{ display: none; }}
#wb-overlay .wb-header {{
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  white-space: nowrap;
}}
#wb-overlay .wb-header:hover {{ color: #fff; }}
#wb-overlay .wb-logo {{
  width: 14px; height: 14px; opacity: 0.7;
  border: 1px solid #555; border-radius: 2px;
  display: flex; align-items: center; justify-content: center;
  font-size: 9px; flex-shrink: 0;
}}
#wb-overlay .wb-date-display {{ font-weight: 600; letter-spacing: 0.5px; }}
#wb-overlay .wb-indicator {{
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}}
#wb-overlay .wb-indicator.wb-new {{ background: #4ade80; box-shadow: 0 0 6px #4ade80; }}
#wb-overlay .wb-indicator.wb-modified {{ background: #fbbf24; box-shadow: 0 0 6px #fbbf24; }}
#wb-overlay .wb-body {{ margin-top: 10px; }}
#wb-overlay .wb-nav {{
  display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
}}
#wb-overlay .wb-btn {{
  background: #222; border: 1px solid #444; color: #ccc;
  padding: 3px 8px; border-radius: 3px; cursor: pointer;
  font-family: inherit; font-size: 11px;
  transition: all 0.15s;
}}
#wb-overlay .wb-btn:hover {{ background: #333; color: #fff; border-color: #666; }}
#wb-overlay .wb-btn:disabled {{ opacity: 0.3; cursor: default; }}
#wb-overlay .wb-select {{
  background: #1a1a1a; border: 1px solid #444; color: #e0e0e0;
  padding: 3px 6px; border-radius: 3px;
  font-family: inherit; font-size: 11px;
  flex: 1; max-width: 180px;
}}
#wb-overlay .wb-summary {{
  color: #888; font-size: 10px; border-top: 1px solid #2a2a2a;
  padding-top: 6px; margin-top: 4px;
}}
#wb-overlay .wb-summary a {{ color: #7dd3fc; text-decoration: none; }}
#wb-overlay .wb-summary a:hover {{ text-decoration: underline; }}
</style>
<div class="wb-header" onclick="document.getElementById('wb-overlay').classList.toggle('wb-collapsed')">
  <span class="wb-logo">⟲</span>
  <span class="wb-date-display">{get_display(current_date)}</span>
  <span class="wb-indicator {change_class}" title="{change_tooltip}"></span>
</div>
<div class="wb-body">
  <div class="wb-nav">
    <button class="wb-btn" id="wb-prev" {"disabled" if not prev_date else ""} onclick="wbNav('{prev_date}')">◀</button>
    <select class="wb-select" id="wb-date-select" onchange="wbNav(this.value)">
      {"".join(f'<option value="{d}" {"selected" if d == current_date else ""}>{get_display(d)}</option>' for d in dates)}
    </select>
    <button class="wb-btn" id="wb-next" {"disabled" if not next_date else ""} onclick="wbNav('{next_date}')">▶</button>
  </div>
  <div class="wb-summary">{summary}</div>
</div>
<script>
(function() {{
  var currentDate = "{current_date}";
  var dates = {dates_json};
  var currentPath = "{current_path}";

  window.wbNav = function(date) {{
    if (date) window.location.href = "/@" + date + currentPath;
  }};

  // Intercept internal page links — maintain date context
  document.addEventListener("click", function(e) {{
    var link = e.target.closest("a");
    if (!link) return;
    var href = link.getAttribute("href");
    if (!href) return;

    // Skip already-rewritten links, blob links, external links, anchors
    if (href.startsWith("/@") || href.startsWith("/_assets") ||
        href.startsWith("#") || href.startsWith("mailto:") ||
        href.startsWith("javascript:")) return;

    // Skip external URLs
    if (href.match(/^https?:\\/\\//)) {{
      var url = new URL(href);
      if (url.hostname !== "{cfg.SITE_DOMAIN}" &&
          url.hostname !== "www.{cfg.SITE_DOMAIN}") return;
      // It's an internal absolute URL — extract the path
      href = url.pathname;
    }}

    // Internal link — prepend date
    if (href.startsWith("/")) {{
      e.preventDefault();
      window.location.href = "/@" + currentDate + href;
    }}
  }});
}})();
</script>
</div>
<!-- ═══ END WAYBACK OVERLAY ═══ -->
"""


# ─── Request Handler ─────────────────────────────────────────────────────────

class WaybackHandler(BaseHTTPRequestHandler):
    manifests: ManifestCache = None  # set by main()

    def log_message(self, format, *args):
        # Quieter logging
        sys.stdout.write(f"  {args[0]}\n")

    def do_GET(self):
        path = unquote(self.path)

        # Keep in sync with the crawler — pick up newly-added snapshots/pages.
        self.manifests.maybe_reload()

        # ── Root → redirect to latest date ──
        if path == "/" or path == "":
            latest = self.manifests.latest_date
            if latest:
                self._redirect(f"/@{latest}/")
            else:
                self._error(503, "No snapshots available. Run site_crawler.py first.")
            return

        # ── API endpoints ──
        if path.startswith("/~api/"):
            self._handle_api(path)
            return

        # ── Blob/asset serving ──
        if path.startswith("/_assets/"):
            self._serve_asset(path)
            return

        # ── Date-routed page serving ──
        match = re.match(r'^/@(\d{4}-\d{2}-\d{2})(/.*)$', path)
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
        self.wfile.write(body)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Project Skyscraper Wayback Server")
    parser.add_argument("--port", type=int, default=cfg.SERVER_PORT)
    parser.add_argument("--host", default=cfg.SERVER_HOST)
    args = parser.parse_args()

    print(f"═══ Project Skyscraper Wayback Machine ═══")
    print()

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
