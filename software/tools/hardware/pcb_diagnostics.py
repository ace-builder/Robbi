"""Explicit PCB bring-up commands. Stop robbi-v2.service before running."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test", choices=("tof", "encoders", "left", "right", "servo"))
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--duty", type=float, default=35)
    parser.add_argument("--servo", type=int, choices=(1, 2), default=1)
    parser.add_argument("--pulse-us", type=int, default=1500)
    args = parser.parse_args()
    state = subprocess.run(["systemctl", "is-active", "robbi-v2.service"], capture_output=True, text=True)
    if state.stdout.strip() in {"active", "activating", "deactivating"}:
        parser.error("Stop robbi-v2.service first; hardware has a single owner.")
    if args.test == "tof":
        from robot.tof import ToFService
        service = ToFService()
        try:
            service.start()
            until = time.monotonic() + max(1, min(60, args.seconds))
            while time.monotonic() < until:
                print(json.dumps(service.status()), flush=True)
                time.sleep(0.5)
        finally:
            service.shutdown()
    elif args.test == "servo":
        from robot.config import settings
        from robot.pcb import available_servo_pin
        if not settings.enable_servos:
            parser.error("Servos disabled. Select pulse limits for your servo before enabling hardware.enable_servos.")
        try:
            pin = available_servo_pin(args.servo)
        except ValueError as exc:
            parser.error(str(exc))
        pin_state = subprocess.check_output(["pinctrl", "get", "7,9"], text=True)
        if Path("/dev/spidev0.1").exists() or "SPI0" in pin_state:
            parser.error("Apply dtoverlay=spi0-1cs,no_miso and reboot before servo testing.")
        if not 1000 <= args.pulse_us <= 2000:
            parser.error("This test supports only 1000..2000 us at 50 Hz. Confirm your servo supports this range.")
        import lgpio
        chip = lgpio.gpiochip_open(0)
        try:
            lgpio.gpio_claim_output(chip, pin, 0)
            lgpio.tx_servo(chip, pin, args.pulse_us, servo_frequency=50)
            time.sleep(max(0.1, min(2, args.seconds)))
        finally:
            lgpio.tx_servo(chip, pin, 0)
            lgpio.gpio_write(chip, pin, 0)
            lgpio.gpiochip_close(chip)
    else:
        from robot import motor_gpio as motor
        try:
            motor.initialize()
            motor.reset_counts()
            if args.test != "encoders":
                # Explicit one-wheel test is capped to one second and 55%.
                duty = max(-55, min(55, args.duty))
                motor.set_wheel_pwm(duty if args.test == "left" else 0,
                                    duty if args.test == "right" else 0)
            until = time.monotonic() + max(0.1, min(60 if args.test == "encoders" else 1, args.seconds))
            while time.monotonic() < until:
                print(json.dumps({"counts": motor.counts(), "raw": motor.physical_counts(),
                                  "levels": motor.raw_levels(), "driver": motor.driver_status()}), flush=True)
                time.sleep(0.1)
        finally:
            motor.cleanup()


if __name__ == "__main__":
    main()
