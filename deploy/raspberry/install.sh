#!/usr/bin/env bash
# Polar: налаштування Raspberry Pi 4B камери (Ubuntu 22.04 Server, arm64).
#   git clone https://github.com/aklex1978-design/Polar.git && cd Polar
#   sudo ./deploy/raspberry/install.sh
# Повторний запуск безпечний: оновлює збірку вузла і файли, не чіпаючи /etc/polar/polar.env та pantilt.yaml.
set -euo pipefail

MEDIAMTX_VERSION=v1.9.3
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
BOOT=/boot/firmware

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Запустіть через sudo" >&2; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "22.04" ] || echo "Увага: очікується Ubuntu 22.04, а тут ${PRETTY_NAME:-?}"
[ "$(dpkg --print-architecture)" = arm64 ] || echo "Увага: очікується arm64 (Raspberry Pi 4B)"

say "1/8 UART на GPIO14/15 для Bus Servo Driver HAT (A)"
REBOOT=0
if [ -f "$BOOT/config.txt" ]; then
  for line in "enable_uart=1" "dtoverlay=disable-bt"; do       # PL011 (ttyAMA0) на GPIO — стабільно на 1 Мбіт/с
    grep -qx "$line" "$BOOT/config.txt" || { echo "$line" >> "$BOOT/config.txt"; REBOOT=1; }
  done
fi
if [ -f "$BOOT/cmdline.txt" ] && grep -q "console=serial0" "$BOOT/cmdline.txt"; then
  cp -n "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.polar-backup"
  sed -i -E 's/ ?console=serial0,[0-9]+//' "$BOOT/cmdline.txt"     # UART — сервоприводам, не консолі
  REBOOT=1
fi
systemctl disable --now hciuart.service 2>/dev/null || true
systemctl disable --now serial-getty@ttyAMA0.service serial-getty@ttyS0.service 2>/dev/null || true

say "2/8 ROS 2 Humble (ros-base) і пакети"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q curl gnupg lsb-release software-properties-common
add-apt-repository -y universe >/dev/null
if [ ! -f /usr/share/keyrings/ros-archive-keyring.gpg ]; then
  curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
fi
echo "deb [arch=arm64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu jammy main" \
  > /etc/apt/sources.list.d/ros2.list
apt-get update -q
apt-get install -y -q ros-humble-ros-base ros-humble-rmw-fastrtps-cpp ros-humble-rmw-cyclonedds-cpp python3-colcon-common-extensions build-essential cmake \
  python3-serial chrony v4l-utils ffmpeg usbutils

say "3/8 Користувач polar (доступ до UART і відео)"
id polar >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin polar
usermod -aG dialout,video polar

say "4/8 Збірка вузла сервоприводів (polar_interfaces, pantilt_driver)"
mkdir -p /opt/polar/ros2_ws/src
rm -rf /opt/polar/ros2_ws/src/polar_interfaces /opt/polar/ros2_ws/src/pantilt_driver
cp -r "$REPO/ros2_ws/src/polar_interfaces" "$REPO/ros2_ws/src/pantilt_driver" /opt/polar/ros2_ws/src/
( cd /opt/polar/ros2_ws && set +u && source /opt/ros/humble/setup.bash && set -u && \
  colcon build --packages-select polar_interfaces pantilt_driver )

say "5/8 MediaMTX $MEDIAMTX_VERSION і скрипт відео"
if [ ! -x /opt/polar/mediamtx ] || ! /opt/polar/mediamtx --version 2>/dev/null | grep -q "$MEDIAMTX_VERSION"; then
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION/mediamtx_${MEDIAMTX_VERSION}_linux_arm64v8.tar.gz" \
    | tar -xz -C "$tmp" mediamtx
  install -m 0755 "$tmp/mediamtx" /opt/polar/mediamtx
  rm -rf "$tmp"
fi
install -m 0644 "$HERE/mediamtx.yml" /opt/polar/mediamtx.yml
install -m 0755 "$HERE/polar-video.sh" /opt/polar/polar-video.sh
install -m 0755 "$HERE/polar-firstboot.sh" /opt/polar/polar-firstboot.sh

say "6/8 Налаштування /etc/polar"
mkdir -p /etc/polar
[ -f /etc/polar/polar.env ] || install -m 0644 "$HERE/polar.env.example" /etc/polar/polar.env
[ -f /etc/polar/pantilt.yaml ] || install -m 0644 "$REPO/ros2_ws/src/pantilt_driver/config/camera_1.yaml" /etc/polar/pantilt.yaml
# копія на boot-розділі (видно на Windows як «system-boot»): зміни в ній застосовуються при завантаженні
if [ -d "$BOOT" ]; then
  if [ -f "$BOOT/polar.env" ]; then
    tr -d '\r' < "$BOOT/polar.env" > /etc/polar/polar.env     # редагували на Windows — він головний
  else
    cp /etc/polar/polar.env "$BOOT/polar.env"
  fi
  mkdir -p /var/lib/polar
  tr -d '\r' < "$BOOT/polar.env" | sha256sum | cut -d' ' -f1 > /var/lib/polar/polar.env.sha256
fi
# shellcheck disable=SC1091
. /etc/polar/polar.env

say "7/8 Час (chrony)"
if [ -n "${NTP_SERVER:-}" ]; then
  mkdir -p /etc/chrony/conf.d
  echo "server $NTP_SERVER iburst prefer" > /etc/chrony/conf.d/polar.conf
else
  rm -f /etc/chrony/conf.d/polar.conf
fi
systemctl restart chrony || true

say "8/8 Служби автозапуску"
install -m 0644 "$HERE/polar-pantilt.service" "$HERE/polar-video.service" "$HERE/polar-firstboot.service" \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable polar-firstboot.service polar-pantilt.service
if [ "${VIDEO_ENABLED:-1}" = "1" ]; then
  systemctl enable polar-video.service
else
  systemctl disable --now polar-video.service 2>/dev/null || true
fi
chown -R polar:polar /opt/polar

say "Готово"
echo "Камера: ${TOPIC_PREFIX:-/polar}/${CAMERA_NS:-camera_1}, ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}, сервоприводи: ${SERVO_BACKEND:-st3020}"
[ "${VIDEO_ENABLED:-1}" = "1" ] && echo "Відео: rtsp://$(hostname -I | awk '{print $1}'):8554/${RTSP_PATH:-camera_1}"
echo "Змініть під цю камеру: sudo nano /boot/firmware/polar.env  (або на Windows: диск system-boot\\polar.env),"
echo "  потім: sudo systemctl restart polar-firstboot"
if [ "$REBOOT" = 1 ]; then
  echo "UART змінено — ПОТРІБНЕ ПЕРЕЗАВАНТАЖЕННЯ: sudo reboot"
else
  systemctl restart polar-pantilt.service
  [ "${VIDEO_ENABLED:-1}" = "1" ] && systemctl restart polar-video.service
  echo "Перевірка: sudo $HERE/check.sh"
fi
