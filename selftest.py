#!/usr/bin/env python3
"""Self-test for the media browser. Load the prod env, e.g. via sudo bash -c:

    sudo /opt/evulmedia/venv/bin/python /opt/evulmedia/selftest.py

Exits non-zero on any failure. Safe: read-only except a scratch remux dir.
"""
import json
import os
import sys
import urllib.parse

os.environ.setdefault("MB_REMUX_DIR", "/tmp/mb-selftest-remux")

from app import Signer, plan_from_probe, build_stream_cmd, create_app  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAILS.append(name)


def main():
    app = create_app()
    c = app.test_client()

    # --- listing ---
    r = c.get("/api/ls")
    check("ls root 200", r.status_code == 200)
    root = r.get_json()
    check("ls root has dirs", bool(root.get("dirs")))
    check("dirs are objects", all(isinstance(d, dict) and "name" in d for d in root["dirs"][:5]))

    # --- traversal defenses ---
    for evil in ("..", "../..", "..%2f..", "/etc", "….%2e/etc"):
        check(f"traversal blocked: {evil}", c.get(f"/api/ls?path={evil}").status_code in (403, 404))
    check("dotfile blocked", c.get("/api/ls?path=.ssh").status_code in (403, 404))

    # --- sign / tokens ---
    secret = os.environ.get("MB_SECRET", "")
    r = c.get("/api/ls")
    # find one video and one image anywhere shallow
    video = image = None
    dirs = [""]
    while dirs and not (video and image):
        d = dirs.pop(0)
        r = c.get("/api/ls?path=" + urllib.parse.quote(d))
        if r.status_code != 200:
            continue
        data = r.get_json()
        for sub in data["dirs"][:5]:
            nm = sub["name"] if isinstance(sub, dict) else sub
            dirs.append(d + "/" + nm if d else nm)
        for f in data["files"]:
            if f["kind"] == "video" and not video:
                video = (data["path"], f["name"])
            if f["kind"] == "image" and not image:
                image = (data["path"], f["name"])
        if video and image:
            break

    if video:
        rel = (video[0] + "/" + video[1]).lstrip("/")
        r = c.get("/api/sign?path=" + urllib.parse.quote(rel))
        s = json.loads(r.data)
        check("sign video -> stream mode", s.get("mode") in ("stream", "direct"), s.get("mode"))
        check("sign stream carries dur field", "dur" in s and s["dur"] >= 0)
        r = c.get("/api/sign?path=" + urllib.parse.quote(rel) + "&mode=mp4")
        check("sign video mode=mp4 -> /remux", "/remux" in json.loads(r.data).get("url", ""))
        # /stream start-param validation (400 before any ffmpeg work)
        check("stream rejects non-numeric start",
              c.get("/stream?path=" + urllib.parse.quote(rel) + "&start=abc").status_code == 400)
        check("stream rejects negative start",
              c.get("/stream?path=" + urllib.parse.quote(rel) + "&start=-5").status_code == 400)

    # --- authz (forward_auth contract, with prefix stripped like handle_path) ---
    if secret:
        signer = Signer(secret)
        bad = c.get("/authz", headers={"X-Forwarded-Uri": "/some/file.mkv?exp=99999999999&sig=deadbeef"})
        check("authz rejects bad sig", bad.status_code == 403)
        if video:
            rel = (video[0] + "/" + video[1]).lstrip("/")
            exp, sig = signer.sign(rel)
            ok = c.get("/authz", headers={
                "X-Forwarded-Uri": f"/{urllib.parse.quote(rel)}?exp={exp}&sig={sig}"})
            check("authz accepts good sig", ok.status_code == 204)
            ok2 = c.get("/authz", headers={
                "X-Forwarded-Uri": f"/files/{urllib.parse.quote(rel)}?exp={exp}&sig={sig}"})
            check("authz accepts /files-prefixed uri", ok2.status_code == 204)
    else:
        check("MB_SECRET set (prod)", False, "env has no MB_SECRET")

    # --- codec plan logic (no ffprobe needed) ---
    V = lambda n: {"codec_type": "video", "codec_name": n}
    A = lambda n: {"codec_type": "audio", "codec_name": n}
    check("plan h264+aac copy", plan_from_probe([V("h264"), A("aac")]) == "copy")
    check("plan hevc+none copy", plan_from_probe([V("hevc")]) == "copy")
    check("plan h264+eac3 audio-tc", plan_from_probe([V("h264"), A("eac3")]) == "transcode-audio")
    check("plan mpeg4 transcode", plan_from_probe([V("mpeg4"), A("mp3")]) == "transcode")
    check("plan forced transcode", plan_from_probe([V("h264"), A("aac")], True) == "transcode")
    check("plan audio-only flac tc", plan_from_probe([A("flac")]) == "transcode-audio")
    check("plan audio-only opus copy", plan_from_probe([A("opus")]) == "copy")

    # --- stream command construction (no ffmpeg needed) ---
    cmd0 = build_stream_cmd("copy", "in.mkv", True, True)
    check("stream: no -ss without start", "-ss" not in cmd0)
    check("stream: -i present", cmd0.index("-i") == cmd0.index("in.mkv") - 1)
    cmd1 = build_stream_cmd("copy", "in.mkv", True, True, start=61.5)
    check("stream: -ss 61.500 before -i",
          cmd1[cmd1.index("-ss") + 1] == "61.500" and cmd1.index("-ss") < cmd1.index("-i"))
    cmd2 = build_stream_cmd("transcode", "in.mkv", True, True, start=3600)
    check("stream: start works in transcode mode too",
          cmd2.index("-ss") < cmd2.index("-i") and "libx264" in cmd2)

    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
