# Posture Tracker (Feather ESP32-S2 Reverse TFT + Adafruit IO + Netlify)

A posture tracker using an Adafruit Feather ESP32-S2 Reverse TFT + ICM-20948 IMU.  
The Feather publishes posture data to **Adafruit IO**. A hosted dashboard reads it through a **Netlify Function proxy** so the Adafruit IO key is **not exposed in the browser**.

---

## Architecture

**Feather (CircuitPython) → Adafruit IO Feeds → Netlify Function Proxy → Dashboard (HTML/JS)**

Feeds (keys must match exactly):
- `posture-angle` (number)
- `posture-status` (`good` / `slouch_pending` / `slouching`)
- `slouch-count` (integer)

---

## Hardware

- Adafruit Feather ESP32-S2 Reverse TFT
- ICM-20948 IMU (STEMMA QT / I2C)

---

## Adafruit IO setup (feeds + key)

1. Create an Adafruit IO account.
2. Create these feed **keys**:
   - `posture-angle`
   - `posture-status`
   - `slouch-count`
3. Go to **Adafruit IO → My Key** and copy the **Active Key**.
   - If you click **Regenerate Key**, the key changes and **all old keys stop working immediately** (401 errors until updated everywhere).

---

## Feather setup (CircuitPython)

1. Install CircuitPython on the Feather.
2. Copy project files to the CIRCUITPY drive:
   - `code.py` at root
   - `Slouch/main.py` inside `Slouch/`
3. Install libraries into `CIRCUITPY/lib/` (minimum):
   - `adafruit_requests.mpy`
   - `adafruit_icm20x.mpy` (+ dependencies from the matching CP bundle)

### `settings.toml` (DO NOT COMMIT)

Create `settings.toml` **on the CIRCUITPY drive only**:

```toml
CIRCUITPY_WIFI_SSID="YOUR_WIFI_SSID"
CIRCUITPY_WIFI_PASSWORD="YOUR_WIFI_PASSWORD"

ADAFRUIT_IO_USERNAME="YOUR_AIO_USERNAME"
ADAFRUIT_IO_KEY="aio_XXXXXXXXXXXXXXXXXXXXXXXX"
```

Add `settings.toml` to `.gitignore` so it never gets pushed.

---

## Website deployment (Netlify)

### Why Netlify (instead of GitHub Pages)

Calling Adafruit IO directly from a static page forces you to put the key in the browser (bad) and can run into browser/CORS issues.
Netlify keeps the key server-side using environment variables + a proxy function.

### Files used

- `index.html` — dashboard UI
- `netlify/functions/aio.js` — proxy function to Adafruit IO
- `netlify.toml` — Netlify config

### Deploy steps

1. In Netlify: **Add new site → Import from Git** and select this repo.
2. In Netlify **Site settings → Environment variables**, add:
   - `AIO_USER` = your Adafruit IO username (e.g., `Bear2026_`)
   - `AIO_KEY` = your Adafruit IO key (`aio_...`)
3. Deploy. Your site will be at: `https://<your-site>.netlify.app`

### Test the proxy

Open:

```
https://<your-site>.netlify.app/.netlify/functions/aio?path=/feeds/posture-angle/data/last
```

You should see JSON. If you get **401**, your env vars are wrong or the key was regenerated.

---

## Real-time tuning (rates + thresholds)

### Why "too real-time" can break

If you publish too fast, Adafruit IO may return **429 rate limit** and the dashboard appears "stuck" (feeds stop updating).

Recommended approach:

- Publish every ~2 seconds
- Only send angle when it changes enough (e.g., ≥ 0.2°)
- Only send status/count when they change

Key knobs in `main.py`:

- `SAMPLE_INTERVAL` (sensor sampling)
- `EMA_ALPHA` (smoothing; higher = more responsive)
- `SLOUCH_ENTER_THRESHOLD` / `SLOUCH_EXIT_THRESHOLD`
- `SLOUCH_TIME_REQUIRED`
- `AIO_PUSH_INTERVAL` (+ angle delta threshold)

---

## Storage note (why more sensitivity uses more space)

In simple terms: **more sensitive = more tiny movements detected = more updates recorded/sent.**
More updates means more data points, so:

- CSV/history logs fill faster (if logging is enabled), and/or
- Adafruit IO receives more messages (can hit rate limits)

### Ideal product direction

A production version would:

- Stream efficiently (MQTT/WebSockets) instead of frequent polling
- Downsample/aggregate (store averages; keep raw briefly)
- Use a proper backend/time-series store (not a growing CSV)
- Buffer locally and upload in batches

---

## Media

Place these in `assets/`:

- Screenshot (Adafruit IO dashboard): `[Slouch_webapp_demo.png](https://github.com/aryaprasad08/slouch/blob/main/media/Slouch_webapp_demo.png)`
- Recording of how posture tracker: [Slouch_screenrecoding.gif](https://github.com/aryaprasad08/slouch/blob/main/media/Slouch_screenrecoding.gif)
- Recording of adafruit ESP32-S2 Rev TFT feather: [feather](https://github.com/aryaprasad08/slouch/blob/main/media/Slouch_screenrecoding.gif)


---

## Troubleshooting

- **401 Unauthorized (Feather or site):** wrong username/key OR key was regenerated.
- **429 rate limit:** increase publish interval and/or only send on change.
- **Dashboard not updating:** confirm Adafruit IO feed timestamps change; then test the Netlify proxy endpoint.
- **Captive portal Wi-Fi:** may "connect" but block requests—use a hotspot for testing.
