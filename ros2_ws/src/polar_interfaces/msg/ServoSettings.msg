# Налаштування одного сервопривода ST3020 (зберігаються в базі сервера, надсилаються на Pi).
uint8 id                    # 1..253
int8 direction              # +1 прямий, -1 зворотний
float64 zero_offset_deg     # кут вала, що відповідає 0° камери
float64 gear_ratio          # 1 — редуктора немає
float64 max_speed_deg_s     # до ≈ 360
uint8 acceleration          # одиниці сервопривода, 0..254
float64 torque_limit_pct    # 0..100
float64 temperature_limit_c
bool continuous             # true — вісь азимуту з ковзним контактом, без меж
bool has_limits             # false — min_deg/max_deg не застосовуються
float64 min_deg
float64 max_deg
