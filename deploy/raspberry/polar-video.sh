#!/usr/bin/env bash
# Відео з USB AV-перетворювача (CVBS) → деінтерлейсинг → H.264 → RTSP (MediaMTX на цьому ж Pi).
# Запускається службою polar-video (через MediaMTX runOnInit); налаштування — /etc/polar/polar.env.
set -euo pipefail

ENV_FILE=${POLAR_ENV:-/etc/polar/polar.env}
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

DEV=${VIDEO_DEVICE:-/dev/video0}
STD=${VIDEO_STANDARD:-pal}
INPUT=${VIDEO_INPUT:-0}
BITRATE=${VIDEO_BITRATE:-2000000}
PATH_NAME=${RTSP_PATH:-camera_1}
ENCODER=${VIDEO_ENCODER:-auto}

case "${STD,,}" in
  ntsc) SIZE=720x480; FPS=30; V4L_STD=ntsc ;;
  *)    SIZE=720x576; FPS=25; V4L_STD=pal ;;
esac

for i in $(seq 1 30); do                     # перетворювач може зʼявитися не одразу після завантаження
  [ -e "$DEV" ] && break
  echo "чекаю $DEV …"; sleep 2
done
[ -e "$DEV" ] || { echo "немає пристрою $DEV — перевірте перетворювач і VIDEO_DEVICE" >&2; exit 1; }

# стандарт і вхід (не всі перетворювачі це підтримують — тоді лише попередження)
v4l2-ctl -d "$DEV" --set-standard="$V4L_STD" >/dev/null 2>&1 || echo "попередження: стандарт $V4L_STD не встановлено"
v4l2-ctl -d "$DEV" --set-input="$INPUT" >/dev/null 2>&1 || echo "попередження: вхід $INPUT не встановлено"

FILTERS="format=yuv420p"
if [ "${VIDEO_DEINTERLACE:-1}" = "1" ]; then
  FILTERS="yadif=mode=0:parity=auto:deint=0,$FILTERS"   # кадр з двох напівкадрів, 25/30 к/с
fi

hw_ok() {   # апаратний кодер Pi 4 (bcm2835-codec) доступний і працює
  ffmpeg -hide_banner -loglevel error -f lavfi -i "testsrc=size=${SIZE}:rate=${FPS}" -frames:v 5 \
    -vf format=yuv420p -c:v h264_v4l2m2m -b:v "$BITRATE" -f null - >/dev/null 2>&1
}
if [ "$ENCODER" = "auto" ]; then
  if hw_ok; then ENCODER=h264_v4l2m2m; else ENCODER=libx264; fi
fi
if [ "$ENCODER" = "h264_v4l2m2m" ]; then
  ENC=(-c:v h264_v4l2m2m -b:v "$BITRATE" -g "$FPS")
else
  ENC=(-c:v libx264 -preset ultrafast -tune zerolatency -b:v "$BITRATE" -g "$FPS" -bf 0)
fi
echo "відео: $DEV ($V4L_STD, вхід $INPUT, $SIZE@$FPS) → $ENCODER → rtsp://…:8554/$PATH_NAME"

exec ffmpeg -hide_banner -loglevel warning \
  -f v4l2 -thread_queue_size 64 -standard "$V4L_STD" -video_size "$SIZE" -framerate "$FPS" -i "$DEV" \
  -vf "$FILTERS" "${ENC[@]}" -an \
  -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:8554/$PATH_NAME"
