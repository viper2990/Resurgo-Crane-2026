import json
import os
import signal
import subprocess
import sys
import RPi.GPIO as GPIO
import time
import threading
import Adafruit_PCA9685
from typing import List, Dict, Optional
from gpio_cleanup import init_gpio, cleanup_gpio


# Module overview:
# - MotorController handles low-level stepper pulse generation for one motor.
# - JoystickMotorController maps joystick/buttons/microswitches to motors and servos.
# - Naming is mostly standard Python descriptive naming, not strict Hungarian notation.

# Minimum delay (seconds) between stepper pulses in the software stepping loop.
# Sub-millisecond sleeps (e.g. the configured 5e-06) are below the Linux scheduler's
# timer granularity, so time.sleep() cannot actually suspend the thread: the loop
# spins, issuing a clock_nanosleep + GPIO write syscall storm that pins a full CPU
# core (80%+ system time) and pushes the Pi toward thermal throttle. Clamping to a
# sane floor lets the OS idle the core between steps. 5e-04 -> ~2000 steps/sec, which
# is well above what these crane axes need mechanically. Tune if you need more speed.
MIN_STEP_DELAY = 0.0002


class MotorController:
    """Controls a stepper motor using GPIO pins."""
    
    def __init__(self, motor_config: Dict, motor_id: int, min_steps: Optional[int] = None, max_steps: Optional[int] = None):
        """
        Initialize motor controller.
        
        Args:
            motor_config: Dict with 'step_pin', 'dir_pin', 'speed', 'direction'
            motor_id: Identifier for this motor
        """
        # Store motor identity and the GPIO/speed settings loaded from config.json.
        self.motor_id = motor_id
        self.step_pin = motor_config.get('step_pin')
        self.dir_pin = motor_config.get('dir_pin')
        self.speed = motor_config.get('speed', 0)
        self.direction = motor_config.get('direction', 'forward')
        self.is_active = False

        # Track optional software step limits and the current estimated position.
        self.min_steps = min_steps
        self.max_steps = max_steps
        self.current_steps = 0
        self._last_dir_state = None   # Tracks last written dir_pin state to avoid redundant writes
        
        # Only create the GPIO outputs and worker thread when this motor is wired.
        if self.step_pin and self.dir_pin:
            GPIO.setup(self.step_pin, GPIO.OUT)
            GPIO.setup(self.dir_pin, GPIO.OUT)
            GPIO.output(self.step_pin, GPIO.LOW)
            GPIO.output(self.dir_pin, GPIO.LOW)
            self._motor_thread = threading.Thread(target=self._step_motor, daemon=True)
            self._motor_thread.start()
    
    def _step_motor(self):
        """Run motor stepping loop."""
        # If the motor has no configured pins, leave the worker idle.
        if not self.step_pin or not self.dir_pin:
            return

        # Absolute time the next step is due. Stepping sleeps until this instant rather
        # than for a fixed gap, so the rate stays constant regardless of system load.
        next_step = time.perf_counter()
        while True:
            if self.is_active and self.speed > 0:
                # Motor 4 uses a stored step count so it can stop at configured travel limits.
                if self.motor_id == 3 and self.max_steps is not None:
                    if self.direction == 'forward' and self.current_steps >= self.max_steps:
                        self.is_active = False
                        print("[LIMIT] Motor 4 at upper limit, stopping.")
                        continue
                    if self.min_steps is not None and self.direction == 'reverse' and self.current_steps <= self.min_steps:
                        self.is_active = False
                        print("[LIMIT] Motor 4 at lower limit, stopping.")
                        continue

                # Only update the direction pin when it actually changes.
                # Writing DIR on every step can cause the driver to misread direction, producing rattle.
                dir_state = GPIO.HIGH if self.direction == 'forward' else GPIO.LOW
                if dir_state != self._last_dir_state:
                    GPIO.output(self.dir_pin, dir_state)
                    self._last_dir_state = dir_state
                    time.sleep(0.001)  # DIR setup time: let driver latch new direction before stepping
                    next_step = time.perf_counter()  # rebase schedule after the DIR setup pause

                # One short HIGH/LOW pulse advances the stepper driver by one step.
                GPIO.output(self.step_pin, GPIO.HIGH)
                time.sleep(0.00001)
                GPIO.output(self.step_pin, GPIO.LOW)

                # Keep the software position counter in sync for the limited lift motor.
                if self.motor_id == 3 and self.max_steps is not None:
                    if self.direction == 'forward':
                        self.current_steps += 1
                    else:
                        self.current_steps -= 1

                # Constant-rate scheduling: advance the due time by one period and sleep
                # until then, so load-induced jitter cannot make the motor run fast or slow.
                # The period is clamped to MIN_STEP_DELAY so a too-small configured speed
                # cannot turn this into a CPU-maxing busy-spin (see MIN_STEP_DELAY note above).
                # If we've fallen behind schedule, rebase to 'now' instead of firing rapid
                # catch-up steps -- that catch-up is exactly what causes speed bursts.
                next_step += max(self.speed, MIN_STEP_DELAY)
                now = time.perf_counter()
                remaining = next_step - now
                if remaining > 0:
                    time.sleep(remaining)
                else:
                    next_step = now
            else:
                time.sleep(0.01)
                next_step = time.perf_counter()  # rebase so the first step after idle isn't early
    
    def set_speed(self, speed: float):
        """Set motor speed (delay between steps in seconds)."""
        self.speed = max(0, speed)
    
    def set_direction(self, direction: str):
        """Set motor direction ('forward' or 'reverse')."""
        if direction in ['forward', 'reverse']:
            self.direction = direction
    
    def start(self):
        """Start motor."""
        self.is_active = True
    
    def stop(self):
        """Stop motor."""
        self.is_active = False
    
    def cleanup(self):
        """Clean up GPIO pins."""
        if self.step_pin and self.dir_pin:
            GPIO.output(self.step_pin, GPIO.LOW)
            GPIO.output(self.dir_pin, GPIO.LOW)


class JoystickMotorController:
    """Main controller coordinating joystick input with motor control."""
    
    def __init__(self, config_path: str = 'config.json'):
        """
        Initialize the joystick motor controller.
        
        Args:
            config_path: Path to config.json file
        """
        # Bring GPIO into a known state before any pins or callbacks are configured.
        init_gpio()
        
        self.config_path = config_path
        
        # Load the full hardware and behavior configuration from disk.
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        
        # Restore motor 4's last saved step position so lift limits persist across restarts.
        self._motor4_state_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), 'motor4_position.json')
        motor4_cfg = self.config.get('motor4_limit', {})
        _default_max = motor4_cfg.get('max_steps', 10000)
        saved_motor4_steps: int = self._load_motor4_position(first_run_default=_default_max) or 0

        # Build one MotorController per configured motor; motor 4 also gets travel limits.
        self.motors: List[MotorController] = []
        for i, motor_config in enumerate(self.config.get('motors', [])):
            # Only motor 4 (index 3) uses persisted step tracking and min/max bounds.
            if i == 3:
                # These limits are editable in the settings GUI and enforced in software.
                min_steps = self.config.get('motor4_limit', {}).get('min_steps', 0)
                max_steps = self.config.get('motor4_limit', {}).get('max_steps', 10000)
                motor = MotorController(motor_config, i, min_steps=min_steps, max_steps=max_steps)
                motor.current_steps = saved_motor4_steps
                print(f"Motor 4 restored to step position: {saved_motor4_steps}")
            else:
                motor = MotorController(motor_config, i)
            self.motors.append(motor)
        
        # Configure limit switches that can immediately block unsafe movement directions.
        self.microswitch_pins = self.config.get('microswitches', {}).get('gpio_pins', [0, 0, 0, 0, 0])
        self._setup_microswitch_pins()
        
        # Track joystick/button inputs and current operator state.
        self.joystick_pins = self.config.get('joystick', {}).get('gpio_pins', [])
        self.button_states = {}
        self.running = False
        self.joystick_button_active = False  # Track if a button is currently held
        self.last_activity = time.time()  # Tracks any crane movement for servo1 idle timeout
        self.button1_pressed = False  # Track if button1 is currently pressed
        self.button2_pressed = False  # Track if button2 is currently pressed (servo control)
        
        # These flags remember which direction is currently being held.
        self.joystick_up_active = False
        self.joystick_down_active = False
        self.joystick_left_active = False
        self.joystick_right_active = False

        # Blocks all joystick input while the startup homing routine is running.
        self.homing = False

        # Latch flags block restart until the operator releases the joystick after a limit hit.
        self.ms_latch_down = False
        self.ms_latch_up = False
        self.ms_latch_left = False
        self.ms_latch_right = False
        self.ms_latch_motor4_up = False  # Blocks Motor 4 UP after top-of-range switch fires
        
        # Servo 1 uses the PCA9685 board for the primary claw/arm servo motion.
        self.servo_channel = 0  # Servo on PCA9685 channel 0
        self.servo_angle = 45  # Start at center
        self.servo_speed = 2  # Degrees per step
        self.servo_direction = 0  # -1=left, 0=stopped, 1=right
        _s1lim = self.config.get('servo1_limits', {})
        self.servo_min_angle = float(_s1lim.get('min_angle', 0.0))
        self.servo_max_angle = float(_s1lim.get('max_angle', 45.0))
        
        # Servo 2 is the MG995 auxiliary servo controlled with Button1 + LEFT/RIGHT.
        self.servo2_channel = 1
        self.servo2_angle = 90  # Start at center (0-180)
        self.servo2_speed = 2  # Degrees per step
        self.servo2_direction = 0  # -1=left, 0=stopped, 1=right
        try:
            self.pca = Adafruit_PCA9685.PCA9685(busnum=1)
            self.pca.set_pwm_freq(40)  # 40Hz for servos (reduced from 50Hz to lower heat)
            print(f"PCA9685 servo initialized on channel {self.servo_channel}")
            print(f"PCA9685 MG995 servo initialized on channel {self.servo2_channel}")
            # Servo motion runs in its own loop so button callbacks stay responsive.
            self._servo_thread = threading.Thread(target=self._servo_loop, daemon=True)
            self._servo_thread.start()
        except Exception as e:
            print(f"Warning: PCA9685 not available: {e}")
            self.pca = None
        
        # Configure joystick event callbacks after the rest of the controller state is ready.
        self._setup_joystick_pins()

        # Now that every state flag exists, start polling the microswitches.
        if self._ms_pins:
            self._ms_poll_thread = threading.Thread(target=self._microswitch_poll_loop, daemon=True)
            self._ms_poll_thread.start()
    
    def _setup_microswitch_pins(self):
        """Set up GPIO pins for microswitch inputs (active LOW with pull-up).

        These pins are POLLED in a background thread rather than using edge
        interrupts. Floating/EMI-prone switch lines were generating a GPIO
        interrupt storm (hundreds of thousands of edges/sec on GPIO 9/10/18) that
        saturated the kernel IRQ threads and the RPi.GPIO callback thread, pinning
        a CPU core even while the crane was idle. Polling reads the level at a
        fixed, modest rate, so electrical noise on the lines costs nothing.
        """
        # List of wired microswitch GPIO pins to poll.
        self._ms_pins: List[int] = []
        for i, pin in enumerate(self.microswitch_pins):
            if pin and pin > 0:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                self._ms_pins.append(pin)
                print(f"Microswitch {i} configured on GPIO {pin} (polled)")
        # The polling thread itself is started at the end of __init__, once all of
        # the controller's state flags (homing, latch/active flags) exist, since the
        # poll loop and its callback read them.

    def _pin_stable(self, pin: int, level: int, samples: int = 8, spacing: float = 0.0006) -> bool:
        """Return True only if `pin` reads `level` on every sample across a short window.

        A genuinely closed (or open) switch holds its level steadily for many ms.
        Stepper-motor EMI on the switch lines produces only brief sub-millisecond
        blips, which cannot survive ~5 ms of consecutive reads — so this rejects the
        false triggers that were stopping motion the instant a motor started.
        """
        for _ in range(samples):
            if GPIO.input(pin) != level:
                return False
            time.sleep(spacing)
        return True

    def _microswitch_poll_loop(self):
        """Poll microswitch inputs and act on confirmed (debounced) switch presses.

        Replaces GPIO.add_event_detect for the limit switches. A raw LOW read only
        triggers the handler after _pin_stable confirms the line is solidly LOW,
        which filters out motor-EMI transients. The per-pin `pressed` latch makes
        the handler fire once per genuine press rather than repeatedly while held.
        """
        POLL_INTERVAL = 0.003   # ~330 Hz scan; confirmation adds ~5 ms only on a LOW
        pressed = {pin: False for pin in self._ms_pins}
        while True:
            # Homing reads MS4 directly and blocks joystick input, so pause polling
            # while it runs to avoid double-handling the same switch.
            if self.homing:
                time.sleep(0.02)
                continue
            for pin in self._ms_pins:
                level = GPIO.input(pin)
                if pressed[pin]:
                    # Already handled; only re-arm once the line is solidly HIGH again.
                    if level == GPIO.HIGH and self._pin_stable(pin, GPIO.HIGH):
                        pressed[pin] = False
                    continue
                # Not yet pressed: act only on a confirmed, sustained LOW.
                if level == GPIO.LOW and self._pin_stable(pin, GPIO.LOW):
                    pressed[pin] = True
                    self._microswitch_callback(pin)
            time.sleep(POLL_INTERVAL)

    def _microswitch_triggered(self, index):
        """Check if a microswitch is genuinely triggered (sustained active LOW)."""
        if index < len(self.microswitch_pins):
            pin = self.microswitch_pins[index]
            if pin and pin > 0:
                # Require a stable LOW so motor EMI blips don't falsely block motion.
                return self._pin_stable(pin, GPIO.LOW)
        return False
    
    def _microswitch_callback(self, channel):
        """Called when a microswitch triggers — stops the relevant motors/servos immediately."""
        # Translate the GPIO channel back to its configured microswitch index.
        if channel not in self.microswitch_pins:
            return
        ms_index = self.microswitch_pins.index(channel)
        
        if ms_index == 0:
            # Switch 0 blocks the normal DOWN motion for the tandem lift motors.
            if self.joystick_down_active:
                print(f"Microswitch 0 triggered: stopping DOWN motors")
                self.ms_latch_down = True
                for motor_id in [0, 1]:
                    if motor_id < len(self.motors):
                        self.motors[motor_id].stop()
        
        elif ms_index == 1:
            # Switch 1 blocks the normal UP motion for the tandem lift motors.
            if self.joystick_up_active:
                print(f"Microswitch 1 triggered: stopping UP motors")
                self.ms_latch_up = True
                for motor_id in [0, 1]:
                    if motor_id < len(self.motors):
                        self.motors[motor_id].stop()
        
        elif ms_index == 2:
            # Switch 2 is the right-end stop — blocks RIGHT travel for motor 3.
            if self.joystick_right_active:
                print(f"Microswitch 2 triggered: stopping RIGHT motor")
                self.ms_latch_right = True
                if 2 < len(self.motors):
                    self.motors[2].stop()
        
        elif ms_index == 3:
            # Switch 3 is the left-end stop — blocks LEFT travel for motor 3.
            if self.joystick_left_active:
                print(f"Microswitch 3 triggered: stopping LEFT motor")
                self.ms_latch_left = True
                if 2 < len(self.motors):
                    self.motors[2].stop()

        elif ms_index == 4:
            # Switch 4 is the Motor 4 top-of-range / home switch.
            # 'forward' (HIGH) is physically UP, so we stop on forward direction.
            if 3 < len(self.motors) and self.motors[3].direction == 'forward' and self.motors[3].is_active:
                print("Microswitch 4 triggered: stopping Motor 4 at top (home position)")
                self.ms_latch_motor4_up = True
                self.motors[3].stop()

    
    def _setup_joystick_pins(self):
        """Set up GPIO pins for joystick input."""
        # Ignore disabled joystick entries and only register real GPIO inputs.
        active_pins = [pin for pin in self.joystick_pins if pin != -1]
        
        if not active_pins:
            print("No joystick GPIO pins configured!")
            return
        
        print(f"Configuring joystick pins: {active_pins}")
        
        for pin in active_pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.button_states[pin] = GPIO.input(pin)
            # Interrupt-style callbacks avoid polling the joystick continuously.
            GPIO.add_event_detect(pin, GPIO.BOTH, callback=self._joystick_callback, bouncetime=20)
        
        print(f"Joystick initialized with {len(active_pins)} GPIO pins")
    
    def _reload_config(self):
        """Reload configuration from config.json file."""
        try:
            with open(self.config_path, 'r') as f:
                new_config = json.load(f)
            
            # Refresh speeds/directions so settings changes can apply without a restart.
            motors_config = new_config.get('motors', [])
            for i, motor in enumerate(self.motors):
                if i < len(motors_config):
                    config = motors_config[i]
                    # Only runtime-safe properties are updated; GPIO pin mappings stay fixed.
                    motor.speed = config.get('speed', motor.speed)
                    motor.direction = config.get('direction', motor.direction)
            # Refresh claw servo travel limits from config.
            _s1lim = new_config.get('servo1_limits', {})
            self.servo_min_angle = float(_s1lim.get('min_angle', self.servo_min_angle))
            self.servo_max_angle = float(_s1lim.get('max_angle', self.servo_max_angle))
        except Exception as e:
            print(f"Error reloading config: {e}")
    
    
    def _set_servo_angle(self, angle):
        """Set servo to a specific angle (0-180)."""
        if not self.pca:
            return
        # Convert a logical angle into a PCA9685 pulse width for servo channel 0.
        pulse_min = 60   # Pulse length for 0 degrees (lowered to extend open range)
        pulse_max = 409  # Pulse length for 180 degrees (2.5ms @ 40Hz)
        pulse = int(pulse_min + (angle / 180.0) * (pulse_max - pulse_min))
        self.pca.set_pwm(self.servo_channel, 0, pulse)
    
    def _set_servo2_angle(self, angle):
        """Set MG995 servo (channel 1) to a specific angle (0-180)."""
        if not self.pca:
            return
        # Convert the requested MG995 angle into the pulse range used at 40 Hz.
        pulse_min = 82   # 0 degrees (@ 40Hz)
        pulse_max = 409  # 180 degrees (@ 40Hz)
        pulse = int(pulse_min + (angle / 180.0) * (pulse_max - pulse_min))
        self.pca.set_pwm(self.servo2_channel, 0, pulse)
    
    def _servo_loop(self):
        """Continuously move servos while direction is set."""
        # This background loop applies incremental motion and releases pulses when idle.
        was_moving = False
        was_moving2 = False
        servo1_idle = False
        SERVO1_IDLE_TIMEOUT = 5.0  # seconds
        # How close to the OPEN limit the claw must be before its holding pulse
        # is allowed to release for cooling. While the claw is closed (gripping a
        # crate) the pulse is kept so it never drops its load.
        SERVO1_OPEN_MARGIN = 3.0  # degrees
        # Overheat safety net: the longest a CLOSED (gripping) claw may be left
        # completely untouched before its pulse is released anyway. Any crane or
        # claw input resets last_activity, so this only fires on an abandoned
        # crane (e.g. a museum visitor grips a crate and walks away), preventing a
        # stalled servo from cooking itself.
        SERVO1_GRIP_SAFETY_TIMEOUT = 30.0  # seconds
        while True:
            moved = False
            # Servo 1 motion is controlled by Button2 mode.
            if self.servo_direction != 0 and self.pca:
                was_moving = True
                servo1_idle = False
                self.servo_angle += self.servo_direction * self.servo_speed
                self.servo_angle = max(self.servo_min_angle, min(self.servo_max_angle, self.servo_angle))
                self._set_servo_angle(self.servo_angle)
                moved = True
            else:
                if was_moving and self.pca:
                    self._set_servo_angle(self.servo_angle)
                    servo1_idle = False
                    was_moving = False
                # Release holding torque after inactivity to reduce heat and power
                # draw, but ONLY when the claw is open (empty). A closed/gripping
                # claw keeps its pulse so it never drops a crate it is carrying.
                claw_is_open = self.servo_angle >= (self.servo_max_angle - SERVO1_OPEN_MARGIN)
                if not servo1_idle and self.pca and claw_is_open:
                    if time.time() - self.last_activity >= SERVO1_IDLE_TIMEOUT:
                        self.pca.set_pwm(self.servo_channel, 0, 0)
                        servo1_idle = True
                        print(f"Servo 1 idle: pulse released after {SERVO1_IDLE_TIMEOUT:.0f} seconds (claw open)")
                # Overheat safety net for an ABANDONED closed claw. Active play keeps
                # last_activity fresh, so this only fires when the crane is left
                # untouched while still gripping. Releasing the pulse here lets go of
                # any held crate, but protects the servo from a long stalled hold.
                if not servo1_idle and self.pca and not claw_is_open:
                    if time.time() - self.last_activity >= SERVO1_GRIP_SAFETY_TIMEOUT:
                        self.pca.set_pwm(self.servo_channel, 0, 0)
                        servo1_idle = True
                        print(f"Servo 1 grip safety: pulse released after {SERVO1_GRIP_SAFETY_TIMEOUT:.0f}s closed (overheat protection)")
                # Re-apply the last position as soon as movement activity resumes.
                if servo1_idle and self.pca:
                    if time.time() - self.last_activity < SERVO1_IDLE_TIMEOUT:
                        self._set_servo_angle(self.servo_angle)
                        servo1_idle = False
            
            # Servo 2 is the MG995 driven by Button1 + LEFT/RIGHT.
            if self.servo2_direction != 0 and self.pca:
                was_moving2 = True
                self.servo2_angle += self.servo2_direction * self.servo2_speed
                # Servo 2 is allowed to use the full configured travel range.
                self.servo2_angle = max(0.0, min(180.0, self.servo2_angle))
                self._set_servo2_angle(self.servo2_angle)
                moved = True
            else:
                if was_moving2 and self.pca:
                    self.pca.set_pwm(self.servo2_channel, 0, 0)
                    was_moving2 = False
            
            time.sleep(0.02 if moved else 0.01)
    
    def _joystick_callback(self, channel):
        """Callback when joystick GPIO pin state changes."""
        # Wait for contact bounce to finish before reading, otherwise a press can
        # be misread as a release because the pin is still oscillating.
        time.sleep(0.003)
        state = GPIO.input(channel)
        self.button_states[channel] = state
        if channel in self.joystick_pins:
            pin_index = self.joystick_pins.index(channel)
            self._handle_joystick_input(pin_index, state)
    
    def _handle_joystick_input(self, pin_index: int, state: int):
        """
        Handle joystick input by pin index.
        
        Args:
            pin_index: Index into joystick_pins list (0=up,1=down,2=left,3=right,4=btn1,5=btn2)
            state: Pin state (0=pressed/LOW, 1=released/HIGH)
        """
        if self.homing:
            return  # All input blocked during startup homing
        
        # Button1 switches the joystick into motor-4 / MG995 auxiliary control mode.
        if pin_index == 4:
            if state == 0:  # Button pressed
                print("Button1 pressed: Motor 4 ready for joystick control")
                self.button1_pressed = True
                # Clear latch flags so the new mode isn't blocked by a previous limit hit.
                self.ms_latch_up = False
                self.ms_latch_down = False
                self.ms_latch_left = False
                self.ms_latch_right = False
                # Disable the normal drive motors while the alternate mode is active.
                self.joystick_up_active = False
                self.joystick_down_active = False
                self.joystick_left_active = False
                self.joystick_right_active = False
                for motor_id in [0, 1, 2]:
                    if motor_id < len(self.motors):
                        self.motors[motor_id].stop()
            else:  # Button released
                print("Button1 released: Motor 4 disabled")
                self.button1_pressed = False
                # Persist motor 4's position when the operator leaves this mode.
                if 3 < len(self.motors):
                    self.motors[3].stop()
                    self._save_motor4_position()
                self.servo2_direction = 0  # Stop MG995 servo
            return
        
        # Button2 switches the joystick into primary servo control mode.
        if pin_index == 5:
            if state == 0:  # Button pressed
                print("Button2 pressed: Servo ready for joystick control")
                self.button2_pressed = True
                # Clear latch flags so the new mode isn't blocked by a previous limit hit.
                self.ms_latch_up = False
                self.ms_latch_down = False
                self.ms_latch_left = False
                self.ms_latch_right = False
                # Reclaim the joystick axes from the motors while servo mode is active.
                self.joystick_up_active = False
                self.joystick_down_active = False
                for motor_id in [0, 1]:
                    if motor_id < len(self.motors):
                        self.motors[motor_id].stop()
                # LEFT/RIGHT motor movement is also blocked in this mode.
                self.joystick_left_active = False
                self.joystick_right_active = False
                if 2 < len(self.motors):
                    self.motors[2].stop()
            else:  # Button released
                print("Button2 released: Servo disabled")
                self.button2_pressed = False
                self.servo_direction = 0  # Stop servo
            return
        
        # Group pins into axis pairs so each pair can share the same press/release logic.
        motor_pair = pin_index // 2
        pin_position = pin_index % 2  # 0 = first pin (up/left), 1 = second pin (down/right)
        
        # The joystick uses pull-ups, so LOW means pressed and HIGH means released.
        if state == 0:  # Button pressed
            self.joystick_button_active = True  # Mark button as active
            if pin_position == 0:  # First pin in pair - UP or LEFT
                # UP/LEFT can map to different outputs depending on the current mode button.
                if self.button1_pressed and motor_pair == 0:
                    print("Joystick: UP - Motor 4 reverse")
                    if 3 < len(self.motors):
                        motor = self.motors[3]
                        motor.set_direction('reverse')
                        motor.start()
                elif self.button1_pressed and motor_pair == 1:
                    print("Joystick: Button1 + LEFT - MG995 servo rotating left")
                    self.servo2_direction = -1
                elif motor_pair == 0 and not self.button1_pressed:  # UP for motors 0,1 only if button1 AND button2 NOT pressed
                    if self.button2_pressed:
                        print("Joystick: Button2 + UP - Servo rotating up")
                        self.servo_direction = 1
                        self.last_activity = time.time()  # Servo 1 input resets idle timer
                    # Normal UP is blocked if the matching lift microswitch is latched or active.
                    elif self.ms_latch_up or self._microswitch_triggered(1):
                        print("Joystick: UP BLOCKED by microswitch 1")
                    else:
                        print("Joystick: UP (Motors 1 & 2)")
                        self.joystick_up_active = True
                        for motor_id in [0, 1]:
                            if motor_id < len(self.motors):
                                motor = self.motors[motor_id]
                                motor.start()
                elif motor_pair == 1:  # LEFT controls motor 3 (unless button2 is held)
                    if self.button2_pressed:
                        print("Joystick: LEFT ignored (button2 mode)")
                    # LEFT is blocked by either left-travel limit switch.
                    elif self.ms_latch_left or self._microswitch_triggered(3):
                        print("Joystick: LEFT BLOCKED by microswitch 3")
                    else:
                        print("Joystick: LEFT - Motor 3")
                        self.joystick_left_active = True
                        motor_id = motor_pair + 1
                        if motor_id < len(self.motors):
                            motor = self.motors[motor_id]
                            motor.start()
            elif pin_position == 1:  # Second pin in pair - DOWN or RIGHT
                # DOWN/RIGHT also remap based on whether Button1 or Button2 is held.
                if self.button1_pressed and motor_pair == 0:
                    # 'forward' (HIGH) = physically UP — blocked by top switch (MS4).
                    if self.ms_latch_motor4_up or self._microswitch_triggered(4):
                        print("Joystick: Motor 4 DOWN (physically UP) BLOCKED by top switch (MS4)")
                    else:
                        print("Joystick: DOWN - Motor 4 forward (physically UP)")
                        if 3 < len(self.motors):
                            motor = self.motors[3]
                            motor.set_direction('forward')
                            motor.start()
                elif self.button1_pressed and motor_pair == 1:
                    print("Joystick: Button1 + RIGHT - MG995 servo rotating right")
                    self.servo2_direction = 1
                elif motor_pair == 0 and not self.button1_pressed:  # DOWN for motors 0,1 only if button1 AND button2 NOT pressed
                    if self.button2_pressed:
                        print("Joystick: Button2 + DOWN - Servo rotating down")
                        self.servo_direction = -1
                        self.last_activity = time.time()  # Servo 1 input resets idle timer
                    # Normal DOWN is blocked if the lower lift limit is active or latched.
                    elif self.ms_latch_down or self._microswitch_triggered(0):
                        print("Joystick: DOWN BLOCKED by microswitch 0")
                    else:
                        print("Joystick: DOWN (Motors 1 & 2)")
                        self.joystick_down_active = True
                        _cfg_motors = self.config.get('motors', [])
                        for motor_id in [0, 1]:
                            if motor_id < len(self.motors):
                                motor = self.motors[motor_id]
                                _cfg_dir = _cfg_motors[motor_id].get('direction', 'forward') if motor_id < len(_cfg_motors) else 'forward'
                                opposite = 'reverse' if _cfg_dir == 'forward' else 'forward'
                                motor.set_direction(opposite)
                                motor.start()
                elif motor_pair == 1:  # RIGHT controls motor 3 (unless button2 is held)
                    if self.button2_pressed:
                        print("Joystick: RIGHT ignored (button2 mode)")
                    # RIGHT checks the travel limit before reversing motor 3.
                    elif self.ms_latch_right or self._microswitch_triggered(2):
                        print("Joystick: RIGHT BLOCKED by microswitch 2")
                    else:
                        print("Joystick: RIGHT - Motor 3")
                        self.joystick_right_active = True
                        motor_id = motor_pair + 1
                        if motor_id < len(self.motors):
                            motor = self.motors[motor_id]
                            opposite = 'reverse' if motor.direction == 'forward' else 'forward'
                            motor.set_direction(opposite)
                            motor.start()
        else:  # Button released (HIGH due to pull-up)
            self.joystick_button_active = False  # Mark button as released
            motors_config = self.config.get('motors', [])

            # Release handling restores the normal idle state for whichever axis was active.
            if motor_pair == 0:  # UP/DOWN pair
                self.joystick_up_active = False
                self.joystick_down_active = False
                self.ms_latch_up = False
                self.ms_latch_down = False
                self.ms_latch_motor4_up = False
                if self.button1_pressed:  # Button1 still pressed, stop motor 4
                    if 3 < len(self.motors):
                        motor = self.motors[3]
                        motor.stop()
                        self._save_motor4_position()
                elif self.button2_pressed:  # Button2 still pressed, stop servo
                    self.servo_direction = 0
                else:
                    for motor_id in [0, 1]:
                        if motor_id < len(self.motors):
                            motor = self.motors[motor_id]
                            motor.stop()
                            if motor_id < len(motors_config):
                                motor.direction = motors_config[motor_id].get('direction', motor.direction)
            elif motor_pair == 1:  # LEFT/RIGHT pair
                self.joystick_left_active = False
                self.joystick_right_active = False
                self.ms_latch_left = False
                self.ms_latch_right = False
                if self.button1_pressed:
                    self.servo2_direction = 0  # Stop MG995 servo on release
                else:
                    motor_id = motor_pair + 1
                    if motor_id < len(self.motors):
                        motor = self.motors[motor_id]
                        motor.stop()
                        if motor_id < len(motors_config):
                            motor.direction = motors_config[motor_id].get('direction', motor.direction)
    
    def run(self):
        """Main event loop for GPIO joystick input."""
        # Validate that at least one joystick input is enabled before entering the loop.
        active_pins = [pin for pin in self.joystick_pins if pin != -1]
        
        if not active_pins:
            print("Cannot run without joystick GPIO pins configured!")
            return
        
        self.running = True
        print("Starting joystick motor controller...")

        # Auto-home motor 4 before accepting joystick commands.
        self.homing = True
        self._home_motor4()
        self.homing = False

        print("Joystick controls:")
        print(f"  Motors 1 & 2 (tandem): GPIO {self.joystick_pins[0]}=UP, GPIO {self.joystick_pins[1]}=DOWN")
        if len(self.joystick_pins) > 2 and (self.joystick_pins[2] != -1 or self.joystick_pins[3] != -1):
            print(f"  Motor 3 (separate): GPIO {self.joystick_pins[2]}=LEFT, GPIO {self.joystick_pins[3]}=RIGHT")
        if len(self.joystick_pins) > 4 and self.joystick_pins[4] != -1:
            print(f"  Button1 (GPIO {self.joystick_pins[4]}) + UP/DOWN: Motor 4 forward/reverse")
            print(f"  Button1 (GPIO {self.joystick_pins[4]}) + LEFT/RIGHT: MG995 servo rotate (180°)")
        if len(self.joystick_pins) > 5 and self.joystick_pins[5] != -1:
            print(f"  Button2 (GPIO {self.joystick_pins[5]}) + LEFT/RIGHT: Servo rotate")
        print("Microswitches:")
        ms_labels = ["DOWN block", "UP block", "LEFT block", "RIGHT block", "Button1+UP block"]
        for i, pin in enumerate(self.microswitch_pins):
            if pin and pin > 0 and i < len(ms_labels):
                print(f"  MS{i} (GPIO {pin}): {ms_labels[i]}")
        print("Press Ctrl+C to exit")
        
        config_reload_counter = 0
        
        try:
            while self.running:
                # Periodically reload config so operator tuning changes take effect live.
                config_reload_counter += 1
                if config_reload_counter >= 50 and not self.joystick_button_active:
                    self._reload_config()
                    config_reload_counter = 0

                # Watchdog: if a motor or servo is active but every joystick pin reads
                # HIGH (physically released), a release interrupt was dropped — stop
                # everything so the crane doesn't run indefinitely.
                any_motor_active = any(m.is_active for m in self.motors)
                any_servo_active = (self.servo_direction != 0 or self.servo2_direction != 0)
                if any_motor_active or any_servo_active:
                    active_pins = [p for p in self.joystick_pins if p not in (-1, 0)]
                    if active_pins and all(GPIO.input(p) == GPIO.HIGH for p in active_pins):
                        print("[WATCHDOG] All joystick pins released but motor/servo still active — stopping (dropped interrupt recovery)")
                        for motor in self.motors:
                            motor.stop()
                        self.servo_direction = 0
                        self.servo2_direction = 0
                        self.joystick_up_active = False
                        self.joystick_down_active = False
                        self.joystick_left_active = False
                        self.joystick_right_active = False
                        self.joystick_button_active = False

                time.sleep(0.1)  # Keep main thread alive
        
        except KeyboardInterrupt:
            print("\nShutting down...")
        
        finally:
            self.cleanup()
    
    def _home_motor4(self):
        """Drive motor 4 upward until the top microswitch (MS4) triggers, then set position.

        This runs synchronously at startup before the joystick event loop begins.
        GPIO is pulsed directly on the main thread so there are no background-thread
        race conditions. MS4 is the top/home switch, so on success current_steps is
        set to max_steps (the top of travel).
        """
        if len(self.motors) <= 3:
            print("Motor 4 not configured, skipping auto-home.")
            return

        # Homing switch is microswitch index 4 (5th pin in the microswitches list).
        if len(self.microswitch_pins) < 5 or not self.microswitch_pins[4]:
            print("Motor 4 homing switch (MS4) not configured, skipping auto-home.")
            return

        motor = self.motors[3]
        if not motor.step_pin or not motor.dir_pin:
            print("Motor 4 GPIO pins not configured, skipping auto-home.")
            return

        if not motor.speed or motor.speed <= 0:
            print("Motor 4 speed is 0, skipping auto-home. Set a speed in settings first.")
            return

        homing_pin = self.microswitch_pins[4]
        step_pin = motor.step_pin
        dir_pin = motor.dir_pin
        # Use a safe minimum step delay for homing: direct GPIO pulsing has no thread
        # overhead, so the configured speed (e.g. 5e-06) would pulse far too fast for
        # most stepper drivers. The base floor is 1 kHz (1ms); HOMING_SPEEDUP divides it
        # so homing runs ~3x faster (~3 kHz). That is still well below motor 4's normal
        # run rate and only applies during the brief homing move, so CPU/thermal impact
        # stays low and the Pi won't hit its throttle threshold (~80°C).
        HOMING_SPEEDUP = 3
        step_delay = max(motor.speed, 0.001) / HOMING_SPEEDUP

        print(f"Motor 4 auto-home: driving UP (step={step_pin}, dir={dir_pin}, speed={step_delay}, switch=GPIO{homing_pin})")

        # Wait for the pin to settle after GPIO interrupt setup, then take 3 readings
        # 10ms apart to confirm the switch is genuinely pressed before skipping homing.
        time.sleep(0.05)
        already_home = True
        for _ in range(3):
            if GPIO.input(homing_pin) != GPIO.LOW:
                already_home = False
                break
            time.sleep(0.01)

        # If the switch is already triggered, we are already at home — just zero and return.
        if already_home:
            print(f"Motor 4: top switch reads LOW at startup (GPIO{homing_pin}), position set to max without moving.")
            motor.current_steps = motor.max_steps if motor.max_steps is not None else 10000
            self._save_motor4_position()
            return

        # Pause the background motor thread so it doesn't interfere with direct GPIO pulsing.
        motor.stop()
        original_max = motor.max_steps

        # 'forward' drives the crane upward — set dir_pin HIGH to match _step_motor logic.
        GPIO.output(dir_pin, GPIO.HIGH)
        time.sleep(0.001)  # Give the driver time to latch the direction

        # No interrupt to disable: the microswitches are polled, and the poll loop
        # pauses itself while self.homing is True, so direct GPIO reads below are safe.

        timeout = time.time() + 60  # 60-second safety timeout
        step_count = 0
        next_status = time.time() + 5  # Print progress every 5 seconds

        try:
            while time.time() < timeout:
                # Pulse one step continuously — no stopping to check the switch.
                GPIO.output(step_pin, GPIO.HIGH)
                time.sleep(0.00001)
                GPIO.output(step_pin, GPIO.LOW)
                time.sleep(step_delay)
                step_count += 1

                # A raw LOW read is not trusted on its own: stepper EMI on the switch
                # line produces brief sub-millisecond LOW blips while the motor runs,
                # which previously stopped homing halfway. On any LOW, stop pulsing and
                # confirm with _pin_stable (the same sustained-read filter the polling
                # loop uses). Only a genuinely held LOW ends homing; EMI blips are
                # rejected and stepping resumes.
                if GPIO.input(homing_pin) == GPIO.LOW:
                    GPIO.output(step_pin, GPIO.LOW)
                    if self._pin_stable(homing_pin, GPIO.LOW):
                        motor.current_steps = original_max if original_max is not None else (motor.max_steps or 10000)
                        self._save_motor4_position()
                        print(f"Motor 4 homed successfully after {step_count} steps. Position set to {motor.current_steps} at top.")
                        return
                    # False trigger (EMI blip) — keep driving up.

                if time.time() >= next_status:
                    print(f"Motor 4 homing: {step_count} steps taken, switch state={GPIO.input(homing_pin)}")
                    next_status = time.time() + 5

        finally:
            GPIO.output(step_pin, GPIO.LOW)
            # Microswitches are polled, so there is no interrupt to re-register here.

        print(f"WARNING: Motor 4 homing timed out after {step_count} steps. Check MS4 wiring on GPIO{homing_pin}.")

    def _load_motor4_position(self, first_run_default: int = 0) -> int:
        """Load saved step position for motor 4 from disk.
        If no save file exists (first run), defaults to first_run_default (mid-range)
        so the claw has room to move both up and down before zeroing.
        """
        # This keeps the lift motor's software position from resetting to zero on every boot.
        try:
            with open(self._motor4_state_path, 'r') as f:
                data = json.load(f)
                return int(data.get('current_steps', first_run_default))
        except FileNotFoundError:
            print(f"Motor 4: no saved position found, starting at {first_run_default} (mid-range). Use Zero Pos after lowering claw to ground.")
            return first_run_default
        except (ValueError, KeyError):
            return first_run_default

    def _save_motor4_position(self):
        """Save current step position of motor 4 to disk."""
        # Write the latest lift position whenever the operator stops or the app exits.
        if 3 < len(self.motors):
            steps = self.motors[3].current_steps
            with open(self._motor4_state_path, 'w') as f:
                json.dump({'current_steps': steps}, f)
            print(f"Motor 4 position saved: {steps} steps")

    def cleanup(self):
        """Clean up resources."""
        # Persist state, drive outputs low, and release the shared GPIO manager.
        self._save_motor4_position()
        for motor in self.motors:
            motor.cleanup()
        cleanup_gpio()
        self.running = False
        print("Controller cleanup complete")


def _stop_crane_service():
    """Stop the crane systemd service when launched manually to prevent GPIO conflicts.

    Skipped when this process IS the service (systemd sets INVOCATION_ID).
    Requires passwordless sudo for 'systemctl stop crane' — see /etc/sudoers.d/crane.
    """
    if os.environ.get('INVOCATION_ID'):
        return  # Running as the service itself — do nothing
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'stop', 'crane'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print("Crane service stopped (manual launch — GPIO released)")
            time.sleep(0.5)  # Allow systemd to fully release GPIO before we claim it
        else:
            print("Note: crane service not running or sudo password required — continuing anyway")
    except Exception:
        pass  # Service not installed yet — continue normally


def _restart_crane_service():
    """Restart the crane systemd service when this manual session ends.

    Only runs when launched manually (not when this process IS the service).
    Requires passwordless sudo for 'systemctl start crane' — see /etc/sudoers.d/crane.
    """
    if os.environ.get('INVOCATION_ID'):
        return  # Running as the service itself — do nothing
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'start', 'crane'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print("Crane service restarted")
        else:
            print("Note: could not restart crane service — start it manually with: sudo systemctl start crane")
    except Exception:
        pass


def main():
    """Main entry point."""
    # Flush every print() immediately so homing progress appears in journalctl in real time
    # rather than accumulating in Python's output buffer until the process exits.
    sys.stdout.reconfigure(line_buffering=True)
    # Convert SIGTERM (sent by VS Code debugger / systemd stop) into SystemExit so
    # the finally block below can restart the crane service cleanly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    _stop_crane_service()
    try:
        controller = JoystickMotorController('config.json')
        controller.run()
    except FileNotFoundError:
        print("Error: config.json not found!")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        _restart_crane_service()


if __name__ == '__main__':
    # Run as a standalone hardware controller script.
    main()