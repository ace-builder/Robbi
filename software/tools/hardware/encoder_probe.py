"""A/B diagnosis independent of Robbi's driver. No robot imports.

By default the motor enable stays LOW for hand rotation. --pulse permits two
350 ms, 60% tests with wheels raised. Compare GPIO polling with kernel edges.
"""

import argparse
import json
import subprocess
import threading
import time


# Standard supported board. Explicit alternate wiring uses the same environment
# setting as the runtime; no robot imports or hardware access during import.
import os
RIGHT_B = int(os.environ.get('ROBBI_RIGHT_ENCODER_B_PIN', '16'))
if RIGHT_B not in (7,16): raise ValueError('Right encoder B must be 7 or 16')
PINS = {27: ('left_a',13), 5: ('left_b',29), 6: ('right_a',31),
        RIGHT_B: ('right_b',36 if RIGHT_B==16 else 26)}
PAIRS = {'left':(27,5),'right':(6,RIGHT_B)}
TRANSITIONS = {(0, 1): 1, (1, 3): 1, (3, 2): 1, (2, 0): 1,
               (1, 0): -1, (3, 1): -1, (2, 3): -1, (0, 2): -1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--pulse", action="store_true", help="WHEELS RAISED: one 350 ms pulse per motor")
    args = parser.parse_args()
    service = subprocess.run(["systemctl", "is-active", "robbi-v2.service"],
                             capture_output=True, text=True)
    if service.stdout.strip() in {"active", "activating", "deactivating"}:
        parser.error("Stop robbi-v2.service first. Do not share GPIO ownership.")
    import lgpio
    chip = lgpio.gpiochip_open(0)
    callbacks = []
    lock = threading.Lock()
    stats, states, counts = {}, {}, {"left": 0, "right": 0}
    enabled_claimed = False
    output_pins = []
    cutout = None
    try:
        lgpio.gpio_claim_output(chip, 26, 0)
        enabled_claimed = True
        if args.pulse:
            for pin in (4, 17, 22, 23):
                lgpio.gpio_claim_output(chip, pin, 0)
                output_pins.append(pin)
        for pin, (name, header) in PINS.items():
            lgpio.gpio_claim_alert(chip, pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
            level = lgpio.gpio_read(chip, pin)
            stats[pin] = dict(name=name, gpio=pin, header_pin=header, level=level,
                              irq_rising=0, irq_falling=0, polled_changes=0,
                              samples_low=0, samples_high=0)
        for side, (a, b) in PAIRS.items():
            states[side] = stats[a]["level"] * 2 + stats[b]["level"]

        def edge(_chip, pin, level, _tick):
            if level not in (0, 1):
                return
            with lock:
                stats[pin]["irq_rising" if level else "irq_falling"] += 1
                side = "left" if pin in PAIRS["left"] else "right"
                a, b = PAIRS[side]
                previous = states[side]
                current = ((level << 1) | (previous & 1)) if pin == a else ((previous & 2) | level)
                counts[side] += TRANSITIONS.get((previous, current), 0)
                states[side] = current

        for pin in PINS:
            callbacks.append(lgpio.callback(chip, pin, lgpio.BOTH_EDGES, edge))
        started = time.monotonic()
        end = started + max(1, min(180, args.seconds))
        next_report = started
        pulse_index = 0
        pulse_end = None
        pulses = [(2.0, "left", 4), (4.0, "right", 22)] if args.pulse else []
        if args.pulse:
            end = max(end, started + 6)
        print("READY: bounded raised-wheel pulses." if args.pulse else
              "READY: motor enable BCM26 held LOW. Turn LEFT, then RIGHT wheel by hand.", flush=True)
        while time.monotonic() < end:
            now = time.monotonic()
            if pulse_end is not None and now >= pulse_end:
                lgpio.gpio_write(chip, 26, 0)
                pulse_end = None
                if cutout:
                    cutout.cancel()
                    cutout.join()
                    cutout = None
            if pulse_index < len(pulses) and now - started >= pulses[pulse_index][0]:
                _, side, pin = pulses[pulse_index]
                # Finite PWM cycles stop even if this polling loop stalls.
                # An independent timer also deasserts the common driver enable.
                cutout = threading.Timer(0.45, lambda: lgpio.gpio_write(chip, 26, 0))
                cutout.daemon = True
                cutout.start()
                lgpio.gpio_write(chip, 26, 1)
                time.sleep(0.002)
                lgpio.tx_pwm(chip, pin, 1000, 60, pulse_cycles=350)
                print(json.dumps({"test_pulse": side, "gpio": pin, "duty": 60, "duration_s": 0.35}), flush=True)
                pulse_end = now + 0.40
                pulse_index += 1
            for pin in PINS:
                level = lgpio.gpio_read(chip, pin)
                with lock:
                    channel = stats[pin]
                    channel["polled_changes"] += int(level != channel["level"])
                    channel["level"] = level
                    channel["samples_high" if level else "samples_low"] += 1
            now = time.monotonic()
            if now >= next_report:
                with lock:
                    print(json.dumps({"elapsed_s": round(now - started, 1),
                                      "counts": counts, "channels": stats,
                                      "motor_enable": lgpio.gpio_read(chip, 26)}), flush=True)
                next_report = now + 1
            time.sleep(0.0005)
    finally:
        if cutout:
            cutout.cancel()
            cutout.join()
        if enabled_claimed:
            lgpio.gpio_write(chip, 26, 0)
        for pin in output_pins:
            lgpio.tx_pwm(chip, pin, 1000, 0)
            lgpio.gpio_write(chip, pin, 0)
        for callback in callbacks:
            callback.cancel()
        lgpio.gpiochip_close(chip)


if __name__ == "__main__":
    main()
