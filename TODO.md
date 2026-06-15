# Project Skyscraper — TODO

Forward-looking work items. Tracked in git so they travel with the code.

## Password-protected pages — serve-time gating (deferred)

**Decided 2026-06-14:** public launch goes out with **no gate** — protected
posts are crawled (with `PAGE_PASSWORD`) and served as plain HTML like any other
page. Accepted for now.

**Future requirement:** if/when we keep password-protected pages as *protected*,
the archive must require the visitor to enter the correct password before the
protected content is shown (rather than baking the unlocked HTML into a public
blob).

This needs work on **both** programs, because today the crawler stores the
already-unlocked HTML and the server serves blobs directly with no gate:

- [ ] **Crawler:** mark which pages were password-protected. Add a `protected:
      true` (and/or which password group) flag to the page entry in
      `manifest.json`. Schema change must stay backwards-compatible (add key,
      never remove) per `CLAUDE.md` → *Manifest schema*.
- [ ] **Crawler:** decide storage model for protected pages — either store the
      *locked* shell + the unlocked content separately, or store unlocked but
      gate at serve time. (Storing unlocked-but-gated still means the plaintext
      sits in the blob store; consider whether that is acceptable.)
- [ ] **Server:** add a serve-time password challenge for pages flagged
      `protected`. Must work **without JavaScript** (server rule in `CLAUDE.md`)
      — e.g. a POST form that sets a signed cookie, server-side verified.
      Note: this is the first server-side *write*/session concept; reconcile
      with the "server is read-only / no mutate" rule.
- [ ] Re-crawl after the schema change so existing snapshots carry the flag
      (old snapshots predate it; server must tolerate its absence).

## Secrets out of source

- [x] `PAGE_PASSWORD = "EMILY"` is intentionally in source — it is the public
      answer to the site's ARG riddle, not a real credential. Repo is public.
      (See `OPERATIONS.local.md` §Secrets posture for full rationale.)
- [x] `ARCHIVE_PASSWORD` (visitor gate) lives in `.env`, gitignored. ✓

## Deploy via GitHub

- [x] Decided 2026-06-14: use a **public** GitHub repo (`GoodGuysFree/skyscraper`)
      as the deploy channel (push from dev → `git pull` on VPS → `systemctl restart`).
      Data (`web_mirror/`) stays out of git. `DEPLOYMENT.md` scrubbed from history.
- [x] Repo created, remote set as `origin`, VPS pulls on deploy. ✓
