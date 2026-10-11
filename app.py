#!/usr/bin/env python3
"""
evulMedia - a no-nonsense media library browser for one directory tree.

One Flask app, deployable multiple times (one instance per media tree) via
the systemd template unit; authentication is delegated to Caddy in front.

Config is env-only:
  MB_ROOT               (required) directory to expose
  MB_NAME               display name (default: root directory name)
  MB_THUMBS             (required) writable dir for cached thumbnails
  MB_SECRET             HMAC key for short-lived media tokens (required for
                        Caddy's /files fast path; also enables /authz)
  MB_SCAN_INTERVAL      dir-listing revalidation seconds         (default 300)
  MB_SLOTS_DIR          transcode slot lock dir
  MB_REMUX_DIR          remux cache dir
  MB_REMUX_MAX_GB       remux cache size budget                  (default 50)
  MB_REMUX_MAX_AGE_DAYS remux cache expiry by last access        (default 14)
  MB_THUMB_MAX_GB       thumbnail cache size budget               (default 2)
  MB_THUMB_MAX_AGE_DAYS thumbnail expiry by last access           (default 30)
  MB_MAX_TRANSCODES     concurrent ffmpeg streams, machine-global (default 2)
  MB_STALL_TIMEOUT      kill stalled streams after N seconds      (default 300)
  MB_SUBS               1 = enable subtitle discovery + WebVTT conversion
  MB_PORT               dev-server port for `python app.py`

Media delivery (fast path): video/audio/image bytes are served by Caddy's
own file_server at /files/* (zero-copy + correct Range), gated by
forward_auth subrequests to /authz which validate short-lived HMAC tokens
minted by /api/sign. Browser-hostile containers (mkv/avi/ts/...) go to
/stream (ffmpeg remux pipe) or /remux (seekable faststart MP4 cache, for
Safari-class browsers). CBZ/ZIP archives render as virtual folders.

Thumbnails are cached on disk, keyed by sha1(relpath|size|mtime), so edited
or replaced media get a new cache key for free.
"""

import fcntl
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import signal
import subprocess
import shutil
import tempfile
import threading
import time
from logging import getLogger

_log = getLogger("evulmedia")
import urllib.parse
import zipfile
from pathlib import Path

try:                              # optional RAR backend (needs an external tool)
    import rarfile                # pip install rarfile
except ImportError:               # noqa: C401
    rarfile = None

from werkzeug.exceptions import HTTPException
from PIL import Image, ImageOps
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/x-matroska", ".mka")
mimetypes.add_type("video/x-msvideo", ".avi")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/mp2t", ".m2ts")
mimetypes.add_type("video/x-flv", ".flv")
mimetypes.add_type("video/mpeg", ".mpg")
mimetypes.add_type("video/mpeg", ".mpeg")
mimetypes.add_type("video/3gpp", ".3gp")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tiff"}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".ts", ".m2ts",
    ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ogv",
}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav",
              ".flac", ".mka", ".wma"}
ARCHIVE_EXTS = {".cbz", ".zip"}
if rarfile and any(shutil.which(t) for t in ("unar", "lsar", "unrar", "bsdtar", "7z", "7za")):
    ARCHIVE_EXTS |= {".rar", ".cbr"}     # RAR browsing enabled (external tool found)

SIDECAR_SUB_EXTS = {".srt", ".vtt", ".ass", ".sub"}
SUBS_ENABLED = os.environ.get("MB_SUBS") == "1"   # opt-in; ffmpeg-converted WebVTT
SUB_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt"}
NATIVE_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav"}
DOC_EXTS = {".nfo", ".txt", ".srt", ".ass", ".sub", ".vtt", ".url", ".m3u"}

# Limit concurrent ffmpeg/Pillow work per worker process.
_thumb_semaphore = threading.Semaphore(2)

# path -> (dir_mtime, fetched_at, payload)
_ls_cache = {}
_ls_lock = threading.Lock()


def natural_key(s):
    parts = re.split(r"(\d+)", s.lower())
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts if p != "")


def safe_resolve(root, rel):
    """Resolve a user-supplied relative path inside root, or abort(404)."""
    if rel.startswith("/") or "\x00" in rel:
        abort(404)
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        abort(404)
    target = (root / Path(*parts)).resolve() if parts else root.resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        abort(404)
    if not target.exists():
        abort(404)
    return target


def classify(ext):
    ext = ext.lower()
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext == ".pdf":
        return "pdf"
    if ext in DOC_EXTS:
        return "doc"
    return "file"


def split_archive(root, rel):
    """Split 'books/x.cbz/page.jpg' into (archive_path, member, archive_rel).
    Returns (None, None, None) for ordinary paths. Abort-safe."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        abort(404)
    for i, p in enumerate(parts):
        if Path(p).suffix.lower() in ARCHIVE_EXTS and i < len(parts) - 1:
            apath = (root / Path(*parts[:i + 1])).resolve()
            try:
                apath.relative_to(root.resolve())
            except ValueError:
                abort(404)
            if not apath.is_file():
                abort(404)
            member = "/".join(parts[i + 1:])
            if ".." in member.split("/"):
                abort(404)
            return apath, member, "/".join(parts[:i + 1])
    return None, None, None


def open_archive(path):
    """Open a zip-family or RAR archive with a common (zipfile-like) API."""
    if rarfile and Path(path).suffix.lower() in (".rar", ".cbr"):
        return rarfile.RarFile(path)
    return zipfile.ZipFile(path)


def read_member(apath, member, max_size=512 * 1024 * 1024):
    """Read one archive entry into memory with a zip-bomb guard."""
    try:
        with open_archive(apath) as zf:
            info = zf.getinfo(member)
            encrypted = getattr(info, "flag_bits", 0) & 0x1 \
                or getattr(info, "encrypted", False)
            np = getattr(info, "needs_password", False)   # rarfile: method, not property
            if callable(np):
                np = np()
            if info.is_dir() or encrypted or np:
                abort(404)                      # dirs / encrypted entries
            if info.file_size > max_size:
                abort(413)
            return zf.read(info), info
    except KeyError:
        abort(404)
    except (zipfile.BadZipFile, OSError):
        abort(404)
    except HTTPException:
        raise                       # keep 413/404 semantics set above
    except Exception:
        abort(404)          # rarfile raises a zoo of tool-specific errors


# ---------------------------------------------------------------------------
# Signed media URLs (Caddy file_server + forward_auth pattern)
# ---------------------------------------------------------------------------

TOKEN_TTL = 3600
NATIVE_VIDEO_EXTS = {".mp4", ".m4v", ".webm"}          # browsers can play these directly
COPY_VIDEO_CODECS = {"h264", "hevc", "vp8", "vp9", "av1"}   # HEVC copies too; players that can't HW-decode fall back client-side
COPY_AUDIO_CODECS = {"aac", "mp3", "opus"}   # ac3/eac3 stay in → audio-only transcode (video still copies)


class Signer:
    """Short-lived HMAC grants for Caddy-served /files/<rel> URLs."""

    def __init__(self, secret):
        self.secret = secret.encode()

    def sign(self, rel):
        exp = int(time.time()) + TOKEN_TTL
        sig = self._sig(rel, exp)
        return exp, sig

    def _sig(self, rel, exp):
        return hmac.new(self.secret, f"{exp}|{rel}".encode(), hashlib.sha256).hexdigest()

    def verify(self, rel, exp_raw, sig):
        try:
            exp = int(exp_raw)
        except (TypeError, ValueError):
            return False
        if exp < time.time():
            return False
        return hmac.compare_digest(self._sig(rel, exp), sig or "")


_qsv_cache = None


def qsv_available():
    """True if Intel Quick Sync can actually open an encode session
    (iGPU present AND the /dev/dri render node is accessible). Probed once,
    lazily; everything falls back to libx264 when False."""
    global _qsv_cache
    if _qsv_cache is None:
        _qsv_cache = False
        import tempfile as _tf
        fd, probe_out = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "color=black:s=64x64:d=0.1", "-c:v", "h264_qsv",
                 "-f", "mp4", "-y", probe_out],
                capture_output=True, timeout=20)
            _qsv_cache = r.returncode == 0
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            try:
                os.unlink(probe_out)
            except OSError:
                pass
    return _qsv_cache


def qsv_video_args():
    return ["-c:v", "h264_qsv", "-global_quality", "23", "-look_ahead", "0"]


def x264_video_args():
    return ["-c:v", "libx264", "-preset", "superfast", "-crf", "23",
            "-vf", "scale='min(1920,iw)':-2", "-pix_fmt", "yuv420p"]


def probe(streams, source):
    """Run ffprobe and return the list of stream dicts."""
    try:
        result = subprocess.run(
            streams + ["-show_entries", "stream=codec_type,codec_name",
                       "-of", "json", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout).get("streams", [])
    except (subprocess.SubprocessError, ValueError):
        return []


def probe_duration(source):
    """Container duration in seconds, or 0.0 when unknown/unavailable."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        return max(0.0, float(result.stdout.strip()))
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def plan_from_probe(probed, force_transcode=False):
    """Decide how to feed this file to a browser from ffprobe stream data:
    'copy', 'transcode-audio', or 'transcode'. Copy-first always; re-encode
    only when codecs demand it or the player forced it (?transcode=1)."""
    if force_transcode:
        return "transcode"
    vcodec = next((s["codec_name"] for s in probed if s["codec_type"] == "video"), None)
    acodec = next((s["codec_name"] for s in probed if s["codec_type"] == "audio"), None)

    if vcodec is None:                     # audio-only container
        if acodec is None:
            return "transcode"
        return "copy" if acodec in COPY_AUDIO_CODECS else "transcode-audio"
    if vcodec not in COPY_VIDEO_CODECS:
        return "transcode"          # mpeg4/etc — browser can't decode
    if acodec is None:
        return "copy"
    if acodec in COPY_AUDIO_CODECS:
        return "copy"
    return "transcode-audio"


def build_stream_cmd(mode, source, has_audio, has_video=True, start=0.0,
                  video_enc=None):
    """Build the ffmpeg fMP4 pipe command. `start` is an input-side seek
    (-ss before -i: fast keyframe seek); the output timeline restarts at 0,
    so clients must track the content offset themselves."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-fflags", "+genpts",
    ]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if has_video:
        cmd += ["-map", "0:v:0"]
    else:
        cmd += ["-vn"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-an"]
    cmd += ["-sn"]

    if not has_video and mode == "transcode":
        mode = "transcode-audio"      # audio-only: nothing to scale/encode

    if mode == "copy":
        cmd += ["-c", "copy", "-b:v", "0"]
    elif mode == "transcode-audio":
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += (video_enc or
                ["-c:v", "libx264", "-preset", "veryfast", "-tune", "fastdecode",
                 "-crf", "23", "-vf", "scale='min(1920,iw)':-2",
                 "-pix_fmt", "yuv420p"])
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-max_muxing_queue_size", "4096",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]
    return cmd


class TranscodePool:
    """Global cap + lifecycle guarantee for /stream ffmpeg jobs.

    - Concurrency: flock'd slot files, so the cap holds across gunicorn
      workers AND both app instances (slot dir is shared).
    - Lifecycle: every child runs in its own process group; a watchdog
      thread reaps exited jobs and SIGKILLs the group of any job that has
      not made progress for `stall_seconds` — that covers the case where
      WSGI abandons the response generator without closing it (client
      vanished), which is how orphaned ffmpegs used to pile up.
    """

    def __init__(self, slots_dir, max_jobs, stall_seconds):
        os.makedirs(slots_dir, exist_ok=True)
        self.slots = [os.path.join(slots_dir, f"job{i}.lock") for i in range(max_jobs)]
        self.stall_seconds = stall_seconds
        self._live = {}          # proc -> [last_progress_ts]
        self._lock = threading.Lock()
        threading.Thread(target=self._monitor, daemon=True).start()

    def acquire(self, wait_seconds=20):
        """Grab a slot, polling up to wait_seconds (transient contention from
        a just-failed playback attempt should not surface as 503)."""
        deadline = time.time() + wait_seconds
        while True:
            for path in self.slots:
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    os.close(fd)
            if time.time() >= deadline:
                return None
            time.sleep(0.5)

    @staticmethod
    def release(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def register(self, proc):
        with self._lock:
            self._live[proc] = [time.time()]

    def touch(self, proc):
        with self._lock:
            entry = self._live.get(proc)
            if entry is not None:
                entry[0] = time.time()

    def retire(self, proc):
        with self._lock:
            self._live.pop(proc, None)
        self._kill(proc)

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # process group, not just ffmpeg
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _monitor(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self._lock:
                items = list(self._live.items())
            for proc, entry in items:
                if proc.poll() is not None:            # exited: reap zombie
                    with self._lock:
                        self._live.pop(proc, None)
                    try:
                        proc.wait()
                    except Exception:
                        pass
                elif now - entry[0] > self.stall_seconds:  # abandoned/paused stream
                    self._kill(proc)
                    with self._lock:
                        self._live.pop(proc, None)


# ---------------------------------------------------------------------------
# One-time MKV->MP4 remux cache (Safari needs seekable, length-known MP4)
# ---------------------------------------------------------------------------

REMUX_FMT = "hvc1"   # bump to invalidate all remux caches after muxer fixes

def probe_lang(source):
    """ffprobe JSON of all streams (codec_type/name/tags); [] on any failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout).get("streams", [])
    except (subprocess.SubprocessError, ValueError):
        return []


def sidecar_subs(video_path):
    """Subtitle files next to the video sharing its stem: movie.en.srt etc."""
    out = []
    stem = video_path.name
    for ext in VIDEO_EXTS | {".m4v"}:
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    try:
        for de in video_path.parent.iterdir():
            if not de.is_file() or de.suffix.lower() not in SIDECAR_SUB_EXTS:
                continue
            if de.name.lower().startswith(stem.lower() + "."):
                lang = de.stem[len(stem):].strip("._-") or ""
                out.append((de.name, lang))
    except OSError:
        pass
    return out


def srt_to_vtt(data):
    """Minimal in-memory SRT -> WebVTT (timestamp comma -> dot + header)."""
    for enc in ("utf-8", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return ("WEBVTT\n\n" + text.lstrip("\ufeff")).encode("utf-8")


def extract_vtt(source, stream_id, cache_path, timeout=120):
    """Convert any subtitle source/stream to a cached WebVTT file via ffmpeg."""
    dest = str(cache_path) + ".part"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i",
           str(source), "-map", stream_id, "-c:s", "webvtt",
           "-f", "webvtt", "-y", dest]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        os.replace(dest, cache_path)
    except Exception:
        try:
            os.unlink(dest)
        except OSError:
            pass
        abort(502)


def remux_cache_path(root, remux_dir, target, rel, force_transcode=False):
    """Cache destination for one remux build (content-keyed on root/rel/
    size/mtime/mode). Returns (path, stat)."""
    st = target.stat()
    h = hashlib.sha1(
        b"%s\0%s\0%s\0%d\0%d\0%d" % (REMUX_FMT.encode(), str(root).encode(),
                             rel.encode("utf-8", "surrogateescape"),
                             st.st_size, st.st_mtime_ns,
                             1 if force_transcode else 0)
    ).hexdigest()
    return Path(remux_dir) / (h + ".mp4"), st


def ensure_remux(root, remux_dir, max_gb, max_age_days, target, rel,
                 force_transcode=False):
    """Remux (stream-copy when possible, or fully re-encode when
    force_transcode) into a faststart MP4 cache file and return its path.
    Blocking; typically a few seconds for one episode."""
    remux_dir = Path(remux_dir)
    remux_dir.mkdir(parents=True, exist_ok=True)
    dest, st = remux_cache_path(root, remux_dir, target, rel, force_transcode)
    if dest.is_file():
        os.utime(dest, None)
        return dest

    lock_path = remux_dir / (dest.name[:-4] + ".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        if dest.is_file():          # another worker won the race while we waited
            os.utime(dest, None)
            return dest

        probed = probe(["ffprobe", "-v", "error"], target)
        vcodec = next((s["codec_name"] for s in probed if s["codec_type"] == "video"), None)
        acodec = next((s["codec_name"] for s in probed if s["codec_type"] == "audio"), None)
        has_audio = acodec is not None

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(target), "-map", "0:v:0"]
        cmd += ["-map", "0:a:0", "-dn"] if has_audio else ["-an"]
        if force_transcode:
            cmd += qsv_video_args() if qsv_available() else x264_video_args()
        elif vcodec in ("h264", "hevc", "vp9", "av1"):
            cmd += ["-c:v", "copy"]
            if vcodec == "hevc":
                cmd += ["-tag:v", "hvc1"]   # Apple needs hvc1, not the default hev1
        else:
            cmd += qsv_video_args() if qsv_available() else x264_video_args()
        if has_audio:
            cmd += ["-c:a", "copy"] if acodec in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-f", "mp4", "-movflags", "+faststart", "-y", str(dest) + ".part"]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
            os.replace(str(dest) + ".part", dest)
        except subprocess.CalledProcessError as exc:
            _log.warning("remux build failed for %s: %s", rel,
                         (exc.stderr or b"")[-600:].decode("utf-8", "replace"))
            try:
                os.unlink(str(dest) + ".part")
            except OSError:
                pass
            raise
        except Exception:
            try:
                os.unlink(str(dest) + ".part")
            except OSError:
                pass
            raise

        _remux_gc(remux_dir, max_gb, max_age_days)
        os.utime(dest, None)
        return dest


def _thumb_gc(thumb_dir, max_gb, max_age_days):
    """Expire/trim thumbnail cache (incl. CBZ/zip member thumbs). Keys embed
    source stat info, so orphans from deleted/changed files simply stop
    being read — atime expiry collects them. Runs periodically, not per-
    thumbnail (dir can hold tens of thousands of entries)."""
    max_bytes = max_gb * (1024 ** 3)
    now = time.time()
    files, total = [], 0
    try:
        for root, _dirs, names in os.walk(thumb_dir):
            if os.path.basename(root) == "slots":
                continue
            for n in names:
                if not n.endswith(".jpg"):
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_atime, st.st_size, p))
                total += st.st_size
    except OSError:
        return

    kept = []
    for atime, size, p in files:
        if max_age_days and now - atime > max_age_days * 86400:
            try:
                os.unlink(p)
                total -= size
            except OSError:
                pass
        else:
            kept.append((atime, size, p))
    for atime, size, p in sorted(kept):
        if total <= max_bytes:
            break
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass


def _remux_gc(remux_dir, max_gb, max_age_days=0):
    """Drop cache entries older than max_age_days (0 = no expiry, atime-based),
    then LRU-trim by atime down to the size budget."""
    max_bytes = max_gb * (1024 ** 3)
    now = time.time()
    files = []
    try:
        for de in os.scandir(remux_dir):
            if de.name.endswith((".mp4", ".vtt")) and de.is_file():
                st = de.stat()
                files.append((st.st_atime, st.st_size, de.path))
    except OSError:
        return

    kept = []
    for atime, size, path in files:
        if max_age_days and now - atime > max_age_days * 86400:
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            kept.append((atime, size, path))

    total = sum(f[1] for f in kept)
    for atime, size, path in sorted(kept):
        if total <= max_bytes:
            break
        try:
            os.unlink(path)
            total -= size
        except OSError:
            pass

    # Drop lock files whose media file is gone (orphans from GC'd entries).
    try:
        for de in os.scandir(remux_dir):
            if de.name.endswith(".lock") and not os.path.exists(
                os.path.join(remux_dir, de.name[:-5] + ".mp4")
            ):
                try:
                    os.unlink(de.path)
                except OSError:
                    pass
    except OSError:
        pass


def list_archive(target, rel):
    """Virtual listing for .cbz/.zip/.rar galleries (image/audio/video/pdf files)."""
    try:
        zf = open_archive(target)
    except Exception:
        abort(404)

    files = []
    with zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/") or ".." in name.split("/") or info.is_dir():
                continue
            base = name.rsplit("/", 1)[-1]
            if name.startswith("__MACOSX/") or base.startswith("."):
                continue
            kind = classify(Path(base).suffix)
            if kind not in ("image", "audio", "video", "pdf"):
                continue
            try:
                mtime = int(time.mktime(info.date_time + (0, 0, -1)))
            except (ValueError, OverflowError):
                mtime = 0
            files.append({
                "name": base if name.count("/") == 0 else name,  # keep subpaths unique
                "kind": kind,
                "size": info.file_size,
                "mtime": mtime,
                "thumb": kind == "image",
            })

    files.sort(key=lambda f: natural_key(f["name"]))
    parts = [p for p in rel.strip("/").split("/") if p]
    return {
        "path": "/".join(parts),
        "parent": "/".join(parts[:-1]) if parts else None,
        "breadcrumbs": [
            {"path": "/".join(parts[:i + 1]), "name": parts[i]} for i in range(len(parts))
        ],
        "archive": True,
        "dirs": [],
        "files": files,
        "generated_at": int(time.time()),
    }


def list_dir(root, rel, ttl, force=False):
    """List a directory with mtime/TTL-checked caching."""
    target = safe_resolve(root, rel)
    if target.is_file():
        if classify(target.suffix) == "archive":
            return list_archive(target, rel)
        abort(404)
    if not target.is_dir():
        abort(404)

    now = time.time()
    with _ls_lock:
        entry = _ls_cache.get(str(target))

    if entry is not None and not force:
        dir_mtime, fetched_at, payload = entry
        try:
            fresh = abs(target.stat().st_mtime - dir_mtime) < 1e-6 and (now - fetched_at) < ttl
        except OSError:
            fresh = False
        if fresh:
            return payload

    dirs, files = [], []
    with os.scandir(target) as it:
        for de in it:
            name = de.name
            if name.startswith("."):
                continue
            try:
                if de.is_dir(follow_symlinks=False):
                    dirs.append({"name": name, "archive": False})
                elif de.is_file(follow_symlinks=True):
                    ext = os.path.splitext(name)[1]
                    kind = classify(ext)
                    if kind == "archive":
                        dirs.append({"name": name, "archive": True})
                        continue
                    st = de.stat(follow_symlinks=True)
                    files.append({
                        "name": name,
                        "kind": kind,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "thumb": kind in ("image", "video"),
                    })
            except OSError:
                continue

    dirs.sort(key=lambda d: natural_key(d["name"]))
    files.sort(key=lambda f: natural_key(f["name"]))

    parts = [p for p in rel.strip("/").split("/") if p]
    payload = {
        "path": "/".join(parts),
        "parent": "/".join(parts[:-1]) if parts else None,
        "breadcrumbs": [
            {"path": "/".join(parts[:i + 1]), "name": parts[i]} for i in range(len(parts))
        ],
        "dirs": dirs,
        "files": files,
        "generated_at": int(now),
    }

    try:
        dir_mtime = target.stat().st_mtime
    except OSError:
        dir_mtime = 0
    with _ls_lock:
        _ls_cache[str(target)] = (dir_mtime, now, payload)
    return payload


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

def thumb_key(rel, st):
    h = hashlib.sha1()
    h.update(rel.encode("utf-8", "surrogateescape"))
    h.update(b"\0%d\0%d" % (st.st_size, st.st_mtime_ns))
    return h.hexdigest()


def _atomic_tmp(dest_dir):
    fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp.jpg")
    os.close(fd)
    os.chmod(tmp_path, 0o644)
    return tmp_path


def make_video_thumb(source, tmp, width=480):
    duration = 0.0
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        duration = 0.0

    if duration > 60:
        offset = 20.0
    elif duration > 2:
        offset = duration / 2
    else:
        offset = 1.0

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{offset:.2f}", "-i", str(source),
         "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-q:v", "4", "-y", tmp],
        capture_output=True, timeout=120, check=True,
    )


def make_image_thumb(source, tmp, max_size=480):
    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_size, max_size))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (18, 18, 18))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.save(tmp, "JPEG", quality=82)


def create_app():
    root = Path(os.environ.get("MB_ROOT", "")).resolve()
    if not str(os.environ.get("MB_ROOT", "")) or not root.is_dir():
        raise RuntimeError("MB_ROOT must be set to an existing directory")

    name = os.environ.get("MB_NAME", root.name)
    thumb_dir = Path(os.environ.get("MB_THUMBS", "data/thumbs/default"))
    thumb_dir.mkdir(parents=True, exist_ok=True)
    ttl = int(os.environ.get("MB_SCAN_INTERVAL", "300"))
    secret = os.environ.get("MB_SECRET", "")
    signer = Signer(secret) if secret else None
    slots_dir = os.environ.get(
        "MB_SLOTS_DIR", str(Path(thumb_dir).parent / "slots")
    )
    remux_dir = Path(os.environ.get("MB_REMUX_DIR", str(Path(thumb_dir).parent / "remux")))
    remux_dir.mkdir(parents=True, exist_ok=True)
    remux_max_gb = float(os.environ.get("MB_REMUX_MAX_GB", "50"))
    remux_max_age_days = float(os.environ.get("MB_REMUX_MAX_AGE_DAYS", "14"))
    thumb_max_gb = float(os.environ.get("MB_THUMB_MAX_GB", "2"))
    thumb_max_age_days = float(os.environ.get("MB_THUMB_MAX_AGE_DAYS", "30"))

    def _gc_loop():
        # Periodic sweep so an idle box still expires caches on time
        # (creation-time GC alone only trims when new work happens).
        while True:
            try:
                _remux_gc(remux_dir, remux_max_gb, remux_max_age_days)
            except OSError:
                pass
            try:
                _thumb_gc(thumb_dir, thumb_max_gb, thumb_max_age_days)
            except OSError:
                pass
            time.sleep(6 * 3600)

    threading.Thread(target=_gc_loop, daemon=True).start()
    pool = TranscodePool(
        slots_dir,
        int(os.environ.get("MB_MAX_TRANSCODES", "2")),
        int(os.environ.get("MB_STALL_TIMEOUT", "300")),
    )

    app = Flask(__name__)

    @app.get("/")
    @app.get("/browse/<path:p>")
    @app.get("/media/<path:p>")
    def index(p=None):
        return render_template("index.html", name=name)

    def files_url(rel, exp, sig):
        quoted = "/".join(
            urllib.parse.quote(p) for p in rel.split("/")
        )
        return f"/files/{quoted}?exp={exp}&sig={sig}"

    @app.get("/authz")
    def authz():
        """Caddy forward_auth endpoint: validate the /files/* subrequest."""
        if signer is None:
            abort(403)
        forwarded = request.headers.get("X-Forwarded-Uri", "")
        parsed = urllib.parse.urlparse(forwarded)
        path = parsed.path
        if path.startswith("/files/"):
            path = path[len("/files/"):]
        elif path.startswith("/"):
            path = path[1:]
        rel = urllib.parse.unquote(path)
        qs = urllib.parse.parse_qs(parsed.query)
        if not signer.verify(rel, qs.get("exp", [""])[0], qs.get("sig", [""])[0]):
            abort(403)
        # Defense in depth: the token must point at a real readable object.
        apath, member, _ = split_archive(root, rel)
        if apath is not None:
            read_member(apath, member)
        else:
            target = safe_resolve(root, rel)
            if not target.is_file():
                abort(403)
        return Response(status=204)

    @app.get("/api/sign")
    def api_sign():
        """Mint a play/download grant for one file (or archive member)."""
        rel = request.args.get("path", "")
        apath, member, _ = split_archive(root, rel)
        if apath is not None:
            read_member(apath, member)
            return jsonify({"url": f"/afile?path={urllib.parse.quote(rel)}",
                            "mode": "raw", "name": Path(member).name})
        target = safe_resolve(root, rel)
        if not target.is_file():
            abort(404)

        kind = classify(target.suffix)
        if kind == "archive":
            return jsonify({"url": f"/browse/{urllib.parse.quote(rel)}",
                            "mode": "dir", "name": target.name})
        mode_req = request.args.get("mode")
        if mode_req == "mp4" and kind == "video":
            # Seekable remux for Safari-class browsers. "transcode" hint:
            # /remux must fully re-encode (slow) vs stream-copy (fast), so
            # the frontend can warn about the wait up front.
            tc = plan_from_probe(probe(["ffprobe", "-v", "error"], target)) == "transcode"
            return jsonify({"url": f"/remux?path={urllib.parse.quote(rel)}",
                            "mode": "mp4", "name": target.name, "transcode": tc})
        force = mode_req == "stream"
        want_stream = force or (
            kind == "video" and target.suffix.lower() not in NATIVE_VIDEO_EXTS
        ) or (
            kind == "audio" and target.suffix.lower() not in NATIVE_AUDIO_EXTS
        )
        if want_stream and kind == "video":
            # App-streamed remux; no token needed (app route is already protected).
            # dur lets the frontend render a pseudo-seek bar for the live pipe.
            return jsonify({"url": f"/stream?path={urllib.parse.quote(rel)}",
                            "mode": "stream", "name": target.name,
                            "dur": probe_duration(target)})
        if signer is None:
            # No Caddy in front (local dev): fall back to app streaming.
            return jsonify({"url": f"/view?path={urllib.parse.quote(rel)}",
                            "mode": "app"})
        exp, sig = signer.sign(rel)
        return jsonify({"url": files_url(rel, exp, sig),
                        "mode": "direct", "name": target.name})

    @app.get("/api/ls")
    def api_ls():
        rel = request.args.get("path", "")
        force = request.args.get("refresh") == "1"
        return jsonify(list_dir(root, rel, ttl, force))

    @app.get("/view")
    def view():
        target = safe_resolve(root, request.args.get("path", ""))
        if not target.is_file():
            abort(404)
        mime, _ = mimetypes.guess_type(str(target))
        return send_file(target, mimetype=mime or "application/octet-stream",
                         conditional=True, max_age=0)

    @app.get("/stream")
    def stream():
        """Remux/transcode any video on the fly to fragmented MP4 and pipe it
        out. No Range support (seek by reconnecting with ?start=SECONDS);
        Concurrency-capped via TranscodePool; paused/abandoned streams are
        killed after MB_STALL_TIMEOUT seconds by design."""
        target = safe_resolve(root, request.args.get("path", ""))
        if not target.is_file() or classify(target.suffix) not in ("video", "audio"):
            abort(404)
        try:
            start = float(request.args.get("start", "0"))
        except ValueError:
            abort(400)
        if start < 0 or start > 7 * 86400:
            abort(400)

        slot = pool.acquire()
        if slot is None:
            return Response(
                f"transcode pool full (max {len(pool.slots)}); retry shortly\n",
                status=503,
                headers={"Retry-After": "10", "Content-Type": "text/plain"},
            )

        probed = probe(["ffprobe", "-v", "error"], target)
        mode = plan_from_probe(probed, request.args.get("transcode") == "1")
        has_audio = any(s["codec_type"] == "audio" for s in probed)
        has_video = any(s["codec_type"] == "video" for s in probed)
        cmd = build_stream_cmd(mode, target, has_audio, has_video, start,
                               video_enc=(qsv_video_args() if qsv_available()
                                          else None))

        def gen():
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            pool.register(proc)
            try:
                while True:
                    chunk = proc.stdout.read(262144)
                    if not chunk:
                        break
                    pool.touch(proc)
                    yield chunk
            finally:
                # Runs on normal end AND on generator close (client gone).
                # If WSGI abandons the generator entirely, the watchdog
                # kills the process group after MB_STALL_TIMEOUT.
                pool.retire(proc)
                pool.release(slot)

        return Response(
            gen(),
            mimetype="video/mp4",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/remux")
    def remux():
        """Seekable, length-known MP4 (stream copy where possible). This is
        what Safari needs; served with full Range support via send_file.
        Non-copyable codecs mean a full video transcode, so those build
        inside the global transcode pool (same cap as /stream, 503 when
        saturated)."""
        rel = request.args.get("path", "")
        target = safe_resolve(root, rel)
        if not target.is_file() or classify(target.suffix) != "video":
            abort(404)
        force = request.args.get("transcode") == "1"
        cached, _ = remux_cache_path(root, remux_dir, target, rel, force)
        if cached.is_file():          # built already: no probe, no slot
            os.utime(cached, None)
            return send_file(cached, mimetype="video/mp4", conditional=True,
                             max_age=86400 * 7)
        slot = None
        probed = probe(["ffprobe", "-v", "error"], target)
        if force or plan_from_probe(probed) == "transcode":
            slot = pool.acquire()
            if slot is None:
                return Response("transcode pool busy\n", status=503,
                                headers={"Retry-After": "10"})
        try:
            dest = ensure_remux(root, remux_dir, remux_max_gb, remux_max_age_days,
                                target, rel, force_transcode=force)
        except subprocess.CalledProcessError:
            return Response("remux build failed (server log has details)\n",
                            status=500)
        finally:
            if slot is not None:
                pool.release(slot)
        return send_file(dest, mimetype="video/mp4", conditional=True,
                         max_age=86400 * 7)

    # ---- subtitles (opt-in via MB_SUBS=1; converted to WebVTT, cached) ----
    subs_dir = remux_dir / "subs"

    def _vtt_response(path):
        os.utime(path, None)
        return Response(path.read_bytes(),
                        headers={"Content-Type": "text/vtt; charset=utf-8",
                                 "Cache-Control": "private, max-age=86400"})

    @app.get("/api/subs")
    def api_subs():
        """Advertise subtitles: embedded text streams + sidecar files."""
        if not SUBS_ENABLED:
            return jsonify({"subs": []})
        rel = request.args.get("path", "")
        target = safe_resolve(root, rel)
        if not target.is_file() or classify(target.suffix) not in ("video", "audio"):
            abort(404)
        out, n = [], 0
        for s in probe_lang(target):
            if s.get("codec_type") != "subtitle":
                continue
            codec = s.get("codec_name", "")
            idx = n
            n += 1
            if codec not in SUB_TEXT_CODECS:      # PGSS/DVD bitmaps not convertible
                continue
            tags = s.get("tags") or {}
            lang = (tags.get("language") or "").strip()
            title = (tags.get("title") or "").strip()
            out.append({"key": f"e:{idx}", "lang": lang,
                        "label": title or lang or f"subtitle {idx + 1}"})
        for i, (name, lang) in enumerate(sidecar_subs(target)):
            out.append({"key": "f:" + name, "lang": lang,
                        "label": lang.upper() if lang else f"subtitles {i + 1}"})
        return jsonify({"subs": out})

    @app.get("/subs")
    def subs():
        """Serve one subtitle track as WebVTT (converted + cached on demand)."""
        if not SUBS_ENABLED:
            abort(404)
        rel = request.args.get("path", "")
        key = request.args.get("key", "")
        target = safe_resolve(root, rel)
        if not target.is_file():
            abort(404)
        st = target.stat()
        if key.startswith("e:"):
            idx = key[2:]
            if not idx.isdigit():
                abort(400)
            h = hashlib.sha1(b"%s\0%s\0%d\0%d\0e%s" % (str(root).encode(),
                     rel.encode("utf-8", "surrogateescape"), st.st_size,
                     st.st_mtime_ns, idx.encode())).hexdigest()
            dest = subs_dir / (h + ".vtt")
            if not dest.is_file():
                subs_dir.mkdir(parents=True, exist_ok=True)
                extract_vtt(target, f"0:s:{idx}", dest)
                _remux_gc(subs_dir, 1, remux_max_age_days)
            return _vtt_response(dest)
        if key.startswith("f:"):
            name = key[2:]
            allowed = dict(sidecar_subs(target))
            if name not in allowed:
                abort(404)
            side = safe_resolve(root, str(Path(rel).parent / name))
            if side.suffix.lower() == ".vtt":
                return Response(side.read_bytes(),
                                headers={"Content-Type": "text/vtt; charset=utf-8",
                                         "Cache-Control": "private, max-age=86400"})
            if side.suffix.lower() == ".srt":
                return Response(srt_to_vtt(side.read_bytes()),
                                headers={"Content-Type": "text/vtt; charset=utf-8",
                                         "Cache-Control": "private, max-age=86400"})
            h = hashlib.sha1(b"%s\0%s\0%d\0%d\0f%s" % (str(root).encode(),
                     rel.encode("utf-8", "surrogateescape"), st.st_size,
                     st.st_mtime_ns, name.encode())).hexdigest()
            dest = subs_dir / (h + ".vtt")
            if not dest.is_file():
                subs_dir.mkdir(parents=True, exist_ok=True)
                extract_vtt(side, "0:s:0", dest)
                _remux_gc(subs_dir, 1, remux_max_age_days)
            return _vtt_response(dest)
        abort(400)

    @app.get("/dl")
    def download():
        target = safe_resolve(root, request.args.get("path", ""))
        if not target.is_file():
            abort(404)
        return send_file(target, as_attachment=True, download_name=target.name)

    @app.get("/afile")
    def afile():
        """Serve a file (or archive member) inline; last-resort generic view."""
        rel = request.args.get("path", "")
        apath, member, _ = split_archive(root, rel)
        if apath is not None:
            data, _info = read_member(apath, member)
            mime, _ = mimetypes.guess_type(member)
            return send_file(io.BytesIO(data),
                             mimetype=mime or "application/octet-stream",
                             download_name=Path(member).name, max_age=3600)
        target = safe_resolve(root, rel)
        if not target.is_file():
            abort(404)
        mime, _ = mimetypes.guess_type(str(target))
        return send_file(target, mimetype=mime or "application/octet-stream",
                         conditional=True, max_age=0)

    @app.get("/thumb")
    def thumb():
        rel = request.args.get("path", "")
        apath, member, _ = split_archive(root, rel)
        if apath is not None:
            if classify(Path(member).suffix) != "image":
                abort(404)
            data, _info = read_member(apath, member, 200 * 1024 * 1024)
            st = apath.stat()
            key = hashlib.sha1(
                b"%s\0%d\0%d\0%s" % (str(apath).encode(), st.st_size,
                                     st.st_mtime_ns, member.encode())
            ).hexdigest()
            dest = thumb_dir / (key + ".jpg")
            if dest.is_file():
                return send_file(dest, mimetype="image/jpeg", conditional=True, max_age=86400)
            tmp = _atomic_tmp(str(thumb_dir))
            try:
                with _thumb_semaphore:
                    with Image.open(io.BytesIO(data)) as img:
                        img = ImageOps.exif_transpose(img)
                        img.thumbnail((480, 480))
                        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                            img = img.convert("RGBA")
                            bg = Image.new("RGB", img.size, (18, 18, 18))
                            bg.paste(img, mask=img.split()[-1])
                            img = bg
                        else:
                            img = img.convert("RGB")
                        img.save(tmp, "JPEG", quality=82)
                os.replace(tmp, dest)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                abort(404)
            return send_file(dest, mimetype="image/jpeg", conditional=True, max_age=86400)

        target = safe_resolve(root, rel)
        if not target.is_file():
            abort(404)

        kind = classify(target.suffix)
        if kind not in ("image", "video"):
            abort(404)

        st = target.stat()
        dest = thumb_dir / (thumb_key(rel, st) + ".jpg")
        if dest.is_file():
            return send_file(dest, mimetype="image/jpeg", conditional=True, max_age=86400)

        tmp = _atomic_tmp(str(thumb_dir))
        try:
            with _thumb_semaphore:
                # Re-check after acquiring the generator slot.
                if dest.is_file():
                    return send_file(dest, mimetype="image/jpeg", conditional=True, max_age=86400)
                if kind == "video":
                    make_video_thumb(target, tmp)
                else:
                    make_image_thumb(target, tmp)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # Fallback: serve the original file (images only really work here).
            if kind == "image":
                return send_file(target, conditional=True, max_age=0)
            abort(404)

        return send_file(dest, mimetype="image/jpeg", conditional=True, max_age=86400)

    @app.after_request
    def no_stale_pages(resp):
        if resp.mimetype in ("text/html", "application/json"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "not found"}), 404

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("MB_PORT", "7081")),
                     threaded=True, debug=False)
