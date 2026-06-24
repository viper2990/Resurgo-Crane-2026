"""
GPIO Cleanup Module
Provides utilities for initializing and cleaning up GPIO resources.

Centralizes GPIO startup/shutdown so multiple scripts can share one pattern.
Use init_gpio() once at startup and cleanup_gpio() at shutdown.
"""

import RPi.GPIO as GPIO
import atexit


# Module-level guard so setup runs at most once even if init_gpio() is called again.
_initialized = False


def init_gpio():
    """Initialize GPIO (BCM mode, warnings off). Safe to call more than once."""
    global _initialized
    if _initialized:
        return
    try:
        # Use BCM numbering consistently across the crane project.
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        # Register a last-chance cleanup handler for normal interpreter shutdown.
        atexit.register(cleanup_gpio)
        _initialized = True
        print("[GPIO] GPIO manager initialized")
    except Exception as e:
        print(f"[GPIO] Error during setup: {e}")


def cleanup_gpio():
    """Clean up all GPIO resources. Allows re-initialization afterwards."""
    global _initialized
    try:
        GPIO.cleanup()
        print("[GPIO] GPIO cleanup complete")
    except Exception as e:
        print(f"[GPIO] Error during cleanup: {e}")
    finally:
        _initialized = False
