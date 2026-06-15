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

## Running tests

```bash
python3 -m pytest tests/ -v
```

92 tests, fully offline (no network, no browser). Run before committing changes and before deploying.

---

## Deploy

Code is deployed via `git push` → `git pull` on the production server. `web_mirror/` (the data) is **not** in git — seed it separately and update via `rsync` or by running the crawler on the production box.

See `CLAUDE.md` for workspace rules and invariants. Operational details (server addresses, service layout, go-live steps) live in `OPERATIONS.local.md` which is git-ignored.

---

## Credits

Built and maintained by the [GoodGuysFree](https://github.com/GoodGuysFree) community.  
Thanks to everyone who contributed to solving the Project Skyscraper ARG.
