# evulMedia

A self-hosted web **media library browser** for one (or more) directory
trees: folder browsing, on-the-fly thumbnails, in-browser playback of
movies/TV/audio/images/PDFs/CBZ galleries — with browser-smart streaming and
zero database. Light weight and simple. Includes Caddy deployment examples
for public and private style shares to your local network or delivered to
remote clients over WireGuard/Tailscale/etc.

Think: *a really good read-only frontend for a folder of media*. Host it
on a server with SMB/NFS mounts to your NAS, or directly on your NAS! No
metadata scraping, no transcoding presets, no user accounts, no library
"import". You already have the files; evulMedia lets you (and the people on
your network) find and watch them in a browser tab.

```
/srv/media/TV                /srv/media/Movies          /srv/media/books
├── Some Show S01/           ├── Great Film.mkv         └── Comic.cbz
│   ├── S01E01.mkv           ├── Another Movie.mp4          └─ page-001.jpg
│   └── ...                                                            ...
```

Every box above is browsable, streamable and shareable via URL.

## Why this exists instead of Emby/Jellyfin/…

- **Nothing to import.** The tree on disk *is* the library. New files show
  up within minutes (mtime-checked listing cache), deleted ones vanish.
- **Nothing to break.** Single Python file + templates, stdlib + Flask +
  Pillow. No database, no daemon-written metadata, no sidecar files.
- **It plays the awkward stuff** in any browser — see "Playback" below.
- **Multiple trees, mixed trust:** one app, N instances (systemd template);
  auth and TLS are delegated to Caddy, which also serves the bytes fast.

## Features

- Dark/light UI, folder cards, lazy thumbnails, client-side filter,
  breadcrumbs, `←/→` media stepping, `Esc` to close.
- **Permalinks:** every folder (`/browse/...`) and every media item
  (`/media/...`) has a hard-loadable URL — share it, bookmark it.
- **Thumbnails** for images and videos, generated lazily (ffmpeg frame
  grab) and content-addressed on disk (`sha1(relpath|size|mtime)`), so
  edits self-invalidate. CBZ/ZIP member thumbnails too.
- **Playback that works everywhere** (see matrix below).
- **CBZ/ZIP galleries** as virtual folders (in-memory, never extracted;
  zip-bomb entry cap included).
- **Audio** plays inline (album/audiobook stepping with arrow keys).
- **PDFs** open in a near-fullscreen native viewer.
- **Filenames-only cache**: a directory walk never blocks a request;
  listings revalidate on directory mtime or TTL (default 5 min).
- **Machine-global transcode pool** (default 2 concurrent ffmpeg streams)
  with process-group kill, disconnect cleanup, and a stall watchdog, so
  transcoding can't fork-bomb your server.
- **Self-expiring caches**: remux and thumbnail caches are LRU + age GC'd
  on service start and every 6 h.
- **Self-test suite** (`selftest.py`) that deploys can run as a smoke test.

## Requirements

- Linux (any recent), Python **3.11+**
- `ffmpeg` + `ffprobe` on PATH (video thumbnails, /stream, /remux)
- [Caddy](https://caddyserver.com) **v2.6+** as the front (TLS + auth +
  the fast `/files` path). nginx is *not* supported — the /files
  fast-path relies on Caddy's `forward_auth` + `file_server`.
- A media tree on a local (or fast) filesystem, mounted **without
  `noatime`/`relatime`-hostile options** if you want cache expiry to work
  (any default mount is fine).

## Quick start

```bash
git clone <this repo> && cd evulMedia
sudo ./scripts/install.sh
sudoedit /opt/evulmedia/env/media.env      # set MB_ROOT + MB_SECRET (openssl rand -hex 32)
sudo systemctl restart 'evulmedia@media'
# wire up HTTPS: see caddy/Caddyfile.example (hosts, roots, TLS)
curl -k https://media.example.com:8443/api/ls
```

Run `sudo /opt/evulmedia/venv/bin/python /opt/evulmedia/selftest.py`
(loads env itself) as a smoke test; it exits non-zero on any failure.

### Dev without Caddy/systemd

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
MB_ROOT=/path/to/media MB_THUMBS=$PWD/data/thumbs/dev venv/bin/python app.py
# browse http://127.0.0.1:7081  (direct mode: bytes are app-streamed;
# /files token fast-path requires the Caddy front)
```

## How playback works (and why each path exists)

| File                          | Path served                                   | Notes |
|-------------------------------|-----------------------------------------------|-------|
| images, native audio/mp4/webm/mov, PDFs | `/files/...` (Caddy `file_server`, HMAC-tokened) | zero-copy, real `Range`/seek |
| `mkv`, `avi`, `ts`, wmv, … (Chrome-class) | `/stream` — ffmpeg pipes **fragmented MP4** | video `-c copy` when h264/HEVC/VP9/AV1; audio→AAC when needed; full h264 transcode last resort |
| same files (Safari-class)     | `/remux` — **one-time MKV→faststart-MP4** cache | copy, ~5 s/episode, then instant + seekable |
| unsupported audio (e.g. FLAC on Safari) | `/stream` audio-only AAC                     | |
| exotic/old codecs             | `/stream` h264 transcode, or the "transcode playback" link | |

The player auto-falls-back `/stream → /remux → forced transcode`, so one
click usually finds the working path for your browser. HEVC is tagged
`hvc1` so Apple devices decode it. Everything the browser can play itself
never touches ffmpeg.

**Why so many paths:** browsers (not servers) decide what they decode.
Safari refuses chunked range-less streams; Chrome can't decode HEVC
without hardware; nobody plays `.mkv`/`.avi` containers at all. evulMedia
picks the cheapest transport that satisfies each browser instead of
pre-transcoding your library.

## Security model

- **The app itself has no auth.** Put Caddy (TLS + whatever auth you want)
  in front and never expose the app port beyond localhost. The example
  Caddyfile shows one open-on-LAN instance and one basic-auth instance.
- `/files/*` (the raw-file fast path) requires a per-path **HMAC token**
  (1 h TTL, key = `MB_SECRET`); the app validates it via Caddy
  `forward_auth`. Forged/expired/unsigned requests are 403'd by Caddy.
- Path handling is jail-checked (`..`, dotfiles, symlink escapes, and
  archive-member traversal are refused); zip entries are size-capped and
  encrypted entries ignored.
- Thumbnails/remuxes are written **world-readable** in the cache dir by
  design (anyone who can fetch the media can derive the key); restrict the
  data dir's unix mode if that bothers you.

## Configuration reference

All config is env vars (per-instance files in `env/`):

| Var | Default | Meaning |
|-----|---------|---------|
| `MB_ROOT` | — | directory to expose (required) |
| `MB_NAME` | dir name | display name |
| `MB_PORT` | 7081 | app listen port (127.0.0.1) |
| `MB_THUMBS` | — | writable thumbnail cache dir (required) |
| `MB_SECRET` | — | HMAC key for tokens + /authz (required in prod) |
| `MB_SCAN_INTERVAL` | 300 | listing revalidation seconds |
| `MB_MAX_TRANSCODES` | 2 | machine-global concurrent ffmpeg streams |
| `MB_STALL_TIMEOUT` | 300 | kill stalled streams after N s (also limits how long a paused stream survives) |
| `MB_REMUX_DIR` / `MB_REMUX_MAX_GB` / `MB_REMUX_MAX_AGE_DAYS` | `../remux` / 50 / 14 | remux cache location/budget/expiry |
| `MB_THUMB_MAX_GB` / `MB_THUMB_MAX_AGE_DAYS` | 2 / 30 | thumb cache budget/expiry (atime-based) |
| `MB_SLOTS_DIR` | `../slots` | transcode lock dir (shared across instances) |

Endpoints worth knowing: `/api/ls?path=`, `/api/sign?path=[&mode=stream|mp4]`,
`/authz` (Caddy subrequest), `/files/*` (Caddy), `/stream`, `/remux`,
`/thumb`, `/afile` (archive members/docs), `/dl`, `/view`, `/browse/<path>`,
`/media/<path>`.

## Operations

- Logs: journald (`journalctl -u evulmedia@media`); gunicorn access log is
  on, with duration + bytes — failures are diagnosable from it.
- Wipe caches any time: `rm -f /opt/evulmedia/data/remux/*.mp4` /
  `rm -rf /opt/evulmedia/data/thumbs/*` — they rebuild themselves.
- Multiple trees = multiple `env/*.env` files; `systemctl enable
  evulmedia@<name>` per instance. Locks are shared, so the transcode cap
  holds across instances.
- Listing freshness: a file landing in a watched directory appears within
  `MB_SCAN_INTERVAL` seconds (or immediately on the ⟳ force-rescan).
- Seeking: `/stream` is a live pipe (no seeking); seekable playback of
  weird containers comes from `/remux` (Safari gets it automatically).

## Repository layout

```
app.py                  the whole server
wsgi.py                 gunicorn entrypoint
selftest.py             22-check smoke suite (read-only, safe on prod)
templates/index.html    single-page frontend (no build step, no CDN)
systemd/evulmedia@.service
env/*.env.example       per-instance config seeds
caddy/Caddyfile.example front proxy config
scripts/install.sh      installer (/opt/evulmedia + venv + unit)
```

## Limitations (honest list)

- No upload/delete/edit — deliberately read-only.
- No multi-user watch state, no metadata enrichment, no subtitles burn-in
  (embedded subtitle tracks are ignored via `-sn`; sidecar subs ignored).
- `/stream` has no seeking and dies after `MB_STALL_TIMEOUT` of
  non-consumption (that includes long pauses).
- PDF/CBZ viewers use the browser's own renderer (very good, not perfect).
- Caddy-only. If you love nginx: the `/files` fast-path needs
  `auth_request`-equivalent work; PRs welcome.
- Very large directories (>10k entries) are served fine but render a lot
  of DOM; the filter box is your friend.

## License

MIT — see [LICENSE](LICENSE).
