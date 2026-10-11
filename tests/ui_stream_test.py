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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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

    env = {**os.environ,
           "MB_ROOT": str(media), "MB_THUMBS": str(data / "thumbs"),
           "MB_PORT": str(PORT), "MB_SECRET": "ui-test-secret",
           "MB_REMUX_DIR": str(data / "remux"), "MB_SLOTS_DIR": str(data / "slots")}
    srv = subprocess.Popen(
        [sys.executable, str(REPO / "app.py")], env=env, cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PORT):
            print("FAIL: dev server did not start")
            sys.exit(1)
        run_ui(sync_playwright)
    finally:
        srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


def run_ui(sync_playwright):
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

        browser.close()


if __name__ == "__main__":
    main()
