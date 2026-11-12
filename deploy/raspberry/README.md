# Raspberry Pi камери Polar: встановлення (Ubuntu 22.04)

Один Raspberry Pi 4B на камеру. Він:
* керує двома сервоприводами ST3020 через Bus Servo Driver HAT (A) — вузол ROS 2 `pantilt_driver`;
* (за потреби) віддає відео аналогової камери з **USB AV-перетворювача (CVBS)** RTSP-потоком —
  MediaMTX + ffmpeg з деінтерлейсингом і апаратним H.264.

Підключення дротів, живлення і ID сервоприводів — у [WIRING.md](WIRING.md).

## 1. Ubuntu на SD-карту

1. Raspberry Pi Imager → *Other general-purpose OS* → **Ubuntu Server 22.04 LTS (64-bit)**.
2. У налаштуваннях Imager (шестерня): імʼя компʼютера (напр. `polar-cam1`), користувач і пароль, SSH.
3. Вставити карту, підключити Ethernet і живлення (через HAT, див. WIRING.md), увійти по SSH.

## 2. Встановлення

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/aklex1978-design/Polar.git
cd Polar
sudo ./deploy/raspberry/install.sh
```

Скрипт:
1. вмикає UART на GPIO14/15 (`enable_uart=1`, `dtoverlay=disable-bt`), прибирає з нього консоль;
2. ставить ROS 2 Humble (ros-base), Cyclone DDS, chrony, v4l-utils, ffmpeg;
3. створює системного користувача `polar` з доступом до UART і відео;
4. збирає `polar_interfaces` і `pantilt_driver` у `/opt/polar/ros2_ws`;
5. ставить MediaMTX і скрипт відео в `/opt/polar`;
6. створює `/etc/polar/polar.env` і `/etc/polar/pantilt.yaml` (якщо їх ще немає);
7. налаштовує chrony;
8. вмикає служби `polar-pantilt` і (якщо `VIDEO_ENABLED=1`) `polar-video`.

Після першого запуску — **`sudo reboot`** (змінено UART).

## 3. Налаштування камери

```bash
sudo nano /etc/polar/polar.env
sudo systemctl restart polar-pantilt polar-video
```

| Параметр | Що вписати |
|---|---|
| `CAMERA_NS` | простір імен з форми камери на сервері, напр. `camera_1` |
| `TOPIC_PREFIX`, `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `ROS_DISCOVERY_SERVER` | як у «Налаштування → ROS 2» на сервері |
| `SERVO_BACKEND` | `st3020`; `sim` — без механіки, для перевірки звʼязку |
| `VIDEO_ENABLED` | `1` — відео з перетворювача через цей Pi; `0` — відео окремо (IP-камера) |
| `VIDEO_DEVICE` | `/dev/video0` або стабільна назва з `/dev/v4l/by-id/…` |
| `VIDEO_STANDARD`, `VIDEO_INPUT` | `pal` (720×576, 25 к/с) або `ntsc`; вхід 0 — зазвичай Composite |
| `RTSP_PATH` | імʼя потоку; адреса — `rtsp://<IP Pi>:8554/<RTSP_PATH>` |
| `NTP_SERVER` | адреса сервера часу (бажано — той самий, що в сервера) |

Межі, швидкості й нуль сервоприводів задаються з сервера («Камери → Управління»): сервер надсилає їх
вузлу при підключенні; `/etc/polar/pantilt.yaml` — лише стартові значення.

**Після зміни `VIDEO_ENABLED`** запустіть `sudo ./deploy/raspberry/install.sh` ще раз — він увімкне або
вимкне службу відео.

## 4. На сервері

* Форма камери: **протокол RTSP**, адреса `rtsp://<IP Pi>:8554/camera_1`, роздільна здатність 720×576
  (PAL) або 720×480 (NTSC), кадри за секунду 25 / 30, HFOV — кут огляду обʼєктива аналогової камери.
  «Перевірити зʼєднання» має показати кадр.
* «Простір імен ROS 2» у формі = `CAMERA_NS`; демо сервоприводів — вимкнено.
* Затримка відео через Pi (захоплення + деінтерлейсинг + кодування) — близько 100–200 мс; її вимірює і
  враховує програма (поле затримки в параметрах камери).
* Аналоговий сигнал шумніший за цифровий — у «Налаштування → Виявлення» поріг різниці, ймовірно, треба
  підняти (підбирається на випробуваннях).

## 5. Перевірка

```bash
sudo ./deploy/raspberry/check.sh
```

Показує: UART (`/dev/serial0`), перетворювач (чип, входи, стандарт), стан служб, чи віддається RTSP
(кодек, розмір, кадри), чи публікується стан камери в ROS 2, синхронізацію часу.

Журнали служб: `journalctl -u polar-pantilt -f`, `journalctl -u polar-video -f`.

## USB AV-перетворювач (CVBS): що підходить

| Чип (`lsusb`) | Драйвер Linux | Підходить |
|---|---|---|
| MacroSilicon MS210x (`534d:…`), клас UVC | `uvcvideo` | так — без налаштувань |
| Fushicai UTV007 «EasyCAP» (`1b71:3002`) | `usbtv` | так |
| Syntek STK1160 «EasyCAP» (`05e1:0408`) | `stk1160` | так |
| Empia EM28xx (`eb1a:…`) | `em28xx` | так |
| Somagic SMI2021 «EasyCAP» (`1c88:…`) | — | **ні** — немає драйвера |

**Деінтерлейсинг обовʼязковий** (`VIDEO_DEINTERLACE=1`): аналоговий кадр складається з двох
напівкадрів, і без нього рухомі обʼєкти мають «гребінку», яку детектор сприймає як рух.

Якщо кадр чорний або чорно-білий — інший вхід (`VIDEO_INPUT=1`) чи стандарт (`VIDEO_STANDARD=ntsc`).
Якщо апаратний кодер недоступний, скрипт сам переходить на програмний `libx264` (720×576@25 — Pi 4
справляється, але гріється: потрібне охолодження).
