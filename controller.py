"""Core controller bridging BLE iDotMatrix client, MQTT Home Assistant discovery, Web API, and System Tray."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt
from PIL import Image as PILImage

from idotmatrix.client import IDotMatrixClient
from idotmatrix.connection_manager import ConnectionListener
from idotmatrix.modules.clock import ClockStyle
from idotmatrix.modules.effect import EffectStyle
from idotmatrix.modules.text import TextColorMode, TextMode
from idotmatrix.screensize import ScreenSize
from idotmatrix.util import color_utils
from idotmatrix.util.image_utils import ResizeMode
from settings_manager import BASE_DIR, SettingsManager

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
DEFAULT_EFFECT_COLORS = ["red", "orange", "yellow", "green", "darkblue", "magenta", "white"]
DEVICE_INFO = {
    "identifiers": ["idotmatrix_3f_4f"],
    "name": "iDotMatrix",
    "manufacturer": "iDotMatrix",
    "model": "LED Screen",
}


class RingBufferLogHandler(logging.Handler):
    """Stores recent log records in memory for the Web UI log viewer."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self._records: Deque[Dict[str, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            with self._lock:
                self._records.append(entry)
        except Exception:
            pass

    def get_entries(self, limit: int = 200) -> List[Dict[str, str]]:
        with self._lock:
            entries = list(self._records)
        if limit > 0:
            return entries[-limit:]
        return entries


def configure_logging() -> RingBufferLogHandler:
    """Configures file, memory ring-buffer, and optional console logging safely under pythonw.exe."""
    import sys

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter("%(asctime)s :: %(levelname)s :: %(name)s :: %(message)s")

    ring_handler = RingBufferLogHandler(capacity=300)
    ring_handler.setLevel(logging.INFO)
    root.addHandler(ring_handler)

    file_handler = RotatingFileHandler(
        BASE_DIR / "idotmatrix.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(logging.INFO)
        root.addHandler(stream_handler)

    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return ring_handler


def parse_rgb_color(value: Any, default: Tuple[int, int, int] = (255, 255, 255)) -> Tuple[int, int, int]:
    """Parses lists, tuples, ints, hex strings, or named colors into an (R, G, B) tuple."""
    if value is None:
        return default
    if isinstance(value, list) and len(value) == 3:
        return (int(value[0]) & 0xFF, int(value[1]) & 0xFF, int(value[2]) & 0xFF)
    if isinstance(value, dict) and {"r", "g", "b"}.issubset(value.keys()):
        return (int(value["r"]) & 0xFF, int(value["g"]) & 0xFF, int(value["b"]) & 0xFF)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        if len(stripped) == 6 and all(c in "0123456789abcdefABCDEF" for c in stripped):
            stripped = f"#{stripped}"
        return color_utils.parse_color_rgb(stripped)
    return color_utils.parse_color_rgb(value)


def resolve_enum(enum_cls: Any, value: Any, default: Any) -> Any:
    """Resolves an enum member from name (case-insensitive), integer value, or member."""
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        idx = int(value)
        for member in enum_cls:
            if member.value == idx:
                return member
    if isinstance(value, str):
        needle = value.strip().lower().replace("-", "_").replace(" ", "_")
        for member in enum_cls:
            if member.name.lower() == needle:
                return member
    raise ValueError(f"Unknown {enum_cls.__name__} value: {value}")


def resolve_screen_size(size_int: int) -> ScreenSize:
    mapping = {
        16: ScreenSize.SIZE_16x16,
        32: ScreenSize.SIZE_32x32,
        64: ScreenSize.SIZE_64x64,
    }
    return mapping.get(int(size_int), ScreenSize.SIZE_32x32)


def resolve_resize_mode(value: Any) -> ResizeMode:
    if isinstance(value, ResizeMode):
        return value
    key = str(value or "FILL").strip().upper()
    return ResizeMode.__members__.get(key, ResizeMode.FILL)


class IDotMatrixController:
    """Thread-safe / asyncio-safe coordinator for the iDotMatrix LED display."""

    def __init__(self, settings: SettingsManager, log_handler: Optional[RingBufferLogHandler] = None) -> None:
        self.logger = logging.getLogger(__name__)
        self.settings = settings
        self.log_handler = log_handler

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ble_lock = asyncio.Lock()
        self.msg_queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()

        cfg = self.settings.get_all()
        screen_size = resolve_screen_size(cfg["device"]["screen_size"])
        self.client = IDotMatrixClient(
            screen_size=screen_size,
            mac_address=cfg.get("device_mac") or None,
        )
        self.client.set_auto_reconnect(True)
        self.client.add_connection_listener(
            ConnectionListener(
                on_connected=self._on_ble_connected_cb,
                on_disconnected=self._on_ble_disconnected_cb,
            )
        )

        self.mqtt_client: Optional[mqtt.Client] = None
        self.mqtt_connected: bool = False
        self.ble_connected: bool = False

        self.power_on: bool = True
        self.brightness: int = int(cfg["device"].get("brightness", 80))
        self.active_color: Tuple[int, int, int] = tuple(cfg["clock"].get("color", [255, 255, 255]))  # type: ignore
        self.current_mode: str = "Clock"
        self.current_image: str = "clear"
        self.current_text: str = ""
        self.current_effect: str = "Effect 0"
        self.last_time_sync: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._last_ble_attempt_monotonic: float = 0.0
        self._stop_event = asyncio.Event()
        self._bg_tasks: List[asyncio.Task[Any]] = []
        self._state_listeners: List[Callable[[], None]] = []
        self._shutdown_requested_cb: Optional[Callable[[], None]] = None

    def set_shutdown_callback(self, cb: Callable[[], None]) -> None:
        self._shutdown_requested_cb = cb

    def request_shutdown(self) -> None:
        self._stop_event.set()
        if self._shutdown_requested_cb:
            self._shutdown_requested_cb()

    def add_state_listener(self, cb: Callable[[], None]) -> None:
        self._state_listeners.append(cb)

    def _notify_state_listeners(self) -> None:
        for cb in self._state_listeners:
            try:
                cb()
            except Exception as exc:
                self.logger.debug("State listener error: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Starts BLE connection, MQTT client, command worker, and periodic sync loop."""
        self.loop = asyncio.get_running_loop()
        self.settings.resolve_image_dir()

        if not self.settings.is_startup_configured():
            self.logger.warning(
                "device_mac or mqtt_server is not configured in config.json. "
                "Web UI is running so you can configure settings."
            )
        else:
            self._start_mqtt()
            try:
                await self.ensure_connected()
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.error("Initial BLE connect failed (will retry automatically): %s", exc)

        self._bg_tasks = [
            asyncio.create_task(self._command_worker(), name="mqtt-command-worker"),
            asyncio.create_task(self._periodic_maintenance_loop(), name="periodic-maintenance"),
        ]
        self._notify_state_listeners()

    async def stop(self) -> None:
        """Gracefully shuts down background loops, MQTT, and BLE connection."""
        self._stop_event.set()
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        if self.mqtt_client is not None:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception as exc:
                self.logger.debug("Error disconnecting MQTT: %s", exc)

        try:
            if self.client._connection_manager.is_connected():
                await self.client.disconnect()
        except Exception as exc:
            self.logger.debug("Error disconnecting BLE: %s", exc)

    # ------------------------------------------------------------------
    # BLE Connection & Callbacks
    # ------------------------------------------------------------------
    def is_ble_connected(self) -> bool:
        try:
            connected = bool(self.client._connection_manager.is_connected())
        except Exception:
            connected = False
        self.ble_connected = connected
        return connected

    async def _on_ble_connected_cb(self) -> None:
        self.ble_connected = True
        self.last_error = None
        self.logger.info("BLE connected to iDotMatrix (%s)", self.client.mac_address)
        self._publish_mqtt("idotmatrix/display/ble/state", "ON", retain=True)
        self._notify_state_listeners()
        if self.loop is not None:
            self.loop.create_task(self._post_connect_sync())

    async def _on_ble_disconnected_cb(self) -> None:
        self.ble_connected = False
        self.logger.warning("BLE disconnected from iDotMatrix")
        self._publish_mqtt("idotmatrix/display/ble/state", "OFF", retain=True)
        self._notify_state_listeners()

    async def _post_connect_sync(self) -> None:
        cfg = self.settings.get_all()
        if cfg["device"].get("sync_on_connect", True):
            try:
                await asyncio.sleep(0.5)
                await self.sync_time(source="connect")
            except Exception as exc:
                self.logger.warning("Post-connect time sync failed: %s", exc)
        if cfg["device"].get("flip_screen", False):
            try:
                async with self.ble_lock:
                    await self._ensure_ble_unlocked()
                    await self.client.common.set_screen_flipped(True)
            except Exception as exc:
                self.logger.warning("Post-connect screen flip apply failed: %s", exc)
        eco_cfg = cfg.get("eco", {})
        if eco_cfg.get("enabled", False):
            try:
                async with self.ble_lock:
                    await self._ensure_ble_unlocked()
                    await self.client.eco.set_mode(
                        enabled=True,
                        start_hour=int(eco_cfg.get("start_hour", 22)),
                        start_minute=int(eco_cfg.get("start_minute", 0)),
                        end_hour=int(eco_cfg.get("end_hour", 6)),
                        end_minute=int(eco_cfg.get("end_minute", 0)),
                        eco_brightness=int(eco_cfg.get("brightness", 10)),
                    )
            except Exception as exc:
                self.logger.warning("Post-connect eco mode apply failed: %s", exc)

    async def ensure_connected(self) -> None:
        async with self.ble_lock:
            self._last_ble_attempt_monotonic = time.monotonic()
            if not self.client._connection_manager.is_connected():
                cfg = self.settings.get_all()
                mac = cfg.get("device_mac")
                if not mac or mac == "00:00:00:00:00:00":
                    raise RuntimeError("device_mac is not configured")
                self.client.mac_address = mac
                self.client._connection_manager.address = mac
                await self.client.connect()
            self.ble_connected = bool(self.client._connection_manager.is_connected())

    async def reconnect_ble(self) -> Dict[str, Any]:
        """Forces a clean BLE disconnect and reconnect."""
        async with self.ble_lock:
            self.logger.info("Manual BLE reconnect requested...")
            try:
                if self.client._connection_manager.is_connected():
                    await self.client.disconnect()
            except Exception as exc:
                self.logger.debug("Disconnect during reconnect raised: %s", exc)
            await asyncio.sleep(1.0)
            self._last_ble_attempt_monotonic = time.monotonic()
            await self.client.connect()
            self.ble_connected = bool(self.client._connection_manager.is_connected())
        self._publish_mqtt("idotmatrix/display/ble/state", "ON" if self.ble_connected else "OFF", retain=True)
        self._notify_state_listeners()
        return self.get_status()

    # ------------------------------------------------------------------
    # Override Lock Logic (HA Smart Coexistence)
    # ------------------------------------------------------------------
    def is_override_locked(self) -> bool:
        cfg = self.settings.get("override", {})
        if not cfg.get("locked", False):
            return False
        lock_until_raw = cfg.get("lock_until")
        if lock_until_raw:
            try:
                lock_until = datetime.fromisoformat(lock_until_raw)
                if datetime.now() >= lock_until:
                    self.settings.update({"override": {"locked": False, "lock_until": None}})
                    self._publish_mqtt("idotmatrix/display/override/state", "OFF", retain=True)
                    self._notify_state_listeners()
                    return False
            except ValueError:
                pass
        return True

    async def set_override_lock(
        self,
        locked: bool,
        minutes: Optional[int] = None,
        return_to_ha_auto: bool = False,
    ) -> Dict[str, Any]:
        lock_until: Optional[str] = None
        if locked and minutes and int(minutes) > 0:
            lock_until = (datetime.now() + timedelta(minutes=int(minutes))).isoformat(timespec="seconds")

        self.settings.update({
            "override": {
                "locked": bool(locked),
                "lock_until": lock_until,
            }
        })
        self._publish_mqtt("idotmatrix/display/override/state", "ON" if locked else "OFF", retain=True)
        self.logger.info(
            "Manual override lock %s%s",
            "ENABLED" if locked else "DISABLED",
            f" until {lock_until}" if lock_until else "",
        )
        if not locked and return_to_ha_auto:
            await self.show_clock()
            self._publish_image_state("clear")
        self._notify_state_listeners()
        return self.get_status()

    # ------------------------------------------------------------------
    # Device Commands
    # ------------------------------------------------------------------
    async def sync_time(self, source: str = "manual") -> Dict[str, Any]:
        now = datetime.now()
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.common.set_time(now)
        self.last_time_sync = now
        self.last_error = None
        self.logger.info("Synced device clock (%s) to %s", source, now.strftime("%Y-%m-%d %H:%M:%S"))
        self._notify_state_listeners()
        return self.get_status()

    async def set_power(
        self,
        is_on: Optional[bool] = None,
        brightness: Optional[int] = None,
        color: Optional[Any] = None,
    ) -> Dict[str, Any]:
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            if is_on is not None:
                if is_on:
                    await self.client.turn_on()
                else:
                    await self.client.turn_off()
                self.power_on = bool(is_on)
            if brightness is not None:
                clamped = max(5, min(100, int(brightness)))
                await self.client.set_brightness(clamped)
                self.brightness = clamped
                self.settings.update({"device": {"brightness": clamped}})
            if color is not None:
                rgb = parse_rgb_color(color, self.active_color)
                await self.client.color.show_color(rgb)
                self.active_color = rgb
                self.current_mode = "Color"
        self._publish_light_state()
        self._notify_state_listeners()
        return self.get_status()

    async def show_clock(
        self,
        style: Optional[Any] = None,
        color: Optional[Any] = None,
        show_date: Optional[bool] = None,
        hour24: Optional[bool] = None,
        save_default: bool = False,
        lock_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        clock_cfg = self.settings.get("clock", {})
        resolved_style = resolve_enum(ClockStyle, style if style is not None else clock_cfg.get("style"), ClockStyle.RGBSwipeOutline)
        resolved_color = parse_rgb_color(color if color is not None else clock_cfg.get("color"), (255, 255, 255))
        resolved_show_date = bool(clock_cfg.get("show_date", True) if show_date is None else show_date)
        resolved_hour24 = bool(clock_cfg.get("hour24", True) if hour24 is None else hour24)

        if save_default:
            self.settings.update({
                "clock": {
                    "style": resolved_style.name,
                    "color": list(resolved_color),
                    "show_date": resolved_show_date,
                    "hour24": resolved_hour24,
                }
            })

        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.clock.show(
                style=resolved_style,
                show_date=resolved_show_date,
                hour24=resolved_hour24,
                color=resolved_color,
            )

        self.power_on = True
        self.current_mode = "Clock"
        self.current_image = "clear"
        self.active_color = resolved_color
        if lock_minutes is not None:
            await self.set_override_lock(lock_minutes != 0, None if lock_minutes < 0 else lock_minutes)

        self._publish_mqtt("idotmatrix/display/mode/state", "Clock", retain=True)
        self._publish_mqtt("idotmatrix/display/clock_style/state", resolved_style.name, retain=True)
        self._publish_image_state("clear")
        self._publish_light_state()
        self._notify_state_listeners()
        return self.get_status()

    async def show_text(
        self,
        text: str,
        mode: Optional[Any] = None,
        speed: Optional[int] = None,
        font_size: Optional[int] = None,
        color_mode: Optional[Any] = None,
        text_color: Optional[Any] = None,
        bg_color: Optional[Any] = None,
        save_default: bool = False,
        lock_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not text:
            raise ValueError("Text cannot be empty")
        text_cfg = self.settings.get("text", {})
        resolved_mode = resolve_enum(TextMode, mode if mode is not None else text_cfg.get("mode"), TextMode.MARQUEE)
        resolved_speed = max(1, min(100, int(speed if speed is not None else text_cfg.get("speed", 95))))
        resolved_font_size = max(6, min(64, int(font_size if font_size is not None else text_cfg.get("font_size", 16))))
        resolved_color_mode = resolve_enum(
            TextColorMode,
            color_mode if color_mode is not None else text_cfg.get("color_mode"),
            TextColorMode.WHITE,
        )
        resolved_text_color = parse_rgb_color(
            text_color if text_color is not None else text_cfg.get("text_color"),
            (255, 255, 255),
        )
        raw_bg = bg_color if bg_color is not None else text_cfg.get("bg_color")
        resolved_bg_color = parse_rgb_color(raw_bg) if raw_bg is not None else None

        if save_default:
            self.settings.update({
                "text": {
                    "mode": resolved_mode.name,
                    "speed": resolved_speed,
                    "font_size": resolved_font_size,
                    "color_mode": resolved_color_mode.name,
                    "text_color": list(resolved_text_color),
                    "bg_color": list(resolved_bg_color) if resolved_bg_color else None,
                }
            })

        font_path = str(BASE_DIR / "fonts" / "Rain-DRM3.otf")
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.text.show_text(
                text=text,
                font_size=resolved_font_size,
                font_path=font_path,
                text_mode=resolved_mode,
                speed=resolved_speed,
                text_color_mode=resolved_color_mode,
                text_color=resolved_text_color,
                text_bg_color=resolved_bg_color,
            )

        self.power_on = True
        self.current_mode = "Text"
        self.current_text = text
        if lock_minutes is not None:
            await self.set_override_lock(lock_minutes != 0, None if lock_minutes < 0 else lock_minutes)

        self._publish_mqtt("idotmatrix/display/text/state", text, retain=True)
        self._notify_state_listeners()
        return self.get_status()

    async def show_image(
        self,
        filename: str,
        resize_mode: Optional[str] = None,
        palletize: Optional[bool] = None,
        lock_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        raw = (filename or "").strip().strip("'\"")
        clean_lower = raw.lower()
        safe_name = Path(raw).name

        if clean_lower in {"clear", "reset", "none", "off", ""} or safe_name.lower() in {"clear", "reset"}:
            self.logger.info("Clearing image display (input=%r) -> returning to Clock mode", filename)
            return await self.show_clock(lock_minutes=lock_minutes)

        image_dir = self.settings.resolve_image_dir()
        img_path = (image_dir / safe_name).resolve()
        if not str(img_path).startswith(str(image_dir.resolve())) or not img_path.exists() or not img_path.is_file():
            raise FileNotFoundError(f"Image '{safe_name}' not found in {image_dir}")

        img_cfg = self.settings.get("image", {})
        resolved_resize = resolve_resize_mode(resize_mode or img_cfg.get("resize_mode", "FILL"))
        resolved_palletize = bool(img_cfg.get("palletize", True) if palletize is None else palletize)

        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            self.logger.info("Uploading %s (resize=%s, palletize=%s)", img_path.name, resolved_resize.name, resolved_palletize)
            if img_path.suffix.lower() == ".gif":
                await self.client.image.set_mode(0)
                await self.client.color.show_color((0, 0, 0))
                await self.client.reset()
                await self.client.gif.upload_gif_file(
                    img_path,
                    resize_mode=resolved_resize,
                    palletize=resolved_palletize,
                )
                await asyncio.sleep(1.5)
            else:
                await self.client.image.set_mode(1)
                await self.client.image.upload_image_file(
                    img_path,
                    resize_mode=resolved_resize,
                    palletize=False if img_path.suffix.lower() in {".png", ".jpg", ".jpeg"} else resolved_palletize,
                )

        self.power_on = True
        self.current_mode = "Image"
        self.current_image = safe_name
        if lock_minutes is not None:
            await self.set_override_lock(lock_minutes != 0, None if lock_minutes < 0 else lock_minutes)

        self._publish_image_state(safe_name)
        self._notify_state_listeners()
        return self.get_status()

    async def show_effect(
        self,
        style: Any,
        colors: Optional[List[Any]] = None,
        lock_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        if isinstance(style, str) and style.lower().startswith("effect"):
            idx_str = style.lower().replace("effect", "").strip()
            resolved = resolve_enum(EffectStyle, int(idx_str), EffectStyle.GRADIENT_HORIZONTAL_RAINBOW)
        else:
            resolved = resolve_enum(EffectStyle, style, EffectStyle.GRADIENT_HORIZONTAL_RAINBOW)

        palette = [parse_rgb_color(c) for c in (colors or DEFAULT_EFFECT_COLORS)]
        if len(palette) < 2:
            palette = [parse_rgb_color(c) for c in DEFAULT_EFFECT_COLORS]

        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.effect.show(resolved, palette[:7])

        self.power_on = True
        self.current_mode = "Effect"
        self.current_effect = f"Effect {resolved.value}"
        if lock_minutes is not None:
            await self.set_override_lock(lock_minutes != 0, None if lock_minutes < 0 else lock_minutes)

        self._publish_mqtt("idotmatrix/display/effect/state", self.current_effect, retain=True)
        self._notify_state_listeners()
        return self.get_status()

    async def start_countdown(self, minutes: int, seconds: int = 0) -> Dict[str, Any]:
        m = max(0, min(59, int(minutes)))
        s = max(0, min(59, int(seconds)))
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            if m == 0 and s == 0:
                await self.client.countdown.stop()
                self.current_mode = "Clock"
            else:
                await self.client.countdown.start(minutes=m, seconds=s)
                self.current_mode = "Countdown"
        self._notify_state_listeners()
        return self.get_status()

    async def chronograph_action(self, action: str) -> Dict[str, Any]:
        act = action.strip().lower()
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            if act in {"start", "start_from_zero"}:
                await self.client.chronograph.start_from_zero()
            elif act == "pause":
                await self.client.chronograph.pause()
            elif act == "resume":
                await self.client.chronograph.resume()
            elif act == "reset":
                await self.client.chronograph.reset()
            else:
                raise ValueError(f"Unsupported chronograph action: {action}")
        self.current_mode = "Chronograph"
        self._publish_mqtt("idotmatrix/display/mode/state", "Chronograph", retain=True)
        self._notify_state_listeners()
        return self.get_status()

    async def show_scoreboard(self, count1: int, count2: int) -> Dict[str, Any]:
        c1 = max(0, min(999, int(count1)))
        c2 = max(0, min(999, int(count2)))
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.scoreboard.show(c1, c2)
        self.current_mode = "Scoreboard"
        self._notify_state_listeners()
        return self.get_status()

    async def apply_eco(
        self,
        enabled: bool,
        start_hour: int = 22,
        start_minute: int = 0,
        end_hour: int = 6,
        end_minute: int = 0,
        brightness: int = 10,
        save_default: bool = True,
    ) -> Dict[str, Any]:
        if save_default:
            self.settings.update({
                "eco": {
                    "enabled": bool(enabled),
                    "start_hour": int(start_hour),
                    "start_minute": int(start_minute),
                    "end_hour": int(end_hour),
                    "end_minute": int(end_minute),
                    "brightness": int(brightness),
                }
            })
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.eco.set_mode(
                enabled=bool(enabled),
                start_hour=int(start_hour),
                start_minute=int(start_minute),
                end_hour=int(end_hour),
                end_minute=int(end_minute),
                eco_brightness=int(brightness),
            )
        self._notify_state_listeners()
        return self.get_status()

    async def set_flip(self, flip: bool) -> Dict[str, Any]:
        self.settings.update({"device": {"flip_screen": bool(flip)}})
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.common.set_screen_flipped(bool(flip))
        self._publish_mqtt("idotmatrix/display/flip/state", "ON" if flip else "OFF", retain=True)
        self._notify_state_listeners()
        return self.get_status()

    async def reset_device(self) -> Dict[str, Any]:
        async with self.ble_lock:
            await self._ensure_ble_unlocked()
            await self.client.reset()
        self.current_image = "clear"
        self._notify_state_listeners()
        return self.get_status()

    async def _ensure_ble_unlocked(self) -> None:
        if not self.client._connection_manager.is_connected():
            self._last_ble_attempt_monotonic = time.monotonic()
            await self.client.connect()
        self.ble_connected = bool(self.client._connection_manager.is_connected())

    # ------------------------------------------------------------------
    # Image Library Management
    # ------------------------------------------------------------------
    def list_images(self) -> List[Dict[str, Any]]:
        image_dir = self.settings.resolve_image_dir()
        items: List[Dict[str, Any]] = []
        for path in sorted(image_dir.iterdir(), key=lambda p: p.name.lower()):
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                stat = path.stat()
                dims = None
                try:
                    with PILImage.open(path) as im:
                        dims = [im.width, im.height]
                except Exception:
                    pass
                items.append({
                    "name": path.name,
                    "url": f"/images/{path.name}",
                    "size_bytes": stat.st_size,
                    "is_gif": path.suffix.lower() == ".gif",
                    "dimensions": dims,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
        return items

    def save_uploaded_image(self, filename: str, content: bytes) -> Dict[str, Any]:
        safe_name = Path(filename).name
        if not safe_name or Path(safe_name).suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(f"Unsupported image extension. Allowed: {sorted(SUPPORTED_IMAGE_EXTENSIONS)}")
        image_dir = self.settings.resolve_image_dir()
        dest = (image_dir / safe_name).resolve()
        if not str(dest).startswith(str(image_dir.resolve())):
            raise ValueError("Invalid filename")
        dest.write_bytes(content)
        self.logger.info("Saved uploaded image %s (%d bytes)", safe_name, len(content))
        self.publish_discovery_messages()
        return {"name": safe_name, "url": f"/images/{safe_name}", "size_bytes": len(content)}

    def delete_image(self, filename: str) -> None:
        safe_name = Path(filename).name
        image_dir = self.settings.resolve_image_dir()
        target = (image_dir / safe_name).resolve()
        if not str(target).startswith(str(image_dir.resolve())) or not target.exists():
            raise FileNotFoundError(f"Image '{safe_name}' not found")
        os.remove(target)
        self.logger.info("Deleted image %s", safe_name)
        self.publish_discovery_messages()

    # ------------------------------------------------------------------
    # Status & Logs
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        cfg = self.settings.get_all()
        override_cfg = cfg.get("override", {})
        locked = self.is_override_locked()
        return {
            "ble_connected": self.is_ble_connected(),
            "mqtt_connected": self.mqtt_connected,
            "device_mac": cfg.get("device_mac", ""),
            "power_on": self.power_on,
            "brightness": self.brightness,
            "active_color": list(self.active_color),
            "current_mode": self.current_mode,
            "current_image": self.current_image,
            "current_text": self.current_text,
            "current_effect": self.current_effect,
            "flip_screen": bool(cfg["device"].get("flip_screen", False)),
            "last_time_sync": self.last_time_sync.isoformat(timespec="seconds") if self.last_time_sync else None,
            "auto_sync_interval_minutes": int(cfg["device"].get("auto_sync_interval_minutes", 60)),
            "override_locked": locked,
            "override_lock_until": override_cfg.get("lock_until") if locked else None,
            "last_error": self.last_error,
            "clock": cfg.get("clock", {}),
            "text": cfg.get("text", {}),
            "eco": cfg.get("eco", {}),
            "image_defaults": cfg.get("image", {}),
            "images": [item["name"] for item in self.list_images()],
        }

    def get_logs(self, limit: int = 200) -> List[Dict[str, str]]:
        if self.log_handler is None:
            return []
        return self.log_handler.get_entries(limit=limit)

    # ------------------------------------------------------------------
    # Background Loops
    # ------------------------------------------------------------------
    async def _periodic_maintenance_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(15)
                if self._stop_event.is_set():
                    break

                # Check override lock expiration
                self.is_override_locked()

                cfg = self.settings.get_all()
                if not self.settings.is_startup_configured():
                    continue

                # Connection watchdog in case initial boot failed while display was powered off
                if not self.is_ble_connected():
                    if time.monotonic() - self._last_ble_attempt_monotonic >= 30:
                        try:
                            await self.ensure_connected()
                        except Exception as exc:
                            self.last_error = str(exc)
                            self.logger.debug("Periodic BLE connect attempt failed: %s", exc)
                    continue

                # Periodic auto time-sync
                interval_min = int(cfg["device"].get("auto_sync_interval_minutes", 60))
                if interval_min > 0:
                    now = datetime.now()
                    due = (
                        self.last_time_sync is None
                        or (now - self.last_time_sync) >= timedelta(minutes=interval_min)
                        or now < self.last_time_sync
                    )
                    if due:
                        await self.sync_time(source="periodic")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.error("Error in periodic maintenance loop: %s", exc)

    async def _command_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                topic, payload = await self.msg_queue.get()
                await self._handle_mqtt_command(topic, payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.error("Unhandled MQTT worker error: %s", exc)

    # ------------------------------------------------------------------
    # MQTT Setup, Discovery & Command Routing
    # ------------------------------------------------------------------
    def _start_mqtt(self) -> None:
        cfg = self.settings.get_all()
        mqtt_server = cfg.get("mqtt_server", "")
        mqtt_port = int(cfg.get("mqtt_port", 1883))
        mqtt_user = cfg.get("mqtt_user", "")
        mqtt_pass = cfg.get("mqtt_pass", "")

        try:
            self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            self.mqtt_client = mqtt.Client()

        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_client.on_message = self._on_mqtt_message
        if mqtt_user and mqtt_pass:
            self.mqtt_client.username_pw_set(mqtt_user, mqtt_pass)

        try:
            self.mqtt_client.connect_async(mqtt_server, mqtt_port, 60)
            self.mqtt_client.loop_start()
        except Exception as exc:
            self.last_error = f"MQTT connection error: {exc}"
            self.logger.error("Failed to start MQTT client: %s", exc)

    def _on_mqtt_connect(self, client: mqtt.Client, userdata: Any, flags: Any, rc: Any, properties: Any = None) -> None:
        self.mqtt_connected = True
        self.logger.info("Connected to MQTT broker")
        client.subscribe("idotmatrix/display/+/command")
        self.publish_discovery_messages()
        self._publish_all_states()
        self._notify_state_listeners()

    def _on_mqtt_disconnect(self, client: mqtt.Client, userdata: Any, *args: Any, **kwargs: Any) -> None:
        self.mqtt_connected = False
        self.logger.warning("Disconnected from MQTT broker")
        self._notify_state_listeners()

    def _on_mqtt_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            if self.loop is not None:
                payload = msg.payload.decode("utf-8", errors="replace")
                self.loop.call_soon_threadsafe(self.msg_queue.put_nowait, (msg.topic, payload))
        except Exception as exc:
            self.logger.error("Failed to queue MQTT message: %s", exc)

    def _publish_mqtt(self, topic: str, payload: str, retain: bool = False) -> None:
        if self.mqtt_client and self.mqtt_connected:
            try:
                self.mqtt_client.publish(topic, payload, retain=retain)
            except Exception as exc:
                self.logger.debug("MQTT publish failed (%s): %s", topic, exc)

    def _publish_json(self, topic: str, data: Dict[str, Any], retain: bool = True) -> None:
        import json
        self._publish_mqtt(topic, json.dumps(data), retain=retain)

    def _publish_light_state(self) -> None:
        r, g, b = self.active_color
        self._publish_json(
            "idotmatrix/display/light/state",
            {
                "state": "ON" if self.power_on else "OFF",
                "brightness": max(1, min(255, int(round(self.brightness * 255 / 100)))),
                "color": {"r": r, "g": g, "b": b},
            },
            retain=True,
        )

    def _publish_image_state(self, image_name: str) -> None:
        self._publish_mqtt("idotmatrix/display/image/state", image_name, retain=True)
        self._publish_mqtt("idotmatrix/display/image_select/state", image_name, retain=True)

    def _publish_all_states(self) -> None:
        cfg = self.settings.get_all()
        self._publish_light_state()
        self._publish_image_state(self.current_image)
        self._publish_mqtt("idotmatrix/display/mode/state", self.current_mode if self.current_mode in ("Clock", "Chronograph") else "Clock", retain=True)
        self._publish_mqtt("idotmatrix/display/clock_style/state", cfg["clock"].get("style", "RGBSwipeOutline"), retain=True)
        self._publish_mqtt("idotmatrix/display/override/state", "ON" if self.is_override_locked() else "OFF", retain=True)
        self._publish_mqtt("idotmatrix/display/flip/state", "ON" if cfg["device"].get("flip_screen", False) else "OFF", retain=True)
        self._publish_mqtt("idotmatrix/display/ble/state", "ON" if self.is_ble_connected() else "OFF", retain=True)

    def publish_discovery_messages(self) -> None:
        if not (self.mqtt_client and self.mqtt_connected):
            return

        image_options = ["clear"] + [item["name"] for item in self.list_images()]
        clock_styles = [member.name for member in ClockStyle]

        discoveries = [
            (
                "homeassistant/light/idotmatrix_3f_4f/light/config",
                {
                    "schema": "json",
                    "name": "iDotMatrix Display",
                    "state_topic": "idotmatrix/display/light/state",
                    "command_topic": "idotmatrix/display/light/command",
                    "brightness": True,
                    "supported_color_modes": ["rgb"],
                    "optimistic": True,
                    "qos": 0,
                    "unique_id": "idotmatrix_light_module",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/text/idotmatrix_3f_4f/input_text/config",
                {
                    "name": "iDotMatrix Input Text",
                    "command_topic": "idotmatrix/display/text/command",
                    "state_topic": "idotmatrix/display/text/state",
                    "optimistic": True,
                    "unique_id": "idotmatrix_text_input",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/text/idotmatrix_3f_4f/image_loader/config",
                {
                    "name": "iDotMatrix Image Loader",
                    "command_topic": "idotmatrix/display/image/command",
                    "state_topic": "idotmatrix/display/image/state",
                    "optimistic": True,
                    "unique_id": "idotmatrix_image_loader",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/select/idotmatrix_3f_4f/mode_select/config",
                {
                    "name": "iDotMatrix Mode",
                    "command_topic": "idotmatrix/display/mode/command",
                    "state_topic": "idotmatrix/display/mode/state",
                    "options": ["Clock", "Chronograph"],
                    "optimistic": True,
                    "unique_id": "idotmatrix_mode_select",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/select/idotmatrix_3f_4f/effect_select/config",
                {
                    "name": "iDotMatrix Effect",
                    "command_topic": "idotmatrix/display/effect/command",
                    "state_topic": "idotmatrix/display/effect/state",
                    "options": [f"Effect {i}" for i in range(7)],
                    "optimistic": True,
                    "unique_id": "idotmatrix_effect_select",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/number/idotmatrix_3f_4f/countdown_input/config",
                {
                    "name": "iDotMatrix Countdown",
                    "command_topic": "idotmatrix/display/countdown/command",
                    "min": 1,
                    "max": 60,
                    "step": 1,
                    "optimistic": True,
                    "unique_id": "idotmatrix_countdown_input",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/button/idotmatrix_3f_4f/sync_time/config",
                {
                    "name": "iDotMatrix Sync Time",
                    "command_topic": "idotmatrix/display/sync_time/command",
                    "payload_press": "SYNC",
                    "unique_id": "idotmatrix_sync_time_button",
                    "device": DEVICE_INFO,
                },
            ),
            # --- New Enhanced Discovery Entities ---
            (
                "homeassistant/select/idotmatrix_3f_4f/clock_style/config",
                {
                    "name": "iDotMatrix Clock Style",
                    "command_topic": "idotmatrix/display/clock_style/command",
                    "state_topic": "idotmatrix/display/clock_style/state",
                    "options": clock_styles,
                    "unique_id": "idotmatrix_clock_style_select",
                    "icon": "mdi:clock-digital",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/select/idotmatrix_3f_4f/image_select/config",
                {
                    "name": "iDotMatrix Image Select",
                    "command_topic": "idotmatrix/display/image_select/command",
                    "state_topic": "idotmatrix/display/image_select/state",
                    "options": image_options,
                    "unique_id": "idotmatrix_image_select",
                    "icon": "mdi:image-multiple",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/switch/idotmatrix_3f_4f/manual_override/config",
                {
                    "name": "iDotMatrix Manual Lock",
                    "command_topic": "idotmatrix/display/override/command",
                    "state_topic": "idotmatrix/display/override/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "unique_id": "idotmatrix_manual_override_switch",
                    "icon": "mdi:lock",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/switch/idotmatrix_3f_4f/screen_flip/config",
                {
                    "name": "iDotMatrix Flip 180",
                    "command_topic": "idotmatrix/display/flip/command",
                    "state_topic": "idotmatrix/display/flip/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "unique_id": "idotmatrix_screen_flip_switch",
                    "icon": "mdi:rotate-180",
                    "device": DEVICE_INFO,
                },
            ),
            (
                "homeassistant/binary_sensor/idotmatrix_3f_4f/ble_connected/config",
                {
                    "name": "iDotMatrix Bluetooth",
                    "state_topic": "idotmatrix/display/ble/state",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": "connectivity",
                    "unique_id": "idotmatrix_ble_status",
                    "device": DEVICE_INFO,
                },
            ),
        ]

        for topic, payload in discoveries:
            self._publish_json(topic, payload, retain=True)

        self.logger.info("Published %d MQTT Auto-Discovery entities", len(discoveries))

    async def _handle_mqtt_command(self, topic: str, payload: str) -> None:
        import json

        self.logger.info("MQTT command: %s -> %s", topic, payload)
        locked = self.is_override_locked()
        critical_alerts = {str(x).lower() for x in self.settings.get("override", {}).get("allow_critical_alerts", [])}

        try:
            if topic == "idotmatrix/display/override/command":
                want_lock = payload.strip().upper() in {"ON", "TRUE", "1", "LOCK"}
                await self.set_override_lock(want_lock, return_to_ha_auto=not want_lock)
                return

            if topic == "idotmatrix/display/sync_time/command":
                await self.sync_time(source="mqtt")
                return

            if topic == "idotmatrix/display/flip/command":
                await self.set_flip(payload.strip().upper() in {"ON", "TRUE", "1"})
                return

            if topic == "idotmatrix/display/light/command":
                data = json.loads(payload)
                is_on = None
                if "state" in data:
                    is_on = data["state"] == "ON"
                bright = None
                if "brightness" in data:
                    bright = max(5, int(data["brightness"] / 255.0 * 100))
                color = None
                if "color" in data:
                    c = data["color"]
                    color = (int(c["r"]), int(c["g"]), int(c["b"]))
                await self.set_power(is_on=is_on, brightness=bright, color=color)
                return

            if topic == "idotmatrix/display/clock_style/command":
                await self.show_clock(style=payload.strip(), save_default=True)
                return

            if topic in {"idotmatrix/display/image/command", "idotmatrix/display/image_select/command"}:
                safe_name = Path(payload.strip().strip("'\"")).name.lower()
                is_critical = safe_name in critical_alerts
                if locked and not is_critical:
                    self.logger.info("Manual override lock active; ignoring MQTT image command %r", payload)
                    return
                await self.show_image(payload)
                return

            if topic == "idotmatrix/display/mode/command":
                if locked:
                    self.logger.info("Manual override lock active; ignoring MQTT mode command %r", payload)
                    return
                if payload == "Clock":
                    await self.show_clock()
                elif payload == "Chronograph":
                    await self.chronograph_action("start")
                return

            if topic == "idotmatrix/display/text/command":
                if locked:
                    self.logger.info("Manual override lock active; ignoring MQTT text command")
                    return
                if payload:
                    await self.show_text(payload)
                return

            if topic == "idotmatrix/display/effect/command":
                if locked:
                    self.logger.info("Manual override lock active; ignoring MQTT effect command")
                    return
                await self.show_effect(payload)
                return

            if topic == "idotmatrix/display/countdown/command":
                if locked:
                    self.logger.info("Manual override lock active; ignoring MQTT countdown command")
                    return
                minutes = min(59, int(float(payload)))
                await self.start_countdown(minutes=minutes)
                return

        except Exception as exc:
            self.last_error = str(exc)
            self.logger.error("Error handling MQTT command %s: %s", topic, exc)
