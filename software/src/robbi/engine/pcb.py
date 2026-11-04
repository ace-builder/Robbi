"""Custom PCB v1 pin ownership. All numbers are BCM GPIO, never header pins."""

import os

ROBOT_EN = 26
LEFT_MOTOR = (4, 17)
RIGHT_MOTOR = (22, 23)
LEFT_ENCODER = (27, 5)
# Fresh installs explicitly select BCM16 through hardware.right_encoder_b_pin.
# BCM7 is available only when explicitly selected for alternate wiring.
# The alternate BCM7 route requires spi0-1cs,no_miso and reserves Servo 1.
RIGHT_ENCODER = (6, int(os.environ.get("ROBBI_RIGHT_ENCODER_B_PIN", "16")))
if RIGHT_ENCODER[1] not in (7, 16):
    raise ValueError("Right encoder B must be BCM16 or BCM7")
TOF_CHANNELS = {"h1": (13, 0x30), "h2": (12, 0x31)}
SERVO_PINS = (7, 9)
I2C_BUS = 1
TOF_BOOT_ADDRESS = 0x29
IMU_ADDRESSES = (0x6A, 0x6B)  # U4 SA0 selects the address; verify WHO_AM_I first.

# Full configurable PWM range, as requested for the custom PCB. The active
# limit comes from motion.pwm_max_duty. PWM is not voltage regulation.
MOTOR_MAX_DUTY = 100.0
MOTOR_WAKE_S = 0.002


def available_servo_pin(number: int) -> int:
    """Reject servo outputs on a signal reassigned to encoder input."""
    if number not in (1, 2):
        raise ValueError("Servo number must be 1 or 2")
    pin = SERVO_PINS[number - 1]
    if pin in (*LEFT_ENCODER, *RIGHT_ENCODER):
        raise ValueError(f"Servo {number} disabled: BCM GPIO{pin} is reserved for an encoder")
    return pin
