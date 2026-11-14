#!/usr/bin/env bash
# Polar: налаштування Pi з boot-розділу SD-карти (видно на Windows як диск «system-boot»).
# Під час кожного завантаження: якщо /boot/firmware/polar.env (або pantilt.yaml) змінено — копіює в
# /etc/polar, задає імʼя компʼютера за камерою (polar-camera-2), вмикає/вимикає відео, NTP, і
# перезапускає служби Polar. Так одна карта-образ тиражується: записати → змінити polar.env → вставити.
set -euo pipefail

BOOT=/boot/firmware
STATE=/var/lib/polar
mkdir -p /etc/polar "$STATE"

# нові SSH-ключі для кожної карти (prepare-image.sh видаляє ключі еталонної)
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
  ssh-keygen -A
  systemctl restart ssh 2>/dev/null || true
fi

changed=0
apply() {   # apply <файл на boot> <куди> — копіює, якщо файл з boot-розділу новий або змінився
  local src="$BOOT/$1" dst="$2" sum
  [ -f "$src" ] || return 0
  sum=$(tr -d '\r' < "$src" | sha256sum | cut -d' ' -f1)
  if [ "$sum" != "$(cat "$STATE/$1.sha256" 2>/dev/null)" ]; then
    tr -d '\r' < "$src" > "$dst"          # файл могли редагувати в Блокноті Windows (CRLF)
    echo "$sum" > "$STATE/$1.sha256"
    echo "застосовано $src → $dst"
    changed=1
  fi
}
apply polar.env /etc/polar/polar.env
apply pantilt.yaml /etc/polar/pantilt.yaml

# імʼя компʼютера за камерою: camera_2 → polar-camera-2 (так Pi легко знайти в мережі) — під час КОЖНОГО
# завантаження: cloud-init Ubuntu інакше повертає імʼя, задане в Raspberry Pi Imager
if [ -f /etc/polar/polar.env ]; then
  # shellcheck disable=SC1091
  . /etc/polar/polar.env
  if [ -d /etc/cloud/cloud.cfg.d ] && [ ! -f /etc/cloud/cloud.cfg.d/99-polar.cfg ]; then
    printf 'preserve_hostname: true\nmanage_etc_hosts: false\n' > /etc/cloud/cloud.cfg.d/99-polar.cfg
  fi
  name="polar-$(echo "${CAMERA_NS:-camera}" | tr '_' '-' | tr -cd 'a-zA-Z0-9-' | tr 'A-Z' 'a-z')"
  if [ "$(hostname)" != "$name" ]; then
    hostnamectl set-hostname "$name"
    sed -i -E "s/^127\.0\.1\.1\s.*/127.0.1.1 $name/" /etc/hosts
    grep -q "^127.0.1.1" /etc/hosts || echo "127.0.1.1 $name" >> /etc/hosts
    echo "імʼя компʼютера: $name"
  fi
fi
[ "$changed" = 1 ] || { echo "налаштування на boot-розділі не змінились"; exit 0; }

if [ -n "${NTP_SERVER:-}" ]; then
  mkdir -p /etc/chrony/conf.d
  echo "server $NTP_SERVER iburst prefer" > /etc/chrony/conf.d/polar.conf
else
  rm -f /etc/chrony/conf.d/polar.conf
fi
systemctl restart chrony 2>/dev/null || true

# --no-block: під час завантаження служби Polar стартують ПІСЛЯ цієї (Before= у polar-firstboot.service);
# синхронний restart чекав би на них, а вони — на неї, і завантаження зависало б (взаємне блокування)
systemctl --no-block restart polar-pantilt.service || true
if [ "${VIDEO_ENABLED:-1}" = "1" ]; then
  systemctl enable polar-video.service && systemctl --no-block restart polar-video.service || true
else
  systemctl disable polar-video.service 2>/dev/null || true
  systemctl --no-block stop polar-video.service || true
fi
echo "Polar: камера ${TOPIC_PREFIX:-/polar}/${CAMERA_NS:-?}, відео ${VIDEO_ENABLED:-1}, потік ${RTSP_PATH:-}"
