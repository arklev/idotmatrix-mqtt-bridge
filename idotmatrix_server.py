"""Main entrypoint for iDotMatrix Control Center (Headless Tray + Web UI + MQTT Bridge)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
import urllib.request
from pathlib import Path

import uvicorn

from controller import IDotMatrixController, configure_logging
from settings_manager import BASE_DIR, SettingsManager
from tray_icon import TrayManager
from web_server import create_app


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _handover_existing_instance(port: int, logger: logging.Logger) -> None:
    """Gracefully asks an already-running instance on `port` to shut down and waits for release."""
    if not _is_port_open(port):
        return

    logger.info("Detected existing instance on port %d; requesting graceful handover...", port)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2.0):
            pass
    except Exception as exc:
        logger.warning("Handover shutdown request failed: %s", exc)
        return

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if not _is_port_open(port):
            time.sleep(0.5)
            logger.info("Previous instance released port %d.", port)
            return
        time.sleep(0.2)
    logger.warning("Previous instance did not release port %d within 6s.", port)


async def run_server(use_tray: bool = True) -> None:
    os.chdir(BASE_DIR)
    log_handler = configure_logging()
    logger = logging.getLogger("idotmatrix_server")

    settings = SettingsManager(BASE_DIR / "config.json")
    cfg = settings.get_all()
    web_host = str(cfg.get("web_host", "0.0.0.0"))
    web_port = int(cfg.get("web_port", 8099))

    _handover_existing_instance(web_port, logger)

    controller = IDotMatrixController(settings=settings, log_handler=log_handler)
    app = create_app(controller)

    uv_config = uvicorn.Config(
        app=app,
        host=web_host,
        port=web_port,
        log_config=None,
        access_log=False,
    )
    uv_server = uvicorn.Server(uv_config)
    uv_server.install_signal_handlers = lambda: None  # Managed by asyncio/tray

    def _request_shutdown() -> None:
        logger.info("Shutdown requested...")
        uv_server.should_exit = True

    controller.set_shutdown_callback(_request_shutdown)

    tray: TrayManager | None = None
    if use_tray:
        tray = TrayManager(controller)

    await controller.start()
    if tray is not None:
        tray.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_request_shutdown))

    logger.info("Web Control Panel running at http://127.0.0.1:%d", web_port)
    try:
        await uv_server.serve()
    finally:
        if tray is not None:
            tray.stop()
        await controller.stop()
        logger.info("iDotMatrix Control Center stopped cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="iDotMatrix Control Center")
    parser.add_argument("--no-tray", action="store_true", help="Run without Windows system tray icon")
    args = parser.parse_args()

    try:
        asyncio.run(run_server(use_tray=not args.no_tray))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
