"""systemd ExecStartPre/ExecStopPost: force only the PCB driver enable LOW."""

import lgpio

chip = lgpio.gpiochip_open(0)
try:
    lgpio.gpio_claim_output(chip, 26, 0)  # BCM ROBOT_EN/nSLEEP, not a PWM pin
    lgpio.gpio_write(chip, 26, 0)
finally:
    lgpio.gpiochip_close(chip)
