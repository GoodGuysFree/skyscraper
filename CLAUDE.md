# Project Skyscraper — Workspace Rules

## What this is

Two cooperating programs that together form a local "wayback machine" for `project-skyscraper.com`:

- **`site_crawler.py`** — Playwright/Firefox headless crawler. Scrapes the live site, sanitizes tracking, rewrites asset URLs to content-addressed paths, writes dated snapshots under `web_mirror/snapshots/<YYYY-MM-DD>/`.
- **`wayback_server.py`** — Stdlib `ThreadingHTTPServer`. Serves the archive with `/@<date>/<path>` routing, a floating nav overlay, and a manifest cache that self-heals on new snapshots without restart.
- **`crawler_config.py`** — Single shared config for both programs. All tunable knobs live here.

All persistent data lives under `web_mirror/`. The server only needs `web_mirror/` to run; the crawler writes into it.

---

## Python version

**Python 3.12+ required.** Both scripts use 3.12 stdlib features (`sys.stdout.reconfigure`). Do not introduce syntax or imports that require 3.13+ or that drop support for 3.12.

---

## Dependencies

- **Server only:** `beautifulsoup4==4.14.3` (see `deploy/requirements-server.txt`)
- **Crawler:** above + `requests==2.34.2` + `playwright==1.60.0` + headless Firefox (see `deploy/requirements-crawler.txt`)

If you add a dependency: pin it in the appropriate requirements file. Never add a dep to the crawler file without checking if the server file also needs it — they share a venv in production. Never use `uv` syntax (no `pyproject.toml` here); plain `pip install -r` is the contract.

---

## Blob store invariant

**Never rename, move, or delete files under `web_mirror/_assets/`.**

The store is content-addressed: each file is named by its SHA-256 hash and sharded by the first 2 hex chars. Manifests reference blobs by hash only. Breaking this reference invalidates every snapshot that uses the blob.

The only correct way to write a blob is via `BlobStore.put_bytes()` / `BlobStore.put_text()`. Both are idempotent — calling them on already-stored data is safe and cheap.

---

## Snapshot immutability

A snapshot for a given date is written once by the crawler. After the crawl completes:
- `manifest.json` must not be overwritten except by `--augment` mode (which adds missing pages to the *current day's* in-progress snapshot).
- `changes.json` must not be modified after the crawl finishes.
- The snapshot directory name is `YYYY-MM-DD` in UTC (derived from system clock at crawl start).

Do not back-fill old dates unless you understand the manifest diff chain.

---

## Manifest schema

```json
{
  "date": "YYYY-MM-DD",
  "crawled_at": "<ISO-8601 UTC>",
  "pages":  { "<url-path>": { "blob": "<sha256>", "content_type": "...", "original_url": "...", "title": "..." } },
  "assets": { "/_assets/<shard>/<hash>.<ext>": { "original_url": "...", "content_type": "...", "size": N, "sha256": "..." } }
}
```

Any change to this schema must be backwards-compatible: the server reads every snapshot ever written. Add new keys; never remove or rename existing ones without a migration that rewrites all existing manifests.

---

## Server rules

- **Read-only.** The server serves blobs and manifests; it never writes to disk. Do not add endpoints that mutate `web_mirror/`.
- **No JS required.** Navigation overlay and link rewriting work without JavaScript. Don't add client-side JS that breaks this.
- **Manifest cache is lock-free.** The cache swaps atomically using Python's GIL. Don't add shared mutable state that isn't GIL-protected or properly locked.
- **`/~api/reload`** triggers a manual cache rebuild. Useful after a crawl; the normal auto-reload (mtime check on each request) is sufficient for production.

---

## Crawler rules

- **Not concurrency-safe.** Run only one crawler instance at a time. A cron job that overlaps itself will corrupt the in-progress snapshot.
- **Browser is required for assets.** The live host returns HTTP 429 to non-browser asset requests. Do not replace the Playwright response-listener with a plain `requests` download loop.
- **Request delay.** `REQUEST_DELAY_SECONDS` (default 1.5 s) controls politeness. Do not lower it below 1.0 without understanding the 429 rate-limit behavior.
- **Page password.** `PAGE_PASSWORD = "EMILY"` in `crawler_config.py` unlocks password-protected posts. This is a plaintext credential — do not log it, print it in error messages, or surface it in server responses.

---

## Config changes (`crawler_config.py`)

- `SITE_DOMAIN` / `SITE_ORIGIN` — changing these invalidates all existing snapshots (asset URLs will mismatch).
- `ASSET_URL_PREFIX` — must stay `/_assets`; changing it breaks every blob path in every stored HTML page.
- `SERVER_PORT` — also update the systemd unit (`deploy/wayback-server.service`) and any firewall rules.
- `BLOCKED_DOMAINS` / `SCRIPT_BLOCK_PATTERNS` / `ELEMENT_REMOVE_SELECTORS` — safe to extend; removing entries will cause previously-stripped content to reappear in new snapshots only (old snapshots are unchanged).

---

## What NOT to commit

```
web_mirror/          # runtime data — blobs + snapshots, grows daily, not code
.venv/               # virtual environment
__pycache__/
*.pyc
crawl.log            # operational log written by cron
*.log
```

Commit: `site_crawler.py`, `wayback_server.py`, `crawler_config.py`, `deploy/`, `DEPLOYMENT.md`, `CLAUDE.md`, `.gitignore`.

---

## Security

- The server binds `0.0.0.0:8070` in production (systemd unit). This exposes the archive to the whole LAN. If that's too broad, bind `127.0.0.1` and use an SSH tunnel or reverse proxy with auth.
- `PAGE_PASSWORD` must not appear in logs, HTTP responses, or error messages.
- The systemd unit applies light sandboxing (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`). Do not weaken these without a reason.

---

## Operations quick-reference

```bash
# Start server (foreground)
.venv/bin/python wayback_server.py --host 0.0.0.0 --port 8070

# Crawl today
.venv/bin/python site_crawler.py

# Augment today's snapshot (add missed pages, no full re-crawl)
.venv/bin/python site_crawler.py --augment

# Service management
sudo systemctl restart wayback-server
journalctl -u wayback-server -f

# Check snapshot integrity
python3 -c "
import json, glob
for m in sorted(glob.glob('web_mirror/snapshots/*/manifest.json')):
    d = json.load(open(m))
    print(m, '->', len(d['pages']), 'pages,', len(d['assets']), 'assets')
"
```
