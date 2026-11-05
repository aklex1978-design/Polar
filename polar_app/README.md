# polar_app

Настільна програма Polar (PyQt5) і вузол керування камерами `polar_control`.

Зараз (етап 1) є екран «Камери / Управління»: список камер зі станом звʼязку, поточні й цільові кути,
ручне керування, «Стоп», налаштування сервоприводів і телеметрія. Решта меню — на етапі 8.

```bash
# Без обладнання і без ROS — камери-імітатори з логікою pantilt_driver
python3 -m pip install -e ../polar_core -e ../ros2_ws/src/pantilt_driver -e ".[gui]"
python3 -m polar_app --sim --demo

# З камерами через ROS 2 (спочатку source /opt/ros/humble/setup.bash і ros2_ws/install/setup.bash)
python3 -m polar_app --db polar.sqlite
```

`--demo` створює тимчасову базу з трьома камерами; без нього використовується `--db`.
