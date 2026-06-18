"""
Project Skyscraper — Full-fidelity site crawler.

Downloads complete HTML pages + all embedded assets, rewrites URLs to local
content-addressed paths, sanitizes tracking/analytics, and produces daily
snapshot manifests.

Asset capture strategy
----------------------
The site's WordPress/Jetpack hosting aggressively rate-limits (HTTP 429) direct
asset requests that bypass the browser session. So instead of downloading assets
with a separate HTTP client, we attach a Playwright ``response`` listener and
capture every asset *as the real browser loads it* — this reuses the browser's
session/cookies and the natural request pacing, which the server does not
throttle. A small fallback fetcher (using Playwright's own request context, which
shares the browser session) handles the few referenced assets the browser never
loads (e.g. unused ``srcset`` variants or sitemap-only images), with exponential
backoff on 429.

Usage:
    uv run site_crawler.py              # crawl today
    uv run site_crawler.py --backfill   # crawl once as a best-effort backfill
"""

import os
import sys
import re
import json
import time
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import crawler_config as cfg

# Ensure UTF-8 output on Windows
sys.stdout.reconfigure(encoding="utf-8")


# ─── Blob Store ──────────────────────────────────────────────────────────────

class BlobStore:
    """Content-addressed file storage.  Files are stored by SHA-256 hash."""

    def __init__(self, root_dir: str):
        self.root = root_dir
        os.makedirs(root_dir, exist_ok=True)

    def _blob_path(self, sha: str, ext: str) -> str:
        prefix = sha[:2]
        d = os.path.join(self.root, prefix)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sha}{ext}")

    def has(self, sha: str, ext: str) -> bool:
        return os.path.exists(self._blob_path(sha, ext))

    def put_bytes(self, data: bytes, ext: str) -> str:
        """Store bytes, return the SHA-256 hex digest."""
        sha = hashlib.sha256(data).hexdigest()
        path = self._blob_path(sha, ext)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return sha

    def put_text(self, text: str, ext: str) -> str:
        return self.put_bytes(text.encode("utf-8"), ext)

    def url_for(self, sha: str, ext: str) -> str:
        """Return the URL path used in rewritten HTML."""
        return f"{cfg.ASSET_URL_PREFIX}/{sha[:2]}/{sha}{ext}"

    def fs_path(self, sha: str, ext: str) -> str:
        return self._blob_path(sha, ext)


# ─── Utility ─────────────────────────────────────────────────────────────────

def ext_from_url(url: str) -> str:
    """Guess file extension from a URL."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext in (".jpeg",):
        ext = ".jpg"
    return ext if ext else ".bin"


def ext_from_content_type(ct: str) -> str:
    """Guess extension from Content-Type header."""
    ct = (ct or "").split(";")[0].strip().lower()
    mapping = {v.split(";")[0].strip(): k for k, v in cfg.MIME_TYPES.items()}
    return mapping.get(ct, ".bin")


def is_internal_page_url(url: str) -> bool:
    """Is this URL an internal page (not an asset) on the target site?"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host in (cfg.SITE_DOMAIN, f"www.{cfg.SITE_DOMAIN}", "")


def is_downloadable_domain(url: str) -> bool:
    """Should we download assets from this domain?"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in cfg.BLOCKED_DOMAINS:
        return False
    return host in cfg.ASSET_DOMAINS or host == ""


def normalize_url(url: str, base_url: str) -> str:
    """Resolve a potentially relative URL against a base, remove fragments."""
    full = urljoin(base_url, url)
    parsed = urlparse(full)
    # Remove fragment
    return urlunparse(parsed._replace(fragment=""))


CSS_URL_RE = re.compile(
    r"""url\(\s*(['"]?)(.+?)\1\s*\)""", re.IGNORECASE
)


# ─── Sitemap Parsing ─────────────────────────────────────────────────────────

def fetch_sitemap(url: str, req_context=None) -> bytes | None:
    """Fetch sitemap XML content (plain requests or Playwright context)."""
    if req_context is not None:
        try:
            resp = req_context.get(url, timeout=20000)
            if resp.status == 200:
                return resp.body()
            print(f"  Warning: sitemap {url} via Playwright returned {resp.status}")
        except Exception as e:
            print(f"  Warning: failed to fetch sitemap {url} via Playwright: {e}")
    try:
        r = requests.get(url, headers={"User-Agent": cfg.USER_AGENT}, timeout=15)
        if r.status_code == 200:
            return r.content
        print(f"  Warning: sitemap {url} returned {r.status_code}")
    except Exception as e:
        print(f"  Warning: failed to fetch sitemap {url}: {e}")
    return None


def parse_sitemap(xml_bytes: bytes, req_context=None) -> tuple[list[dict], list[str]]:
    """Parse a sitemap or sitemap index.

    Returns (page_entries, image_urls) where each page_entry is
    {"url": ..., "lastmod": ...}.
    """
    pages = []
    images = []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  Warning: XML parse error: {e}")
        return pages, images

    # Strip namespaces
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    if root.tag == "sitemapindex":
        for sitemap_elem in root.findall(".//sitemap/loc"):
            child_url = sitemap_elem.text.strip()
            child_bytes = fetch_sitemap(child_url, req_context)
            if child_bytes:
                child_pages, child_images = parse_sitemap(child_bytes, req_context)
                pages.extend(child_pages)
                images.extend(child_images)
    elif root.tag == "urlset":
        for url_elem in root.findall(".//url"):
            loc = url_elem.find("loc")
            if loc is None or not loc.text:
                continue
            page_url = loc.text.strip()
            lastmod_elem = url_elem.find("lastmod")
            lastmod = lastmod_elem.text.strip() if lastmod_elem is not None else None

            # Skip direct media file URLs
            lower = page_url.lower()
            if any(lower.endswith(e) for e in (".jpg", ".png", ".jpeg", ".gif", ".pdf")):
                images.append(page_url)
                continue

            pages.append({"url": page_url, "lastmod": lastmod})

            # Collect <image:image><image:loc> inside this <url> (namespaces stripped above)
            for img_loc in url_elem.findall(".//image/loc"):
                if img_loc.text:
                    images.append(img_loc.text.strip())

    return pages, images


# ─── Asset Extraction ────────────────────────────────────────────────────────

def extract_asset_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Extract all asset URLs referenced in an HTML document."""
    urls = []

    # <img src>
    for tag in soup.find_all("img"):
        src = tag.get("src")
        if src and not src.startswith("data:"):
            urls.append(normalize_url(src, page_url))
        # Lazy-load attributes used by Jetpack / lazy loaders
        for attr in ("data-src", "data-orig-src", "data-lazy-src"):
            val = tag.get(attr)
            if val and not val.startswith("data:"):
                urls.append(normalize_url(val, page_url))
        # srcset (and lazy variants)
        for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
            srcset = tag.get(attr)
            if srcset:
                for part in srcset.split(","):
                    toks = part.strip().split()
                    if toks and not toks[0].startswith("data:"):
                        urls.append(normalize_url(toks[0], page_url))

    # <link rel="stylesheet">
    for tag in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        href = tag.get("href")
        if href:
            urls.append(normalize_url(href, page_url))

    # <link rel="icon"> / <link rel="shortcut icon"> / <link rel="apple-touch-icon">
    for tag in soup.find_all("link", rel=lambda r: r and any(
        x in r for x in ("icon", "apple-touch-icon", "manifest")
    )):
        href = tag.get("href")
        if href:
            urls.append(normalize_url(href, page_url))

    # <script src>
    for tag in soup.find_all("script", src=True):
        src = tag["src"]
        urls.append(normalize_url(src, page_url))

    # <video>/<audio> <source src> and poster
    for tag in soup.find_all("source"):
        src = tag.get("src")
        if src:
            urls.append(normalize_url(src, page_url))
    for tag in soup.find_all("video"):
        poster = tag.get("poster")
        if poster:
            urls.append(normalize_url(poster, page_url))

    # Inline <style> blocks → url() references
    for tag in soup.find_all("style"):
        if tag.string:
            for match in CSS_URL_RE.finditer(tag.string):
                raw = match.group(2)
                if not raw.startswith("data:") and not raw.startswith("#"):
                    urls.append(normalize_url(raw, page_url))

    # <a href> pointing directly to a downloadable media file (e.g. a text
    # link to an uploaded .jpg/.pdf). These aren't <img>/<link> assets, but we
    # still want them available offline.
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if not href or href.startswith(("data:", "#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = normalize_url(href, page_url)
        if ASSET_EXT_RE.search(urlparse(absolute).path) and is_downloadable_domain(absolute):
            urls.append(absolute)

    # OG image, etc.
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"og:image")}):
        content = tag.get("content")
        if content:
            urls.append(normalize_url(content, page_url))

    # Inline style attributes with url()
    for tag in soup.find_all(style=True):
        style_val = tag["style"]
        for match in CSS_URL_RE.finditer(style_val):
            raw = match.group(2)
            if not raw.startswith("data:") and not raw.startswith("#"):
                urls.append(normalize_url(raw, page_url))

    # De-duplicate while preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# File extensions that indicate an asset/download, not a navigable page.
ASSET_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|css|js|json|xml|rss|txt|csv|"
    r"pdf|zip|gz|tar|rar|7z|mp[34]|m4a|webm|mov|avi|wav|ogg|"
    r"woff2?|ttf|otf|eot|doc[xm]?|xls[xm]?|ppt[xm]?)$",
    re.IGNORECASE,
)
# Path prefixes that are WordPress internals / non-content.
EXCLUDED_PATH_PREFIXES = (
    "/wp-admin", "/wp-content", "/wp-includes", "/wp-json",
)
EXCLUDED_EXACT_PATHS = {"/wp-login.php", "/xmlrpc.php"}


def extract_internal_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Find every internal, navigable link back to project-skyscraper.com.

    The sitemap only lists a subset of pages, so we follow all on-site links
    (pagination, category/tag/author/date archives, in-post links, etc.) to
    achieve full coverage. Assets, WordPress internals, feeds, and query-string
    duplicates are filtered out; paths are normalized (trailing slash, no query
    or fragment) so each page is crawled at most once.
    """
    found = []
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        parsed = urlparse(normalize_url(href, page_url))
        if parsed.hostname not in (cfg.SITE_DOMAIN, f"www.{cfg.SITE_DOMAIN}"):
            continue

        path = parsed.path or "/"
        if ASSET_EXT_RE.search(path):
            continue
        if path in EXCLUDED_EXACT_PATHS:
            continue
        if any(path.startswith(p) for p in EXCLUDED_PATH_PREFIXES):
            continue
        if path.rstrip("/").endswith("/feed"):
            continue

        # Normalize: trailing slash, drop query + fragment (path-based permalinks)
        if not path.endswith("/"):
            path += "/"
        clean = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        found.append(clean)

    return list(dict.fromkeys(found))


def extract_css_asset_urls(css_text: str, css_url: str) -> list[str]:
    """Extract url() references from CSS content."""
    urls = []
    for match in CSS_URL_RE.finditer(css_text):
        raw = match.group(2)
        if not raw.startswith("data:") and not raw.startswith("#"):
            urls.append(normalize_url(raw, css_url))
    return urls


# ─── HTML Sanitization ───────────────────────────────────────────────────────

def sanitize_html(soup: BeautifulSoup) -> None:
    """Remove tracking, analytics, and dangerous elements in-place."""

    # Remove elements by selector
    for selector in cfg.ELEMENT_REMOVE_SELECTORS:
        try:
            # BeautifulSoup doesn't support :contains, handle it specially
            if ":contains(" in selector:
                # e.g. style:contains("img#wpstats")
                tag_name = selector.split(":")[0]
                search_text = selector.split('"')[1]
                for tag in soup.find_all(tag_name):
                    if tag.string and search_text in tag.string:
                        tag.decompose()
            else:
                for tag in soup.select(selector):
                    tag.decompose()
        except Exception:
            pass  # selector might not match, that's fine

    # Remove <script> blocks matching block patterns
    for script in soup.find_all("script"):
        src = script.get("src", "")
        content = script.string or ""
        for pattern in cfg.SCRIPT_BLOCK_PATTERNS:
            if re.search(pattern, src, re.IGNORECASE) or re.search(pattern, content, re.IGNORECASE):
                script.decompose()
                break

    # Neutralize POST forms (they have nowhere to submit in the archive) without
    # altering the page's appearance — no visible marker.
    for form in soup.find_all("form"):
        method = (form.get("method") or "GET").upper()
        if method == "POST":
            # Don't disable the password form — we already submitted it.
            # If it's still here, it means we failed to enter the password.
            if form.find("input", {"name": "post_password"}):
                continue
            form["method"] = "GET"
            form["onsubmit"] = "return false;"

    # Remove wp-json / API links
    for link in soup.find_all("link", rel="https://api.w.org/"):
        link.decompose()

    # Remove comments containing analytics hints
    from bs4 import Comment
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        text = str(comment).lower()
        if any(w in text for w in ("analytics", "wp statistics", "jetpack open graph")):
            comment.extract()


# ─── URL Rewriting ───────────────────────────────────────────────────────────

def rewrite_html_assets(soup: BeautifulSoup, asset_map: dict, page_url: str) -> None:
    """Rewrite asset URLs in the HTML to local blob paths.

    asset_map: {original_absolute_url: "/_assets/ab/abcdef...ext"}
    """

    def _rewrite(url_str: str) -> str | None:
        if not url_str or url_str.startswith("data:") or url_str.startswith("#"):
            return None
        absolute = normalize_url(url_str, page_url)
        return asset_map.get(absolute)

    # <img src> and srcset (+ lazy variants → collapse onto src/srcset)
    for tag in soup.find_all("img"):
        # Promote lazy-load source onto the real src so it shows offline
        for attr in ("data-src", "data-orig-src", "data-lazy-src"):
            if tag.get(attr):
                new = _rewrite(tag[attr])
                if new:
                    tag["src"] = new
                del tag[attr]
        src = tag.get("src")
        new_src = _rewrite(src)
        if new_src:
            tag["src"] = new_src

        def _rewrite_srcset(value):
            parts = []
            for part in value.split(","):
                tokens = part.strip().split()
                if tokens:
                    new_url = _rewrite(tokens[0])
                    if new_url:
                        tokens[0] = new_url
                    parts.append(" ".join(tokens))
            return ", ".join(parts)

        for attr in ("data-srcset", "data-lazy-srcset"):
            if tag.get(attr):
                tag["srcset"] = _rewrite_srcset(tag[attr])
                del tag[attr]
        if tag.get("srcset"):
            tag["srcset"] = _rewrite_srcset(tag["srcset"])

    # <link href> (stylesheets, icons, etc.)
    for tag in soup.find_all("link", href=True):
        new_href = _rewrite(tag["href"])
        if new_href:
            tag["href"] = new_href

    # <script src>
    for tag in soup.find_all("script", src=True):
        new_src = _rewrite(tag["src"])
        if new_src:
            tag["src"] = new_src

    # <a href> to a downloaded media file → local blob path (page links, whose
    # URL isn't in the asset map, are left untouched for the server to route).
    for tag in soup.find_all("a", href=True):
        new_href = _rewrite(tag["href"])
        if new_href:
            tag["href"] = new_href

    # <source src> and <video poster>
    for tag in soup.find_all("source", src=True):
        new_src = _rewrite(tag["src"])
        if new_src:
            tag["src"] = new_src
    for tag in soup.find_all("video", attrs={"poster": True}):
        new_poster = _rewrite(tag["poster"])
        if new_poster:
            tag["poster"] = new_poster

    # <meta property="og:image">
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"og:image")}):
        content = tag.get("content")
        new_content = _rewrite(content)
        if new_content:
            tag["content"] = new_content

    # Inline <style> url() references
    for tag in soup.find_all("style"):
        if tag.string:
            new_css = rewrite_css_assets(tag.string, asset_map, page_url)
            if new_css != tag.string:
                tag.string = new_css

    # Inline style attributes with url()
    for tag in soup.find_all(style=True):
        style_val = tag["style"]
        new_style = rewrite_css_assets(style_val, asset_map, page_url)
        if new_style != style_val:
            tag["style"] = new_style


def rewrite_css_assets(css_text: str, asset_map: dict, css_url: str) -> str:
    """Rewrite url() references in CSS content."""
    def replacer(m):
        raw = m.group(2)
        if raw.startswith("data:") or raw.startswith("#"):
            return m.group(0)
        absolute = normalize_url(raw, css_url)
        if absolute in asset_map:
            return f"url('{asset_map[absolute]}')"
        return m.group(0)

    return CSS_URL_RE.sub(replacer, css_text)


_CANONICAL_IGNORE_RES = [re.compile(p) for p in cfg.CANONICAL_IGNORE_PATTERNS]
_CANONICAL_REPLACE_RES = [(re.compile(p, re.DOTALL), r)
                          for p, r in cfg.CANONICAL_REPLACE_PATTERNS]


def canonical_hash(html: str) -> str:
    """SHA-256 of html after stripping/normalizing known dynamic content."""
    normalized = html
    for pat in _CANONICAL_IGNORE_RES:
        normalized = pat.sub("", normalized)
    for pat, repl in _CANONICAL_REPLACE_RES:
        normalized = pat.sub(repl, normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ─── Main Crawler ────────────────────────────────────────────────────────────

class SiteCrawler:
    def __init__(self):
        self.blobs = BlobStore(cfg.BLOB_DIR)
        self.asset_map: dict[str, str] = {}    # original_url → local_path
        self.asset_meta: dict[str, dict] = {}  # local_path → metadata
        self.pending_css: dict[str, str] = {}  # css_url → raw text (awaiting recursion)
        self.req_context = None                # Playwright APIRequestContext (set in crawl)
        self._fallback_fetches = 0

    # ── Blob recording ───────────────────────────────────────────────────

    def _record_blob(self, url: str, body: bytes, ct: str) -> tuple[str, str]:
        """Store bytes in the blob store and register in asset_map/meta.

        Returns (local_url, ext).
        """
        ext = ext_from_url(url)
        if ext == ".bin":
            ext = ext_from_content_type(ct)
        sha = self.blobs.put_bytes(body, ext)
        local_url = self.blobs.url_for(sha, ext)
        self.asset_map[url] = local_url
        self.asset_meta[local_url] = {
            "original_url": url,
            "content_type": (ct or "").split(";")[0].strip(),
            "size": len(body),
            "sha256": sha,
        }
        return local_url, ext

    # ── Playwright response capture ──────────────────────────────────────

    def _on_response(self, response):
        """Capture every asset the browser loads (reuses browser session)."""
        try:
            url = response.url
            if url in self.asset_map or not is_downloadable_domain(url):
                return
            if response.status != 200:
                return
            ct = (response.headers or {}).get("content-type", "")
            if "text/html" in ct:
                return  # page documents are handled separately
            body = response.body()
        except Exception:
            return

        # Strip fragment for consistent keying with HTML references
        key = urlunparse(urlparse(url)._replace(fragment=""))
        if key in self.asset_map:
            return
        _, ext = self._record_blob(key, body, ct)
        if ext == ".css" or "text/css" in ct:
            self.pending_css[key] = body.decode("utf-8", errors="replace")

    # ── Fallback fetch (browser request context, with 429 backoff) ───────

    def _fetch_with_backoff(self, url: str, max_retries: int = 4):
        """Fetch an asset the browser didn't load, via the browser's request
        context (shares session/cookies). Backs off on HTTP 429."""
        if self.req_context is None:
            return None, None
        delay = 2.0
        for attempt in range(max_retries):
            try:
                resp = self.req_context.get(url, timeout=20000)
                status = resp.status
                if status == 200:
                    body = resp.body()
                    ct = (resp.headers or {}).get("content-type", "")
                    self._fallback_fetches += 1
                    return body, ct
                if status == 429:
                    print(f"    ⏳ 429 — backing off {delay:.0f}s: {url[:80]}")
                    time.sleep(delay)
                    delay *= 2
                    continue
                print(f"    ⚠ Asset {status}: {url[:90]}")
                return None, None
            except Exception as e:
                print(f"    ⚠ fetch error {url[:70]} — {e}")
                time.sleep(delay)
                delay *= 2
        return None, None

    # ── Asset acquisition (referenced assets / sitemap images) ────────────

    def download_asset(self, url: str) -> str | None:
        """Ensure an asset is in the blob store; return its local URL path.

        Prefers the browser-captured copy; falls back to a session-shared fetch.
        Returns None if the asset can't/shouldn't be downloaded.
        """
        if url in self.asset_map:
            return self.asset_map[url]
        if not is_downloadable_domain(url):
            return None

        body, ct = self._fetch_with_backoff(url)
        if body is None:
            return None

        local_url, ext = self._record_blob(url, body, ct)
        if ext == ".css" or "text/css" in (ct or ""):
            self._process_css(body.decode("utf-8", errors="replace"), url)
        return local_url

    def _process_css(self, css_text: str, css_url: str):
        """Download assets referenced in CSS and rewrite the CSS to local paths."""
        for nested_url in extract_css_asset_urls(css_text, css_url):
            if nested_url not in self.asset_map:
                self.download_asset(nested_url)

        # Rewrite the CSS content with local URLs and re-store
        rewritten = rewrite_css_assets(css_text, self.asset_map, css_url)
        if rewritten != css_text:
            sha = self.blobs.put_text(rewritten, ".css")
            new_local = self.blobs.url_for(sha, ".css")
            # The original URL now points to the rewritten version
            self.asset_map[css_url] = new_local
            self.asset_meta[new_local] = {
                "original_url": css_url,
                "content_type": "text/css",
                "size": len(rewritten.encode("utf-8")),
                "sha256": sha,
            }

    # ── Page Crawl ───────────────────────────────────────────────────────

    def crawl_page(self, pw_page, url: str) -> tuple[dict | None, list[str]]:
        """Crawl a single page.

        Returns (page_manifest_entry_or_None, discovered_pagination_urls).
        """
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        if not path.endswith("/"):
            path += "/"

        print(f"  📄 {path}")

        try:
            # Clear cookies before every page so a wp-postpass cookie set while
            # unlocking one protected page can't silently unlock a different
            # same-password page later. Each page is captured in a clean state;
            # only pages we explicitly unlock below get unlocked.
            try:
                pw_page.context.clear_cookies()
            except Exception:
                pass

            pw_page.goto(url, wait_until="load", timeout=cfg.PAGE_LOAD_TIMEOUT_MS)
            pw_page.wait_for_timeout(800)

            # Password-protected pages: submit the known password ONLY for pages
            # we deliberately unlock (cfg.PROTECTED_PAGES), so the archive stores
            # the real content. The server re-gates them client-side. Pages not in
            # the map keep their authentic password prompt (we don't submit).
            if pw_page.locator('input[name="post_password"]').count() > 0:
                known_pw = cfg.PROTECTED_PAGES.get(path)
                if known_pw:
                    print(f"    🔑 Password protected — submitting to capture content")
                    try:
                        pw_page.fill('input[name="post_password"]', known_pw)
                        pw_page.locator(
                            'form.post-password-form button[type="submit"], '
                            'form.post-password-form input[type="submit"]'
                        ).first.click()
                        pw_page.wait_for_load_state(
                            "load", timeout=cfg.PAGE_LOAD_TIMEOUT_MS)
                        pw_page.wait_for_timeout(800)
                        if pw_page.locator('input[name="post_password"]').count() > 0:
                            print(f"    ⚠ password did not unlock — storing prompt")
                    except Exception as e:
                        print(f"    ⚠ unlock failed ({e}) — storing prompt")
                else:
                    print(f"    🔑 Password protected — storing prompt (not submitting)")

            # Scroll through the page to trigger lazy-loaded images so the
            # response listener captures them.
            try:
                pw_page.evaluate(
                    """async () => {
                        await new Promise((resolve) => {
                            let total = 0;
                            const step = 500;
                            const timer = setInterval(() => {
                                window.scrollBy(0, step);
                                total += step;
                                if (total >= document.body.scrollHeight) {
                                    clearInterval(timer);
                                    window.scrollTo(0, 0);
                                    resolve();
                                }
                            }, 80);
                        });
                    }"""
                )
            except Exception:
                pass

            try:
                pw_page.wait_for_load_state("networkidle", timeout=cfg.NETWORK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass  # some pages never reach network idle

            html = pw_page.content()

        except Exception as e:
            print(f"    ❌ Failed to load: {e}")
            return None, []

        # Process any CSS the browser loaded during this page (download nested
        # url() assets + rewrite the CSS to local paths).
        if self.pending_css:
            for css_url, css_text in list(self.pending_css.items()):
                self.pending_css.pop(css_url, None)
                self._process_css(css_text, css_url)

        # Parse HTML
        soup = BeautifulSoup(html, "html.parser")

        # Extract page title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Ensure every referenced asset is captured (fallback-fetch the few the
        # browser never loaded — e.g. unused srcset variants).
        asset_urls = extract_asset_urls(soup, url)
        captured = sum(1 for a in asset_urls if a in self.asset_map)
        missing = 0
        for asset_url in asset_urls:
            if asset_url not in self.asset_map:
                if self.download_asset(asset_url):
                    missing += 1
        print(f"    Assets: {len(asset_urls)} referenced, {captured} captured, "
              f"{missing} fallback-fetched")

        # Sanitize
        sanitize_html(soup)

        # Rewrite asset URLs
        rewrite_html_assets(soup, self.asset_map, url)

        # Discover all internal links (pages not in the sitemap) to crawl too
        discovered = extract_internal_links(soup, url)

        # Store the rewritten HTML
        final_html = str(soup)
        sha = self.blobs.put_text(final_html, ".html")
        c_hash = canonical_hash(final_html)

        entry = {
            "blob": sha,
            "content_type": "text/html",
            "original_url": url,
            "title": title,
            "path": path,
        }
        if c_hash != sha:
            entry["canonical_hash"] = c_hash
        return entry, discovered

    # ── Queue-based crawl loop (handles pagination discovery) ────────────

    def _crawl_loop(self, pw_page, seed_urls: list[str], manifest: dict,
                    skip_existing: bool = False):
        """Crawl ``seed_urls``, following discovered pagination links.

        If ``skip_existing`` is set, pages already present in the manifest are
        still visited (to discover pagination) but not overwritten.
        """
        existing = set(manifest["pages"].keys())
        to_visit = list(dict.fromkeys(seed_urls))
        queued = set(to_visit)
        idx = 0
        while idx < len(to_visit):
            url = to_visit[idx]
            idx += 1
            print(f"\n[{idx}/{len(to_visit)}] {url}")

            page_data, discovered = self.crawl_page(pw_page, url)
            if page_data:
                pg = dict(page_data)
                pg_path = pg.pop("path")
                if not (skip_existing and pg_path in existing):
                    manifest["pages"][pg_path] = pg

            for d in discovered:
                if d not in queued:
                    queued.add(d)
                    to_visit.append(d)
                    print(f"    ➕ discovered page: {urlparse(d).path}")

            if idx < len(to_visit):
                time.sleep(cfg.REQUEST_DELAY_SECONDS)

    # ── Augment (add pagination pages to an existing snapshot) ───────────

    def augment(self):
        """Fill gaps in the most recent snapshot by following on-site links
        from the homepage, without overwriting already-crawled pages. Discovers
        pages the sitemap missed (pagination, archives, in-post links, etc.)."""
        existing = sorted(
            d for d in os.listdir(cfg.SNAPSHOT_DIR)
            if os.path.isfile(os.path.join(cfg.SNAPSHOT_DIR, d, "manifest.json"))
        ) if os.path.isdir(cfg.SNAPSHOT_DIR) else []
        if not existing:
            print("❌ No snapshots found. Run a full crawl first.")
            return
        today = existing[-1]
        snapshot_dir = os.path.join(cfg.SNAPSHOT_DIR, today)
        manifest_path = os.path.join(snapshot_dir, "manifest.json")

        if not os.path.exists(manifest_path):
            print(f"❌ No snapshot for {today}. Run a full crawl first.")
            return

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        before = len(manifest["pages"])
        print(f"═══ Augmenting {today} — {before} existing pages ═══")
        print("🔎 Following on-site links from the homepage...")

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            context = browser.new_context(
                viewport=cfg.VIEWPORT,
                user_agent=cfg.USER_AGENT,
            )
            self.req_context = context.request
            pw_page = context.new_page()
            pw_page.on("response", self._on_response)

            self._crawl_loop(
                pw_page, [cfg.SITE_ORIGIN + "/"], manifest, skip_existing=True
            )

            browser.close()
            self.req_context = None

        # Merge any newly captured assets
        for local_path, meta in self.asset_meta.items():
            manifest["assets"].setdefault(local_path, meta)

        added = len(manifest["pages"]) - before
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        print(f"\n✅ Augment complete: +{added} pages "
              f"(total {len(manifest['pages'])}), "
              f"{len(manifest['assets'])} assets")

    # ── Full Crawl ───────────────────────────────────────────────────────

    def crawl(self):
        """Run a full site crawl."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%dT%H%M")
        snapshot_dir = os.path.join(cfg.SNAPSHOT_DIR, today)

        # Check if a snapshot with this exact timestamp already exists
        manifest_path = os.path.join(snapshot_dir, "manifest.json")
        if os.path.exists(manifest_path):
            print(f"⚠ Snapshot {today} already exists. Skipping.")
            print(f"  Delete {snapshot_dir} to re-crawl.")
            return

        print(f"═══ Project Skyscraper Crawler — {today} ═══")
        print()

        manifest = {
            "date": now.strftime("%Y-%m-%d"),
            "crawled_at": now.isoformat(),
            "pages": {},
            "assets": {},
        }

        # Start Playwright first so we can use its context to fetch the sitemap
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            context = browser.new_context(
                viewport=cfg.VIEWPORT,
                user_agent=cfg.USER_AGENT,
            )
            self.req_context = context.request

            # 1. Fetch sitemaps
            print("📡 Fetching sitemaps...")
            sitemap_bytes = fetch_sitemap(cfg.SITEMAP_URL, self.req_context)
            if not sitemap_bytes:
                print("❌ Failed to fetch sitemap. Aborting.")
                browser.close()
                self.req_context = None
                return

            pages, image_urls = parse_sitemap(sitemap_bytes, self.req_context)
            # De-duplicate
            seen = set()
            uniq_pages = []
            for pg in pages:
                if pg["url"] not in seen:
                    seen.add(pg["url"])
                    uniq_pages.append(pg)
            pages = uniq_pages
            image_urls = list(dict.fromkeys(image_urls))
            print(f"  Found {len(pages)} pages, {len(image_urls)} images in sitemap")

            # 2. Crawl each page (assets captured live via the response listener)
            print()
            print("🌐 Crawling pages...")
            pw_page = context.new_page()
            pw_page.on("response", self._on_response)

            self._crawl_loop(pw_page, [e["url"] for e in pages], manifest)

            # 3. Pick up sitemap-only images that no page referenced.
            print()
            print("🖼️  Fetching sitemap images not seen during crawl...")
            extra = 0
            for img_url in image_urls:
                if img_url not in self.asset_map:
                    if self.download_asset(img_url):
                        extra += 1
                    time.sleep(0.5)  # gentle pacing for the fallback fetcher
            print(f"  Fetched {extra} extra sitemap images")

            browser.close()
            self.req_context = None

        # 4. Record asset metadata
        manifest["assets"] = dict(self.asset_meta)

        # 5. Compute changes from previous snapshot
        changes = self._compute_changes(today, manifest)

        # 6. Skip if nothing changed (but always save the very first snapshot)
        if changes["previous_date"] is not None and not any([
            changes["pages_added"],
            changes["pages_modified"],
            changes["pages_removed"],
            changes["assets_added"],
        ]):
            print(f"\n⏭  No changes from {changes['previous_date']} — snapshot skipped.")
            return

        # 7. Save
        os.makedirs(snapshot_dir, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Manifest saved: {manifest_path}")

        changes_path = os.path.join(snapshot_dir, "changes.json")
        with open(changes_path, "w", encoding="utf-8") as f:
            json.dump(changes, f, indent=2, ensure_ascii=False)
        print(f"💾 Changes saved:  {changes_path}")

        # Save crawler state
        state = {
            "last_crawl": today,
            "total_pages": len(manifest["pages"]),
            "total_assets": len(manifest["assets"]),
            "fallback_fetches": self._fallback_fetches,
        }
        with open(cfg.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        print(f"\n✅ Crawl complete: {len(manifest['pages'])} pages, "
              f"{len(manifest['assets'])} assets "
              f"({self._fallback_fetches} fallback fetches)")

    def _compute_changes(self, today: str, manifest: dict) -> dict:
        """Diff this manifest against the previous day's."""
        changes = {
            "date": today,
            "previous_date": None,
            "pages_added": [],
            "pages_modified": [],
            "pages_removed": [],
            "assets_added": 0,
            "assets_modified": 0,
        }

        # Find previous snapshot
        if os.path.exists(cfg.SNAPSHOT_DIR):
            dates = sorted(d for d in os.listdir(cfg.SNAPSHOT_DIR) if d != today)
            if dates:
                prev_date = dates[-1]
                prev_manifest_path = os.path.join(cfg.SNAPSHOT_DIR, prev_date, "manifest.json")
                if os.path.exists(prev_manifest_path):
                    with open(prev_manifest_path, "r", encoding="utf-8") as f:
                        prev = json.load(f)

                    changes["previous_date"] = prev_date
                    prev_pages = prev.get("pages", {})
                    curr_pages = manifest.get("pages", {})

                    for path in curr_pages:
                        if path not in prev_pages:
                            changes["pages_added"].append(path)
                        else:
                            c = curr_pages[path]
                            p = prev_pages[path]
                            curr_key = c.get("canonical_hash") or c["blob"]
                            prev_key = p.get("canonical_hash") or p["blob"]
                            if curr_key != prev_key:
                                changes["pages_modified"].append(path)

                    for path in prev_pages:
                        if path not in curr_pages:
                            changes["pages_removed"].append(path)

                    prev_asset_urls = {v["original_url"]
                                       for v in prev.get("assets", {}).values()}
                    curr_asset_urls = {v["original_url"]
                                       for v in manifest.get("assets", {}).values()}
                    changes["assets_added"] = len(curr_asset_urls - prev_asset_urls)

        n_add = len(changes["pages_added"])
        n_mod = len(changes["pages_modified"])
        n_rem = len(changes["pages_removed"])
        changes["summary"] = (
            f"{n_add} added, {n_mod} modified, {n_rem} removed, "
            f"{changes['assets_added']} new assets"
        )

        return changes


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Project Skyscraper site crawler")
    parser.add_argument(
        "--backfill", action="store_true",
        help="Run a one-time best-effort backfill crawl"
    )
    parser.add_argument(
        "--augment", action="store_true",
        help="Add newly-discovered pages to the most recent existing snapshot"
    )
    parser.add_argument(
        "--trigger", default="manual", choices=["cron", "manual", "api"],
        help="What triggered this crawl (recorded in trigger log)"
    )
    args = parser.parse_args()

    os.makedirs(cfg.MIRROR_DIR, exist_ok=True)
    os.makedirs(cfg.BLOB_DIR, exist_ok=True)
    os.makedirs(cfg.SNAPSHOT_DIR, exist_ok=True)

    mode = "augment" if args.augment else "crawl"
    trigger_log_path = os.path.join(cfg.MIRROR_DIR, "crawl_triggers.log")
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trigger": args.trigger,
        "mode": mode,
    })
    with open(trigger_log_path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

    crawler = SiteCrawler()
    if args.augment:
        crawler.augment()
    else:
        crawler.crawl()


if __name__ == "__main__":
    main()
