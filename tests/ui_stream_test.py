#!/usr/bin/env python3
"""Playwright UI test for /stream pseudo-seek, pause-resume, and seekable mode.

Self-contained: builds a throwaway media tree (ffmpeg-generated clip), starts
the dev server, drives headless Chromium, tears everything down. Never touches
MB_ROOT of a prod instance.

    venv/bin/pip install playwright && venv/bin/playwright install chromium
    venv/bin/python tests/ui_stream_test.py

Skips (exit 0 with a message) when ffmpeg or Chromium is unavailable.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PORT = 7099
BASE = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAILS.append(name)


def wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: ffmpeg/ffprobe not on PATH")
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed")
        return

    tmp = tempfile.mkdtemp(prefix="mb-ui-")
    media, data = Path(tmp) / "media", Path(tmp) / "data"
    media.mkdir(); (data / "thumbs").mkdir(parents=True)
    clip = media / "test_clip.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=20:size=320x180:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         "-shortest", "-y", str(clip)],
        check=True, timeout=120)
    (media / "broken.avi").write_bytes(os.urandom(4096))   # fails any build
    (media / "Show S01").mkdir()                           # folder-memory target

    # second clip: same video + one embedded SRT track + one sidecar SRT
    srt = media / "sub_src.srt"
    srt.write_text("1\n00:00:02,000 --> 00:00:06,000\nhello subs\n")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
         "-i", str(srt), "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt",
         "-y", str(media / "test_sub.mkv")], check=True, timeout=120)
    srt.rename(media / "test_sub.en.srt")   # sidecar for test_sub.mkv

    env = {**os.environ,
           "MB_ROOT": str(media), "MB_THUMBS": str(data / "thumbs"),
           "MB_PORT": str(PORT), "MB_SECRET": "ui-test-secret",
           "MB_SUBS": "1", "MB_MAX_TRANSCODES": "1",
           "MB_REMUX_DIR": str(data / "remux"), "MB_SLOTS_DIR": str(data / "slots")}
    errlog = open(data / "srv.log", "wb")
    srv = subprocess.Popen(
        [sys.executable, str(REPO / "app.py")], env=env, cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=errlog)
    try:
        if not wait_port(PORT):
            print("FAIL: dev server did not start")
            sys.exit(1)
        run_ui(sync_playwright, data / "slots", data)
    finally:
        srv.terminate()
        errlog.close()


def stream_urls(data_dir):
    """Query strings of every /stream request, in order, from server log."""
    with open(data_dir / "srv.log", "rb") as f:
        return re.findall(rb"GET /stream\?([^ ]+) HTTP", f.read())
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


def _status(url):
    try:
        return urllib.request.urlopen(url, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def run_ui(sync_playwright, slots_dir, data_dir):
    import urllib.request, json as js

    # --- server-side: /stream pipes an fMP4, ?start= seeks ---
    req = urllib.request.urlopen(f"{BASE}/stream?path=test_clip.mkv", timeout=30)
    head = req.read(64)
    check("/stream 200 fMP4", req.status == 200 and b"ftyp" in head)
    req2 = urllib.request.urlopen(f"{BASE}/stream?path=test_clip.mkv&start=10", timeout=30)
    check("/stream?start=10 200", req2.status == 200 and b"ftyp" in req2.read(64))
    for bad in ("abc", "-5", "999999999"):
        code = None
        try:
            urllib.request.urlopen(f"{BASE}/stream?path=test_clip.mkv&start={bad}", timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        check(f"/stream rejects start={bad}", code == 400, f"got {code}")
    sign = js.load(urllib.request.urlopen(f"{BASE}/api/sign?path=test_clip.mkv", timeout=10))
    check("sign reports ~20s duration", 15 <= sign.get("dur", 0) <= 25, str(sign.get("dur")))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page()
        page.goto(BASE + "/")
        page.wait_for_selector('[data-file="test_clip.mkv"]')
        check("UI: clip card rendered", True)

        # --- open stream playback ---
        page.goto(f"{BASE}/media/test_clip.mkv")
        v = page.locator("video")
        v.wait_for()
        check("UI: src is /stream", "/stream" in v.get_attribute("src"))
        rng = page.locator(".seekctl input[type=range]")
        rng.wait_for(timeout=10000)
        check("UI: seek bar present with full duration",
              abs(int(rng.get_attribute("max")) - 20) <= 1)
        page.wait_for_function(
            "() => { const v=document.querySelector('video'); return v && v.currentTime > 1; }",
            timeout=15000)
        check("UI: stream autoplays", True)

        # --- pseudo-seek via the seek bar ---
        page.evaluate("""() => { const r=document.querySelector('.seekctl input');
                                 r.value=10; r.dispatchEvent(new Event('input',{bubbles:true}));
                                 r.dispatchEvent(new Event('change',{bubbles:true})); }""")
        page.wait_for_function("() => document.querySelector('video').src.includes('start=10')",
                               timeout=10000)
        page.wait_for_function(
            "() => { const v=document.querySelector('video'); return v.currentTime > 1; }",
            timeout=15000)
        tspan = page.locator(".seekctl .t").inner_text()
        check("UI: position label tracks content offset", tspan.startswith("0:1"), tspan)

        # --- pause tears down the pipe; overlay resumes at position ---
        page.evaluate("() => document.querySelector('video').pause()")
        page.locator(".resumeov").wait_for(timeout=5000)
        src_after_pause = page.evaluate("() => document.querySelector('video').getAttribute('src')")
        check("UI: pause tears the stream down (frees transcode slot)",
              src_after_pause is None or src_after_pause == "")
        page.locator(".resumeov").click()
        page.wait_for_function(
            "() => { const v=document.querySelector('video'); return v.src.includes('start=') && v.currentTime > 0.5; }",
            timeout=15000)
        check("UI: resume reconnects and plays", True)

        # --- settings cog: seekable mode -> remuxed, natively seekable MP4 ---
        # (cog lives in the header, which the open modal covers: close first)
        page.keyboard.press("Escape")
        page.locator("#cogBtn").click()
        page.locator("#pbSel").select_option("seekable")
        page.goto(f"{BASE}/media/test_clip.mkv")
        v = page.locator("video")
        v.wait_for(timeout=60000)  # remux sign + one-time remux run
        page.wait_for_function(
            "() => (document.querySelector('video') || {}).src && " +
            "document.querySelector('video').src.includes('/remux')",
            timeout=60000)   # src attaches once the (async) build reports ready
        src = page.evaluate("() => document.querySelector('video').src")
        check("UI: seekable toggle switches to /remux", "/remux" in src, src)
        page.evaluate("() => document.querySelector('video').play()")
        page.wait_for_function(
            "() => { const v=document.querySelector('video'); return isFinite(v.duration) && v.duration > 15; }",
            timeout=30000)
        page.evaluate("() => { document.querySelector('video').currentTime = 12; }")
        page.wait_for_function(
            "() => document.querySelector('video').currentTime >= 11", timeout=15000)
        check("UI: native seek works in seekable mode", True)
        check("UI: seekable pref persisted",
              page.evaluate("() => localStorage.getItem('mbPbMode')") == "seekable")

        # reload: preference sticks, straight to remux without a click
        page.goto(f"{BASE}/media/test_clip.mkv")
        v.wait_for(timeout=60000)
        page.wait_for_function("() => document.querySelector('video').src.includes('/remux')",
                               timeout=30000)
        check("UI: seekable mode used on fresh page load", True)

        # --- subtitles (server runs with MB_SUBS=1) ---
        subs = js.load(urllib.request.urlopen(f"{BASE}/api/subs?path=test_sub.mkv"))
        keys = sorted(s["key"] for s in subs["subs"])
        check("api/subs lists embedded + sidecar",
              keys == ["e:0", "f:test_sub.en.srt"], str(keys))
        head = urllib.request.urlopen(
            f"{BASE}/subs?path=test_sub.mkv&key=e:0").read(6)
        check("embedded track serves WEBVTT", head == b"WEBVTT")
        head = urllib.request.urlopen(
            f"{BASE}/subs?path=test_sub.mkv&key=f:test_sub.en.srt").read(6)
        check("sidecar SRT serves WEBVTT (in-memory)", head == b"WEBVTT")
        check("subs jail rejected",
              _status(f"{BASE}/subs?path=test_sub.mkv&key=f:..%2Fnope") in (400, 404))

        # UI: opt in via the cog, open the video, CC button paints cues itself
        # (self-rendered overlay, not <track>: Safari won't list dynamic tracks)
        page.keyboard.press("Escape")
        page.locator("#cogBtn").click()
        page.locator("#subsChk").check()
        page.goto(f"{BASE}/media/test_sub.mkv")
        page.locator("video").wait_for(timeout=60000)
        page.wait_for_selector(".ccctl", timeout=15000)
        check("UI: exactly one CC button", page.locator(".ccctl").count() == 1)
        page.click(".ccctl")                      # off -> track 1
        page.evaluate("""() => { const v = document.querySelector('video');
            try { v.currentTime = 3; v.pause(); } catch (e) {}
            v.dispatchEvent(new Event('timeupdate')); }""")
        page.wait_for_function(
            "() => document.querySelector('.subov').textContent.includes('hello subs')",
            timeout=20000)
        check("UI: CC on paints a cue", True)
        page.click(".ccctl")                      # track 1 -> track 2 (sidecar)
        page.wait_for_function(
            "() => /cc (?!off)/i.test(document.querySelector('.ccctl').textContent)",
            timeout=5000)
        check("UI: CC cycles to second track", True)
        page.click(".ccctl")                      # -> off, overlay clears
        page.wait_for_function("() => /cc off/i.test(document.querySelector('.ccctl').textContent)",
                               timeout=5000)
        check("UI: CC cycles back to off",
              page.text_content(".subov").strip() == "")
        # stepping media must never stack CC buttons
        page.keyboard.press("ArrowRight")
        page.locator("video").wait_for(timeout=30000)
        page.wait_for_timeout(2500)
        check("UI: CC button not duplicated on media switch",
              page.locator(".ccctl").count() <= 1,
              f"count={page.locator('.ccctl').count()}")

        # --- /remux transcode build: pool gating, Retry-After, cache hit ---
        # (last: we intentionally squat the single pool slot)
        import fcntl
        lk = open(Path(slots_dir) / "job0.lock", "w")
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:                                    # build spawned but slot-held
            urllib.request.urlopen(f"{BASE}/remux?path=test_clip.mkv&transcode=1")
            check("remux build-in-flight returns 409", False, "no exception")
        except urllib.error.HTTPError as e:
            check("remux build-in-flight returns 409", e.code == 409, str(e.code))
            check("409 carries Retry-After", e.headers.get("Retry-After") == "5",
                  str(e.headers.get("Retry-After")))
        st = js.load(urllib.request.urlopen(f"{BASE}/remux?path=test_clip.mkv&transcode=1&status=1"))
        check("status endpoint reports building", st.get("state") == "building", str(st))
        fcntl.flock(lk, fcntl.LOCK_UN)          # queued job may proceed
        deadline = time.time() + 60
        while time.time() < deadline:
            st = js.load(urllib.request.urlopen(f"{BASE}/remux?path=test_clip.mkv&transcode=1&status=1"))
            if st.get("state") == "ready":
                break
            time.sleep(0.5)
        check("detached build reaches ready", st.get("state") == "ready", str(st))
        head = urllib.request.urlopen(
            f"{BASE}/remux?path=test_clip.mkv&transcode=1").read(16)
        check("ready remux serves MP4", head[4:8] == b"ftyp")

        # --- error contract: broken file -> error state that status polls
        #     can OBSERVE (respawning only on plain GET, never on status) ---
        st = None
        for _ in range(40):
            st = js.load(urllib.request.urlopen(f"{BASE}/remux?path=broken.avi&status=1"))
            if st.get("state") == "error":
                break
            time.sleep(0.5)
        check("broken build reaches error state", st.get("state") == "error", str(st))
        st2 = js.load(urllib.request.urlopen(f"{BASE}/remux?path=broken.avi&status=1"))
        check("status poll observes (does not respawn) error",
              st2.get("state") == "error", str(st2))

        # --- watch list / resume / remember-folder (all default ON) ---
        page.goto(f"{BASE}/")
        page.wait_for_selector("#cogBtn")
        page.click("#cogBtn")
        defaults = page.evaluate("""() => [watchChk.checked, resumeChk.checked,
                                            dirChk.checked]""")
        check("UI: watch/resume/remember default to on", defaults == [True, True, True])
        page.keyboard.press("Escape")
        page.evaluate("""() => localStorage.setItem('mbWatch', JSON.stringify({
            'test_clip.mkv': { s: 'p', t: 12, d: 20, ts: Date.now() } }))""")
        page.goto(f"{BASE}/")
        page.wait_for_selector('[data-file="test_clip.mkv"] .pip.p')
        check("UI: partial-watch ring pip rendered on card", True)
        page.click('[data-file="test_clip.mkv"]')
        page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v && v.currentTime > 10; }", timeout=5000)
        check("UI: seekable video resumes at stored position", True)
        page.evaluate("""() => { const v = document.querySelector('video');
            v.currentTime = 19.5; v.pause();
            v.dispatchEvent(new Event('pause')); }""")
        pip = page.evaluate("""() => {
            const c = document.querySelector('[data-file="test_clip.mkv"] .pip');
            return c ? c.className : ''; }""")
        check("UI: watched-to-end upgrades pip to solid (watched)", "d" in pip, pip)

        # --- resumed LIVE stream: start= param single-valued and correct ---
        # (m.base must stay clean; a second start= in the URL would be the
        #  second value of a duplicate param — Werkzeug takes the FIRST,
        #  silently clamping every seek back to the resume point)
        page.keyboard.press("Escape")
        page.evaluate("() => localStorage.setItem('mbPbMode', 'auto')")
        page.evaluate("""() => localStorage.setItem('mbWatch', JSON.stringify({
            'test_clip.mkv': { s: 'p', t: 12, d: 20, ts: Date.now() } }))""")
        page.goto(f"{BASE}/")
        page.click('[data-file="test_clip.mkv"]')
        page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v && v.readyState >= 2; }", timeout=30000)
        page.wait_for_timeout(1200)
        ups = stream_urls(data_dir)
        last = urllib.parse.parse_qs(ups[-1].decode()) if ups else {}
        ok = last.get("start") and len(ups[-1].split(b"start=")) == 2 \
             and abs(float(last["start"][0]) - 12) <= 1.5
        check("UI: resumed live stream sends single start=12", bool(ok),
              ups[-1].decode() if ups else "no /stream request")
        page.evaluate("""() => { const i = document.querySelector('.seekctl input');
            i.value = 6; i.dispatchEvent(new Event('change', { bubbles: true })); }""")
        page.wait_for_timeout(1800)
        ups = stream_urls(data_dir)
        last = urllib.parse.parse_qs(ups[-1].decode()) if ups else {}
        ok = last.get("start") and len(ups[-1].split(b"start=")) == 2 \
             and abs(float(last["start"][0]) - 6) <= 1.5
        check("UI: scrub after resume lands on target (no stale start)", bool(ok),
              ups[-1].decode() if ups else "no /stream request")

        # --- live pipe: native bar hidden, click pauses -> resume overlay ---
        has_ctrl = page.evaluate("() => document.querySelector('video').controls")
        check("UI: live pipe hides native controls", has_ctrl is False)
        page.click("video")
        page.wait_for_selector(".resumeov", timeout=5000)
        check("UI: click-to-pause tears pipe down, resume overlay appears", True)
        page.click(".resumeov")
        page.wait_for_function(
            "() => document.querySelector('video').currentTime > 1", timeout=15000)
        check("UI: resume overlay restarts the pipe", True)
        page.keyboard.press("Escape")

        # --- remember-last-folder ---
        page.keyboard.press("Escape")
        page.click('[data-dir="Show S01"]')
        page.wait_for_timeout(1200)
        page.goto(f"{BASE}/")                      # bare root visit…
        page.wait_for_selector("nav a[data-path='Show S01']", timeout=8000)
        check("UI: bare root returns to last folder", True)
        page.evaluate("() => localStorage.setItem('mbLastFolder', 'gone/nope')")
        page.goto(f"{BASE}/")                      # remembered dir no longer exists
        page.wait_for_selector(".card", timeout=8000)
        fell_back = page.evaluate(
            "() => !document.querySelector(\"nav a[data-path='gone/nope']\")"
            " && !content.textContent.includes('not found')")
        check("UI: deleted remembered folder falls back to root", fell_back)
        page.click("#cogBtn")
        page.uncheck("#dirChk")
        page.keyboard.press("Escape")
        page.goto(f"{BASE}/")
        page.wait_for_selector(".card")
        at_root = page.evaluate(
            "() => location.pathname === '/' && "
            "!document.querySelector(\"nav a[data-path='Show S01']\")")
        check("UI: setting off disables folder memory", at_root)
        page.click("#cogBtn")
        check("UI: folder-memory toggle kept off", page.evaluate("() => dirChk.checked") is False)

        # --- autoplay OFF: no play() at all, tap-to-play button appears ---
        page.goto(f"{BASE}/")
        page.wait_for_selector("#cogBtn")
        page.click("#cogBtn")
        page.uncheck("#autoplayChk")
        page.keyboard.press("Escape")
        page.evaluate("""() => { window.__plays = 0;
            const op = HTMLMediaElement.prototype.play;
            HTMLMediaElement.prototype.play = function () { window.__plays++; return op.apply(this, arguments); }; }""")
        page.click('[data-file="test_clip.mkv"]')
        page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v && v.readyState >= 1; }", timeout=60000)
        page.wait_for_timeout(1500)
        check("UI: autoplay-off never calls play()",
              page.evaluate("() => window.__plays") == 0)
        page.wait_for_selector(".ppbtn", timeout=5000)
        vis = page.evaluate("() => getComputedStyle(document.querySelector('.ppbtn')).display")
        check("UI: autoplay-off reveals tap-to-play button", vis == "flex", vis)
        page.click(".ppbtn")
        page.wait_for_function(
            "() => document.querySelector('video').currentTime > 0", timeout=15000)
        check("UI: tap-to-play button starts playback", True)
        fcntl.flock(lk, fcntl.LOCK_EX)          # cache hit must not need the pool
        try:
            t0 = time.time()
            urllib.request.urlopen(f"{BASE}/remux?path=test_clip.mkv&transcode=1")
            hit_dt = time.time() - t0
            ok_busy_hit = True
        except urllib.error.HTTPError:
            hit_dt = 99
            ok_busy_hit = False
        fcntl.flock(lk, fcntl.LOCK_UN)
        check("cached remux serves with pool saturated", ok_busy_hit)
        check("cache hit skips probe (fast)", hit_dt < 1.0, f"{hit_dt:.2f}s")

        browser.close()


if __name__ == "__main__":
    main()
