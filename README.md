# Polar

Система спостереження з поворотними камерами: будує панораму 360° × 90° для кожної камери,
виявляє рух, порівнюючи кадри з панорамою, і визначає координати цілей за кількома камерами.

Технічне завдання та план розробки ведуться в окремому документі; цей репозиторій містить код.

## Структура

| Каталог | Що там | Де працює |
| --- | --- | --- |
| `ros2_ws/src/polar_interfaces` | Повідомлення ROS 2: команди камерам і їхній стан | Сервер і Raspberry Pi |
| `ros2_ws/src/pantilt_driver` | Вузол ROS 2, що керує двома сервоприводами ST3020 камери через Bus Servo Driver HAT (A); має режим імітатора | Raspberry Pi 4B (ROS 2 Humble, Ubuntu 22.04) |
| `polar_core` | Бібліотека Python: кути, схема бази SQLite, імітатор камери; далі — проєкція, панорама, виявлення руху, координати цілей | Сервер |
| `polar_app` | Настільна програма на PyQt (етап 8) | Сервер |

## Етапи

Код пишеться за етапами з «Плану розробки». Зараз — **етап 0, підготовка**:
структура репозиторію, інтерфейси ROS 2, імітатор сервоприводів і камери, схема бази даних, автоматичні тести.

## Швидкий старт

### Бібліотека `polar_core` і тести (без обладнання)

```bash
cd polar_core
python3 -m pip install -e ".[test]"
python3 -m pytest
```

### Логіка керування сервоприводами (без ROS)

```bash
cd ros2_ws/src/pantilt_driver
python3 -m pytest test
```

### Вузли ROS 2 (Ubuntu 22.04, ROS 2 Humble)

```bash
cd ros2_ws
source /opt/ros/humble/setup.bash
colcon build
source install/setup.bash

# Імітатор: працює без сервоприводів
ros2 launch pantilt_driver pantilt.launch.py backend:=sim camera:=camera_1

# Реальні сервоприводи ST3020 на Raspberry Pi
ros2 launch pantilt_driver pantilt.launch.py backend:=st3020 camera:=camera_1
```

Перевірка:

```bash
ros2 topic echo /polar/camera_1/state
ros2 topic pub --once /polar/camera_1/cmd polar_interfaces/msg/PanTiltCommand \
  "{azimuth_deg: 90.0, elevation_deg: 30.0, speed_deg_s: 60.0}"
ros2 service call /polar/camera_1/stop std_srvs/srv/Trigger
```

## Інтерфейс ROS 2

Простір імен камери — `/polar/<камера>`. Кути — у градусах, кути камери (до зміщення від півночі,
яке додає сервер).

| Топік / сервіс | Тип | Напрямок |
| --- | --- | --- |
| `cmd` | `polar_interfaces/msg/PanTiltCommand` | Сервер → Pi |
| `jog` | `polar_interfaces/msg/PanTiltJog` | Сервер → Pi |
| `state` | `polar_interfaces/msg/PanTiltState` | Pi → сервер |
| `heartbeat` | `std_msgs/msg/Empty` | Сервер → Pi |
| `stop` | `std_srvs/srv/Trigger` | Сервер → Pi |

Якщо Pi довше за тайм-аут не отримує від сервера жодної команди чи сигналу присутності
(`heartbeat`, `std_msgs/msg/Empty`), він зупиняє сервоприводи.

## Що ще треба перевірити на обладнанні

- Адреси регістрів ST3020 у `pantilt_driver/st3020.py` взято з протоколу Feetech STS (позначено `TODO: verify`);
  їх треба звірити з посібником ST3020 і перевірити на сервоприводі.
- Скидання лічильника обертів азимуту без руху вала (`rebase`) для ST3020 поки не реалізовано:
  до перевірки камера працює в межах ±7,5 оберту від нуля.
