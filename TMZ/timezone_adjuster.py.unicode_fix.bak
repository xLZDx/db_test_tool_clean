"""
TimeZone Checker - No admin rights required.
Ensures the system timezone is Eastern Time (ET) for St. Petersburg, FL
and that the clock is accurate by querying an NTP server.
Shows a Windows popup alert and opens Date & Time settings if anything is wrong.

Usage:
    python timezone_adjuster.py              # Check timezone + time now
    python timezone_adjuster.py --watch      # Monitor continuously (checks every 30s)
    python timezone_adjuster.py --install    # Add to Windows Startup folder (uses --watch)
    python timezone_adjuster.py --uninstall  # Remove from Startup folder
"""

import subprocess
import sys
import os
import struct
import socket
import time
import ctypes
import winsound
from datetime import datetime, timezone, timedelta


# --- Configuration ---
# St. Petersburg, FL is in Eastern Time (EST/EDT)
EXPECTED_TZ_NAME = "Eastern Standard Time"  # Windows name for ET (covers both EST & EDT)
EXPECTED_UTC_OFFSETS = {-5, -4}  # EST = UTC-5, EDT = UTC-4
LOCATION = "St. Petersburg, FL"
NTP_SERVER = "time.windows.com"
NTP_TIMEOUT = 5  # seconds
MAX_TIME_DRIFT_SECONDS = 30  # warn if clock drifts more than this
WATCH_INTERVAL = 30  # seconds between checks in watch mode
SCRIPT_NAME = "TimeZoneChecker"


def get_current_timezone():
    """Get the current Windows timezone name and UTC offset using tzutil."""
    result = subprocess.run(
        ["tzutil.exe", "/g"], capture_output=True, text=True
    )
    tz_name = result.stdout.strip() if result.returncode == 0 else "Unknown"

    # Get UTC offset from Python
    local_now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    offset_hours = round((local_now - utc_now).total_seconds() / 3600)

    return tz_name, offset_hours


def query_ntp_time(server=NTP_SERVER, timeout=NTP_TIMEOUT):
    """Query an NTP server for the current time. Returns UTC datetime or None."""
    NTP_EPOCH = datetime(1900, 1, 1)
    NTP_PACKET_FORMAT = "!12I"
    NTP_PACKET_SIZE = 48

    # Build NTP request packet (client mode, version 3)
    packet = b"\x1b" + b"\0" * 47

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(NTP_PACKET_SIZE)
        sock.close()
    except (socket.timeout, socket.gaierror, OSError):
        return None

    if len(data) < NTP_PACKET_SIZE:
        return None

    unpacked = struct.unpack(NTP_PACKET_FORMAT, data)
    # Transmit timestamp is at index 10 (seconds) and 11 (fraction)
    ntp_seconds = unpacked[10] + unpacked[11] / (2**32)
    ntp_time = NTP_EPOCH + timedelta(seconds=ntp_seconds)
    return ntp_time


def check_time_accuracy():
    """Compare local clock to NTP server. Returns (drift_seconds, ntp_time) or (None, None)."""
    ntp_time = query_ntp_time()
    if ntp_time is None:
        return None, None

    local_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    drift = abs((local_utc - ntp_time).total_seconds())
    return drift, ntp_time


def show_notification(title, message):
    """Show a Windows message box notification (no admin needed)."""
    MB_OK = 0x00000000
    MB_ICONWARNING = 0x00000030
    MB_ICONINFORMATION = 0x00000040
    MB_TOPMOST = 0x00040000

    is_warning = "WARNING" in title.upper() or "WRONG" in title.upper()
    icon = MB_ICONWARNING if is_warning else MB_ICONINFORMATION

    ctypes.windll.user32.MessageBoxW(
        0, message, title, MB_OK | icon | MB_TOPMOST
    )


def open_datetime_settings():
    """Open Windows Date & Time settings page (no admin needed)."""
    subprocess.Popen(["explorer.exe", "ms-settings:dateandtime"])


def fix_timezone():
    """Attempt to set timezone back to Eastern Time. Returns (success, message)."""
    result = subprocess.run(
        ["tzutil.exe", "/s", EXPECTED_TZ_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, f"Timezone automatically changed to '{EXPECTED_TZ_NAME}'."
    else:
        return False, f"Could not auto-set timezone (access denied). {result.stderr.strip()}"


def sync_time():
    """Attempt to resync the Windows time service. Returns (success, message)."""
    result = subprocess.run(
        ["w32tm", "/resync", "/nowait"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, "Time sync requested successfully."
    else:
        return False, f"Could not sync time automatically. {result.stderr.strip()}"


def get_startup_folder():
    """Get the current user's Startup folder path."""
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )


def install_to_startup():
    """Create a shortcut/batch launcher in the user's Startup folder."""
    startup = get_startup_folder()
    python_exe = sys.executable
    script_path = os.path.abspath(__file__)

    # Create a small .bat file that runs this script in watch mode silently
    bat_path = os.path.join(startup, f"{SCRIPT_NAME}.bat")
    bat_content = f'@echo off\r\nstart /min "" "{python_exe}" "{script_path}" --watch\r\n'

    with open(bat_path, "w") as f:
        f.write(bat_content)

    print(f"[OK] Installed to Startup folder:")
    print(f"     {bat_path}")
    print(f"     Script will run automatically on logon.")
    return True


def uninstall_from_startup():
    """Remove the startup launcher."""
    startup = get_startup_folder()
    bat_path = os.path.join(startup, f"{SCRIPT_NAME}.bat")

    if os.path.exists(bat_path):
        os.remove(bat_path)
        print(f"[OK] Removed from Startup folder: {bat_path}")
    else:
        print(f"[INFO] Not found in Startup folder. Nothing to remove.")
    return True


def run_check():
    """Run timezone + time check. Auto-fixes timezone if wrong. Returns (issues, fixes)."""
    issues = []
    fixes = []

    # --- Check 1: Timezone must be Eastern Time (St. Petersburg, FL) ---
    tz_name, offset_hours = get_current_timezone()

    if tz_name != EXPECTED_TZ_NAME:
        old_tz = tz_name
        # Try to auto-fix
        success, msg = fix_timezone()
        if success:
            fixes.append(
                f"Timezone was '{old_tz}' — automatically changed back to "
                f"'{EXPECTED_TZ_NAME}' (Eastern Time for {LOCATION})."
            )
        else:
            issues.append(
                f"WRONG TIMEZONE: Your PC is set to '{old_tz}' "
                f"but it must be '{EXPECTED_TZ_NAME}' (Eastern Time for {LOCATION}).\n"
                f"Auto-fix failed: {msg}\n"
                f"Go to Settings > Time & Language > Date & Time > Time zone "
                f"and select '(UTC-05:00) Eastern Time (US & Canada)'."
            )
    elif offset_hours not in EXPECTED_UTC_OFFSETS:
        issues.append(
            f"Timezone name is correct but UTC offset ({offset_hours:+d}h) "
            f"is unexpected for {LOCATION} Eastern Time."
        )

    # --- Check 2: Clock accuracy via NTP ---
    drift, ntp_time = check_time_accuracy()
    if drift is not None and drift > MAX_TIME_DRIFT_SECONDS:
        # Try to auto-sync
        success, msg = sync_time()
        if success:
            fixes.append(f"Clock was off by {drift:.0f}s — time sync requested.")
        else:
            issues.append(
                f"CLOCK DRIFT: Your clock is off by {drift:.0f} seconds "
                f"compared to {NTP_SERVER}.\n"
                f"Auto-sync failed: {msg}\n"
                f"Go to Settings > Time & Language > Date & Time and click 'Sync now'."
            )

    return issues, fixes


def show_fix_popup(fixes):
    """Show a popup confirming automatic corrections were made."""
    full_msg = (
        f"Location: {LOCATION}\n\n"
        + "\n\n".join(fixes)
    )

    MB_OK = 0x00000000
    MB_ICONINFORMATION = 0x00000040
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000

    try:
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass

    ctypes.windll.user32.MessageBoxW(
        0, full_msg,
        f"TimeZone Auto-Corrected — {LOCATION}",
        MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND
    )


def show_alert_popup(issues):
    """Show a popup for issues that could not be auto-fixed."""
    full_msg = (
        f"Location: {LOCATION} — Expected timezone: Eastern Time (ET)\n\n"
        + "\n\n".join(issues)
        + "\n\nOpen Date & Time settings now?"
    )

    MB_YESNO = 0x00000004
    MB_ICONERROR = 0x00000010
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    IDYES = 6

    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass

    result = ctypes.windll.user32.MessageBoxW(
        0, full_msg,
        f"TIME/TIMEZONE ERROR — {LOCATION}",
        MB_YESNO | MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND
    )
    if result == IDYES:
        open_datetime_settings()


def watch_mode():
    """Continuously monitor timezone and clock accuracy. Auto-fixes when possible."""
    print(f"TimeZone Checker - Watch Mode for {LOCATION}")
    print(f"Expected timezone: {EXPECTED_TZ_NAME}")
    print(f"Auto-correction: ENABLED (silent)")
    print(f"Checking every {WATCH_INTERVAL}s.")
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            issues, fixes = run_check()
            now = datetime.now().strftime("%H:%M:%S")

            if fixes:
                print(f"[{now}] AUTO-FIXED:")
                for fix in fixes:
                    print(f"  >> {fix}")

            if issues:
                print(f"[{now}] ** ISSUES (could not auto-fix): {len(issues)} **")
                for issue in issues:
                    print(f"  - {issue}")

            if not issues and not fixes:
                print(f"[{now}] OK - Eastern Time, clock accurate.")

            time.sleep(WATCH_INTERVAL)

        except KeyboardInterrupt:
            print("\nWatch mode stopped.")
            break


def main():
    # Handle install/uninstall/watch commands
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "--install":
            install_to_startup()
            return
        elif cmd == "--uninstall":
            uninstall_from_startup()
            return
        elif cmd == "--watch":
            watch_mode()
            return
        else:
            print(f"Unknown option: {sys.argv[1]}")
            print("Usage: python timezone_adjuster.py [--watch | --install | --uninstall]")
            return

    print("=" * 55)
    print(f"  TimeZone Checker — {LOCATION}")
    print(f"  Expected: {EXPECTED_TZ_NAME} (ET)")
    print(f"  Auto-correction: ENABLED")
    print("=" * 55)

    # --- Run check with auto-fix ---
    tz_name, offset_hours = get_current_timezone()
    print(f"\nTimezone : {tz_name}")
    print(f"UTC Offset: {offset_hours:+d} hours")

    issues, fixes = run_check()

    if fixes:
        print()
        for fix in fixes:
            print(f"[AUTO-FIXED] {fix}")

    # --- Check clock accuracy for display ---
    print(f"\nChecking clock accuracy against {NTP_SERVER}...")
    drift, ntp_time = check_time_accuracy()

    if drift is None:
        print("[INFO] Could not reach NTP server. Skipping time accuracy check.")
    else:
        print(f"NTP Time  : {ntp_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"Local UTC : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"Drift     : {drift:.1f} seconds")
        if drift <= MAX_TIME_DRIFT_SECONDS:
            print("[OK] Clock is accurate.")

    # --- Show results ---
    print("\n" + "=" * 55)
    if fixes and not issues:
        print("Issues were detected and auto-corrected!")
    elif issues:
        print(f"[!] {len(issues)} issue(s) could not be auto-fixed.")
    else:
        print("All checks passed. Your timezone and clock are correct!")


if __name__ == "__main__":
    main()
