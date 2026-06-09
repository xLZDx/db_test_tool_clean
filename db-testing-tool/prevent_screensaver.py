import ctypes
import time

# Windows API constants for SetThreadExecutionState
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# Windows API constants for keybd_event
VK_SHIFT = 0x10
KEYEVENTF_KEYUP = 0x0002

# Windows API constants for mouse_event
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def press_shift_key():
    # Simulate Shift key down and up.
    ctypes.windll.user32.keybd_event(VK_SHIFT, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)


def double_click_mouse():
    # Simulate two left mouse clicks at the current cursor position.
    for _ in range(2):
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.05)


def prevent_screensaver():
    print("Preventing sleep/screensaver (Shift + double click every 4 min). Press Ctrl+C to stop.")
    try:
        while True:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            press_shift_key()
            double_click_mouse()
            time.sleep(240)
    except KeyboardInterrupt:
        print("\nStopped. System may now enter sleep/screensaver mode.")
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


if __name__ == "__main__":
    prevent_screensaver()
