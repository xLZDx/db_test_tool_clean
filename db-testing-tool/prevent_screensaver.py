import ctypes
import time

# Windows API constants
import ctypes
import time

# Windows API constants
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def prevent_screensaver():
    print("Preventing screen saver (Shift key every 4 min). Press Ctrl+C to stop.")
    try:
        while True:
            # Prevent sleep and screen saver
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            press_shift_key()
            time.sleep(240)  # 4 minutes
    except KeyboardInterrupt:
        print("\nStopped. System may now enter sleep/screensaver mode.")
        # Clear the override
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

def press_shift_key():
    # Simulate Shift key down and up
    ctypes.windll.user32.keybd_event(VK_SHIFT, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)

# Windows API constants for keybd_event
VK_SHIFT = 0x10
KEYEVENTF_KEYUP = 0x0002

if __name__ == "__main__":
    prevent_screensaver()
