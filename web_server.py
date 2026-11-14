"""FastAPI Local Web Dashboard & REST API for iDotMatrix Control Center."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from controller import IDotMatrixController
from idotmatrix.modules.clock import ClockStyle
from idotmatrix.modules.effect import EffectStyle
from idotmatrix.modules.text import TextColorMode, TextMode
from idotmatrix.util.image_utils import ResizeMode
from settings_manager import BASE_DIR

STATIC_DIR = BASE_DIR / "static"


class PowerRequest(BaseModel):
    state: Optional[bool] = None
    brightness: Optional[int] = Field(default=None, ge=5, le=100)
    color: Optional[Union[str, int, List[int], Dict[str, int]]] = None


class ClockRequest(BaseModel):
    style: Optional[Union[str, int]] = None
    color: Optional[Union[str, int, List[int], Dict[str, int]]] = None
    show_date: Optional[bool] = None
    hour24: Optional[bool] = None
    save_default: bool = False
    lock_minutes: Optional[int] = None


class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)
    mode: Optional[Union[str, int]] = None
    speed: Optional[int] = Field(default=None, ge=1, le=100)
    font_size: Optional[int] = Field(default=None, ge=6, le=64)
    color_mode: Optional[Union[str, int]] = None
    text_color: Optional[Union[str, int, List[int], Dict[str, int]]] = None
    bg_color: Optional[Union[str, int, List[int], Dict[str, int]]] = None
    save_default: bool = False
    lock_minutes: Optional[int] = None


class ImageRequest(BaseModel):
    name: str
    resize_mode: Optional[str] = None
    palletize: Optional[bool] = None
    lock_minutes: Optional[int] = None


class EffectRequest(BaseModel):
    style: Union[str, int] = 0
    colors: Optional[List[Union[str, int, List[int]]]] = None
    lock_minutes: Optional[int] = None


class CountdownRequest(BaseModel):
    minutes: int = Field(default=5, ge=0, le=59)
    seconds: int = Field(default=0, ge=0, le=59)


class ChronographRequest(BaseModel):
    action: str = Field(default="start")


class ScoreboardRequest(BaseModel):
    count1: int = Field(default=0, ge=0, le=999)
    count2: int = Field(default=0, ge=0, le=999)


class EcoRequest(BaseModel):
    enabled: bool = True
    start_hour: int = Field(default=22, ge=0, le=23)
    start_minute: int = Field(default=0, ge=0, le=59)
    end_hour: int = Field(default=6, ge=0, le=23)
    end_minute: int = Field(default=0, ge=0, le=59)
    brightness: int = Field(default=10, ge=0, le=100)
    save_default: bool = True


class FlipRequest(BaseModel):
    flip: bool


class OverrideRequest(BaseModel):
    locked: bool
    minutes: Optional[int] = None
    return_to_ha_auto: bool = False


def create_app(controller: IDotMatrixController) -> FastAPI:
    """Builds the FastAPI application wired to `IDotMatrixController`."""
    app = FastAPI(
        title="iDotMatrix Control Center",
        description="Local Web UI & AI-Ready REST API for iDotMatrix LED Display and Home Assistant MQTT Bridge",
        version="2.0.0",
    )

    image_dir = controller.settings.resolve_image_dir()
    app.mount("/images", StaticFiles(directory=str(image_dir)), name="images")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="Web UI index.html not found")
        return FileResponse(index_file)

    @app.get("/api/status")
    async def get_status() -> Dict[str, Any]:
        status = controller.get_status()
        status["options"] = {
            "clock_styles": [{"value": m.value, "name": m.name} for m in ClockStyle],
            "text_modes": [{"value": m.value, "name": m.name} for m in TextMode],
            "text_color_modes": [{"value": m.value, "name": m.name} for m in TextColorMode],
            "effect_styles": [{"value": m.value, "name": m.name} for m in EffectStyle],
            "resize_modes": [m.name for m in ResizeMode],
        }
        return status

    @app.post("/api/sync-time")
    async def sync_time() -> Dict[str, Any]:
        try:
            return await controller.sync_time(source="web")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/power")
    async def set_power(req: PowerRequest) -> Dict[str, Any]:
        try:
            return await controller.set_power(is_on=req.state, brightness=req.brightness, color=req.color)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/clock")
    async def show_clock(req: ClockRequest) -> Dict[str, Any]:
        try:
            return await controller.show_clock(
                style=req.style,
                color=req.color,
                show_date=req.show_date,
                hour24=req.hour24,
                save_default=req.save_default,
                lock_minutes=req.lock_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/text")
    async def show_text(req: TextRequest) -> Dict[str, Any]:
        try:
            return await controller.show_text(
                text=req.text,
                mode=req.mode,
                speed=req.speed,
                font_size=req.font_size,
                color_mode=req.color_mode,
                text_color=req.text_color,
                bg_color=req.bg_color,
                save_default=req.save_default,
                lock_minutes=req.lock_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/image")
    async def show_image(req: ImageRequest) -> Dict[str, Any]:
        try:
            return await controller.show_image(
                filename=req.name,
                resize_mode=req.resize_mode,
                palletize=req.palletize,
                lock_minutes=req.lock_minutes,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/images")
    async def list_images() -> Dict[str, Any]:
        return {"images": controller.list_images()}

    @app.post("/api/images/upload")
    async def upload_image(file: UploadFile = File(...)) -> Dict[str, Any]:
        try:
            content = await file.read()
            if not content:
                raise ValueError("Uploaded file is empty")
            info = controller.save_uploaded_image(file.filename or "", content)
            return {"status": "ok", "image": info}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.delete("/api/images/{filename}")
    async def delete_image(filename: str) -> Dict[str, Any]:
        try:
            controller.delete_image(filename)
            return {"status": "deleted", "name": Path(filename).name}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/effect")
    async def show_effect(req: EffectRequest) -> Dict[str, Any]:
        try:
            return await controller.show_effect(
                style=req.style,
                colors=req.colors,
                lock_minutes=req.lock_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/countdown")
    async def start_countdown(req: CountdownRequest) -> Dict[str, Any]:
        try:
            return await controller.start_countdown(minutes=req.minutes, seconds=req.seconds)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/chronograph")
    async def chronograph(req: ChronographRequest) -> Dict[str, Any]:
        try:
            return await controller.chronograph_action(req.action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/scoreboard")
    async def scoreboard(req: ScoreboardRequest) -> Dict[str, Any]:
        try:
            return await controller.show_scoreboard(req.count1, req.count2)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/eco")
    async def set_eco(req: EcoRequest) -> Dict[str, Any]:
        try:
            return await controller.apply_eco(
                enabled=req.enabled,
                start_hour=req.start_hour,
                start_minute=req.start_minute,
                end_hour=req.end_hour,
                end_minute=req.end_minute,
                brightness=req.brightness,
                save_default=req.save_default,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/flip")
    async def set_flip(req: FlipRequest) -> Dict[str, Any]:
        try:
            return await controller.set_flip(req.flip)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/reset")
    async def reset_device() -> Dict[str, Any]:
        try:
            return await controller.reset_device()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/reconnect")
    async def reconnect_ble() -> Dict[str, Any]:
        try:
            return await controller.reconnect_ble()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/override")
    async def set_override(req: OverrideRequest) -> Dict[str, Any]:
        try:
            return await controller.set_override_lock(
                locked=req.locked,
                minutes=req.minutes,
                return_to_ha_auto=req.return_to_ha_auto,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/settings")
    async def get_settings() -> Dict[str, Any]:
        return controller.settings.get_public_settings()

    @app.put("/api/settings")
    async def update_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
        try:
            controller.settings.update(patch)
            return controller.settings.get_public_settings()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/logs")
    async def get_logs(limit: int = Query(default=200, ge=1, le=300)) -> JSONResponse:
        return JSONResponse({"logs": controller.get_logs(limit=limit)})

    return app
