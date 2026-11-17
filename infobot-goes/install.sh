#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ $EUID -ne 0 ]]; then
    echo 'Run: sudo bash install.sh' >&2
    exit 1
fi
/usr/bin/python3 -c 'import zoneinfo; zoneinfo.ZoneInfo("America/Detroit")'
id brad >/dev/null
install -d -m 755 /opt/infobot-goes
install -m 644 goes_api.py maps.py gallery.html /opt/infobot-goes/
install -m 644 infobot-goes.service /etc/systemd/system/infobot-goes.service
systemctl daemon-reload
systemctl enable infobot-goes.service
systemctl restart infobot-goes.service
systemctl --no-pager --full status infobot-goes.service
