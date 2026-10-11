#!/usr/bin/env bash
# evulMedia installer. Copies the app to /opt/evulmedia, creates a venv,
# installs the systemd template unit, and (for each env/*.env you created
# from env/*.env.example) enables that instance.
#
#   ./install.sh                # install/update everything
#   PREFIX=/srv/evulmedia ./install.sh
#
# After installing: edit $PREFIX/env/*.env, set MB_SECRET + MB_ROOT, then
#   sudo systemctl restart 'evulmedia@*'
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-/opt/evulmedia}"
RUNUSER="${RUNUSER:-www-data}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (or: sudo $0)"; exit 1; }

install -d -m 755 "$PREFIX" "$PREFIX/env" "$PREFIX/templates"
install -m 644 "$SRC/app.py" "$SRC/wsgi.py" "$SRC/requirements.txt" \
                 "$SRC/selftest.py" "$PREFIX/"
install -m 644 "$SRC/templates/index.html" "$PREFIX/templates/"

# env files: copy only *.env (not the *.example seeds); never clobber edits
shopt -s nullglob
for f in "$SRC"/env/*.env; do
    install -m 640 "$f" "$PREFIX/env/$(basename "$f")"
done
# seeds for reference
install -m 644 "$SRC"/env/*.env.example "$PREFIX/env/" 2>/dev/null || true

# writable cache tree (thumbnails, remuxes, transcode locks)
install -d -m 755 -o "$RUNUSER" -g "$RUNUSER" \
    "$PREFIX/data" "$PREFIX/data/thumbs" "$PREFIX/data/slots" "$PREFIX/data/remux"

# venv
[ -x "$PREFIX/venv/bin/gunicorn" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements.txt"

# Quick Sync (optional): give the service user the render node
if getent group video >/dev/null 2>&1; then
    usermod -aG video "$RUNUSER" 2>/dev/null \
        && echo "NOTE: added $RUNUSER to group video (Quick Sync); restart units" \
        || echo "NOTE: run: usermod -aG video $RUNUSER   (enables Quick Sync)"
fi

# systemd
install -m 644 "$SRC/systemd/evulmedia@.service" /etc/systemd/system/
systemctl daemon-reload
for f in "$PREFIX"/env/*.env; do
    inst="$(basename "$f" .env)"
    systemctl enable "evulmedia@${inst}.service"
done
echo
echo "Installed. Next:"
echo "  1. edit $PREFIX/env/*.env   (MB_ROOT, MB_SECRET=openssl rand -hex 32)"
echo "  2. sudo systemctl restart 'evulmedia@*'"
echo "  3. add the Caddy routes (see caddy/Caddyfile.example)"
echo "  4. verify: systemctl status 'evulmedia@*'; journalctl -u evulmedia@media -n 50"
