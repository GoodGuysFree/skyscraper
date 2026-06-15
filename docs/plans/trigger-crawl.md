# Trigger-Crawl API — Design Plan

External webhook that lets Ekimo's tracker bot tell the wayback server
to start a new crawl. Fire-and-forget for the caller; coalesced and
rate-limited on our side.

---

## Endpoint

```
POST /~api/trigger-crawl
```

Hosted on the existing wayback server (already behind Caddy TLS).  
Returns `202 Accepted` immediately — caller never waits for the crawl.

---

## Authentication — HMAC-SHA256 Signatures

The shared secret **never travels over the wire**. Instead, the caller
signs each request and we verify the signature.

### Request headers

| Header | Value |
|--------|-------|
| `X-Timestamp` | Unix epoch seconds (UTC), as a string |
| `X-Signature` | `sha256=<hex digest>` |
| `Content-Type` | `application/json` (body can be `{}` or empty) |

### Signature construction

```
message  = timestamp_string + "." + request_body
signature = HMAC-SHA256(secret, message)
header    = "sha256=" + hex(signature)
```

Where `timestamp_string` is the exact string in `X-Timestamp`.

### Server verification

1. Reject if `X-Timestamp` is missing or not a valid integer → `400`
2. Reject if `|now - timestamp| > 300` seconds → `401` (replay protection)
3. Recompute `HMAC-SHA256(secret, timestamp + "." + body)`
4. Compare with `X-Signature` using constant-time comparison → `401` on mismatch
5. Accept → `202`

### Secret storage

- Server side: `TRIGGER_TOKEN=<hex>` in `.env` (git-ignored), loaded at startup alongside `ARCHIVE_PASSWORD`
- Caller side: stored securely in Ekimo's bot config
- Rotation: update `.env`, restart the wayback server

---

## Coalescing & Rate-Limiting — `CrawlScheduler`

A background thread in the server process manages crawl timing.  
Two config knobs in `crawler_config.py`:

```python
TRIGGER_DEBOUNCE_SECONDS = 180   # wait after last trigger before starting crawl
TRIGGER_COOLDOWN_SECONDS = 300   # mandatory rest after crawl finishes
```

### State

```python
_debounce_timer   # threading.Timer | None
_crawl_thread     # Thread (wraps subprocess) | None
_queued           # bool — at most one pending re-crawl
_lock             # threading.Lock
```

### Logic

**On trigger received:**
- Crawl running → set `_queued = True`, return. Subsequent triggers while
  running are no-ops (already queued).
- Not running → cancel + restart debounce timer. Rapid triggers reset the
  clock; only one crawl fires when the dust settles.

**When debounce fires:**
- Spawn crawl thread → `subprocess.run(site_crawler.py)` appending to `crawl.log`

**When crawl thread exits:**
- Sleep `COOLDOWN_SECONDS`
- If `_queued`: clear flag, restart debounce timer (crawl will follow after debounce)
- Else: go idle

### State diagram

```
IDLE ──[trigger]──► DEBOUNCING ──[timer fires]──► RUNNING ──[done + cooldown]──► IDLE
                        │ [trigger]                   │ [trigger]                    │
                    (reset timer)               _queued = True                [queued?]
                                                                                     │
                                                              DEBOUNCING ◄───────────┘
```

---

## Implementation Notes

### Where code lives
- `CrawlScheduler` class in `wayback_server.py`
- `_load_trigger_token()` alongside `_load_gate_password()`
- `do_POST` extended: `/~gate` (existing) + `/~api/trigger-crawl` (new)
- `TRIGGER_DEBOUNCE_SECONDS`, `TRIGGER_COOLDOWN_SECONDS` in `crawler_config.py`
- `TRIGGER_TOKEN=<hex>` in `.env`

### Subprocess environment
The crawl subprocess needs `SKYSCRAPER_HOME` set so `crawler_config.py`
resolves paths correctly. The systemd unit already sets this env var —
inherit it via `env=os.environ` in `subprocess.run`.

### Logging
Trigger events (received, accepted, queued, debounce fired, crawl
started/finished) logged to stdout (captured by `journalctl`).

### Security properties
- Secret never in transit — only HMAC digest sent
- Replay window: ±5 minutes (300 s timestamp check)
- Constant-time comparison prevents timing attacks on signature check
- Worst-case abuse: extra crawls triggered (no data access, no writes via server)
- Token rotation requires only `.env` edit + server restart

---

## Testing

New unit tests needed:
- `verify_trigger_signature(secret, timestamp, body, header)` — valid, wrong sig, expired timestamp, future timestamp
- `CrawlScheduler` state transitions (mock subprocess + time)
