# =============================================================================
# POSTURE MONITOR + WEB DASHBOARD -- All on the ESP32-S3
# =============================================================================
# File: CIRCUITPY/Slouch/main.py
# Launched by: CIRCUITPY/code.py
#
# CIRCUITPY layout:
#   code.py                <- root launcher (imports this)
#   boot.py                <- OPTIONAL (only needed if no SD card)
#   settings.toml          <- WiFi: CIRCUITPY_WIFI_SSID / PASSWORD
#   Slouch/
#     main.py              <- THIS FILE
#     dashboard.html       <- web dashboard
#   sd/                    <- SD card (preferred for log writes)
#   lib/                   <- CircuitPython libraries
#
# READ-ONLY FIX:
#   CircuitPython won't let the board write to CIRCUITPY while USB
#   is connected. Two options:
#     A) SD card at /sd/ -- code auto-detects and writes there (no boot.py needed)
#     B) Add boot.py with storage.remount("/", readonly=False)
#        Then your Mac can't edit CIRCUITPY (use safe mode to edit)
# =============================================================================

import time
import math
import board
import gc
import adafruit_requests
import os
import json
import ssl

try:
    import adafruit_icm20x
except ImportError:
    print("ERROR: adafruit_icm20x library missing!")
    raise

try:
    import wifi
    import socketpool
    WIFI_AVAILABLE = True
except ImportError:
    WIFI_AVAILABLE = False
    print("WARN: WiFi unavailable")

try:
    import displayio
    import terminalio
    from adafruit_display_text import label as text_label
    DISPLAY_AVAILABLE = True
except ImportError:
    DISPLAY_AVAILABLE = False


# =============================================================================
# CONFIGURATION
# =============================================================================

WIFI_SSID     = ""
WIFI_PASSWORD = ""

SLOUCH_ENTER_THRESHOLD = 4.0
SLOUCH_EXIT_THRESHOLD  = 2.5
SLOUCH_TIME_REQUIRED   = 0.3

SAMPLE_INTERVAL = 0.05
EMA_ALPHA       = 0.45

CALIBRATION_SECONDS = 3
CALIBRATION_SAMPLES = int(CALIBRATION_SECONDS / SAMPLE_INTERVAL)

HISTORY_INTERVAL = 2.0
MAX_HISTORY      = 5000

SCREEN_W    = 240
SCREEN_H    = 135
ICM_ADDRESS = 0x69
GC_INTERVAL = 15.0

COL_BG   = 0x0D1117
COL_GOOD = 0x3FB950
COL_WARN = 0xF0B030
COL_BAD  = 0xF85149
COL_TEXT = 0xE6EDF3


# =============================================================================
# FIND A WRITABLE PATH FOR THE LOG FILE
# =============================================================================

LOG_FILE = None        # Set during init
LOG_ENABLED = False

def find_writable_path():
    """Try SD card first, then CIRCUITPY root. Return path or None."""
    # Option A: SD card (always writable by the board)
    sd_path = "/sd/posture_log.csv"
    try:
        # Check if /sd exists
        os.listdir("/sd")
        # Try writing
        with open(sd_path, "a") as f:
            pass
        print("Log path: %s (SD card)" % sd_path)
        return sd_path
    except OSError:
        pass

    # Option B: CIRCUITPY root (only works if boot.py remounted as writable)
    root_path = "/posture_log.csv"
    try:
        with open(root_path, "a") as f:
            pass
        print("Log path: %s (CIRCUITPY root)" % root_path)
        return root_path
    except OSError:
        pass

    # Neither worked
    print("WARNING: Cannot write log file!")
    print("  Option A: Insert SD card (writes to /sd/posture_log.csv)")
    print("  Option B: Add boot.py with: import storage; storage.remount('/', readonly=False)")
    print("  Dashboard will still work, but no history chart.")
    return None


# =============================================================================
# GLOBAL STATE
# =============================================================================

baseline_g     = (0.0, 0.0, 1.0)
filtered_angle = 0.0

posture_state       = "good"
state_enter_time    = 0.0
slouch_event_logged = False
slouch_start_mono   = 0.0

day_slouch_count   = 0
day_slouch_time    = 0.0
day_start_mono     = 0.0
day_last_good      = 0.0
day_best_streak    = 0.0
day_current_streak = 0.0

lbl_status = None
lbl_data   = None
lbl_ip     = None


# =============================================================================
# VECTOR MATH
# =============================================================================

def _clampf(x, lo, hi):
    if x < lo: return lo
    if x > hi: return hi
    return x

def normalize3(ax, ay, az):
    m = math.sqrt(ax*ax + ay*ay + az*az)
    if m < 1e-6: return (0.0, 0.0, 0.0)
    return (ax/m, ay/m, az/m)

def dot3(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def angle_between_deg(g0, g1):
    d = _clampf(dot3(g0, g1), -1.0, 1.0)
    return math.degrees(math.acos(d))

def ema_filter(old_val, new_val, alpha=EMA_ALPHA):
    return alpha * new_val + (1.0 - alpha) * old_val


# =============================================================================
# WIFI + WEB SERVER
# =============================================================================

def connect_wifi():
    if not WIFI_AVAILABLE:
        return None
    ssid = WIFI_SSID
    pw = WIFI_PASSWORD
    try:
        env_ssid = os.getenv("CIRCUITPY_WIFI_SSID")
        env_pw = os.getenv("CIRCUITPY_WIFI_PASSWORD")
        if env_ssid: ssid = env_ssid
        if env_pw: pw = env_pw
    except Exception:
        pass
    if not ssid:
        print("No WiFi SSID configured")
        return None
    print("WiFi: connecting to '%s'..." % ssid)
    try:
        wifi.radio.connect(ssid, pw)
        ip = str(wifi.radio.ipv4_address)
        print("WiFi: connected! IP = %s" % ip)
        return ip
    except Exception as e:
        print("WiFi failed: %s" % str(e))
        return None


_requests = None
_aio_user = None
_aio_key = None

def aio_init():
    global _requests, _aio_user, _aio_key
    _aio_user = os.getenv("ADAFRUIT_IO_USERNAME")
    _aio_key  = os.getenv("ADAFRUIT_IO_KEY")
    if not _aio_user or not _aio_key:
        print("Missing ADAFRUIT_IO_USERNAME / ADAFRUIT_IO_KEY in settings.toml")
        return False
    pool = socketpool.SocketPool(wifi.radio)
    ctx = ssl.create_default_context()
    _requests = adafruit_requests.Session(pool, ctx)
    print("AIO user:", _aio_user)
    print("AIO key starts:", (_aio_key or "")[:8])
    print("AIO key ends:", (_aio_key or "")[-4:])
    return True

def aio_send(feed_key, value):
    url = f"https://io.adafruit.com/api/v2/{_aio_user}/feeds/{feed_key}/data"
    headers = {"X-AIO-Key": _aio_key, "Content-Type": "application/json"}
    try:
        r = _requests.post(url, json={"value": value}, headers=headers)
        print("AIO POST", feed_key, "->", r.status_code)
        r.close()
    except Exception as e:
        print("AIO POST failed", feed_key, e)

AIO_FEED_ANGLE  = "posture-angle"
AIO_FEED_STATUS = "posture-status"
AIO_FEED_COUNT  = "slouch-count"

AIO_PUSH_INTERVAL = 2.0
ANGLE_DELTA_SEND = 0.2

_last_aio_push = 0.0
_last_angle_sent = None
_last_status = None
_last_count = None

def aio_publish(angle, status, slouch_count):
    global _last_aio_push, _last_angle_sent, _last_status, _last_count

    now = time.monotonic()
    if (now - _last_aio_push) < AIO_PUSH_INTERVAL:
        return
    _last_aio_push = now

    angle_rounded = round(angle, 1)

    # Angle: only if moved enough
    if _last_angle_sent is None or abs(angle_rounded - _last_angle_sent) >= ANGLE_DELTA_SEND:
        aio_send(AIO_FEED_ANGLE, angle_rounded)
        _last_angle_sent = angle_rounded

    # Status: only if changed
    if status != _last_status:
        aio_send(AIO_FEED_STATUS, status)
        _last_status = status

    # Count: only if changed
    if slouch_count != _last_count:
        aio_send(AIO_FEED_COUNT, int(slouch_count))
        _last_count = slouch_count



# =============================================================================
# HISTORY LOGGING
# =============================================================================

_last_history_write = 0.0
_history_count = 0

def init_history():
    global _history_count, LOG_FILE, LOG_ENABLED
    LOG_FILE = find_writable_path()
    if LOG_FILE is None:
        LOG_ENABLED = False
        return

    LOG_ENABLED = True
    try:
        with open(LOG_FILE, "r") as f:
            _history_count = sum(1 for line in f if line.strip() and not line.startswith("t,"))
    except OSError:
        with open(LOG_FILE, "w") as f:
            f.write("t,angle\n")
        _history_count = 0
    print("History: %d existing samples" % _history_count)


def append_history(angle):
    global _last_history_write, _history_count
    if not LOG_ENABLED:
        return

    now = time.monotonic()
    if (now - _last_history_write) < HISTORY_INTERVAL:
        return
    _last_history_write = now

    t = time.time()
    try:
        if _history_count >= MAX_HISTORY:
            truncate_history()
        with open(LOG_FILE, "a") as f:
            f.write("%.0f,%.1f\n" % (t, angle))
        _history_count += 1
    except OSError as e:
        print("CSV write error: %s" % str(e))


def truncate_history():
    global _history_count
    if not LOG_FILE:
        return
    try:
        lines = []
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        header = lines[0] if lines and lines[0].startswith("t,") else "t,angle\n"
        data_lines = [l for l in lines if l.strip() and not l.startswith("t,")]
        keep = data_lines[len(data_lines)//2:]
        with open(LOG_FILE, "w") as f:
            f.write(header)
            for l in keep:
                f.write(l)
        _history_count = len(keep)
        print("History truncated to %d rows" % _history_count)
    except OSError as e:
        print("Truncate error: %s" % str(e))


# =============================================================================
# CALIBRATION
# =============================================================================

def calibrate_baseline(icm):
    global baseline_g, filtered_angle
    print("")
    print("=== SIT UPRIGHT for %d seconds ===" % CALIBRATION_SECONDS)
    if lbl_status:
        lbl_status.text = "CALIBRATE"
        lbl_status.color = COL_WARN
    time.sleep(0.3)
    sx = sy = sz = 0.0
    for _ in range(CALIBRATION_SAMPLES):
        ax, ay, az = icm.acceleration
        gx, gy, gz = normalize3(ax, ay, az)
        sx += gx; sy += gy; sz += gz
        time.sleep(SAMPLE_INTERVAL)
    baseline_g = normalize3(sx, sy, sz)
    filtered_angle = 0.0
    print("Baseline g: (%+.3f, %+.3f, %+.3f)" % baseline_g)
    print("=== DONE ===")


# =============================================================================
# POSTURE STATE MACHINE
# =============================================================================

def update_posture_state(angle_deg, now):
    global posture_state, state_enter_time, slouch_event_logged, slouch_start_mono
    global day_slouch_count, day_slouch_time, day_best_streak, day_current_streak
    global day_last_good

    prev = posture_state

    if posture_state == "good":
        day_current_streak = now - day_last_good
        if day_current_streak > day_best_streak:
            day_best_streak = day_current_streak
        if angle_deg >= SLOUCH_ENTER_THRESHOLD:
            posture_state = "slouch_pending"
            state_enter_time = now

    elif posture_state == "slouch_pending":
        if angle_deg < SLOUCH_EXIT_THRESHOLD:
            posture_state = "good"
        elif (now - state_enter_time) >= SLOUCH_TIME_REQUIRED:
            posture_state = "slouching"
            slouch_start_mono = state_enter_time
            slouch_event_logged = False

    elif posture_state == "slouching":
        if angle_deg < SLOUCH_EXIT_THRESHOLD:
            dur = now - slouch_start_mono
            day_slouch_count += 1
            day_slouch_time += dur
            day_last_good = now
            day_current_streak = 0
            posture_state = "good"

    return posture_state, posture_state != prev


# =============================================================================
# DISPLAY (optional TFT)
# =============================================================================

def setup_display():
    global lbl_status, lbl_data, lbl_ip
    if not DISPLAY_AVAILABLE:
        return False
    try:
        disp = board.DISPLAY
    except AttributeError:
        return False
    g = displayio.Group()
    bg = displayio.Bitmap(SCREEN_W, SCREEN_H, 1)
    bp = displayio.Palette(1)
    bp[0] = COL_BG
    g.append(displayio.TileGrid(bg, pixel_shader=bp))
    font = terminalio.FONT
    lbl_status = text_label.Label(font, text="STARTING", color=COL_GOOD, x=8, y=12, scale=2)
    g.append(lbl_status)
    lbl_data = text_label.Label(font, text="Angle: --", color=COL_TEXT, x=8, y=45)
    g.append(lbl_data)
    lbl_ip = text_label.Label(font, text="WiFi: ...", color=0x6B7280, x=8, y=SCREEN_H - 15)
    g.append(lbl_ip)
    disp.root_group = g
    return True


def update_display(angle, state, ip_addr):
    icons = {"good": ":)", "slouch_pending": ":|", "slouching": "!!"}
    print("[%s] %.1fdeg %s" % (icons.get(state, "?"), angle, state))
    if lbl_status is None:
        return
    if state == "good":
        lbl_status.text = "GOOD"
        lbl_status.color = COL_GOOD
    elif state == "slouch_pending":
        lbl_status.text = "Hmm..."
        lbl_status.color = COL_WARN
    else:
        lbl_status.text = "SLOUCH!"
        lbl_status.color = COL_BAD
    lbl_data.text = "Angle: %.1f" % angle
    if lbl_ip:
        lbl_ip.text = "AIO: online" if ip_addr else "WiFi: offline"


# =============================================================================
# MAIN
# =============================================================================

def main():
    global filtered_angle, _last_gc, day_start_mono, day_last_good

    print("Init ICM-20948...")
    icm = adafruit_icm20x.ICM20948(board.STEMMA_I2C(), address=ICM_ADDRESS)
    print("Sensor OK.")

    setup_display()

    # WiFi + Adafruit IO
    ip_addr = connect_wifi()
    if ip_addr and aio_init():
        aio_send("posture-status", "booted")
        if lbl_ip: lbl_ip.text = "AIO OK"
    else:
        if lbl_ip: lbl_ip.text = "AIO FAIL"

    # History logging
    init_history()

    # Calibrate
    calibrate_baseline(icm)

    day_start_mono = time.monotonic()
    day_last_good = day_start_mono

    _last_gc = time.monotonic()
    last_display = 0.0
    display_int = 0.25

    print("Monitoring posture...")
    print("")

    while True:
        now = time.monotonic()

        # Sensor (guard against occasional I2C glitch)
        try:
            ax, ay, az = icm.acceleration
        except OSError as e:
            print("IMU I2C glitch:", e)
            time.sleep(0.1)
            continue

        g1 = normalize3(ax, ay, az)
        raw_angle = angle_between_deg(baseline_g, g1)
        filtered_angle = ema_filter(filtered_angle, raw_angle)

        # State machine
        state, changed = update_posture_state(filtered_angle, now)
        aio_publish(filtered_angle, state, day_slouch_count)

        # History
        append_history(filtered_angle)

        # Display
        if (now - last_display) >= display_int:
            update_display(filtered_angle, state, ip_addr)
            last_display = now

        # GC
        if (now - _last_gc) >= GC_INTERVAL:
            gc.collect()
            _last_gc = now

        time.sleep(SAMPLE_INTERVAL)
