"""FastAPI archive web application."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .capture import CaptureService
from .config import load_settings
from .db import JudgmentRow, Database, VerificationRow
from .timeline import build_view
from .timeutil import iso_from_ms, now_ms, parse_iso_ms

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _parse_time(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    try:
        return parse_iso_ms(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid UTC time {value!r}; expected ISO-8601 like 2026-11-20T00:01:00Z",
        )


class VerificationRequest(BaseModel):
    channel_id: str
    phrase: str
    from_at: str
    to_at: str


def _judgment_payload(row: JudgmentRow) -> dict:
    return {
        "id": row.id,
        "conclusion": row.conclusion,
        "first_seen_ms": row.first_seen_ms,
        "first_seen_utc": iso_from_ms(row.first_seen_ms)
        if row.first_seen_ms is not None
        else None,
        "first_seen_seq": row.first_seen_seq,
        "matched_text": row.matched_text,
        "uncovered": [
            {
                "start_ms": start,
                "end_ms": end,
                "start_utc": iso_from_ms(start),
                "end_utc": iso_from_ms(end),
            }
            for start, end in row.uncovered
        ],
        "created_ms": row.created_ms,
        "created_utc": iso_from_ms(row.created_ms),
    }


def _verification_payload(
    row: VerificationRow, judgments: list[JudgmentRow]
) -> dict:
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "phrase": row.phrase,
        "from_ms": row.from_ms,
        "to_ms": row.to_ms,
        "from_utc": iso_from_ms(row.from_ms),
        "to_utc": iso_from_ms(row.to_ms),
        "created_ms": row.created_ms,
        "created_utc": iso_from_ms(row.created_ms),
        "judgments": [_judgment_payload(judgment) for judgment in judgments],
        "current": _judgment_payload(judgments[-1]) if judgments else None,
    }


def create_app(
    *,
    settings=None,
    db: Optional[Database] = None,
    capture: Optional[CaptureService] = None,
    origin_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    settings = settings or load_settings()
    database = db or Database(settings.database_path)
    service = capture or CaptureService(
        database, timeout_s=settings.request_timeout_s
    )
    channels = settings.channels
    # Serialises capture runs across channels so two browser tabs or a
    # double-click cannot interleave playlist observations.
    capture_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Make sure every configured channel exists so the UI can list it
        # before the first capture.
        for channel in channels:
            database.upsert_channel(channel["id"], channel["name"], channel["master_url"], 0)
        yield

    app = FastAPI(title="电视台字幕归档", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.capture = service
    app.state.channels = channels

    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

    def channel_or_404(channel_id: str) -> dict:
        for channel in channels:
            if channel["id"] == channel_id:
                return channel
        raise HTTPException(status_code=404, detail=f"unknown channel {channel_id!r}")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {"channels": channels, "active": channels[0]["id"] if channels else None},
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ------------------------------------------------------------------
    # Archive API
    # ------------------------------------------------------------------
    @app.get("/api/channels")
    async def list_channels():
        result = []
        for channel in channels:
            row = database.get_channel(channel["id"])
            result.append(
                {
                    **channel,
                    "window_start_high": row.window_start_high if row else 0,
                    "window_end_high": row.window_end_high if row else -1,
                }
            )
        return {"channels": result}

    @app.post("/api/channels/{channel_id}/capture")
    async def capture_channel(channel_id: str):
        channel = channel_or_404(channel_id)
        async with capture_lock:
            result = await service.capture(
                channel["id"], channel["master_url"], channel["name"]
            )
        return {"ok": True, "result": result.as_dict()}

    @app.get("/api/channels/{channel_id}/archive")
    async def archive(
        channel_id: str,
        from_: Optional[str] = Query(default=None, alias="from"),
        to: Optional[str] = Query(default=None),
    ):
        channel = channel_or_404(channel_id)
        from_ms = _parse_time(from_)
        to_ms = _parse_time(to)
        explicit_range = from_ is not None or to is not None

        bounds = database.segment_bounds(channel_id)
        # Each open side falls back to the corresponding edge of the archive, so
        # a lone "from" still returns everything up to the newest segment.
        if bounds is not None:
            if from_ms is None:
                from_ms = bounds[0]
            if to_ms is None:
                to_ms = bounds[1]
        else:
            from_ms = from_ms if from_ms is not None else 0
            to_ms = to_ms if to_ms is not None else 0
        if to_ms < from_ms:
            raise HTTPException(status_code=400, detail="'to' must not precede 'from'")
        return build_view(
            database,
            channel_id,
            channel["name"],
            from_ms,
            to_ms,
            explicit_range=explicit_range,
        )

    @app.get("/api/channels/{channel_id}/segments")
    async def segments(channel_id: str):
        channel_or_404(channel_id)
        return {
            "segments": [
                {
                    "seq": segment.seq,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "start_utc": iso_from_ms(segment.start_ms),
                    "end_utc": iso_from_ms(segment.end_ms),
                    "status": segment.status,
                    "captions_empty": segment.captions_empty,
                    "error": segment.last_error,
                }
                for segment in database.segments(channel_id)
            ]
        }

    # ------------------------------------------------------------------
    # Phrase verification
    # ------------------------------------------------------------------
    @app.get("/api/verifications")
    async def list_verifications():
        result = []
        for verification in database.verifications_all():
            channel = next(
                (item for item in channels if item["id"] == verification.channel_id),
                None,
            )
            result.append(
                {
                    **_verification_payload(
                        verification, database.judgments(verification.id)
                    ),
                    "channel_name": channel["name"] if channel else verification.channel_id,
                }
            )
        return {"verifications": result}

    @app.post("/api/verifications", status_code=201)
    async def create_verification(body: VerificationRequest):
        channel = channel_or_404(body.channel_id)
        phrase = body.phrase
        if not phrase:
            raise HTTPException(status_code=400, detail="phrase must not be empty")
        from_ms = _parse_time(body.from_at)
        to_ms = _parse_time(body.to_at)
        if from_ms is None or to_ms is None:
            raise HTTPException(status_code=400, detail="from_at and to_at are required")
        if to_ms <= from_ms:
            raise HTTPException(status_code=400, detail="'to_at' must follow 'from_at'")
        verification, _ = database.create_verification(
            channel["id"], phrase, from_ms, to_ms, now_ms()
        )
        rows = database.judgments(verification.id)
        return {
            **_verification_payload(verification, rows),
            "channel_name": channel["name"],
        }

    @app.get("/api/verifications/{verification_id}")
    async def get_verification(verification_id: int):
        verification = database.get_verification(verification_id)
        if verification is None:
            raise HTTPException(status_code=404, detail="unknown verification")
        channel = next(
            (item for item in channels if item["id"] == verification.channel_id), None
        )
        return {
            **_verification_payload(
                verification, database.judgments(verification.id)
            ),
            "channel_name": channel["name"] if channel else verification.channel_id,
        }

    # ------------------------------------------------------------------
    # Demo origin control proxy
    # ------------------------------------------------------------------
    @app.api_route("/api/demo/{path:path}", methods=["GET", "POST"])
    async def demo_proxy(path: str, request: Request):
        url = f"{settings.origin_base_url}/__control/{path}"
        body = None
        if request.method == "POST":
            raw = await request.body()
            body = raw or b"{}"
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_s, transport=origin_transport
            ) as client:
                response = await client.request(
                    request.method,
                    url,
                    content=body,
                    headers={"content-type": "application/json"} if body else None,
                )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": f"demo origin unreachable at {url}: {exc}"},
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        return JSONResponse(status_code=response.status_code, content=payload)

    return app


app = create_app()
