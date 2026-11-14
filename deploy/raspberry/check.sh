#!/usr/bin/env bash
# Polar: перевірка Raspberry Pi камери — UART, перетворювач, служби, RTSP, ROS 2, час.
ENV_FILE=/etc/polar/polar.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mНІ\033[0m   %s\n' "$*"; }
info() { printf '       %s\n' "$*"; }

echo "UART (сервоприводи)"
if [ -e /dev/serial0 ]; then ok "/dev/serial0 → $(readlink -f /dev/serial0)"; else bad "/dev/serial0 немає — enable_uart=1 у /boot/firmware/config.txt і перезавантаження"; fi
grep -q "console=serial0" /boot/firmware/cmdline.txt 2>/dev/null && bad "консоль ще на serial0 (cmdline.txt) — запустіть install.sh і перезавантажте"

echo "USB AV-перетворювач (CVBS)"
DEV=${VIDEO_DEVICE:-/dev/video0}
lsusb | grep -iE "video|capture|grabber|easycap|macrosilicon|534d|1b71|05e1|eb1a|1f71|fushicai|syntek" | sed 's/^/       /'
if lsusb | grep -qiE "1c88:|somagic"; then bad "чип Somagic (SMI2021) — у Linux не підтримується, потрібен інший перетворювач"; fi
if [ -e "$DEV" ]; then
  ok "$DEV: $(v4l2-ctl -d "$DEV" --info 2>/dev/null | awk -F: '/Card type/{print $2}' | xargs)"
  info "входи:   $(v4l2-ctl -d "$DEV" --list-inputs 2>/dev/null | awk -F: '/Name/{print $2}' | xargs | sed 's/ /, /g')"
  info "стандарт: $(v4l2-ctl -d "$DEV" --get-standard 2>/dev/null | head -1)"
  info "стабільна назва: $(ls -1 /dev/v4l/by-id/ 2>/dev/null | head -3 | tr '\n' ' ')"
else
  bad "$DEV немає (VIDEO_DEVICE у $ENV_FILE); пристрої: $(ls /dev/video* 2>/dev/null | xargs)"
fi

echo "Служби"
for s in polar-pantilt polar-rosbridge polar-video chrony; do
  if systemctl is-active --quiet "$s"; then ok "$s працює"; else bad "$s: $(systemctl is-active "$s") — journalctl -u $s -n 30"; fi
done

if [ "${VIDEO_ENABLED:-1}" = "1" ]; then
  echo "RTSP"
  URL="rtsp://127.0.0.1:8554/${RTSP_PATH:-camera_1}"
  if out=$(timeout 15 ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
        -show_entries stream=codec_name,width,height,avg_frame_rate -of csv=p=0 "$URL" 2>&1); then
    ok "$URL → $out"
    info "у формі камери на сервері: rtsp://$(hostname -I | awk '{print $1}'):8554/${RTSP_PATH:-camera_1}"
  else
    bad "$URL не відповідає: $out"
  fi
fi

echo "ROS 2"
NS="${TOPIC_PREFIX:-/polar}/${CAMERA_NS:-camera_1}"
# від імені користувача polar і з тими ж змінними, що в служби (Fast DDS між різними користувачами
# через спільну памʼять може не передавати дані); перший запуск ros2 на Pi — до ~15 с
ROSCMD="set -a; . $ENV_FILE; set +a; [ -z \"\$ROS_DISCOVERY_SERVER\" ] && unset ROS_DISCOVERY_SERVER; \
  source /opt/ros/humble/setup.bash; source /opt/polar/ros2_ws/install/setup.bash"
if out=$(sudo -u polar -H bash -c "$ROSCMD; timeout 20 ros2 topic echo --once $NS/state" 2>&1); then
  ok "$NS/state публікується: $(echo "$out" | grep -E "azimuth_deg|elevation_deg|moving" | xargs | cut -c1-90)"
else
  bad "$NS/state не публікується"
  info "вузли: $(sudo -u polar -H bash -c "$ROSCMD; timeout 15 ros2 node list" 2>/dev/null | xargs)"
  info "журнал вузла: journalctl -u polar-pantilt -n 50 --no-pager"
fi

echo "rosbridge (звʼязок із Polar на Windows)"
BP="${ROSBRIDGE_PORT:-9090}"
if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$BP" 2>/dev/null; then
  ok "порт $BP відкритий; у Polar: «Адреса Raspberry Pi» = $(hostname -I | awk '{print $1}') (або порожньо, якщо RTSP з цього Pi)"
else
  bad "rosbridge не слухає порт $BP — journalctl -u polar-rosbridge -n 30"
fi

echo "Час"
chronyc tracking 2>/dev/null | awk -F': ' '/Reference ID|System time/{print "       " $0}'
