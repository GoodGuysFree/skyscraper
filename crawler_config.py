"""
Configuration for the Project Skyscraper site crawler and wayback server.

All paths use /_assets/<hash>.<ext> format so the archive can be served
by any static file server or replicated to external hosting.
"""

import os

# ── Target Site ──────────────────────────────────────────────────────────────

SITE_DOMAIN = "project-skyscraper.com"
SITE_ORIGIN = f"https://{SITE_DOMAIN}"
SITEMAP_URL = f"{SITE_ORIGIN}/sitemap.xml"
PAGE_PASSWORD = "EMILY"

# ── Local Paths ──────────────────────────────────────────────────────────────
# WORKSPACE_DIR is portable: it defaults to the directory containing this file
# (so the code runs wherever it's deployed — Windows or Linux), and can be
# overridden with the SKYSCRAPER_HOME environment variable. web_mirror/ is
# expected to live directly under WORKSPACE_DIR.

WORKSPACE_DIR = os.environ.get(
    "SKYSCRAPER_HOME",
    os.path.dirname(os.path.abspath(__file__)),
)
MIRROR_DIR = os.path.join(WORKSPACE_DIR, "web_mirror")
BLOB_DIR = os.path.join(MIRROR_DIR, "_assets")       # content-addressed store
SNAPSHOT_DIR = os.path.join(MIRROR_DIR, "snapshots")
STATE_FILE = os.path.join(MIRROR_DIR, "crawler_state.json")

# ── Crawler Behavior ─────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/115.0"
)
VIEWPORT = {"width": 1280, "height": 1080}
REQUEST_DELAY_SECONDS = 1.5        # delay between page fetches
PAGE_LOAD_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 5_000

# Asset URL prefix used in rewritten HTML.
# This path is portable: works with the local server AND as a static dir.
ASSET_URL_PREFIX = "/_assets"

# ── Domains to Treat as "Internal" (download assets from these) ──────────────

ASSET_DOMAINS = {
    SITE_DOMAIN,
    f"www.{SITE_DOMAIN}",
    "i0.wp.com",           # Jetpack Photon image CDN
    "i1.wp.com",
    "i2.wp.com",
    "c0.wp.com",           # WP static assets
    "s0.wp.com",
    "s1.wp.com",
    "s2.wp.com",
    "fonts.wp.com",        # WordPress hosted fonts
    "secure.gravatar.com", # Gravatar (for site logo/icon)
    "0.gravatar.com",
    "1.gravatar.com",
    "2.gravatar.com",
}

# ── Domains / URL patterns to BLOCK entirely ─────────────────────────────────

BLOCKED_DOMAINS = {
    "pixel.wp.com",         # WP tracking pixel
    "stats.wp.com",         # Jetpack stats
    "public-api.wordpress.com",  # WP public API
    "widgets.wp.com",       # Social widgets
}

# ── Script Sanitization ──────────────────────────────────────────────────────
# Patterns matched against <script> tag contents or src URLs.
# Matching scripts are removed entirely.

SCRIPT_BLOCK_PATTERNS = [
    r"wp-statistics",
    r"_stq\s*=",                    # Jetpack stats queue
    r"new\s+Image\(\).*wpstats",    # WP stats pixel
    r"quantcast",
    r"google-analytics",
    r"gtag\(",
    r"fbq\(",                       # Facebook pixel
    r"hotjar",
    r"JETPACK_MU_WPCOM_SETTINGS",   # Jetpack mu-plugin settings
]

# ── Elements to Remove from HTML ─────────────────────────────────────────────
# CSS selectors for elements stripped during sanitization.

ELEMENT_REMOVE_SELECTORS = [
    'link[rel="dns-prefetch"]',
    'link[rel="preconnect"]',
    'link[rel="https://api.w.org/"]',    # wp-json API discovery
    'link[rel="EditURI"]',                # RSD/XML-RPC
    'link[rel="shortlink"]',              # wp.me shortlink
    'link[rel="pingback"]',
    'link[rel="wlwmanifest"]',
    'meta[name="generator"]',
    'img#wpstats',                        # tracking pixel
    'style:contains("img#wpstats")',      # style hiding the pixel
    '#wpadminbar',                        # admin bar
    '.jetpack-likes-widget-wrapper',      # Jetpack likes
    '#jp-post-flair',                     # Jetpack social
    'script[src*="wp-statistics"]',
    'script[src*="stats.wp.com"]',
    'script[src*="widgets.wp.com"]',
    'script[src*="public-api.wordpress.com"]',
]

# ── File Extension → MIME Type ───────────────────────────────────────────────

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".eot":  "application/vnd.ms-fontobject",
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".pdf":  "application/pdf",
    ".xml":  "application/xml",
}

# ── Server ───────────────────────────────────────────────────────────────────

SERVER_HOST = "localhost"
SERVER_PORT = 8070

# ── Front-Page Gate ──────────────────────────────────────────────────────────
# GATE_MODE controls what visitors see before entering the archive.
#   "password" — ask for a password (read from .env → ARCHIVE_PASSWORD)
#   "button"   — show a splash page with an "Enter Archive" button, no password
GATE_MODE = "password"

# Path to the environment file that supplies ARCHIVE_PASSWORD and TRIGGER_TOKEN.
# Never commit this file — it is git-ignored.
GATE_ENV_FILE = os.path.join(WORKSPACE_DIR, ".env")

# ── Canonical Hash — dynamic content to ignore in change detection ────────────
# Regex patterns applied to page HTML before computing canonical_hash.
# Pages whose only differences match these patterns are not marked as modified.
# Safe to extend; removing patterns affects new snapshots only.
CANONICAL_IGNORE_PATTERNS = [
    r"Memory_bloc_\w+: \d+/\d+ Completed",
    # Blob paths are content-addressed: same asset re-downloaded with different
    # bytes produces a new hash and a new /_assets/... URL, even if visually
    # identical. Strip them so canonical_hash reflects text/structure only.
    r"/_assets/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+",
    # WordPress nonces are time-based tokens that change every ~12h. Strip the
    # entire hidden input so nonce rotation doesn't flag pages as modified.
    r'<input[^>]*name=["\']_wpnonce["\'][^>]*/?>',
    r'<input[^>]*name=["\']_wp_http_referer["\'][^>]*/?>',
    # Jetpack Carousel and other WP plugins embed rotating nonces in inline JS.
    r'"nonce"\s*:\s*"[^"]*"',
    # Previous/Next post navigation links: adding a page reshuffles the chain
    # for neighbouring posts but is not a content change.
    r'<a\b[^>]*>\s*(?:Previous|Next)\s*</a>',
    # Jetpack contact form JWT rotates on every request.
    r'<input[^>]*name=["\']jetpack_contact_form_jwt["\'][^>]*/?>',
    # Akismet honeypot textarea value is dynamic anti-spam noise.
    r'<textarea[^>]*name=["\']ak_hp_textarea["\'][^>]*>.*?</textarea>',
]

# ── Trigger-Crawl Webhook ─────────────────────────────────────────────────────
# Debounce: wait this many seconds after the last trigger before starting a crawl.
# Coalesces rapid triggers into a single crawl.
TRIGGER_DEBOUNCE_SECONDS = 180
# Cooldown: mandatory rest after a crawl finishes before a queued crawl may start.
TRIGGER_COOLDOWN_SECONDS = 300
