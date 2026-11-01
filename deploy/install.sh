#!/usr/bin/env bash
# Inštalácia do Debian LXC (spúšťať ako root z priečinka projektu): bash deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/energy-archiv
ETC=/etc/energy-archiv

apt-get update -qq
apt-get install -y -qq python3 python3-venv sqlite3 >/dev/null

mkdir -p "$APP" "$ETC" /var/lib/energy-archiv
cp -r "$SRC/energy_archiv" "$SRC/requirements.txt" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

[ -f "$ETC/config.yaml" ] || cp "$SRC/config.example.yaml" "$ETC/config.yaml"
if [ ! -f "$ETC/.env" ]; then
  if [ -f "$SRC/.env" ]; then cp "$SRC/.env" "$ETC/.env"; else cp "$SRC/.env.example" "$ETC/.env"; fi
fi
chmod 600 "$ETC/.env"

cat > /usr/local/bin/energy-archiv <<EOF
#!/bin/sh
cd $ETC && exec $APP/.venv/bin/python -m energy_archiv -c $ETC/config.yaml "\$@"
EOF
chmod +x /usr/local/bin/energy-archiv
# modul musí byť importovateľný
echo "$APP" > "$("$APP/.venv/bin/python" -c 'import site;print(site.getsitepackages()[0])')/energy-archiv.pth"

cp "$SRC"/deploy/*.service "$SRC"/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now energy-archiv-web.service energy-archiv-nightly.timer
systemctl restart energy-archiv-web.service

echo
echo "Hotovo. Ďalej:"
echo "  1) doplň $ETC/.env (HA_TOKEN, DEYE_EMAIL, DEYE_PASSWORD) a skontroluj $ETC/config.yaml"
echo "  2) energy-archiv sync        # prvý zber celej histórie z HA"
echo "  3) web: http://$(hostname -I | awk '{print $1}'):8765"
