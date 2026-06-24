#!/usr/bin/env python3
"""
claw_test.py — Interactive claw servo limit finder.
Controls the claw servo (PCA9685 channel 0, 40 Hz) and lets you mark
the open and closed positions so the limits can be saved to config.json.

Commands (type and press Enter):
  +<n>   move forward by n degrees, e.g.  +10
  -<n>   move backward by n degrees, e.g. -10
  <n>    go directly to angle n, e.g.      45
  open   mark current angle as OPEN limit (max_angle)
  close  mark current angle as CLOSED limit (min_angle)
  save   save marked limits to config.json and quit
  q      quit without saving
"""

import json
import sys
import os

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
CHANNEL = 0
PWM_FREQ = 40
PULSE_MIN = 60
PULSE_MAX = 409

def angle_to_pulse(angle):
    return int(PULSE_MIN + (angle / 180.0) * (PULSE_MAX - PULSE_MIN))

def set_angle(pca, angle):
    angle = max(0.0, min(180.0, float(angle)))
    pca.set_pwm(CHANNEL, 0, angle_to_pulse(angle))
    return angle

def status(angle, marked_open, marked_closed):
    pulse = angle_to_pulse(angle)
    open_str   = f"{marked_open:.1f}°"   if marked_open   is not None else "not set"
    closed_str = f"{marked_closed:.1f}°" if marked_closed is not None else "not set"
    print(f"  angle={angle:.1f}°  pulse={pulse}  | open={open_str}  closed={closed_str}")

def main():
    try:
        import Adafruit_PCA9685
    except ImportError:
        print("ERROR: Adafruit_PCA9685 not installed.")
        sys.exit(1)

    pca = Adafruit_PCA9685.PCA9685(busnum=1)
    pca.set_pwm_freq(PWM_FREQ)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    angle = 90.0  # start at mid-range
    marked_open = None
    marked_closed = None

    angle = set_angle(pca, angle)

    print("\n=== Claw Servo Test ===")
    print("  +<n>   move up n degrees      e.g. +10")
    print("  -<n>   move down n degrees    e.g. -10")
    print("  <n>    go to exact angle       e.g. 45")
    print("  open   mark current as OPEN limit")
    print("  close  mark current as CLOSED limit")
    print("  save   save limits to config.json")
    print("  q      quit without saving\n")
    status(angle, marked_open, marked_closed)

    while True:
        try:
            cmd = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Quit without saving.")
            break

        if not cmd:
            status(angle, marked_open, marked_closed)
            continue

        if cmd == 'q':
            print("  Quit without saving.")
            break
        elif cmd == 'open':
            marked_open = angle
            print(f"  → OPEN marked at {angle:.1f}°")
        elif cmd == 'close':
            marked_closed = angle
            print(f"  → CLOSED marked at {angle:.1f}°")
        elif cmd == 'save':
            if marked_open is None or marked_closed is None:
                print("  Mark both open and close positions first.")
                continue
            config['servo1_limits'] = {
                'min_angle': marked_closed,
                'max_angle': marked_open
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"  Saved: min_angle={marked_closed:.1f}° (closed)  max_angle={marked_open:.1f}° (open)")
            break
        elif cmd.startswith('+'):
            try:
                angle = set_angle(pca, angle + float(cmd[1:]))
                status(angle, marked_open, marked_closed)
            except ValueError:
                print("  Usage: +<degrees>  e.g.  +10")
        elif cmd.startswith('-'):
            try:
                angle = set_angle(pca, angle - float(cmd[1:]))
                status(angle, marked_open, marked_closed)
            except ValueError:
                print("  Usage: -<degrees>  e.g.  -10")
        else:
            try:
                angle = set_angle(pca, float(cmd))
                status(angle, marked_open, marked_closed)
            except ValueError:
                print("  Unknown command. Try: +10  -5  45  open  close  save  q")

    pca.set_pwm(CHANNEL, 0, 0)

if __name__ == '__main__':
    main()
