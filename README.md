# Project Skyscraper — Wayback Archive

A self-hosted wayback machine for [project-skyscraper.com](https://project-skyscraper.com), a community ARG (alternate reality game) built around *No Man's Sky*. Captures periodic snapshots of the live site and serves them as a fully-navigable offline replica.

---

## What it does

- **Crawls** the live site with a headless Firefox browser, sanitizes tracking/analytics, rewrites asset URLs to a local content-addressed store, and writes dated snapshot manifests.
- **Serves** the archive at `/@<snapshot>/` with time-travel routing, a floating snapshot picker, and a per-page mirror header — no JavaScript required for navigation.
- **Gates** public access with a configurable front page (password prompt or click-through splash).

---

## Architecture

```
site_crawler.py        — Playwright/Firefox crawler → web_mirror/
wayback_server.py      — stdlib ThreadingHTTPServer serving the archive
crawler_config.py      — shared config (domain, paths, blocklists, port)

web_mirror/
  _assets/             — content-addressed blob store (SHA-256, sharded)
  snapshots/
    2026-06-15T0330/
      manifest.json    — page-path → blob hash + metadata
      changes.json     — diff vs previous snapshot
```

Blobs are stored once by SHA-256 hash. Snapshots are just manifests — unchanged assets cost nothing extra per day. Snapshot directories are named `YYYY-MM-DDTHHMM` (UTC).

---

## Requirements

- Python 3.12+
- **Server only:** `beautifulsoup4==4.14.3`
- **Crawler:** above + `requests==2.34.2` + `playwright==1.60.0` + headless Firefox

---

## Setup

```bash
git clone https://github.com/GoodGuysFree/skyscraper.git
cd skyscraper

python3 -m venv .venv
.venv/bin/pip install -r deploy/requirements-crawler.txt   # superset; runs server too

# Download headless Firefox (crawler only)
.venv/bin/playwright install firefox
sudo .venv/bin/playwright install-deps firefox             # Ubuntu/Debian OS libs
```

Create a `.env` file for the archive gate password (never committed):

```
ARCHIVE_PASSWORD=your_password_here
```

---

## Usage

### Run the server

```bash
.venv/bin/python wayback_server.py --host 0.0.0.0 --port 8070
```

Browse to `http://localhost:8070/` — enter the password to access the archive.

### Crawl the live site

```bash
.venv/bin/python site_crawler.py              # full crawl → new snapshot
.venv/bin/python site_crawler.py --augment    # add missed pages to latest snapshot
```

The running server picks up new snapshots automatically on the next request — no restart needed.

### Check snapshot integrity

```bash
python3 -c "
import json, glob
for m in sorted(glob.glob('web_mirror/snapshots/*/manifest.json')):
    d = json.load(open(m))
    print(m, '->', len(d['pages']), 'pages,', len(d['assets']), 'assets')
"
```

---

## Front-page gate

Controlled by `GATE_MODE` in `crawler_config.py`:

| Mode | Behaviour |
|------|-----------|
| `"password"` | Visitor must enter `ARCHIVE_PASSWORD` from `.env` before accessing the archive. |
| `"button"` | Splash page only — click to enter, no password. |

---

## Mirroring more than one site

The crawler and server are site-agnostic; everything site-specific lives in
`crawler_config.py` as the **default profile** (the primary site). A second site is
run as a **separate instance of the same code** — no fork — by pointing the
`WB_SITE_CONFIG` environment variable at a small **profile module** that overrides
only the knobs that differ.

A profile is a plain Python file that redefines any of the uppercase config names.
See [`sites/recalldreams.py`](sites/recalldreams.py) for a complete example. Typical
overrides:

```python
# sites/yoursite.py
SITE_DOMAIN  = "example.org"          # SITE_ORIGIN / SITEMAP_URL re-derive from this
SERVER_PORT  = 8071                   # second instance → second port
SITE_TITLE   = "Example Archive"      # chrome branding
GATE_MODE    = "password"             # or "button"
PROTECTED_PAGES = {}                  # per-site password-gated pages
HAS_INBOX    = False                  # site-specific features off by default
EXPOSE_STATS = False                  # record access but hide the stats UI
# theme: SITE_BG_IMAGE, SITE_ACCENT, ... ; canonical-noise patterns; cross-link; etc.
```

Anything the profile doesn't set is inherited from `crawler_config.py`. When
`WB_SITE_CONFIG` is unset, the primary site runs exactly as before.

**Each instance is fully isolated** via its own `SKYSCRAPER_HOME` — its own
`web_mirror/` (snapshots + blob store), its own `.env`, its own `stats.db`. The two
instances share only the code checkout and the virtualenv.

```bash
# one-time: create the data dir + its own gate secrets (never committed)
mkdir -p /srv/example/web_mirror
printf 'ARCHIVE_PASSWORD=...\nTRIGGER_TOKEN=...\n' > /srv/example/.env

# crawl the second site
SKYSCRAPER_HOME=/srv/example WB_SITE_CONFIG="$(pwd)/sites/yoursite.py" \
  .venv/bin/python site_crawler.py

# serve the second site on its own port
SKYSCRAPER_HOME=/srv/example WB_SITE_CONFIG="$(pwd)/sites/yoursite.py" \
  .venv/bin/python wayback_server.py --host 0.0.0.0 --port 8071
```

Each instance reads its `ARCHIVE_PASSWORD` / `TRIGGER_TOKEN` from
`$SKYSCRAPER_HOME/.env` — **passwords and tokens never live in the repo or in a
profile module.** Run a separate cron entry per site (each with its own
`SKYSCRAPER_HOME` + `WB_SITE_CONFIG`), and put a reverse proxy in front that routes
each hostname to the right port (name-based virtual hosting — one IP, many sites).

---

## Running tests

```bash
python3 -m pytest tests/ -v
```

297 tests, fully offline (no network, no browser). Run before committing changes and before deploying.

---

## Deploy

Code is deployed via `git push` → `git pull` on the production server. `web_mirror/` (the data) is **not** in git — seed it separately and update via `rsync` or by running the crawler on the production box.

See `CLAUDE.md` for workspace rules and invariants. Operational details (server addresses, service layout, go-live steps) live in `OPERATIONS.local.md` which is git-ignored.

---

## Credits

Built and maintained by the [GoodGuysFree](https://github.com/GoodGuysFree) community.  
Thanks to everyone who contributed to solving the Project Skyscraper ARG.
