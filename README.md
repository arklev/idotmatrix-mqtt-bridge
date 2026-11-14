# iDotMatrix Control Center & MQTT Bridge

A headless Windows System Tray application, local Web Control Panel, AI-ready REST API, and Home Assistant MQTT Auto-Discovery bridge for iDotMatrix LED pixel displays.

This project builds on the Bluetooth LE protocol implementation from [markusressel/idotmatrix-api-client](https://github.com/markusressel/idotmatrix-api-client).

## Key Features

- **Headless Windows System Tray (`pystray`)**: Runs cleanly in the background via `pythonw.exe` without a console window. Right-click for 1-click **Sync Time Now**, Power, Clock Styles, Image/GIF picker, Manual Override Lock, Screen Flip, BLE Reconnect, and Web UI launch.
- **Local Web Control Panel (`http://localhost:8099`)**: Dark-themed dashboard for live status, one-click time sync, brightness/color control, Clock & Text Studio, Countdown/Chronograph/Scoreboard/Eco Night Mode, image/GIF thumbnail gallery with drag-and-drop upload, persistent settings editor, and live logs.
- **Automatic Time Synchronization**: Syncs the display clock on startup, on every Bluetooth reconnect, and periodically on a configurable interval (default: every 60 minutes).
- **Smart Home Assistant Coexistence**:
  - Preserves all core MQTT Auto-Discovery entities (`light`, `text`, `image_loader`, `mode`, `effect`, `countdown`, `sync_time`).
  - Adds **Clock Style Select**, **Image Select Dropdown** (auto-populated from `images/`), **Manual Lock Switch**, **Screen Flip 180° Switch**, and **Bluetooth Connectivity Binary Sensor**.
  - **Manual Override Lock**: Hold manually selected images/text/effects (indefinitely or for a timer) without being overwritten by periodic Home Assistant fallback automations, while optionally allowing critical alert filenames (e.g., `bomb.gif`) to bypass the lock.
- **Persistent Hot-Reloadable Config**: All preferences (default clock style/color, text animation settings, Eco schedule, flip state, sync interval) are stored in `config.json`.
- **AI-Ready REST API**: Full OpenAPI/JSON API at `http://localhost:8099/docs` for automation scripts and AI assistants.

## Installation & Setup

1. Ensure Python 3.11+ and a Bluetooth LE adapter are available on the host machine.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `config.example.json` to `config.json` and enter your `device_mac` and MQTT broker details (`mqtt_server`, `mqtt_port`, `mqtt_user`, `mqtt_pass`).
4. Run the application:
   - **Windowless (Windows)**: Double-click `run_idotmatrix.pyw` or run `pythonw.exe idotmatrix_server.py`.
   - **With console output / headless server**: `python idotmatrix_server.py` (add `--no-tray` to run without a tray icon).
5. Open `http://localhost:8099` in your browser (or click **Open Control Panel** from the system tray icon).

## Configuration (`config.json`)

See `config.example.json` for all available fields:

- `device_mac`, `mqtt_server`, `mqtt_port`, `mqtt_user`, `mqtt_pass`, `image_dir`
- `web_host` (default `"0.0.0.0"`), `web_port` (default `8099`)
- `device`: `screen_size` (`16`, `32`, or `64`), `flip_screen`, `auto_sync_interval_minutes`, `sync_on_connect`, `brightness`
- `clock`: `style`, `color` (`[R, G, B]`), `show_date`, `hour24`
- `text`: `mode`, `speed`, `font_size`, `color_mode`, `text_color`, `bg_color`
- `eco`: `enabled`, `start_hour`, `start_minute`, `end_hour`, `end_minute`, `brightness`
- `image`: `resize_mode` (`FILL`, `FIT`, `STRETCH`), `palletize`
- `override`: `locked`, `lock_until`, `allow_critical_alerts`

## REST API Overview

Interactive API docs are available at `http://localhost:8099/docs`:

- `GET /api/status` — Live BLE/MQTT state, current display mode, and available enum options
- `POST /api/sync-time` — Immediate BLE clock synchronization
- `POST /api/power` — Toggle power, set brightness (5–100), or fill solid RGB color
- `POST /api/clock` — Display clock with custom style/color and optionally save as default
- `POST /api/text` — Display animated text with custom mode/speed/font/colors
- `POST /api/image` — Display an image/GIF from `images/` or `"clear"` to return to Clock
- `GET /api/images`, `POST /api/images/upload`, `DELETE /api/images/{filename}` — Manage media library
- `POST /api/effect`, `/api/countdown`, `/api/chronograph`, `/api/scoreboard`, `/api/eco`, `/api/flip`
- `POST /api/override` — Enable/disable/time-limit the Manual Override Lock
- `GET /api/settings`, `PUT /api/settings` — Read (secrets masked) or update configuration
- `GET /api/logs` — Fetch recent application logs
