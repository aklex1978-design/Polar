# Стан камери: поточні кути з енкодерів сервоприводів і телеметрія.
std_msgs/Header header   # stamp — час зчитування енкодерів

float64 azimuth_deg      # 0..360, кут камери (без зміщення від півночі)
float64 elevation_deg    # 0..90
bool moving

uint16 ERROR_NONE = 0
uint16 ERROR_SERVO_OFFLINE = 1
uint16 ERROR_OVERHEAT = 2
uint16 ERROR_LINK_TIMEOUT = 4   # немає команд/heartbeat від сервера — сервоприводи зупинено
uint16 ERROR_STOPPED = 8        # спрацював «Стоп»
uint16 error_flags               # бітова маска з констант вище

ServoTelemetry pan       # горизонтальний сервопривід (азимут)
ServoTelemetry tilt      # вертикальний сервопривід (кут місця)
