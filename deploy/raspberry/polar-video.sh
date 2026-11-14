#!/usr/bin/env bash
# Відео з USB AV-перетворювача (CVBS) → деінтерлейсинг → 640×480 → H.264 / MJPEG → RTSP (MediaMTX на цьому ж Pi).
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

# Стандарт (PAL/NTSC) задається лише перетворювачам, що його підтримують (usbtv, stk1160, em28xx).
# UVC-перетворювачі (MacroSilicon MS210x тощо) визначають сигнал самі — для них стандарт не передається.
STD_ARGS=()
if v4l2-ctl -d "$DEV" --get-standard >/dev/null 2>&1; then
  v4l2-ctl -d "$DEV" --set-standard="$V4L_STD" >/dev/null 2>&1 || echo "попередження: стандарт $V4L_STD не встановлено"
  STD_ARGS=(-standard "$V4L_STD")
else
  echo "перетворювач без вибору стандарту (UVC) — PAL/NTSC визначає сам"
fi
v4l2-ctl -d "$DEV" --set-input="$INPUT" >/dev/null 2>&1 || echo "попередження: вхід $INPUT не встановлено"

# формат і розмір: з тих, що перетворювач справді віддає (UVC може мати інший набір розмірів)
FORMATS=$(v4l2-ctl -d "$DEV" --list-formats-ext 2>/dev/null || true)
FMT_ARGS=()
if echo "$FORMATS" | grep -q "'YUYV'"; then
  FMT_ARGS=(-input_format yuyv422)           # без стискання — краще для деінтерлейсингу і детектора
elif echo "$FORMATS" | grep -q "'MJPG'"; then
  FMT_ARGS=(-input_format mjpeg)
fi
if [ -n "$FORMATS" ] && ! echo "$FORMATS" | grep -q "Size: Discrete $SIZE"; then
  for alt in 720x576 720x480 640x480; do
    if echo "$FORMATS" | grep -q "Size: Discrete $alt"; then SIZE=$alt; break; fi
  done
  echo "розмір $SIZE (з підтримуваних перетворювачем)"
fi

# Розмір кадру в потоці. Аналоговий кадр 720×480 / 720×576 — формат 4:3 з НЕквадратними пікселями;
# модель камери в Polar рахує пікселі квадратними, тож auto = 640×480 (4:3, квадратні пікселі). До того ж
# 640 ділиться на 32 — так працює апаратний кодер Pi. VIDEO_OUT_SIZE=same — як знімає перетворювач.
OUT=${VIDEO_OUT_SIZE:-auto}
case "$OUT" in
  auto) case "$SIZE" in 720x480|720x576) OUT=640x480 ;; *) OUT=$SIZE ;; esac ;;
  same) OUT=$SIZE ;;
esac
OW=${OUT%x*}; OH=${OUT#*x}

LAST=""                                                # останнє перетворення формату перед кодером
case "${ENCODER,,}" in
  h264_v4l2m2m|hw|h264_hw) ENCODER=h264_hw ;;
  # MJPEG — повний діапазон яскравості (yuvj420p); лише останнім кроком: geq/overlay мітки часу з yuvj не працюють
  mjpeg|mjpg) ENCODER=mjpeg; LAST=",format=yuvj420p" ;;
esac

FILTERS=""
if [ "${VIDEO_DEINTERLACE:-1}" = "1" ]; then
  FILTERS="yadif=mode=0:parity=auto:deint=0,"          # кадр з двох напівкадрів, 25/30 к/с
fi
[ "$OUT" != "$SIZE" ] && FILTERS="${FILTERS}scale=${OW}:${OH}:flags=bilinear,"
FILTERS="${FILTERS}format=yuv420p"

hw_ok() {   # апаратний кодер Pi 4 (bcm2835-codec) доступний і працює
  ffmpeg -hide_banner -loglevel error -f lavfi -i "testsrc=size=${OUT}:rate=${FPS}" -frames:v 5 \
    -vf format=yuv420p -c:v h264_v4l2m2m -b:v "$BITRATE" -f null - >/dev/null 2>&1
}
# Апаратний кодер вирівнює рядок кадру на 32 пікселі: ширина, що не ділиться на 32 (напр. 720),
# дає нахилені смуги і зелену смугу внизу — тоді лише програмний libx264.
case "$ENCODER" in
  auto)
    if [ $((OW % 32)) -eq 0 ] && hw_ok; then ENCODER=h264_hw; else ENCODER=libx264; fi ;;
  h264_hw)
    if [ $((OW % 32)) -ne 0 ]; then
      echo "попередження: апаратний кодер не працює з шириною $OW (не ділиться на 32) — libx264"; ENCODER=libx264
    elif ! hw_ok; then
      echo "попередження: апаратний кодер недоступний — libx264"; ENCODER=libx264
    fi ;;
  mjpeg|libx264) ;;
  *) echo "невідомий VIDEO_ENCODER=$ENCODER — libx264"; ENCODER=libx264 ;;
esac
case "$ENCODER" in
  h264_hw)   # апаратний H.264: процесор вільний; ключовий кадр щосекунди (кодер не приймає -g)
    ENC=(-c:v h264_v4l2m2m -b:v "$BITRATE" -force_key_frames "expr:gte(t,n_forced*1)") ;;
  mjpeg)     # кожен кадр — окремий JPEG: найменша затримка, без буфера кадрів; потік у 5–10 разів більший
    ENC=(-c:v mjpeg -q:v "${VIDEO_MJPEG_QUALITY:-5}") ;;
  *)         # програмний H.264; sliced-threads — кадр ділиться між ядрами, без затримки на кадр
    ENC=(-c:v libx264 -preset ultrafast -tune zerolatency -x264-params sliced-threads=1 -b:v "$BITRATE"
         -g "$FPS" -bf 0) ;;
esac

# Мітка часу Pi в кадрі — лише на час вимірювання затримки з Polar («Затримка по етапах»): вузол сервоприводів
# пише 1 у файл-прапорець, служба polar-video-ts перезапускає відео. Смужка 224×8 у лівому верхньому куті:
# маркер, 24 біти часу Pi в мс (за модулем 2^24), маркер — див. polar_core/timestamp.py (GEQ_LUM — той самий).
TS_FLAG=${VIDEO_TIMESTAMP_FLAG:-/var/lib/polar/video/timestamp}
GEQ_LUM='if(lt(X,8),235,if(lt(X,16),16,if(gte(X,216),235,if(gte(X,208),16,if(mod(floor(mod(floor(T*1000),16777216)/pow(2,25-floor(X/8))),2),235,16)))))'
VF=(-vf "${FILTERS}${LAST}")
if [ "$(cat "$TS_FLAG" 2>/dev/null)" = "1" ]; then
  # setpts=RTCTIME — час кадру = настінний годинник Pi у мить обробки (≈ захоплення); потім знову від нуля
  VF=(-filter_complex "[0:v]${FILTERS},setpts=RTCTIME/(1000000*TB),split[a][b];[b]crop=224:8:0:0,geq=lum='${GEQ_LUM}':cb=128:cr=128[s];[a][s]overlay=0:0,setpts=PTS-STARTPTS${LAST}[v]" -map "[v]")
  echo "мітка часу в кадрі: увімкнено (вимірювання затримки)"
fi
echo "відео: $DEV ($V4L_STD, вхід $INPUT, $SIZE@$FPS) → $OUT → $ENCODER → rtsp://…:8554/$PATH_NAME"

exec ffmpeg -hide_banner -loglevel warning \
  -f v4l2 -thread_queue_size 64 "${STD_ARGS[@]}" "${FMT_ARGS[@]}" -video_size "$SIZE" -framerate "$FPS" -i "$DEV" \
  "${VF[@]}" "${ENC[@]}" -an \
  -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:8554/$PATH_NAME"
