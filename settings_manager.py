"""Thread-safe, hot-reloadable configuration manager for iDotMatrix Control Center."""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.json"
MASKED_SECRET = "********"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "device_mac": "00:00:00:00:00:00",
    "mqtt_server": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "image_dir": "images",
    "web_host": "0.0.0.0",
    "web_port": 8099,
    "device": {
        "screen_size": 32,
        "flip_screen": False,
        "auto_sync_interval_minutes": 60,
        "sync_on_connect": True,
        "brightness": 80,
    },
    "clock": {
        "style": "RGBSwipeOutline",
        "color": [255, 255, 255],
        "show_date": True,
        "hour24": True,
    },
    "text": {
        "mode": "MARQUEE",
        "speed": 95,
        "font_size": 16,
        "color_mode": "WHITE",
        "text_color": [255, 255, 255],
        "bg_color": None,
    },
    "eco": {
        "enabled": False,
        "start_hour": 22,
        "start_minute": 0,
        "end_hour": 6,
        "end_minute": 0,
        "brightness": 10,
    },
    "image": {
        "resize_mode": "FILL",
        "palletize": True,
    },
    "override": {
        "locked": False,
        "lock_until": None,
        "allow_critical_alerts": ["bomb.gif"],
    },
}


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merges `updates` into a copy of `base`."""
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class SettingsManager:
    """Manages loading, validating, persisting, and hot-reloading `config.json`."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.logger = logging.getLogger(__name__)
        self.config_path: Path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._last_mtime_ns: Optional[int] = None
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> Dict[str, Any]:
        """Loads settings from disk, migrating missing schema keys with defaults."""
        with self._lock:
            if not self.config_path.exists():
                if EXAMPLE_CONFIG_PATH.exists():
                    shutil.copy(EXAMPLE_CONFIG_PATH, self.config_path)
                else:
                    self._write_atomic(DEFAULT_SETTINGS)

            raw: Dict[str, Any] = {}
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        raw = loaded
            except Exception as exc:
                self.logger.error("Failed to read %s: %s", self.config_path, exc)
                raise

            merged = _deep_merge(DEFAULT_SETTINGS, raw)
            # Persist back if new default keys were added during migration
            if merged != raw:
                self._data = merged
                self._write_atomic(self._data)
            else:
                self._data = merged

            self._update_mtime()
            return copy.deepcopy(self._data)

    def reload_if_changed(self) -> bool:
        """Reloads `config.json` from disk if modified externally. Returns True if reloaded."""
        with self._lock:
            try:
                current_mtime = self.config_path.stat().st_mtime_ns
            except FileNotFoundError:
                return False

            if self._last_mtime_ns is not None and current_mtime == self._last_mtime_ns:
                return False

            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = _deep_merge(DEFAULT_SETTINGS, loaded)
                    self._last_mtime_ns = current_mtime
                    self.logger.info("Hot-reloaded settings from %s", self.config_path)
                    return True
            except Exception as exc:
                self.logger.warning("Failed hot-reloading %s: %s", self.config_path, exc)
            return False

    def get_all(self) -> Dict[str, Any]:
        """Returns a deep copy of all configuration values (including secrets)."""
        with self._lock:
            self.reload_if_changed()
            return copy.deepcopy(self._data)

    def get_public_settings(self) -> Dict[str, Any]:
        """Returns settings safe for REST API / Web UI responses (masks MQTT password)."""
        data = self.get_all()
        if data.get("mqtt_pass"):
            data["mqtt_pass"] = MASKED_SECRET
        return data

    def get(self, key: str, default: Any = None) -> Any:
        """Gets a top-level key or section."""
        with self._lock:
            self.reload_if_changed()
            return copy.deepcopy(self._data.get(key, default))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merges `patch` into settings, validates values, and saves atomically."""
        if not isinstance(patch, dict):
            raise ValueError("Settings patch must be a JSON object")

        with self._lock:
            self.reload_if_changed()
            sanitized_patch = copy.deepcopy(patch)
            if sanitized_patch.get("mqtt_pass") == MASKED_SECRET:
                sanitized_patch.pop("mqtt_pass", None)

            candidate = _deep_merge(self._data, sanitized_patch)
            self._validate_candidate(candidate)
            self._data = candidate
            self._write_atomic(self._data)
            return copy.deepcopy(self._data)

    def resolve_image_dir(self) -> Path:
        """Resolves the image directory path relative to the project root."""
        raw_dir = Path(self.get("image_dir", "images"))
        if not raw_dir.is_absolute():
            raw_dir = BASE_DIR / raw_dir
        raw_dir.mkdir(parents=True, exist_ok=True)
        return raw_dir

    def is_startup_configured(self) -> bool:
        """Checks whether mandatory MAC address and MQTT broker settings are filled in."""
        data = self.get_all()
        mqtt_server = str(data.get("mqtt_server", "")).strip()
        mac = str(data.get("device_mac", "")).strip()
        return bool(mqtt_server and mac and mac != "00:00:00:00:00:00")

    def _validate_candidate(self, data: Dict[str, Any]) -> None:
        port = int(data.get("web_port", 8099))
        if not (1 <= port <= 65535):
            raise ValueError("web_port must be between 1 and 65535")
        data["web_port"] = port

        mqtt_port = int(data.get("mqtt_port", 1883))
        if not (1 <= mqtt_port <= 65535):
            raise ValueError("mqtt_port must be between 1 and 65535")
        data["mqtt_port"] = mqtt_port

        device = data["device"]
        screen_size = int(device.get("screen_size", 32))
        if screen_size not in (16, 32, 64):
            raise ValueError("device.screen_size must be 16, 32, or 64")
        device["screen_size"] = screen_size

        device["auto_sync_interval_minutes"] = max(0, int(device.get("auto_sync_interval_minutes", 60)))
        device["brightness"] = max(5, min(100, int(device.get("brightness", 80))))

        text_cfg = data["text"]
        text_cfg["speed"] = max(1, min(100, int(text_cfg.get("speed", 95))))
        text_cfg["font_size"] = max(6, min(64, int(text_cfg.get("font_size", 16))))

        eco = data["eco"]
        eco["start_hour"] = max(0, min(23, int(eco.get("start_hour", 22))))
        eco["start_minute"] = max(0, min(59, int(eco.get("start_minute", 0))))
        eco["end_hour"] = max(0, min(23, int(eco.get("end_hour", 6))))
        eco["end_minute"] = max(0, min(59, int(eco.get("end_minute", 0))))
        eco["brightness"] = max(0, min(100, int(eco.get("brightness", 10))))

    def _write_atomic(self, payload: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        os.replace(tmp_path, self.config_path)
        self._update_mtime()

    def _update_mtime(self) -> None:
        try:
            self._last_mtime_ns = self.config_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._last_mtime_ns = None
