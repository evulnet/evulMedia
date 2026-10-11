# AGENTS.md

## Project mode: REAL

Public GitHub repo: `nkavassalis/evulMedia` (https://github.com/nkavassalis/evulMedia).
See "Guardrails" at the bottom before pushing anything.

## Purpose

evulMedia is a self-hosted, **read-only** web frontend for a media directory
tree (movies/TV/audio/images/PDFs/CBZ). The filesystem *is* the database: no
imports, no metadata, no user accounts. It exists to be "a really good
read-only frontend for a folder of media" — dramatically simpler than
Emby/Jellyfin. Keep that spirit in every change: fewer moving parts, not more.

## Layout

```
app.py                  the entire server (single file, ~1050 lines; see section below)
wsgi.py                 gunicorn entrypoint (calls create_app())
selftest.py             32-check smoke suite; read-only, safe to run on prod
templates/index.html    whole frontend: one HTML file, inline CSS/JS, no build step,
                        no frameworks, no CDN
tests/ui_stream_test.py Playwright (Chromium) UI test for stream seek/pause/seekable;
                        dev-only, self-contained, skips without ffmpeg/Chromium
env/*.env.example       per-instance config seeds (real *.env files are gitignored)
caddy/Caddyfile.example front-proxy config (TLS, auth, /files fast path)
systemd/evulmedia@.service  template unit; instance name = env/<name>.env basename
scripts/install.sh      installs to /opt/evulmedia (venv + unit); never touches env files
requirements.txt        Flask, Pillow, gunicorn (+ optional rarfile for RAR)
```

## How to run (dev)

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
MB_ROOT=/path/to/media MB_THUMBS=$PWD/data/thumbs/dev venv/bin/python app.py
# http://127.0.0.1:7081 — direct mode: app streams all bytes; the /files
# HMAC-token fast path and /authz only matter behind Caddy.
```

All config is environment variables (see README table). `MB_SECRET` may be
empty in dev; production requires it.

## How to run the tests

```bash
# 1) Smoke/logic suite — run against real env on deploys:
venv/bin/python selftest.py                       # must exit 0; 32 checks

# 2) Browser UI suite (dev only) — needs ffmpeg + `playwright install chromium`:
venv/bin/pip install playwright && venv/bin/playwright install chromium
venv/bin/python tests/ui_stream_test.py           # 17 checks; prints SKIP (exit 0) without deps

# 3) Lint: no configured linter; keep `python3 -m py_compile app.py selftest.py` clean
```

Both suites currently pass on Python 3.12 + Flask 3.x + ffmpeg 6.1 +
Chromium (headless Playwright). UI test generates its own throwaway media
tree in `tempfile` — never point dev servers or tests at someone's real library.

## Architecture notes (the non-obvious why)

- **App has NO auth.** Caddy sits in front (TLS + basic_auth or network
  isolation). Never expose the app port beyond localhost. Don't add
  login/session logic to app.py — delegation to the front proxy is a
  deliberate design pillar.
- **`/files/*` zero-copy path:** Caddy `file_server` serves bytes; access is
  gated by Caddy `forward_auth` → app `/authz`, which validates short-lived
  (1 h) HMAC-SHA256 tokens over `exp|relpath` minted by `/api/sign` with
  `MB_SECRET`. If you touch signing, keep `selftest.py`'s authz checks green.
- **Playback paths, cheapest first:** natively playable files → `/files`;
  browser-hostile containers → `/stream` (fragmented-MP4 pipe from ffmpeg,
  copy-first, no index); Safari-class → `/remux` (one-time faststart MP4
  cache, fully Range-seekable); exotic codecs → force transcode. On Safari
  the forced transcode is always **file-based** (`/remux?transcode=1`,
  detected client-side via `IS_SAFARI`) because Safari rejects the chunked
  live pipe entirely; other browsers use the live pipe (`/stream?transcode=1`).
  Frontend falls back `/stream → /remux → forced transcode` automatically
  on media errors.
- **Live-stream pseudo-seek:** `/stream?...&start=SECONDS` input-seeks
  (`-ss` *before* `-i`) so the output timeline restarts at 0; the frontend
  (`attachStreamControls` in index.html) tracks the content offset and
  fakes a seekable bar. Pause closes the pipe immediately (freeing a
  transcode slot) and shows a resume overlay — this is why pause no longer
  burns `MB_STALL_TIMEOUT`. Do not replace with `-copyts`/output seek
  without browser-testing all fallback paths.
- **Subtitles are opt-in** (`MB_SUBS=1`, default off): `/api/subs` lists
  embedded text streams (ffprobe) + sidecar files; `/subs` serves one track
  as WebVTT (ffmpeg conversion for embedded/ASS, cached under
  `remux_dir/subs`, 1 GB budget, atime GC — subtitle conversion is a
  one-shot `subprocess.run`, pool-free by design, like `ensure_remux`).
  The video pipeline itself never touches subs (`/stream` keeps `-sn`).
  Bitmap tracks (PGS/DVD) are skipped. Sidecar access via `/subs` is
  double-jail-checked: the key must match an enumerated sibling AND pass
  `safe_resolve`.
- **RAR/CBR** ride the same virtual-folder code as zip when `rarfile` is
  installed *and* an external tool (`unar`/`unrar`/`bsdtar`/`7z`) exists;
  `.rar/.cbr` degrade to plain files otherwise. `ARCHIVE_EXTS` is decided
  once at import time. `rarfile` is the one non-core dependency, import-
  guarded; removing it must keep the guarded-import contract.
- **Client preferences** live in `localStorage` only (never the server):
  `mbPbMode` (video playback: auto|seekable|transcode), `mbTheme`,
  `mbAutoplay`, `mbShowSize`, plus legacy `mbSeekable` (migrated as a
  fallback inside `pbMode()`). UI lives in the ⚙ settings popover; the
  settings gear is deliberately unreachable while the lightbox is open
  (maintainer-approved design: close the lightbox first, via Esc or ✕,
  to change playback mode).
- **TranscodePool** (app.py): flock-based slot files in `MB_SLOTS_DIR` cap
  concurrent ffmpeg across *all workers and instances*; every child runs in
  its own process group; a watchdog thread SIGKILLs groups whose generator
  stopped making progress (covers abandoned generators on client disconnect).
  Keep new subprocess spawns inside pool.register/retire discipline.
- **Caches are content-addressed and disposable:** thumbnails keyed
  `sha1(relpath|size|mtime)`, remuxes `sha1(fmt|root|rel|size|mtime_ns|transcode?)`
  (bump `REMUX_FMT` to invalidate all; the `transcode?` flag was added with
  the Safari file-transcode fix — old flag-less entries become orphans and
  are reaped by atime GC, which is the migration plan). Everything under
  `data/` can be deleted at any time. GC runs on create + every 6 h,
  atime-based.
- **Jail discipline:** all user paths flow through `safe_resolve()` /
  `split_archive()` — reject `..`, dotfiles, symlink escapes, archive-member
  traversal; zip entries size-capped (`read_member`), encrypted entries
  ignored. Any new route that takes a path **must** use these.

## Conventions

- Backend: stdlib first; dependencies beyond Flask/Pillow/gunicorn need a
  strong argument (`rarfile` is grandfathered: optional, import-guarded,
  feature-degrades without it). Keep app.py one file unless it genuinely
  hurts.
- Frontend: no build step, no frameworks, no CDN; keep everything inline in
  templates/index.html. Vanilla ES6, small DOM helpers, match existing style.
- Error pages/JSON: terse (`{"error": "not found"}`).
- Tests: new server logic → extend `selftest.py` (must stay prod-safe,
  read-only, fast); new player/UI behavior → extend
  `tests/ui_stream_test.py` (must skip cleanly without ffmpeg/Chromium).
- Commits: imperative, terse, one logical change each. Branch names
  `feat/*`, `fix/*`.
- Env var namespace is `MB_*`; document any new one in README's config table
  and app.py's module docstring.

## Known issues / TODOs

- Audio in non-native codecs can also go through `/stream` on some browsers;
  the frontend seek bar currently targets video stream mode only.
- `/stream` has no index: seeks land on keyframes; native scrubber is
  inactive during live mode (by design).
- `/stream` remains subject to `MB_STALL_TIMEOUT` for genuinely abandoned
  (not UI-paused) connections.
- Subtitle ASS/SSA styling is flattened to plain WebVTT; bitmap subs
  (PGS/DVD) are not offered at all. Only text codecs convert.
- `/subs` conversions are not TranscodePool-capped (short one-shots;
  revisit if abusive).
- Caddy-only deployment story (the `/files` fast path relies on
  `forward_auth` + `file_server`).

## Testing limitations / unverified (as of the stream-seek feature, 2026-10)

- Caddy `forward_auth` + `file_server` end-to-end was **not** exercised in
  CI (no Caddy installed on the dev box); auth contract is covered by
  `selftest.py`'s `/authz` checks via Flask test client only.
- Safari/WebKit fallback ladder (Safari-style rejection of chunked
  `/stream` → `/remux` auto-switch) was not tested with a real WebKit;
  the frontend error-fallback handlers were unchanged and unchanged code
  paths were regression-run only for Chromium.
- HEVC/`hvc1` tagging and hardware-decode behavior unverified (no HEVC
  test assets or Apple devices in CI).
- `scripts/install.sh` and the systemd units were not re-run/changed; they
  require a root Linux box to verify.
- RAR/CBR: listing, member serving, thumbnails, traversal blocking and
  graceful degradation were verified end-to-end with the `unar` backend
  (created via `rar`, read via rarfile+lsar/unar). The `unrar`/`bsdtar`/`7z`
  backends and password-protected RARs remain unverified.
- Subtitles verified for embedded SRT-in-MKV and sidecar SRT in headless
  Chromium and on Safari (macOS) through the self-rendered CC overlay —
  deliberately no reliance on native `<track>` menus (Safari does not list
  dynamically inserted tracks). ASS/SSA styling fidelity unverified.
- UI tests run headless Chromium only; mobile browsers untested.

## Do-not-touch list

- `LICENSE` (MIT), repo visibility (public), and the repo's GitHub location
  (`nkavassalis/evulMedia`).
- The auth-delegation model (app stays auth-free; Caddy owns auth/TLS).
- The jail checks in `safe_resolve`/`split_archive` — loosening any of them
  (dotfiles, `..`, symlink resolution, zip caps) is a security regression.
  `selftest.py` traversal checks encode the contract.
- Cache-key formats (`thumb_key`, remux hash) without a migration/GC plan.
- `env/*.env.example` placeholders — never replace with real values; real
  env files are gitignored and must stay out of the repo.
- Token TTL (1 h) and HMAC scheme without raising it explicitly.

## Guardrails in effect

- GitHub account: **nkavassalis**; verify with `gh auth status` before
  pushing (token lives in `gh`, never in the repo).
- Work on feature branches; open PRs; PRs must have `selftest.py` green
  (28+/28) and, for any player/UI change, `tests/ui_stream_test.py` green
  (or an explicit SKIP note plus manual verification described in the PR).
- Never push with failing checks; never force-push master; never commit
  anything resembling a secret, hostname, private IP, path under a real
  user's home directory, or the user's actual deployment topology — README,
  AGENTS.md, comments and examples use placeholders only (`example.com`,
  `change-me`, `/srv/media`).
- New dependencies and new network egress from the app require an explicit
  OK from the maintainer.
