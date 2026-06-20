"""
Per-site profile: recalldreams.dev — the ARG's secondary site ("The Tower").

Loaded by crawler_config.py when WB_SITE_CONFIG points here. Only the knobs that
differ from the project-skyscraper "System" defaults are redefined; everything
else (MIME types, user agent, timeouts, WordPress tracking strips, the wp.com CDN
asset hosts, trigger debounce/cooldown, ASSET_URL_PREFIX) is inherited.

The Tower starts on a CLEAN quirk slate: no protected pages, no canonical-ignore
patterns (System's Memory_bloc / scramble-text gimmicks do not apply here), the
/inbox/ School-Code feature off, and the public stats UI hidden until go-live.
Add Tower-specific patterns here as we learn its behaviour.
"""

# ── Target site ──────────────────────────────────────────────────────────────
SITE_DOMAIN = "recalldreams.dev"
# SITE_ORIGIN and SITEMAP_URL are re-derived from SITE_DOMAIN by the overlay.

# ── Server ───────────────────────────────────────────────────────────────────
SERVER_PORT = 8071          # second instance; behind Caddy vhost recalldreams.*

# ── Front-page gate ──────────────────────────────────────────────────────────
# Open access (no password) — click-through splash, same as the primary site.
GATE_MODE = "button"

# ── Branding (server chrome) ─────────────────────────────────────────────────
SITE_TITLE = "RecallDreams (The Tower)"
SITE_TAGLINE = "Wayback Archive — community-maintained preservation mirror"
SITE_ABOUT = (
    "This archive preserves snapshots of recalldreams.dev, the secondary site "
    "of the Project Skyscraper ARG (alternate reality game) for the No Man's Sky "
    "community. Pages are captured periodically so the record is never lost."
)
# Cross-link back to the primary ("System") archive's front page.
CROSS_SITE_URL = "https://archive.goodguysfree.com/"
CROSS_SITE_LABEL = "↪ Enter the System archive — project-skyscraper.com"

# ── Theme — match bg2.jpg (dark near-black with a teal-green network mesh) ────
SITE_BG_IMAGE = "/_static/bg2.jpg"
SITE_ACCENT = "#7fd6b4"            # pale teal-green (the mesh lines)
SITE_ACCENT_HOVER = "#a9ead0"
SITE_ACCENT_DIM = "#5f9e84"        # muted teal (cross-link)
SITE_ACCENT_DIM_HOVER = "#8fc7ad"
SITE_BAR_FILL = "#2f9f78"          # teal-green bars
# Subtle teal/cyan tint on the chrome (top bar + picker), matching bg2.jpg —
# a dark hue, not a bright fill.
SITE_CHROME_BG = "rgba(6,20,18,0.94)"   # top header bar
SITE_PANEL_BG = "rgba(8,23,21,0.94)"    # picker panel
SITE_LIST_BG = "#08140f"                # picker dropdown list

# ── Feature flags ────────────────────────────────────────────────────────────
HAS_INBOX = False           # no /inbox/ School-Code thread on the Tower
EXPOSE_STATS = False        # record access, but hide the stats UI until go-live

# ── Quirk profile — blank slate ──────────────────────────────────────────────
# The Tower has none of System's password pages or dynamic-content gimmicks yet.
PROTECTED_PAGES = {}
PAGE_PASSWORD = ""
CANONICAL_IGNORE_PATTERNS = [
    # WordPress nonces still rotate on any WP site; keep these generic strips so
    # nonce churn doesn't flag pages as modified. System-specific patterns
    # (Memory_bloc counters, scramble-text) are intentionally NOT inherited.
    r"/_assets/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+",
    r'<input[^>]*name=["\']_wpnonce["\'][^>]*/?>',
    r'<input[^>]*name=["\']_wp_http_referer["\'][^>]*/?>',
    r'"nonce"\s*:\s*"[^"]*"',
]
CANONICAL_REPLACE_PATTERNS = []
