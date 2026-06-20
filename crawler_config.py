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

# ── Archive branding (server chrome) ─────────────────────────────────────────
# Site-coupled display strings. A second instance (see "Per-site profile" at the
# bottom of this file) overrides these via its WB_SITE_CONFIG module.
SITE_TITLE = "Project Skyscraper"
SITE_TAGLINE = "Wayback Archive — community-maintained preservation mirror"
SITE_ABOUT = (
    "This archive preserves snapshots of project-skyscraper.com, an ARG "
    "(alternate reality game) created for the No Man's Sky community. "
    "Pages are captured periodically so the record is never lost."
)
# Cross-link to the project's OTHER archive, shown as a plain link under the gate
# button. Points at the other instance's front page (its own gate). Empty = hide.
CROSS_SITE_URL = "https://recalldreams.goodguysfree.com/"
CROSS_SITE_LABEL = "↪ Enter the Tower archive — recalldreams.dev"

# ── Theme — gate background + chrome accent colors ───────────────────────────
# System palette: blue accent over the bg.jpg skyline. A second instance overrides
# these (e.g. the Tower's teal-green over bg2.jpg). SITE_BG_IMAGE is a /_static/
# path served from the static/ dir next to wayback_server.py.
SITE_BG_IMAGE = "/_static/bg.jpg"
SITE_ACCENT = "#7dd3fc"            # primary links / accents
SITE_ACCENT_HOVER = "#bae6fd"
SITE_ACCENT_DIM = "#6a93b0"        # muted secondary (cross-link)
SITE_ACCENT_DIM_HOVER = "#9ec3dc"
SITE_BAR_FILL = "#2563eb"          # stats horizontal bars
# Chrome backgrounds — the fixed top bar + the snapshot picker panel/list. Kept
# near-black for the primary site; a second instance can tint them toward its
# theme (a subtle hue, not a bright fill).
SITE_CHROME_BG = "rgba(8,8,8,0.93)"    # top header bar
SITE_PANEL_BG = "rgba(10,10,10,0.93)"  # picker panel
SITE_LIST_BG = "#0e0e0e"               # picker dropdown list

# ── Per-site feature flags ───────────────────────────────────────────────────
# HAS_INBOX    — serve the /inbox/ School-Code translator feature (System only).
# EXPOSE_STATS — show the SITE STATS header link + /~api/stats page. Access is
#                still RECORDED regardless; this only gates the public UI.
HAS_INBOX = True
EXPOSE_STATS = True

# ── Password-protected pages ─────────────────────────────────────────────────
# Pages behind a WordPress post-password gate whose content the crawler should
# UNLOCK and capture (by submitting the password), so the archive holds the real
# content. The server then re-creates the gate CLIENT-SIDE (validates the typed
# password in-browser) so wayback visitors still experience entering it.
#
# These passwords are PUBLIC ARG answers (already on YouTube / community forums),
# not secrets — embedding them client-side is acceptable. See OPERATIONS.local.md
# §Secrets posture.
#
# Path keys are normalized with a trailing slash. To gate a new page, add it here
# (the crawler will submit the password; the server will gate + validate it).
PROTECTED_PAGES = {
    "/request-memory-timestamp-094317/": "EMILY",
    "/report-bru-ent-reunion-peak/": "EVENT HORIZON",
    "/recon-protocol/": "vector_cmdr",
}

# ── Local Paths ──────────────────────────────────────────────────────────────
# WORKSPACE_DIR is portable: it defaults to the directory containing this file
# (so the code runs wherever it's deployed — Windows or Linux), and can be
# overridden with the SKYSCRAPER_HOME environment variable. web_mirror/ is
# expected to live directly under WORKSPACE_DIR.

# CODE_DIR is where the programs live (this file + site_crawler.py +
# wayback_server.py). It is ALWAYS the directory of this module, independent of
# SKYSCRAPER_HOME. For a second instance, SKYSCRAPER_HOME points at a separate
# data tree while CODE_DIR stays on the shared checkout — so the server can still
# find site_crawler.py to spawn the api-trigger crawl.
CODE_DIR = os.path.dirname(os.path.abspath(__file__))

WORKSPACE_DIR = os.environ.get(
    "SKYSCRAPER_HOME",
    CODE_DIR,
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

# Shared CDN/avatar hosts — same for any WordPress.com-hosted site (System or
# Tower). The per-site origin is added on top so the set tracks SITE_DOMAIN.
_CDN_ASSET_DOMAINS = {
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
ASSET_DOMAINS = {SITE_DOMAIN, f"www.{SITE_DOMAIN}"} | _CDN_ASSET_DOMAINS

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
GATE_MODE = "button"

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

# Regex replacements applied before hashing (pattern, replacement) pairs.
# Use when the match must be kept but its variable inner content stripped.
CANONICAL_REPLACE_PATTERNS = [
    # scramble-text anchors: JS shuffles the visible text on every load but the
    # real message lives in data-word. Strip the shuffled text, keep the tag so
    # data-word changes are still detected as real content changes.
    (r'(<a\b[^>]*\bclass="[^"]*\bscramble-text\b[^"]*"[^>]*>)[^<]*(</a>)', r'\1\2'),
    # WordPress core block-library inline CSS (<style id="wp-block-*-inline-css">)
    # is regenerated verbatim on every page whenever WordPress.com bumps its core
    # / Gutenberg version (e.g. ":where([style*=border-color])" became
    # ":where([style^=border-color],[style*=";border-color"],...)"). It is pure
    # plumbing — identical across all pages, no site content — so a core update
    # would otherwise flag the ENTIRE site as modified with no visible change.
    # Strip these blocks from the canonical hash only; the stored blob keeps them.
    # global-styles / core-block-supports are intentionally NOT stripped — those
    # can carry real design/content changes.
    (r'<style id="wp-block-[^"]*-inline-css"[^>]*>.*?</style>', ''),
]

# ── Trigger-Crawl Webhook ─────────────────────────────────────────────────────
# Debounce: wait this many seconds after the last trigger before starting a crawl.
# Coalesces rapid triggers into a single crawl.
TRIGGER_DEBOUNCE_SECONDS = 180
# Cooldown: mandatory rest after a crawl finishes before a queued crawl may start.
TRIGGER_COOLDOWN_SECONDS = 300

# ── Per-site profile overlay ──────────────────────────────────────────────────
# This project runs more than one wayback machine off ONE codebase. The defaults
# above are the project-skyscraper "System" profile. A second instance (e.g. the
# recalldreams "Tower") sets WB_SITE_CONFIG to a small module that redefines only
# the site-coupled knobs — domain, branding, PROTECTED_PAGES, the canonical /
# block / element lists, the HAS_INBOX / EXPOSE_STATS flags, GATE_MODE, etc.
#
# When WB_SITE_CONFIG is unset, nothing below runs and the main instance behaves
# exactly as before. The profile only overrides UPPERCASE names that already
# exist here, so it cannot introduce surprises — it starts from a clean inherit
# of these defaults and replaces the few knobs it names.
_SITE_PROFILE = os.environ.get("WB_SITE_CONFIG")
if _SITE_PROFILE:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location("wb_site_profile", _SITE_PROFILE)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    _overridden = set()
    for _name in dir(_mod):
        if _name.isupper() and _name in globals():
            globals()[_name] = getattr(_mod, _name)
            _overridden.add(_name)

    # Recompute derived values unless the profile set them explicitly.
    if "SITE_DOMAIN" in _overridden and "SITE_ORIGIN" not in _overridden:
        SITE_ORIGIN = f"https://{SITE_DOMAIN}"
    if "SITE_DOMAIN" in _overridden and "SITEMAP_URL" not in _overridden:
        SITEMAP_URL = f"{SITE_ORIGIN}/sitemap.xml"
    if "SITE_DOMAIN" in _overridden and "ASSET_DOMAINS" not in _overridden:
        ASSET_DOMAINS = {SITE_DOMAIN, f"www.{SITE_DOMAIN}"} | _CDN_ASSET_DOMAINS
