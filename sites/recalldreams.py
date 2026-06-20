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
# Phase 1 (build/test): password-gated while we build. At go-live this flips to a
# two-button splash (ENTER TOWER + Enter System). Password read from this
# instance's own .env -> ARCHIVE_PASSWORD.
GATE_MODE = "password"

# ── Branding (server chrome) ─────────────────────────────────────────────────
SITE_TITLE = "RecallDreams (The Tower)"
SITE_TAGLINE = "Wayback Archive — community-maintained preservation mirror"
SITE_ABOUT = (
    "This archive preserves snapshots of recalldreams.dev, the secondary site "
    "of the Project Skyscraper ARG (alternate reality game) for the No Man's Sky "
    "community. Pages are captured periodically so the record is never lost."
)

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
