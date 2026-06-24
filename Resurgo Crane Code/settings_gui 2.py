import pygame
import json
import os
import copy
import datetime
import shutil
import subprocess
import threading
import time


# Module overview:
# - Stores editable crane settings in config.json.
# - Renders a pygame-based settings and manual control panel.
# - Includes a lightweight direct-hardware helper for testing motors and servos from the GUI.


# Constants for GUI styling and layout.
# These values define the dark theme palette, sizing, and section placement.
BG_COLOR = (30, 30, 30)           # Main background
SURFACE_COLOR = (45, 45, 48)      # Card/box background
FIELD_COLOR = (60, 60, 65)        # Input field background
FIELD_ACTIVE = (40, 80, 120)      # Active field
FIELD_CHANGED = (120, 75, 0)      # Changed field (dark orange)
BORDER_COLOR = (80, 80, 85)       # Subtle borders
TEXT_COLOR = (220, 220, 220)      # Primary text
TEXT_DIM = (160, 160, 165)        # Secondary/label text
ACCENT_GREEN = (45, 160, 80)     # Save button
ACCENT_RED = (180, 55, 55)       # Revert/undo button
ACCENT_BLUE = (55, 120, 200)     # Highlights
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
FONT_SIZE = 20

# Layout constants
MARGIN_TOP = 70
MARGIN_LEFT = 40
MOTOR_BOX_WIDTH = 650
MOTOR_BOX_HEIGHT = 70
LABEL_WIDTH = 70
FIELD_WIDTH = 75
FIELD_HEIGHT = 26
SPACING_X = 10
SPACING_Y = 35
NUM_MOTORS = 4
NUM_GPIO = 6
NUM_MICROSWITCHES = 5

# Calculate required height
motors_height = NUM_MOTORS * (MOTOR_BOX_HEIGHT + SPACING_Y)
servo_limits_section_top = MARGIN_TOP + motors_height + SPACING_Y
servo_limits_box_height = FIELD_HEIGHT + 55  # Two fields + title + padding
microswitch_section_top = servo_limits_section_top + servo_limits_box_height + SPACING_Y
microswitch_box_height = NUM_MICROSWITCHES * (FIELD_HEIGHT + SPACING_Y) + 30
gpio_section_top = microswitch_section_top + microswitch_box_height + SPACING_Y
gpio_box_height = NUM_GPIO * (FIELD_HEIGHT + SPACING_Y) + 30
# Controls section layout
controls_section_top = gpio_section_top + gpio_box_height + SPACING_Y
CTRL_BTN_W = 100
CTRL_BTN_H = 32
CTRL_ROW_H = 42
NUM_CTRL_ROWS = 5
controls_box_height = 35 + NUM_CTRL_ROWS * CTRL_ROW_H + 10

content_height = controls_section_top + controls_box_height + 100
WIDTH = MOTOR_BOX_WIDTH + 2 * MARGIN_LEFT + 40
HEIGHT = 800  # Fixed height with scrolling

SETTINGS_FILE = 'config.json'

def _stop_crane_service():
    """Stop the crane service before opening the settings GUI to prevent GPIO conflicts."""
    if os.environ.get('INVOCATION_ID'):
        return
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'stop', 'crane'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print("Crane service stopped (settings GUI opening)")
            # Poll until the service process is fully gone so GPIO pins are released.
            deadline = time.time() + 8
            while time.time() < deadline:
                check = subprocess.run(
                    ['systemctl', 'is-active', 'crane'],
                    capture_output=True, text=True
                )
                if check.stdout.strip() not in ('active', 'deactivating'):
                    break
                time.sleep(0.1)
            time.sleep(0.2)  # brief extra wait for kernel GPIO release
        else:
            print("Note: crane service not running or sudo password required — continuing anyway")
    except Exception:
        pass


def _start_crane_service():
    """Restart the crane service after the settings GUI closes."""
    if os.environ.get('INVOCATION_ID'):
        return
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'start', 'crane'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print("Crane service restarted")
    except Exception:
        pass


# Default config structure used on first run or when fields are missing from disk.
default_settings = {
    'motors': [
        {'speed': 0.0, 'direction': 'forward', 'step_pin': 0, 'dir_pin': 0},
        {'speed': 0.0, 'direction': 'forward', 'step_pin': 0, 'dir_pin': 0},
        {'speed': 0.0, 'direction': 'forward', 'step_pin': 0, 'dir_pin': 0},
        {'speed': 0.0, 'direction': 'forward', 'step_pin': 0, 'dir_pin': 0}
    ],
    'joystick': {
        'gpio_pins': [0, 0, 0, 0, 0, 0]  # up, down, left, right, button1, button2
    },
    'microswitches': {
        'gpio_pins': [0, 0, 0, 0, 0]
    },
    'motor4_limit': {
        'min_steps': 0,
        'max_steps': 10000
    },
    'servo1_limits': {
        'min_angle': 0.0,
        'max_angle': 45.0
    }
}

def load_settings():
    # Load config.json, create it if missing, and normalize it to the expected shape.
    if not os.path.exists(SETTINGS_FILE):
        # First run: create a config file with safe defaults.
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(default_settings, f, indent=2)
        return default_settings.copy()
    else:
        with open(SETTINGS_FILE, 'r') as f:
            try:
                data = json.load(f)
                settings = default_settings.copy()
                settings.update(data)
                # Repair incomplete arrays so the GUI always has the expected number of fields.
                if len(settings['motors']) != NUM_MOTORS:
                    settings['motors'] = default_settings['motors']
                if len(settings['joystick']['gpio_pins']) != NUM_GPIO:
                    settings['joystick']['gpio_pins'] = default_settings['joystick']['gpio_pins']
                if 'microswitches' not in settings:
                    settings['microswitches'] = default_settings['microswitches']
                if len(settings['microswitches']['gpio_pins']) != NUM_MICROSWITCHES:
                    settings['microswitches']['gpio_pins'] = default_settings['microswitches']['gpio_pins']
                if 'motor4_limit' not in settings:
                    settings['motor4_limit'] = default_settings['motor4_limit']
                if 'servo1_limits' not in settings:
                    settings['servo1_limits'] = default_settings['servo1_limits']
                return settings
            except Exception as e:
                print(f"Error loading settings: {e}")
    return default_settings.copy()

def draw_text_field(screen, font, label, value, rect, active, flash=0, changed=False):
    # Draw a text input box with visual states for focus, edits, and validation flashes.
    # flash: 0=normal, >0=red flash, fades
    if flash > 0:
        fade = min(flash, 15)
        r = 200
        g = int(55 + 10 * (15 - fade))
        b = int(55 + 10 * (15 - fade))
        color = (r, g, b)
    elif changed:
        color = FIELD_CHANGED
    elif active:
        color = FIELD_ACTIVE
    else:
        color = FIELD_COLOR
    pygame.draw.rect(screen, color, rect, border_radius=4)
    pygame.draw.rect(screen, BORDER_COLOR, rect, 1, border_radius=4)
    value_surface = font.render(str(value), True, TEXT_COLOR)
    screen.blit(value_surface, (rect.x + 5, rect.y + 5))

def draw_dropdown(screen, font, label, options, selected, rect, active, changed=False):
    # Draw a simple two-state dropdown-style field for forward/reverse selection.
    if changed:
        color = FIELD_CHANGED
    elif active:
        color = FIELD_ACTIVE
    else:
        color = FIELD_COLOR
    pygame.draw.rect(screen, color, rect, border_radius=4)
    pygame.draw.rect(screen, BORDER_COLOR, rect, 1, border_radius=4)
    value_surface = font.render(options[selected], True, TEXT_COLOR)
    screen.blit(value_surface, (rect.x + 5, rect.y + 5))

def get_current_settings(motor_speed_values, motor_dir_selected, motor_step_values, motor_dir_pin_values, gpio_values, microswitch_values, dir_options, motor4_limit_value='10000', motor4_min_value='0', servo1_min_value='0.0', servo1_max_value='45.0'):
    # Convert the current GUI field values back into the config.json structure.
    return {
        'motors': [
            {
                'speed': float(motor_speed_values[j]),
                'direction': dir_options[motor_dir_selected[j]],
                'step_pin': int(motor_step_values[j]) if motor_step_values[j] else 0,
                'dir_pin': int(motor_dir_pin_values[j]) if motor_dir_pin_values[j] else 0
            } for j in range(NUM_MOTORS)
        ],
        'joystick': {
            'gpio_pins': [int(gpio_values[j]) if gpio_values[j] else 0 for j in range(NUM_GPIO)]
        },
        'microswitches': {
            'gpio_pins': [int(microswitch_values[j]) if microswitch_values[j] else 0 for j in range(NUM_MICROSWITCHES)]
        },
        'motor4_limit': {
            'min_steps': int(motor4_min_value) if motor4_min_value else 0,
            'max_steps': int(motor4_limit_value) if motor4_limit_value else 10000
        },
        'servo1_limits': {
            'min_angle': float(servo1_min_value) if servo1_min_value else 0.0,
            'max_angle': float(servo1_max_value) if servo1_max_value else 45.0
        }
    }

def has_changes(original, current):
    # Any structural difference means the GUI has unsaved edits.
    return original != current

def cleanup_old_backups(max_backups=4):
    """Delete old backup files, keeping only the most recent ones."""
    try:
        # Collect timestamped config backups created by the Save action.
        backup_files = []
        for filename in os.listdir('.'):
            if filename.startswith('config_') and filename.endswith('.bak'):
                filepath = os.path.join('.', filename)
                backup_files.append((filepath, os.path.getmtime(filepath)))
        
        # Keep only the newest backups so the folder does not fill with stale copies.
        if len(backup_files) > max_backups:
            # Delete the oldest files first.
            backup_files.sort(key=lambda x: x[1])
            for filepath, _ in backup_files[:-max_backups]:
                try:
                    os.remove(filepath)
                    print(f"Deleted old backup: {filepath}")
                except Exception as e:
                    print(f"Error deleting backup {filepath}: {e}")
    except Exception as e:
        print(f"Error in cleanup_old_backups: {e}")

class CraneHardware:
    """Direct hardware control for crane motors and servos from GUI."""

    def __init__(self, motors_config):
        # Mirror the runtime controller state needed for manual testing from the GUI.
        self.motors_config = motors_config
        self.motor_active = {}
        self.motor_direction = {}
        self.servo1_angle = 50.0
        self.servo2_angle = 90.0
        self.servo1_dir = 0
        self.servo2_dir = 0
        self.pca = None
        self.GPIO = None
        self._running = True

        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            # Configure all valid step/dir pins so the GUI can jog motors directly.
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            for m in self.motors_config:
                sp, dp = m.get('step_pin', 0), m.get('dir_pin', 0)
                if sp and dp:
                    GPIO.setup(sp, GPIO.OUT)
                    GPIO.setup(dp, GPIO.OUT)
                    GPIO.output(sp, GPIO.LOW)
                    GPIO.output(dp, GPIO.LOW)
            print("GPIO initialized for crane control")
        except Exception as e:
            print(f"GPIO not available: {e}")

        try:
            import Adafruit_PCA9685
            self.pca = Adafruit_PCA9685.PCA9685(busnum=1)
            self.pca.set_pwm_freq(40)  # 40Hz (reduced from 50Hz to lower servo heat)
            print("PCA9685 initialized for servo control")
        except Exception as e:
            print(f"PCA9685 not available: {e}")

        # Background loop keeps motors stepping and servos moving while buttons are held.
        self._thread = threading.Thread(target=self._control_loop, daemon=True)
        self._thread.start()

    def _control_loop(self):
        # Continuously service motor step pulses and servo motion commands.
        s1_was_moving = False
        s2_was_moving = False
        while self._running:
            stepped = False
            for idx, active in list(self.motor_active.items()):
                if active and idx < len(self.motors_config):
                    m = self.motors_config[idx]
                    sp, dp = m.get('step_pin', 0), m.get('dir_pin', 0)
                    if sp and dp and self.GPIO:
                        # Emit one step pulse using the currently requested direction.
                        d = self.motor_direction.get(idx, 'forward')
                        self.GPIO.output(dp, self.GPIO.HIGH if d == 'forward' else self.GPIO.LOW)
                        self.GPIO.output(sp, self.GPIO.HIGH)
                        time.sleep(0.00001)
                        self.GPIO.output(sp, self.GPIO.LOW)
                        stepped = True

            if self.servo1_dir != 0 and self.pca:
                # Servo 1 uses a shorter travel range than servo 2.
                s1_was_moving = True
                self.servo1_angle = max(0.0, min(63.0, self.servo1_angle + self.servo1_dir * 2))
                self._set_servo(0, self.servo1_angle)
            elif s1_was_moving and self.pca:
                self._set_servo(0, self.servo1_angle)
                s1_was_moving = False

            if self.servo2_dir != 0 and self.pca:
                # Servo 2 can use the full 0-180 degree range.
                s2_was_moving = True
                self.servo2_angle = max(0.0, min(180.0, self.servo2_angle + self.servo2_dir * 2))
                self._set_servo(1, self.servo2_angle)
            elif s2_was_moving and self.pca:
                self.pca.set_pwm(1, 0, 0)
                s2_was_moving = False

            if stepped:
                # Use the first active motor's configured speed as the stepping delay.
                speed = 0.001
                for idx in list(self.motor_active.keys()):
                    if self.motor_active.get(idx) and idx < len(self.motors_config):
                        s = float(self.motors_config[idx].get('speed', 0))
                        if s > 0:
                            speed = s
                            break
                time.sleep(speed)
            else:
                time.sleep(0.02)

    def _set_servo(self, channel, angle):
        # Convert GUI angle values into the pulse width used by the PCA9685 board.
        if self.pca:
            pulse = int(82 + (angle / 180.0) * 327)  # 40Hz: min=82, max=409
            self.pca.set_pwm(channel, 0, pulse)

    def start_motors(self, indices, direction):
        # Latch the selected motors on until the matching mouse button release event.
        for idx in indices:
            self.motor_direction[idx] = direction
            self.motor_active[idx] = True

    def stop_motors(self, indices):
        # Stop the requested motors without affecting the rest of the test state.
        for idx in indices:
            self.motor_active[idx] = False

    def cleanup(self):
        # Stop all test motion and release hardware resources on GUI shutdown.
        self._running = False
        for k in list(self.motor_active.keys()):
            self.motor_active[k] = False
        self.servo1_dir = 0
        self.servo2_dir = 0
        if self.pca:
            try:
                self.pca.set_pwm(0, 0, 0)
                self.pca.set_pwm(1, 0, 0)
            except Exception:
                pass
        if self.GPIO:
            try:
                self.GPIO.cleanup()
            except Exception:
                pass


def main():
    # Initialize pygame, load settings, and build all GUI state containers.
    _stop_crane_service()
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption('Motor Settings')
    font = pygame.font.SysFont(None, FONT_SIZE)

    settings = load_settings()
    original_settings = copy.deepcopy(settings)

    # Per-field rectangles, values, and focus flags for the motor settings section.
    motor_speed_rects = []
    motor_speed_values = [str(m['speed']) for m in settings['motors']]
    motor_speed_active = [False] * 4
    motor_dir_rects = []
    motor_dir_selected = [0 if m['direction'] == 'forward' else 1 for m in settings['motors']]
    motor_dir_active = [False] * 4
    motor_step_rects = []
    motor_step_values = [str(m.get('step_pin', 0)) for m in settings['motors']]
    motor_step_active = [False] * 4
    motor_dir_pin_rects = []
    motor_dir_pin_values = [str(m.get('dir_pin', 0)) for m in settings['motors']]
    motor_dir_pin_active = [False] * 4
    motor_speed_undo_rects = []
    motor_dir_undo_rects = []
    motor_step_undo_rects = []
    motor_dir_pin_undo_rects = []
    dir_options = ['forward', 'reverse']

    # State for joystick GPIO and microswitch configuration sections.
    gpio_rects = []
    gpio_values = [str(pin) for pin in settings['joystick']['gpio_pins']]
    gpio_active = [False] * 6

    microswitch_rects = []
    microswitch_values = [str(pin) for pin in settings.get('microswitches', {}).get('gpio_pins', [0]*NUM_MICROSWITCHES)]
    microswitch_active = [False] * NUM_MICROSWITCHES

    motor4_limit_value = str(settings.get('motor4_limit', {}).get('max_steps', 10000))
    motor4_min_value = str(settings.get('motor4_limit', {}).get('min_steps', 0))
    motor4_limit_rect = None
    motor4_min_rect = None

    servo1_min_value = str(settings.get('servo1_limits', {}).get('min_angle', 0.0))
    servo1_max_value = str(settings.get('servo1_limits', {}).get('max_angle', 45.0))
    servo1_min_rect = None
    servo1_max_rect = None

    # Bottom action buttons for saving and reverting config edits.
    save_button_rect = pygame.Rect(WIDTH - 140, content_height - 60, 120, 40)
    revert_button_rect = pygame.Rect(WIDTH - 280, content_height - 60, 120, 40)

    # Precompute widget positions for each section so the draw loop can stay simple.
    for i in range(NUM_MOTORS):
        y = MARGIN_TOP + i * (MOTOR_BOX_HEIGHT + SPACING_Y)
        speed_rect = pygame.Rect(MARGIN_LEFT + 10, y + 30, FIELD_WIDTH, FIELD_HEIGHT)
        dir_rect = pygame.Rect(MARGIN_LEFT + 10 + FIELD_WIDTH + SPACING_X, y + 30, FIELD_WIDTH, FIELD_HEIGHT)
        step_rect = pygame.Rect(MARGIN_LEFT + 10 + 2 * (FIELD_WIDTH + SPACING_X), y + 30, FIELD_WIDTH, FIELD_HEIGHT)
        dir_pin_rect = pygame.Rect(MARGIN_LEFT + 10 + 3 * (FIELD_WIDTH + SPACING_X), y + 30, FIELD_WIDTH, FIELD_HEIGHT)
        speed_undo_rect = pygame.Rect(speed_rect.x + FIELD_WIDTH - 25, speed_rect.y, 20, 20)
        dir_undo_rect = pygame.Rect(dir_rect.x + FIELD_WIDTH - 25, dir_rect.y, 20, 20)
        step_undo_rect = pygame.Rect(step_rect.x + FIELD_WIDTH - 25, step_rect.y, 20, 20)
        dir_pin_undo_rect = pygame.Rect(dir_pin_rect.x + FIELD_WIDTH - 25, dir_pin_rect.y, 20, 20)
        motor_speed_rects.append(speed_rect)
        motor_dir_rects.append(dir_rect)
        motor_step_rects.append(step_rect)
        motor_dir_pin_rects.append(dir_pin_rect)
        motor_speed_undo_rects.append(speed_undo_rect)
        motor_dir_undo_rects.append(dir_undo_rect)
        motor_step_undo_rects.append(step_undo_rect)
        motor_dir_pin_undo_rects.append(dir_pin_undo_rect)
        if i == 3:
            motor4_limit_rect = pygame.Rect(MARGIN_LEFT + 10 + 4 * (FIELD_WIDTH + SPACING_X), y + 30, FIELD_WIDTH, FIELD_HEIGHT)
            motor4_min_rect = pygame.Rect(MARGIN_LEFT + 10 + 5 * (FIELD_WIDTH + SPACING_X), y + 30, FIELD_WIDTH, FIELD_HEIGHT)

    # Build servo 1 limit field rectangles in the dedicated claw limits section.
    servo1_min_rect = pygame.Rect(MARGIN_LEFT + 220, servo_limits_section_top + 30, FIELD_WIDTH, FIELD_HEIGHT)
    servo1_max_rect = pygame.Rect(MARGIN_LEFT + 220 + FIELD_WIDTH + SPACING_X, servo_limits_section_top + 30, FIELD_WIDTH, FIELD_HEIGHT)

    # Build the microswitch field rectangles below the motor settings cards.
    for i in range(NUM_MICROSWITCHES):
        ms_rect = pygame.Rect(MARGIN_LEFT + 220, microswitch_section_top + 30 + i * (FIELD_HEIGHT + SPACING_Y), FIELD_WIDTH, FIELD_HEIGHT)
        microswitch_rects.append(ms_rect)

    # Build the joystick GPIO field rectangles below the microswitch section.
    for i in range(NUM_GPIO):
        y = gpio_section_top + 30 + i * (FIELD_HEIGHT + SPACING_Y)
        gpio_rect = pygame.Rect(MARGIN_LEFT + 220, y, FIELD_WIDTH, FIELD_HEIGHT)
        gpio_rects.append(gpio_rect)

    # Manual control buttons let the operator jog motors and servos from the GUI.
    ctrl_labels = ["Motors 1 & 2", "Motor 3", "Motor 4", "Servo 1", "Servo 2 (MG995)"]
    ctrl_btn1_texts = ["UP", "LEFT", "FWD", "UP", "LEFT"]
    ctrl_btn2_texts = ["DOWN", "RIGHT", "REV", "DOWN", "RIGHT"]
    ctrl_btn1_rects = []
    ctrl_btn2_rects = []
    ctrl_zero_motor4_rect = None  # Zero position button for Motor 4
    for i in range(NUM_CTRL_ROWS):
        y = controls_section_top + 35 + i * CTRL_ROW_H
        btn1 = pygame.Rect(MARGIN_LEFT + 170, y, CTRL_BTN_W, CTRL_BTN_H)
        btn2 = pygame.Rect(MARGIN_LEFT + 280, y, CTRL_BTN_W, CTRL_BTN_H)
        ctrl_btn1_rects.append(btn1)
        ctrl_btn2_rects.append(btn2)
        if i == 2:  # Motor 4 row
            ctrl_zero_motor4_rect = pygame.Rect(MARGIN_LEFT + 390, y, 110, CTRL_BTN_H)
    active_ctrl_btn = None
    zero_motor4_flash = 0  # Flash counter for visual feedback

    # Start the optional direct-hardware helper used by the on-screen control buttons.
    crane = CraneHardware(settings.get('motors', []))

    # The window is fixed-height, so the full content is scrolled vertically.
    scroll_y = 0
    max_scroll = max(0, content_height - HEIGHT)
    scroll_speed = 30

    clock = pygame.time.Clock()
    running = True
    input_text = ''
    input_idx = None
    input_type = None  # 'speed', 'gpio'
    prev_valid_speed = motor_speed_values.copy()
    speed_selected = [False] * NUM_MOTORS

    show_confirm = False
    confirm_rect = pygame.Rect(WIDTH // 2 - 140, HEIGHT // 2 - 60, 280, 120)
    confirm_yes_rect = pygame.Rect(confirm_rect.x + 30, confirm_rect.y + 60, 80, 40)
    confirm_no_rect = pygame.Rect(confirm_rect.x + 170, confirm_rect.y + 60, 80, 40)

    # Flash counters provide short visual feedback for invalid or clamped values.
    speed_flash = [0] * NUM_MOTORS
    pin_flash = [0] * NUM_MOTORS  # For step_pin and dir_pin validation

    while running:
        # Redraw the full UI every frame using the current field values and active states.
        screen.fill(BG_COLOR)

        # Draw into an off-screen surface, then blit the visible scrolled slice to the window.
        content_surface = pygame.Surface((WIDTH, content_height))
        content_surface.fill(BG_COLOR)

        # Compare current field values against the original settings to highlight edits.
        speed_changed = [abs(float(motor_speed_values[i]) - original_settings['motors'][i]['speed']) > 1e-6 for i in range(NUM_MOTORS)]
        dir_changed = [dir_options[motor_dir_selected[i]] != original_settings['motors'][i]['direction'] for i in range(NUM_MOTORS)]
        step_changed = [(int(motor_step_values[i]) if motor_step_values[i] else 0) != original_settings['motors'][i]['step_pin'] for i in range(NUM_MOTORS)]
        dir_pin_changed = [(int(motor_dir_pin_values[i]) if motor_dir_pin_values[i] else 0) != original_settings['motors'][i]['dir_pin'] for i in range(NUM_MOTORS)]
        gpio_changed = [(int(gpio_values[i]) if gpio_values[i] else 0) != original_settings['joystick']['gpio_pins'][i] for i in range(NUM_GPIO)]
        ms_changed = [(int(microswitch_values[i]) if microswitch_values[i] else 0) != original_settings.get('microswitches', {}).get('gpio_pins', [0]*NUM_MICROSWITCHES)[i] for i in range(NUM_MICROSWITCHES)]
        motor4_limit_changed = (int(motor4_limit_value) if motor4_limit_value else 10000) != original_settings.get('motor4_limit', {}).get('max_steps', 10000)
        motor4_min_changed = (int(motor4_min_value) if motor4_min_value else 0) != original_settings.get('motor4_limit', {}).get('min_steps', 0)
        servo1_min_changed = abs((float(servo1_min_value) if servo1_min_value else 0.0) - original_settings.get('servo1_limits', {}).get('min_angle', 0.0)) > 1e-6
        servo1_max_changed = abs((float(servo1_max_value) if servo1_max_value else 45.0) - original_settings.get('servo1_limits', {}).get('max_angle', 45.0)) > 1e-6

        # Rebuild the full settings object so Save/Revert can operate on one source of truth.
        current_settings = get_current_settings(motor_speed_values, motor_dir_selected, motor_step_values, motor_dir_pin_values, gpio_values, microswitch_values, dir_options, motor4_limit_value, motor4_min_value, servo1_min_value, servo1_max_value)
        changes_exist = has_changes(original_settings, current_settings)

        # Render the motor settings cards and their per-field change indicators.
        motors_title = font.render("Motors", True, TEXT_COLOR)
        content_surface.blit(motors_title, (MARGIN_LEFT, MARGIN_TOP - FONT_SIZE - 35))

        # Draw each motor row as a boxed group of related settings.
        for i in range(NUM_MOTORS):
            y = MARGIN_TOP + i * (MOTOR_BOX_HEIGHT + SPACING_Y)
            box_rect = pygame.Rect(MARGIN_LEFT, y, MOTOR_BOX_WIDTH, MOTOR_BOX_HEIGHT)
            pygame.draw.rect(content_surface, SURFACE_COLOR, box_rect, border_radius=8)
            pygame.draw.rect(content_surface, BORDER_COLOR, box_rect, 1, border_radius=8)
            # Motor label
            label = font.render(f"Motor {i+1}", True, ACCENT_BLUE)
            content_surface.blit(label, (MARGIN_LEFT + 10, y - 22))
            
            # Speed label and field
            speed_label = font.render("Speed", True, TEXT_DIM)
            content_surface.blit(speed_label, (motor_speed_rects[i].x, motor_speed_rects[i].y - 18))
            draw_text_field(content_surface, font, "", motor_speed_values[i], motor_speed_rects[i], motor_speed_active[i], speed_flash[i], speed_changed[i])
            
            # Direction label and dropdown
            dir_label = font.render("Dir", True, TEXT_DIM)
            content_surface.blit(dir_label, (motor_dir_rects[i].x, motor_dir_rects[i].y - 18))
            draw_dropdown(content_surface, font, "", dir_options, motor_dir_selected[i], motor_dir_rects[i], motor_dir_active[i], dir_changed[i])
            
            # Step Pin label and field
            step_label = font.render("Step Pin", True, TEXT_DIM)
            content_surface.blit(step_label, (motor_step_rects[i].x, motor_step_rects[i].y - 18))
            draw_text_field(content_surface, font, "", motor_step_values[i], motor_step_rects[i], motor_step_active[i], pin_flash[i], step_changed[i])
            
            # Dir Pin label and field
            dir_pin_label = font.render("Dir Pin", True, TEXT_DIM)
            content_surface.blit(dir_pin_label, (motor_dir_pin_rects[i].x, motor_dir_pin_rects[i].y - 18))
            draw_text_field(content_surface, font, "", motor_dir_pin_values[i], motor_dir_pin_rects[i], motor_dir_pin_active[i], pin_flash[i], dir_pin_changed[i])

            # Each changed field gets its own small undo button.
            if speed_changed[i]:
                pygame.draw.rect(content_surface, ACCENT_RED, motor_speed_undo_rects[i], border_radius=3)
                undo_label = font.render("↶", True, WHITE)
                content_surface.blit(undo_label, (motor_speed_undo_rects[i].x + 2, motor_speed_undo_rects[i].y + 2))
            if dir_changed[i]:
                pygame.draw.rect(content_surface, ACCENT_RED, motor_dir_undo_rects[i], border_radius=3)
                undo_label = font.render("↶", True, WHITE)
                content_surface.blit(undo_label, (motor_dir_undo_rects[i].x + 2, motor_dir_undo_rects[i].y + 2))
            if step_changed[i]:
                pygame.draw.rect(content_surface, ACCENT_RED, motor_step_undo_rects[i], border_radius=3)
                undo_label = font.render("↶", True, WHITE)
                content_surface.blit(undo_label, (motor_step_undo_rects[i].x + 2, motor_step_undo_rects[i].y + 2))
            if dir_pin_changed[i]:
                pygame.draw.rect(content_surface, ACCENT_RED, motor_dir_pin_undo_rects[i], border_radius=3)
                undo_label = font.render("↶", True, WHITE)
                content_surface.blit(undo_label, (motor_dir_pin_undo_rects[i].x + 2, motor_dir_pin_undo_rects[i].y + 2))

            # Motor 4 includes extra limit fields because it uses persisted step tracking.
            if i == 3 and motor4_limit_rect:
                limit_label = font.render("Max Steps", True, TEXT_DIM)
                content_surface.blit(limit_label, (motor4_limit_rect.x, motor4_limit_rect.y - 18))
                draw_text_field(content_surface, font, "", motor4_limit_value, motor4_limit_rect, input_type == 'motor4_limit', 0, motor4_limit_changed)
            if i == 3 and motor4_min_rect:
                min_label = font.render("Min Steps", True, TEXT_DIM)
                content_surface.blit(min_label, (motor4_min_rect.x, motor4_min_rect.y - 18))
                draw_text_field(content_surface, font, "", motor4_min_value, motor4_min_rect, input_type == 'motor4_min', 0, motor4_min_changed)

        # Draw the claw servo limits section.
        sl_box_rect = pygame.Rect(MARGIN_LEFT, servo_limits_section_top, MOTOR_BOX_WIDTH, servo_limits_box_height)
        pygame.draw.rect(content_surface, SURFACE_COLOR, sl_box_rect, border_radius=8)
        pygame.draw.rect(content_surface, BORDER_COLOR, sl_box_rect, 1, border_radius=8)
        sl_title = font.render("Claw Servo (Servo 1) Travel Limits", True, ACCENT_BLUE)
        content_surface.blit(sl_title, (MARGIN_LEFT + 10, servo_limits_section_top + 8))
        s1min_label = font.render("Min Angle °", True, TEXT_DIM)
        content_surface.blit(s1min_label, (servo1_min_rect.x, servo1_min_rect.y - 18))
        draw_text_field(content_surface, font, "", servo1_min_value, servo1_min_rect, input_type == 'servo1_min', 0, servo1_min_changed)
        s1max_label = font.render("Max Angle °", True, TEXT_DIM)
        content_surface.blit(s1max_label, (servo1_max_rect.x, servo1_max_rect.y - 18))
        draw_text_field(content_surface, font, "", servo1_max_value, servo1_max_rect, input_type == 'servo1_max', 0, servo1_max_changed)

        # Draw the microswitch configuration section.
        ms_box_rect = pygame.Rect(MARGIN_LEFT, microswitch_section_top, MOTOR_BOX_WIDTH, microswitch_box_height)
        pygame.draw.rect(content_surface, SURFACE_COLOR, ms_box_rect, border_radius=8)
        pygame.draw.rect(content_surface, BORDER_COLOR, ms_box_rect, 1, border_radius=8)
        ms_title = font.render("Microswitch GPIO Pins", True, ACCENT_BLUE)
        content_surface.blit(ms_title, (MARGIN_LEFT + 10, microswitch_section_top + 8))

        ms_labels = ["Front Switch", "Back Switch", "Left Switch", "Right Switch", "Motor4 Home"]
        for i in range(NUM_MICROSWITCHES):
            y = microswitch_section_top + 30 + i * (FIELD_HEIGHT + SPACING_Y)
            label = font.render(ms_labels[i], True, TEXT_DIM)
            content_surface.blit(label, (MARGIN_LEFT + 10, y + 5))
            draw_text_field(content_surface, font, "", microswitch_values[i], microswitch_rects[i], microswitch_active[i], changed=ms_changed[i])

        # Draw the joystick GPIO configuration section.
        gpio_box_rect = pygame.Rect(MARGIN_LEFT, gpio_section_top, MOTOR_BOX_WIDTH, gpio_box_height)
        pygame.draw.rect(content_surface, SURFACE_COLOR, gpio_box_rect, border_radius=8)
        pygame.draw.rect(content_surface, BORDER_COLOR, gpio_box_rect, 1, border_radius=8)
        gpio_title = font.render("Joystick GPIO Pins", True, ACCENT_BLUE)
        content_surface.blit(gpio_title, (MARGIN_LEFT + 10, gpio_section_top + 8))

        # Each joystick input pin can be edited independently.
        gpio_pin_labels = ["Pin 1 - Up", "Pin 2 - Down", "Pin 3 - Left", "Pin 4 - Right", "Pin 5 - Trigger", "Pin 6 - Top Button"]
        for i in range(NUM_GPIO):
            y = gpio_section_top + 30 + i * (FIELD_HEIGHT + SPACING_Y)
            label = font.render(gpio_pin_labels[i], True, TEXT_DIM)
            content_surface.blit(label, (MARGIN_LEFT + 10, y + 5))
            draw_text_field(content_surface, font, "", gpio_values[i], gpio_rects[i], gpio_active[i], changed=gpio_changed[i])

        # Draw the manual control panel used for live hardware jog/testing.
        ctrl_box_rect = pygame.Rect(MARGIN_LEFT, controls_section_top, MOTOR_BOX_WIDTH, controls_box_height)
        pygame.draw.rect(content_surface, SURFACE_COLOR, ctrl_box_rect, border_radius=8)
        pygame.draw.rect(content_surface, BORDER_COLOR, ctrl_box_rect, 1, border_radius=8)
        ctrl_title = font.render("Crane Controls", True, ACCENT_BLUE)
        content_surface.blit(ctrl_title, (MARGIN_LEFT + 10, controls_section_top + 8))

        for i in range(NUM_CTRL_ROWS):
            y = controls_section_top + 35 + i * CTRL_ROW_H
            lbl = font.render(ctrl_labels[i], True, TEXT_DIM)
            content_surface.blit(lbl, (MARGIN_LEFT + 15, y + 7))

            b1_color = ACCENT_BLUE if active_ctrl_btn == ('btn1', i) else FIELD_COLOR
            pygame.draw.rect(content_surface, b1_color, ctrl_btn1_rects[i], border_radius=5)
            pygame.draw.rect(content_surface, BORDER_COLOR, ctrl_btn1_rects[i], 1, border_radius=5)
            b1_lbl = font.render(ctrl_btn1_texts[i], True, TEXT_COLOR)
            content_surface.blit(b1_lbl, (ctrl_btn1_rects[i].x + 10, ctrl_btn1_rects[i].y + 8))

            b2_color = ACCENT_BLUE if active_ctrl_btn == ('btn2', i) else FIELD_COLOR
            pygame.draw.rect(content_surface, b2_color, ctrl_btn2_rects[i], border_radius=5)
            pygame.draw.rect(content_surface, BORDER_COLOR, ctrl_btn2_rects[i], 1, border_radius=5)
            b2_lbl = font.render(ctrl_btn2_texts[i], True, TEXT_COLOR)
            content_surface.blit(b2_lbl, (ctrl_btn2_rects[i].x + 10, ctrl_btn2_rects[i].y + 8))

            # Zero Pos resets motor 4's saved software position file to step 0.
            if i == 2 and ctrl_zero_motor4_rect:
                fade = min(zero_motor4_flash, 15)
                zero_color = (45, 180, 100) if fade > 0 else (80, 60, 140)
                pygame.draw.rect(content_surface, zero_color, ctrl_zero_motor4_rect, border_radius=5)
                pygame.draw.rect(content_surface, BORDER_COLOR, ctrl_zero_motor4_rect, 1, border_radius=5)
                zero_lbl = font.render("Zero Pos", True, WHITE)
                content_surface.blit(zero_lbl, (ctrl_zero_motor4_rect.x + 8, ctrl_zero_motor4_rect.y + 8))

        # Save writes the current GUI state back to config.json.
        pygame.draw.rect(content_surface, ACCENT_GREEN, save_button_rect, border_radius=6)
        save_label = font.render("Save", True, WHITE)
        content_surface.blit(save_label, (save_button_rect.x + 25, save_button_rect.y + 10))

        # Revert restores the last saved settings snapshot.
        if changes_exist:
            pygame.draw.rect(content_surface, ACCENT_RED, revert_button_rect, border_radius=6)
            revert_label = font.render("Revert", True, WHITE)
            content_surface.blit(revert_label, (revert_button_rect.x + 20, revert_button_rect.y + 10))

        # Present the scrolled content surface in the fixed-size window.
        screen.blit(content_surface, (0, -scroll_y))

        # Overlay a modal confirmation dialog before saving changes.
        if show_confirm:
            # Dim overlay
            overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 150))
            screen.blit(overlay, (0, 0))
            pygame.draw.rect(screen, SURFACE_COLOR, confirm_rect, border_radius=10)
            pygame.draw.rect(screen, BORDER_COLOR, confirm_rect, 1, border_radius=10)
            msg = font.render("Save changes to settings?", True, TEXT_COLOR)
            screen.blit(msg, (confirm_rect.x + 20, confirm_rect.y + 20))
            pygame.draw.rect(screen, ACCENT_GREEN, confirm_yes_rect, border_radius=6)
            pygame.draw.rect(screen, ACCENT_RED, confirm_no_rect, border_radius=6)
            yes_label = font.render("Yes", True, WHITE)
            no_label = font.render("No", True, WHITE)
            screen.blit(yes_label, (confirm_yes_rect.x + 18, confirm_yes_rect.y + 8))
            screen.blit(no_label, (confirm_no_rect.x + 22, confirm_no_rect.y + 8))

        # Handle mouse, keyboard, and scroll input for editing and manual control.
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                mx, my = event.pos
                if show_confirm:
                    # Confirmation dialog buttons either save the current state or cancel.
                    if confirm_yes_rect.collidepoint(mx, my):
                        # Save the current field values back to config.json.
                        try:
                            new_settings = {
                                'motors': [
                                    {
                                        'speed': float(motor_speed_values[j]),
                                        'direction': dir_options[motor_dir_selected[j]],
                                        'step_pin': int(motor_step_values[j]) if motor_step_values[j] else 0,
                                        'dir_pin': int(motor_dir_pin_values[j]) if motor_dir_pin_values[j] else 0
                                    } for j in range(NUM_MOTORS)
                                ],
                                'joystick': {
                                    'gpio_pins': [int(gpio_values[j]) if gpio_values[j] else 0 for j in range(NUM_GPIO)]
                                },
                                'microswitches': {
                                    'gpio_pins': [int(microswitch_values[j]) if microswitch_values[j] else 0 for j in range(NUM_MICROSWITCHES)]
                                },
                                'motor4_limit': {
                                    'min_steps': int(motor4_min_value) if motor4_min_value else 0,
                                    'max_steps': int(motor4_limit_value) if motor4_limit_value else 10000
                                },
                                'servo1_limits': {
                                    'min_angle': float(servo1_min_value) if servo1_min_value else 0.0,
                                    'max_angle': float(servo1_max_value) if servo1_max_value else 45.0
                                }
                            }
                            # Create a timestamped backup before overwriting an existing config.
                            if new_settings != original_settings:
                                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                backup_file = f"config_{timestamp}.bak"
                                if os.path.exists(SETTINGS_FILE):
                                    shutil.copy2(SETTINGS_FILE, backup_file)
                                    print(f"Backup created: {backup_file}")
                                    # Keep the backup set small and recent.
                                    cleanup_old_backups(max_backups=2)
                            with open(SETTINGS_FILE, 'w') as f:
                                json.dump(new_settings, f, indent=2)
                            # The saved settings now become the new clean baseline for change tracking.
                            original_settings = copy.deepcopy(new_settings)
                            crane.motors_config = new_settings.get('motors', [])
                        except Exception as e:
                            print(f"Error saving: {e}")
                        show_confirm = False
                    elif confirm_no_rect.collidepoint(mx, my):
                        show_confirm = False
                else:
                    my += scroll_y
                    # Field clicks activate in-place editing for the selected setting.
                    for i, rect in enumerate(motor_speed_rects):
                        if rect.collidepoint(mx, my):
                            motor_speed_active = [False] * NUM_MOTORS
                            gpio_active = [False] * NUM_GPIO
                            motor_dir_active = [False] * NUM_MOTORS
                            motor_step_active = [False] * NUM_MOTORS
                            motor_dir_pin_active = [False] * NUM_MOTORS
                            motor_speed_active[i] = True
                            input_text = ''  # Start fresh for replacement
                            input_idx = i
                            input_type = 'speed'
                            speed_selected = [False] * NUM_MOTORS
                            speed_selected[i] = True
                            break
                    # Clicking a direction field toggles between forward and reverse.
                    for i, rect in enumerate(motor_dir_rects):
                        if rect.collidepoint(mx, my):
                            motor_dir_selected[i] = 1 - motor_dir_selected[i]
                            break
                    # Step pin and direction pin fields use numeric text entry.
                    for i, rect in enumerate(motor_step_rects):
                        if rect.collidepoint(mx, my):
                            motor_speed_active = [False] * NUM_MOTORS
                            motor_step_active = [False] * NUM_MOTORS
                            motor_dir_pin_active = [False] * NUM_MOTORS
                            gpio_active = [False] * NUM_GPIO
                            motor_dir_active = [False] * NUM_MOTORS
                            motor_step_active[i] = True
                            input_text = ''  # Start fresh for replacement
                            input_idx = i
                            input_type = 'step_pin'
                            break
                    for i, rect in enumerate(motor_dir_pin_rects):
                        if rect.collidepoint(mx, my):
                            motor_speed_active = [False] * NUM_MOTORS
                            motor_step_active = [False] * NUM_MOTORS
                            motor_dir_pin_active = [False] * NUM_MOTORS
                            gpio_active = [False] * NUM_GPIO
                            motor_dir_active = [False] * NUM_MOTORS
                            motor_dir_pin_active[i] = True
                            input_text = ''  # Start fresh for replacement
                            input_idx = i
                            input_type = 'dir_pin'
                            break
                    for i, rect in enumerate(motor_speed_undo_rects):
                        if rect.collidepoint(mx, my) and speed_changed[i]:
                            motor_speed_values[i] = str(original_settings['motors'][i]['speed'])
                            motor_speed_active[i] = False
                            speed_flash[i] = False
                            if input_idx == i and input_type == 'speed':
                                input_type = None
                                input_idx = None
                                input_text = ''
                            break
                    for i, rect in enumerate(motor_dir_undo_rects):
                        if rect.collidepoint(mx, my) and dir_changed[i]:
                            motor_dir_selected[i] = dir_options.index(original_settings['motors'][i]['direction'])
                            motor_dir_active[i] = False
                            if input_idx == i and input_type == 'dir':
                                input_type = None
                                input_idx = None
                                input_text = ''
                            break
                    for i, rect in enumerate(motor_step_undo_rects):
                        if rect.collidepoint(mx, my) and step_changed[i]:
                            motor_step_values[i] = str(original_settings['motors'][i].get('step_pin', 0))
                            motor_step_active[i] = False
                            pin_flash[i] = False
                            if input_idx == i and input_type == 'step_pin':
                                input_type = None
                                input_idx = None
                                input_text = ''
                            break
                    for i, rect in enumerate(motor_dir_pin_undo_rects):
                        if rect.collidepoint(mx, my) and dir_pin_changed[i]:
                            motor_dir_pin_values[i] = str(original_settings['motors'][i].get('dir_pin', 0))
                            motor_dir_pin_active[i] = False
                            pin_flash[i] = False
                            if input_idx == i and input_type == 'dir_pin':
                                input_type = None
                                input_idx = None
                                input_text = ''
                            break
                    # Joystick GPIO fields are edited the same way as motor pin fields.
                    for i, rect in enumerate(gpio_rects):
                        if rect.collidepoint(mx, my):
                            motor_speed_active = [False] * NUM_MOTORS
                            gpio_active = [False] * NUM_GPIO
                            motor_dir_active = [False] * NUM_MOTORS
                            motor_step_active = [False] * NUM_MOTORS
                            motor_dir_pin_active = [False] * NUM_MOTORS
                            microswitch_active = [False] * NUM_MICROSWITCHES
                            gpio_active[i] = True
                            input_text = ''
                            input_idx = i
                            input_type = 'gpio'
                            break
                    # Microswitch GPIO fields are tracked in their own section.
                    for i, rect in enumerate(microswitch_rects):
                        if rect.collidepoint(mx, my):
                            motor_speed_active = [False] * NUM_MOTORS
                            gpio_active = [False] * NUM_GPIO
                            motor_dir_active = [False] * NUM_MOTORS
                            motor_step_active = [False] * NUM_MOTORS
                            motor_dir_pin_active = [False] * NUM_MOTORS
                            microswitch_active = [False] * NUM_MICROSWITCHES
                            microswitch_active[i] = True
                            input_text = ''
                            input_idx = i
                            input_type = 'microswitch'
                            break
                    # Motor 4 limit fields are single-value numeric inputs.
                    if motor4_limit_rect and motor4_limit_rect.collidepoint(mx, my):
                        motor_speed_active = [False] * NUM_MOTORS
                        gpio_active = [False] * NUM_GPIO
                        motor_dir_active = [False] * NUM_MOTORS
                        motor_step_active = [False] * NUM_MOTORS
                        motor_dir_pin_active = [False] * NUM_MOTORS
                        microswitch_active = [False] * NUM_MICROSWITCHES
                        input_text = ''
                        input_idx = 0
                        input_type = 'motor4_limit'
                    if motor4_min_rect and motor4_min_rect.collidepoint(mx, my):
                        motor_speed_active = [False] * NUM_MOTORS
                        gpio_active = [False] * NUM_GPIO
                        motor_dir_active = [False] * NUM_MOTORS
                        motor_step_active = [False] * NUM_MOTORS
                        motor_dir_pin_active = [False] * NUM_MOTORS
                        microswitch_active = [False] * NUM_MICROSWITCHES
                        input_text = ''
                        input_idx = 0
                        input_type = 'motor4_min'
                    # Claw servo limit fields.
                    if servo1_min_rect and servo1_min_rect.collidepoint(mx, my):
                        motor_speed_active = [False] * NUM_MOTORS
                        gpio_active = [False] * NUM_GPIO
                        motor_dir_active = [False] * NUM_MOTORS
                        motor_step_active = [False] * NUM_MOTORS
                        motor_dir_pin_active = [False] * NUM_MOTORS
                        microswitch_active = [False] * NUM_MICROSWITCHES
                        input_text = ''
                        input_idx = 0
                        input_type = 'servo1_min'
                    if servo1_max_rect and servo1_max_rect.collidepoint(mx, my):
                        motor_speed_active = [False] * NUM_MOTORS
                        gpio_active = [False] * NUM_GPIO
                        motor_dir_active = [False] * NUM_MOTORS
                        motor_step_active = [False] * NUM_MOTORS
                        motor_dir_pin_active = [False] * NUM_MOTORS
                        microswitch_active = [False] * NUM_MICROSWITCHES
                        input_text = ''
                        input_idx = 0
                        input_type = 'servo1_max'
                    # Save opens a confirmation prompt instead of writing immediately.
                    if save_button_rect.collidepoint(mx, my):
                        show_confirm = True
                    # Revert restores the original in-memory snapshot.
                    elif changes_exist and revert_button_rect.collidepoint(mx, my):
                        motor_speed_values = [str(m['speed']) for m in original_settings['motors']]
                        motor_dir_selected = [0 if m['direction'] == 'forward' else 1 for m in original_settings['motors']]
                        motor_step_values = [str(m.get('step_pin', 0)) for m in original_settings['motors']]
                        motor_dir_pin_values = [str(m.get('dir_pin', 0)) for m in original_settings['motors']]
                        gpio_values = [str(pin) for pin in original_settings['joystick']['gpio_pins']]
                        microswitch_values = [str(pin) for pin in original_settings.get('microswitches', {}).get('gpio_pins', [0]*NUM_MICROSWITCHES)]
                        motor4_limit_value = str(original_settings.get('motor4_limit', {}).get('max_steps', 10000))
                        motor4_min_value = str(original_settings.get('motor4_limit', {}).get('min_steps', 0))
                        servo1_min_value = str(original_settings.get('servo1_limits', {}).get('min_angle', 0.0))
                        servo1_max_value = str(original_settings.get('servo1_limits', {}).get('max_angle', 45.0))
                        # Clear focus and editing state after a revert.
                        motor_speed_active = [False] * NUM_MOTORS
                        motor_step_active = [False] * NUM_MOTORS
                        motor_dir_pin_active = [False] * NUM_MOTORS
                        gpio_active = [False] * NUM_GPIO
                        motor_dir_active = [False] * NUM_MOTORS
                        microswitch_active = [False] * NUM_MICROSWITCHES
                        input_type = None
                        input_idx = None
                        input_text = ''
                        # Reset any validation flash indicators as well.
                        speed_flash = [0] * NUM_MOTORS
                        pin_flash = [0] * NUM_MOTORS
                    # Manual control buttons directly jog hardware while the mouse is held down.
                    for i in range(NUM_CTRL_ROWS):
                        if ctrl_btn1_rects[i].collidepoint(mx, my):
                            active_ctrl_btn = ('btn1', i)
                            if i == 0: crane.start_motors([0, 1], 'forward')
                            elif i == 1: crane.start_motors([2], 'reverse')
                            elif i == 2: crane.start_motors([3], 'forward')
                            elif i == 3: crane.servo1_dir = -1
                            elif i == 4: crane.servo2_dir = -1
                            break
                        if ctrl_btn2_rects[i].collidepoint(mx, my):
                            active_ctrl_btn = ('btn2', i)
                            if i == 0: crane.start_motors([0, 1], 'reverse')
                            elif i == 1: crane.start_motors([2], 'forward')
                            elif i == 2: crane.start_motors([3], 'reverse')
                            elif i == 3: crane.servo1_dir = 1
                            elif i == 4: crane.servo2_dir = 1
                            break
                    # Zero Pos resets the persisted motor 4 position file used by the runtime controller.
                    if ctrl_zero_motor4_rect and ctrl_zero_motor4_rect.collidepoint(mx, my):
                        try:
                            _pos_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'motor4_position.json')
                            with open(_pos_path, 'w') as f:
                                json.dump({'current_steps': 0}, f)
                            zero_motor4_flash = 15
                            print("Motor 4 position zeroed.")
                        except Exception as e:
                            print(f"Error zeroing motor 4 position: {e}")
            elif event.type == pygame.KEYDOWN:
                if show_confirm:
                    if event.key == pygame.K_ESCAPE:
                        show_confirm = False
                elif input_type is not None and input_idx is not None:
                    if event.key == pygame.K_RETURN:
                        # Commit and validate the active text field when Enter is pressed.
                        if input_type == 'speed':
                            text = input_text.strip()
                            clamped = False
                            keyword_map = {
                                's': 0.005, 'S': 0.005, 'slow': 0.005, 'Slow': 0.005,
                                'm': 0.0005, 'M': 0.0005, 'med': 0.0005, 'Med': 0.0005, 'medium': 0.0005, 'Medium': 0.0005,
                                'f': 0.000005, 'F': 0.000005, 'fast': 0.000005, 'Fast': 0.000005
                            }
                            if text in keyword_map:
                                val = keyword_map[text]
                                motor_speed_values[input_idx] = f"{val:.6f}" if val < 0.01 else str(val)
                                prev_valid_speed[input_idx] = motor_speed_values[input_idx]
                            else:
                                try:
                                    val = float(text)
                                    # Clamp out-of-range motor speeds and flash the field if adjusted.
                                    if val > 1.0:
                                        clamped = True
                                        val = 1.0
                                    elif val < 0.000005:
                                        clamped = True
                                        val = 0.000005
                                    motor_speed_values[input_idx] = f"{val:.6f}" if val < 0.01 else str(val)
                                    if clamped:
                                        speed_flash[input_idx] = 15  # Start flash
                                    prev_valid_speed[input_idx] = motor_speed_values[input_idx]
                                except ValueError:
                                    # Reject invalid speed text and restore the previous valid value.
                                    speed_flash[input_idx] = 15
                                    motor_speed_values[input_idx] = prev_valid_speed[input_idx]
                        elif input_type == 'gpio':
                            text = input_text.strip()
                            try:
                                val = int(text) if text else 0
                                if val < 1 or val > 29:
                                    pin_flash[input_idx] = 15
                                    val = max(1, min(29, val))
                                gpio_values[input_idx] = str(val)
                            except ValueError:
                                pin_flash[input_idx] = 15
                                gpio_values[input_idx] = '0'
                        elif input_type == 'step_pin':
                            text = input_text.strip()
                            try:
                                val = int(text) if text else 0
                                if val < 1 or val > 29:
                                    pin_flash[input_idx] = 15
                                    val = max(1, min(29, val))
                                motor_step_values[input_idx] = str(val)
                            except ValueError:
                                pin_flash[input_idx] = 15
                                motor_step_values[input_idx] = '0'
                        elif input_type == 'dir_pin':
                            text = input_text.strip()
                            try:
                                val = int(text) if text else 0
                                if val < 1 or val > 29:
                                    pin_flash[input_idx] = 15
                                    val = max(1, min(29, val))
                                motor_dir_pin_values[input_idx] = str(val)
                            except ValueError:
                                pin_flash[input_idx] = 15
                                motor_dir_pin_values[input_idx] = '0'
                        elif input_type == 'microswitch':
                            text = input_text.strip()
                            try:
                                val = int(text) if text else 0
                                if val < 1 or val > 29:
                                    val = max(1, min(29, val))
                                microswitch_values[input_idx] = str(val)
                            except ValueError:
                                microswitch_values[input_idx] = '0'
                        elif input_type == 'motor4_limit':
                            text = input_text.strip()
                            try:
                                val = int(text) if text else 10000
                                if val < 1:
                                    val = 1
                                motor4_limit_value = str(val)
                            except ValueError:
                                motor4_limit_value = '10000'
                        elif input_type == 'motor4_min':
                            text = input_text.strip()
                            try:
                                motor4_min_value = str(int(text)) if text else '0'
                            except ValueError:
                                motor4_min_value = '0'
                        elif input_type == 'servo1_min':
                            text = input_text.strip()
                            try:
                                val = float(text) if text else 0.0
                                val = max(0.0, min(179.0, val))
                                servo1_min_value = str(val)
                            except ValueError:
                                servo1_min_value = '0.0'
                        elif input_type == 'servo1_max':
                            text = input_text.strip()
                            try:
                                val = float(text) if text else 45.0
                                val = max(0.0, min(180.0, val))
                                servo1_max_value = str(val)
                            except ValueError:
                                servo1_max_value = '45.0'
                        input_type = None
                        input_idx = None
                        input_text = ''
                    elif event.key == pygame.K_BACKSPACE:
                        input_text = input_text[:-1]
                    else:
                        # Restrict each field to the characters that make sense for that setting type.
                        if input_type == 'speed':
                            if (event.unicode.isalnum() or event.unicode in '. ' or event.unicode == '-') and len(input_text) < 12:
                                input_text += event.unicode
                        elif input_type == 'gpio':
                            if len(input_text) < 8 and (event.unicode.isdigit() or event.unicode == '-'):
                                input_text += event.unicode
                        elif input_type in ('step_pin', 'dir_pin'):
                            if len(input_text) < 2 and event.unicode.isdigit():
                                input_text += event.unicode
                        elif input_type == 'microswitch':
                            if len(input_text) < 2 and event.unicode.isdigit():
                                input_text += event.unicode
                        elif input_type == 'motor4_limit':
                            if len(input_text) < 8 and event.unicode.isdigit():
                                input_text += event.unicode
                        elif input_type == 'motor4_min':
                            if len(input_text) < 8 and (event.unicode.isdigit() or (event.unicode == '-' and len(input_text) == 0)):
                                input_text += event.unicode
                        elif input_type in ('servo1_min', 'servo1_max'):
                            if len(input_text) < 6 and (event.unicode.isdigit() or (event.unicode == '.' and '.' not in input_text)):
                                input_text += event.unicode
                        # Mirror the temporary edit text into the visible field as the user types.
                        if input_type == 'speed':
                            motor_speed_values[input_idx] = input_text
                        elif input_type == 'gpio':
                            gpio_values[input_idx] = input_text
                        elif input_type == 'step_pin':
                            motor_step_values[input_idx] = input_text
                        elif input_type == 'dir_pin':
                            motor_dir_pin_values[input_idx] = input_text
                        elif input_type == 'microswitch':
                            microswitch_values[input_idx] = input_text
                        elif input_type == 'motor4_limit':
                            motor4_limit_value = input_text
                        elif input_type == 'motor4_min':
                            motor4_min_value = input_text
                        elif input_type == 'servo1_min':
                            servo1_min_value = input_text
                        elif input_type == 'servo1_max':
                            servo1_max_value = input_text
            elif event.type == pygame.MOUSEBUTTONUP:
                # Releasing the mouse stops any jog action started by a control button.
                if active_ctrl_btn:
                    kind, row = active_ctrl_btn
                    if row == 0: crane.stop_motors([0, 1])
                    elif row == 1: crane.stop_motors([2])
                    elif row == 2: crane.stop_motors([3])
                    elif row == 3: crane.servo1_dir = 0
                    elif row == 4: crane.servo2_dir = 0
                    active_ctrl_btn = None
            elif event.type == pygame.MOUSEWHEEL:
                # Mouse wheel scrolls through the taller-than-window content surface.
                if not show_confirm:
                    scroll_y -= event.y * scroll_speed
                    scroll_y = max(0, min(scroll_y, max_scroll))

        # Countdown visual feedback timers once per frame.
        for i in range(NUM_MOTORS):
            if speed_flash[i] > 0:
                speed_flash[i] -= 1
            if pin_flash[i] > 0:
                pin_flash[i] -= 1
        if zero_motor4_flash > 0:
            zero_motor4_flash -= 1

        pygame.display.flip()
        clock.tick(30)

    crane.cleanup()
    pygame.quit()
    _start_crane_service()

if __name__ == '__main__':
    # Run the settings editor as a standalone desktop tool.
    main()

