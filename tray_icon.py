"""Windows System Tray integration for iDotMatrix Control Center using pystray."""

from __future__ import annotations

import asyncio
import logging
import threading
import webbrowser
from typing import Any, Coroutine, List, Optional

import pystray
from PIL import Image, ImageDraw

from controller import IDotMatrixController
from idotmatrix.modules.clock import ClockStyle


def render_tray_icon(ble_connected: bool, locked: bool, power_on: bool) -> Image.Image:
    """Renders a crisp 64x64 pixel-matrix icon reflecting live device state."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer rounded frame
    draw.rounded_rectangle([2, 2, 61, 61], radius=12, fill=(20, 24, 33, 255), outline=(48, 54, 61, 255), width=2)

    if not power_on:
        dot_color = (110, 118, 129, 255)
        alt_color = (72, 79, 88, 255)
    elif ble_connected:
        dot_color = (63, 185, 80, 255)    # Green
        alt_color = (88, 166, 255, 255)   # Blue accent
    else:
        dot_color = (210, 153, 34, 255)   # Orange / reconnecting
        alt_color = (248, 81, 73, 255)    # Red accent

    # 4x4 LED matrix grid
    margin = 13
    cell = 10
    gap = 2
    for row in range(4):
        for col in range(4):
            x0 = margin + col * (cell + gap)
            y0 = margin + row * (cell + gap)
            fill = alt_color if (row + col) % 3 == 0 else dot_color
            draw.rounded_rectangle([x0, y0, x0 + cell - 2, y0 + cell - 2], radius=2, fill=fill)

    # Blue lock badge in top-right corner when Manual Override Lock is active
    if locked:
        draw.ellipse([44, 4, 60, 20], fill=(88, 166, 255, 255), outline=(255, 255, 255, 255), width=2)

    return img


class TrayManager:
    """Manages the pystray icon in a dedicated background thread."""

    def __init__(self, controller: IDotMatrixController) -> None:
        self.logger = logging.getLogger(__name__)
        self.controller = controller
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._last_visual_state: Optional[tuple] = None

    def start(self) -> None:
        """Starts the tray icon thread and subscribes to controller state updates."""
        self.controller.add_state_listener(self.refresh)
        initial_img = render_tray_icon(
            ble_connected=self.controller.is_ble_connected(),
            locked=self.controller.is_override_locked(),
            power_on=self.controller.power_on,
        )
        self._icon = pystray.Icon(
            name="iDotMatrixControlCenter",
            icon=initial_img,
            title="iDotMatrix Control Center",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._run, name="pystray-thread", daemon=True)
        self._thread.start()
        self.logger.info("System tray icon started")

    def _run(self) -> None:
        try:
            if self._icon is not None:
                self._icon.run()
        except Exception as exc:
            self.logger.error("System tray loop crashed: %s", exc)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def refresh(self) -> None:
        if self._icon is None:
            return
        ble = self.controller.is_ble_connected()
        locked = self.controller.is_override_locked()
        power = self.controller.power_on
        mode = self.controller.current_mode
        visual_key = (ble, locked, power)
        if visual_key != self._last_visual_state:
            self._last_visual_state = visual_key
            try:
                self._icon.icon = render_tray_icon(ble, locked, power)
            except Exception:
                pass
        lock_label = "Locked" if locked else "HA Auto"
        ble_label = "Connected" if ble else "Disconnected"
        self._icon.title = f"iDotMatrix | BLE: {ble_label} | {mode} | {lock_label}"[:127]

    def _dispatch(self, coro_factory: Any) -> None:
        loop = self.controller.loop
        if loop is None or loop.is_closed():
            return

        async def _runner() -> None:
            try:
                await coro_factory()
            except Exception as exc:
                self.logger.error("Tray action failed: %s", exc)

        asyncio.run_coroutine_threadsafe(_runner(), loop)

    def _open_web_ui(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        port = int(self.controller.settings.get("web_port", 8099))
        webbrowser.open(f"http://127.0.0.1:{port}")

    def _exit_app(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.logger.info("Exit clicked in system tray")
        loop = self.controller.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.controller.request_shutdown)
        self.stop()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open Control Panel (Web UI)", self._open_web_ui, default=True),
            pystray.MenuItem("Sync Time Now", lambda i, it: self._dispatch(lambda: self.controller.sync_time(source="tray"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Power",
                pystray.Menu(
                    pystray.MenuItem("Turn On", lambda i, it: self._dispatch(lambda: self.controller.set_power(is_on=True))),
                    pystray.MenuItem("Turn Off", lambda i, it: self._dispatch(lambda: self.controller.set_power(is_on=False))),
                ),
            ),
            pystray.MenuItem(
                "Mode",
                pystray.Menu(
                    pystray.MenuItem("Clock", lambda i, it: self._dispatch(lambda: self.controller.show_clock())),
                    pystray.MenuItem("Clear Image (Clock)", lambda i, it: self._dispatch(lambda: self.controller.show_image("clear"))),
                    pystray.MenuItem("Start Chronograph", lambda i, it: self._dispatch(lambda: self.controller.chronograph_action("start"))),
                    pystray.MenuItem("Reset Chronograph", lambda i, it: self._dispatch(lambda: self.controller.chronograph_action("reset"))),
                ),
            ),
            pystray.MenuItem("Clock Style", pystray.Menu(self._clock_style_items)),
            pystray.MenuItem("Display Image / GIF", pystray.Menu(self._image_items)),
            pystray.MenuItem(
                "Manual Override Lock",
                pystray.Menu(
                    pystray.MenuItem(
                        "Unlock (HA Auto)",
                        lambda i, it: self._dispatch(lambda: self.controller.set_override_lock(False, return_to_ha_auto=True)),
                        checked=lambda it: not self.controller.is_override_locked(),
                    ),
                    pystray.MenuItem(
                        "Lock 15 min",
                        lambda i, it: self._dispatch(lambda: self.controller.set_override_lock(True, minutes=15)),
                    ),
                    pystray.MenuItem(
                        "Lock 1 hour",
                        lambda i, it: self._dispatch(lambda: self.controller.set_override_lock(True, minutes=60)),
                    ),
                    pystray.MenuItem(
                        "Lock Until Cleared",
                        lambda i, it: self._dispatch(lambda: self.controller.set_override_lock(True, minutes=None)),
                        checked=lambda it: self.controller.is_override_locked() and not self.controller.settings.get("override", {}).get("lock_until"),
                    ),
                ),
            ),
            pystray.MenuItem(
                "Device",
                pystray.Menu(
                    pystray.MenuItem(
                        "Flip 180°",
                        lambda i, it: self._dispatch(lambda: self.controller.set_flip(not bool(self.controller.settings.get("device", {}).get("flip_screen", False)))),
                        checked=lambda it: bool(self.controller.settings.get("device", {}).get("flip_screen", False)),
                    ),
                    pystray.MenuItem("Reconnect BLE", lambda i, it: self._dispatch(self.controller.reconnect_ble)),
                    pystray.MenuItem("Hardware Reset", lambda i, it: self._dispatch(self.controller.reset_device)),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._exit_app),
        )

    def _clock_style_items(self) -> List[pystray.MenuItem]:
        current_style = self.controller.settings.get("clock", {}).get("style", "RGBSwipeOutline")

        def make_cb(style_name: str) -> Any:
            return lambda i, it: self._dispatch(lambda: self.controller.show_clock(style=style_name, save_default=True))

        return [
            pystray.MenuItem(
                f"{member.value}: {member.name}",
                make_cb(member.name),
                checked=lambda it, name=member.name: current_style == name,
            )
            for member in ClockStyle
        ]

    def _image_items(self) -> List[pystray.MenuItem]:
        items: List[pystray.MenuItem] = [
            pystray.MenuItem(
                "Clear (Clock)",
                lambda i, it: self._dispatch(lambda: self.controller.show_image("clear")),
                checked=lambda it: self.controller.current_image == "clear",
            )
        ]

        def make_cb(fname: str) -> Any:
            return lambda i, it: self._dispatch(lambda: self.controller.show_image(fname))

        for entry in self.controller.list_images():
            name = entry["name"]
            items.append(
                pystray.MenuItem(
                    name,
                    make_cb(name),
                    checked=lambda it, n=name: self.controller.current_image == n,
                )
            )
        return items
