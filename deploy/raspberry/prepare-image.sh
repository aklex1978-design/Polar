#!/usr/bin/env bash
# Polar: підготовка еталонної карти до зняття образу.
#   sudo ./deploy/raspberry/prepare-image.sh      (у кінці Pi вимикається)
# Прибирає все, що має бути своїм на кожній карті (machine-id, SSH-ключі, журнали, історію), і кладе
# polar.env на boot-розділ — його змінюють на Windows перед першим запуском копії.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустіть через sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
BOOT=/boot/firmware

systemctl is-enabled --quiet polar-firstboot.service || {
  echo "Спершу запустіть install.sh (служба polar-firstboot не встановлена)" >&2; exit 1; }

echo "== налаштування камери → boot-розділ (system-boot/polar.env)"
cp /etc/polar/polar.env "$BOOT/polar.env"
cp /etc/polar/pantilt.yaml "$BOOT/pantilt.yaml"
rm -f /var/lib/polar/*.sha256            # перший запуск копії застосує polar.env з boot-розділу

echo "== унікальне для кожної карти"
truncate -s 0 /etc/machine-id            # systemd створить новий (інакше однакові DHCP-ідентифікатори)
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*                # нові ключі створить polar-firstboot

echo "== журнали, кеш, історія"
apt-get clean
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
find /var/log -type f \( -name "*.gz" -o -name "*.1" -o -name "*.old" \) -delete
find /var/log -type f -exec truncate -s 0 {} +
rm -f /root/.bash_history /home/*/.bash_history
rm -rf /tmp/* /var/tmp/* 2>/dev/null || true

echo "== перевірка служб"
for s in polar-firstboot polar-pantilt polar-video; do
  printf '   %-16s %s\n' "$s" "$(systemctl is-enabled "$s" 2>/dev/null || echo '-')"
done

sync
echo
echo "Готово. Pi вимикається. Далі — образ карти на Windows (README.md, «Образ для тиражування»)."
echo "Увага: не вмикайте цю карту знову до зняття образу — інакше вона створить собі нові ключі й machine-id."
sleep 3
poweroff
