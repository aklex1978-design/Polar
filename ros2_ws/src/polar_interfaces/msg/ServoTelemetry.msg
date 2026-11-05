# Телеметрія одного сервопривода ST3020.
uint8 id
bool online              # сервопривід відповідає на шині
float64 position_deg     # кут осі камери (з урахуванням нуля і напрямку)
float64 speed_deg_s
float32 load_pct
float32 voltage_v
float32 current_a
float32 temperature_c
bool moving
