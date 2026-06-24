# Resurgo Crane Controller

## Claw / Servo Not Responding?

If the claw servo stops working or becomes unresponsive, the I2C bus has likely crashed. This can happen after a power cycle or unexpected shutdown.

**Fix:** Run the command in `I2C_Bus_reset.txt`.

Open a terminal and enter:

```
sudo rmmod i2c_designware_platform i2c_designware_core && sleep 2 && sudo modprobe i2c_designware_platform
```

Then restart the crane controller.

---

## I2C Wiring (Required Pins)

The PCA9685 servo board communicates over I2C. It **must** be connected to these Raspberry Pi pins:

| Signal | Raspberry Pi Pin | GPIO |
|--------|-----------------|------|
| SDA (Data) | Physical Pin 3 | GPIO 2 |
| SCL (Clock) | Physical Pin 5 | GPIO 3 |
| 3.3V Power | Physical Pin 1 | — |
| Ground | Physical Pin 6 | — |

> **Note:** These pins are fixed by the Raspberry Pi hardware — I2C bus 1 only works on GPIO 2 (SDA) and GPIO 3 (SCL). Using any other pins will not work.

---

## Settings GUI

The settings GUI lets you change motor speeds, direction pins, servo travel limits, joystick GPIO pins, and microswitch pins — all without editing `config.json` by hand.

### How to start it

Open a terminal in the project folder and run:

```
python "settings_gui 2.py"
```

> The GUI automatically stops the crane service while it is open and restarts it when you close it, so you don't need to do that manually.

### What you can change

| Section | What it controls |
|---------|-----------------|
| **Motors 1–4** | Step delay (speed), direction (forward/reverse), step pin, direction pin |
| **Claw Servo Limits** | Min and max angle (degrees) that Servo 1 (claw) is allowed to travel |
| **Microswitches** | GPIO pin numbers for each limit switch |
| **Joystick GPIO** | GPIO pin numbers for each joystick/button input |
| **Motor 4 Limits** | Min and max step counts for the claw height motor travel |

### How to save or revert

- **Save** — Click the green **Save** button. Changes are written to `config.json` immediately. A timestamped backup of the previous config is created automatically.
- **Revert** — Click the red **Revert** button next to any field (or the main Revert button) to undo unsaved changes back to the last saved values.
- Changed fields are highlighted in **orange** so you can see what has been modified before saving.

---

## Files

| File | Purpose |
|------|---------|
| `Newjoystick4.py` | Main crane controller (joystick, motors, servos) |
| `crane_web.py` | Web interface on port 5200 |
| `settings_gui 2.py` | pygame settings editor (motor speeds, servo limits, etc.) |
| `config.json` | All hardware configuration (pins, speeds, limits) |
| `motor4_position.json` | Saved step position for Motor 4 (persists across restarts) |
| `I2C_Bus_reset.txt` | I2C bus reset command for when servos stop working |
| `gpio_cleanup.py` | GPIO cleanup helper |
