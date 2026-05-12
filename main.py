"""DJ's Art Engine — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from pipeline import (
    cleanup,
    config,
    intake,
    package,
    quality_check,
    separate,
    upscale,
    vectorize,
)
from pipeline.export import render_separation_pdf
from pipeline.intake import IntakeError
from pipeline.separate import NoColorsDetected
from pipeline.vectorize import PHOTOPEA_FALLBACK_URL, VectorizerError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("djs_art_engine")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

ACCEPTED_CONTENT_TYPES = intake.SUPPORTED_CONTENT_TYPES
ACCEPTED_EXTENSIONS = intake.SUPPORTED_EXTENSIONS
_ACCEPTED_UPLOAD_MESSAGE = "Please upload a JPEG, PNG, HEIC, WebP, BMP, or PDF"

# Paths exempt from basic-auth (Render health checks must succeed unauthenticated).
_AUTH_EXEMPT_PATHS = ("/health",)
_BASIC_REALM = 'Basic realm="DJ Art Engine"'
_SUPPORT_PHONE = "+1 (603) 555-0000"
_ORPHAN_AGE_SECONDS = 24 * 3600
_PDF_THREAD_POOL_SIZE = 4

# Rough per-call cost constants for the per-job cost-estimate log line.
# Real numbers drift with provider pricing changes — these are good-enough
# rounded values for an "is this job expensive?" alert, not billing.
_COST_REPLICATE_UPSCALE = 0.005
_COST_VECTORIZER_CREDIT = 0.20
_COST_QC_HAIKU = 0.005
_COST_QC_SONNET = 0.020
_COST_QC_OPUS = 0.030

# Bounding-box crop margin so we don't shave off the edge of the actual logo.
_BBOX_MARGIN_FRACTION = 0.05


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind basic auth when DJ_BASIC_USER/PASS are configured.

    If credentials are unset (empty strings), auth is bypassed — keeps local
    dev frictionless. /health is always public so Render's health probe works.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)

        user, password = config.get_basic_auth()
        if not user or not password:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.lower().startswith("basic "):
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": _BASIC_REALM},
            )
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            client_user, _, client_pass = decoded.partition(":")
        except Exception:
            return Response(
                status_code=401,
                content="Invalid Authorization header",
                headers={"WWW-Authenticate": _BASIC_REALM},
            )
        if not (
            secrets.compare_digest(client_user, user)
            and secrets.compare_digest(client_pass, password)
        ):
            return Response(
                status_code=401,
                content="Invalid credentials",
                headers={"WWW-Authenticate": _BASIC_REALM},
            )
        return await call_next(request)


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="DJ's Art Engine", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(BasicAuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _purge_orphan_jobs() -> None:
    """Delete any job folders in TEMP_DIR older than 24h.

    Render's filesystem is ephemeral so this is a no-op there, but on long-
    running local dev a crashed mid-job leaks a per-job folder. Cheap pass
    on every startup keeps disk usage bounded.
    """
    try:
        temp_root = config.get_temp_dir()
    except Exception as exc:
        log.warning("Skipping orphan cleanup — temp dir unavailable: %s", exc)
        return

    now = time.time()
    purged = 0
    for entry in temp_root.iterdir() if temp_root.exists() else []:
        if not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > _ORPHAN_AGE_SECONDS:
            shutil.rmtree(entry, ignore_errors=True)
            purged += 1
    if purged:
        log.info("Startup orphan cleanup: removed %s stale job dirs from %s.", purged, temp_root)


def _estimate_qc_cost(model_used: str) -> float:
    if not model_used:
        return 0.0
    lower = model_used.lower()
    if "opus" in lower:
        return _COST_QC_OPUS
    if "sonnet" in lower:
        return _COST_QC_SONNET
    return _COST_QC_HAIKU


def _crop_with_margin(image, bbox: tuple[int, int, int, int]):
    """Crop with a margin so we don't slice through the edge of the logo."""
    x1, y1, x2, y2 = bbox
    w, h = image.size
    bbox_w = x2 - x1
    bbox_h = y2 - y1
    mx = int(bbox_w * _BBOX_MARGIN_FRACTION)
    my = int(bbox_h * _BBOX_MARGIN_FRACTION)
    return image.crop((
        max(0, x1 - mx),
        max(0, y1 - my),
        min(w, x2 + mx),
        min(h, y2 + my),
    ))


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(INDEX_FILE), media_type="text/html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process")
@limiter.limit("10/minute")
async def process(
    request: Request,
    file: UploadFile = File(...),
    job_name: str | None = Form(default=None),
):
    # --- Validate content type ------------------------------------------------
    content_type = (file.content_type or "").lower()
    if not intake.is_supported(content_type, file.filename):
        raise HTTPException(status_code=400, detail=_ACCEPTED_UPLOAD_MESSAGE)

    # --- Validate size up-front via Content-Length BEFORE reading the body ---
    # Prevents a malicious 500 MB upload from being fully buffered into RAM
    # before we reject it. Content-Length covers the whole multipart body
    # (form fields + file) but is a tight enough bound for our 20 MB cap.
    max_mb = config.get_max_file_mb()
    max_bytes = max_mb * 1024 * 1024
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            announced_size = int(content_length_header)
        except ValueError:
            announced_size = 0
        if announced_size > max_bytes + (1 * 1024 * 1024):  # 1 MB slack for form overhead
            raise HTTPException(status_code=400, detail=f"File must be under {max_mb}MB")

    raw_bytes = await file.read()

    # Belt-and-suspenders: re-check after read in case Content-Length lied.
    if len(raw_bytes) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File must be under {max_mb}MB")
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # --- Per-job temp directory ----------------------------------------------
    job_id = uuid.uuid4().hex
    temp_root = config.get_temp_dir()
    job_dir = temp_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log.info("Job %s started — file=%s size=%sB job_name=%r",
             job_id, file.filename, len(raw_bytes), job_name)

    try:
        # --- Intake: decode any supported format -> in-memory PIL.Image ------
        try:
            intake_result = intake.decode(
                raw_bytes,
                filename=file.filename,
                content_type=content_type,
            )
        except IntakeError as exc:
            log.warning("Job %s intake failure: %s", job_id, exc)
            package.cleanup(job_dir)
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

        working_image = intake_result.image

        # --- Upscale + QC in parallel (both fail open) ----------------------
        # Both stages run against the post-intake image, so any bounding box
        # Claude returns is in working_image coordinates. If upscale succeeds
        # afterward, we rescale the bbox before cropping.
        upscale_result, qc_verdict = await asyncio.gather(
            upscale.maybe_upscale(working_image),
            quality_check.assess(working_image),
        )
        upscaled_image, was_upscaled = upscale_result

        log.info(
            "Job %s intake size=%s low_res=%s upscaled=%s qc_model=%s qc_printable=%s",
            job_id,
            intake_result.original_size,
            intake_result.low_resolution,
            was_upscaled,
            qc_verdict.model_used,
            qc_verdict.printable,
        )

        # QC short-circuit BEFORE the Vectorizer.ai credit is spent. The 422
        # body carries both dj_message and customer_ask so the frontend can
        # render the actionable rejection screen.
        if not qc_verdict.printable:
            log.info(
                "Job %s rejected by QC: category=%s reason=%r",
                job_id, qc_verdict.verdict_category, qc_verdict.dj_message,
            )
            package.cleanup(job_dir)
            return JSONResponse(
                status_code=422,
                content={
                    "error": qc_verdict.dj_message,
                    "verdict_category": qc_verdict.verdict_category,
                    "dj_message": qc_verdict.dj_message,
                    "customer_ask": qc_verdict.customer_ask,
                    "photopea_url": PHOTOPEA_FALLBACK_URL,
                },
            )

        if was_upscaled:
            working_image = upscaled_image

        # Photo-of-object: crop to bbox so cleanup + vectorize only see the
        # logo region. Bbox arrives in pre-upscale coords; rescale if needed.
        if qc_verdict.is_photo_of_object and qc_verdict.logo_bbox:
            bbox = qc_verdict.logo_bbox
            if was_upscaled:
                ow, oh = intake_result.image.size
                nw, nh = working_image.size
                if ow > 0 and oh > 0:
                    sx, sy = nw / ow, nh / oh
                    bbox = (
                        int(bbox[0] * sx),
                        int(bbox[1] * sy),
                        int(bbox[2] * sx),
                        int(bbox[3] * sy),
                    )
            working_image = _crop_with_margin(working_image, bbox)
            log.info("Job %s cropped to logo bbox; new size=%s", job_id, working_image.size)

        # --- Pre-vectorization cleanup (PIL.Image -> PIL.Image, no JPEG hop)--
        cleaned_image = cleanup.clean_image(working_image)
        # Persist the cleaned image as PNG for debugging — lossless and matches
        # exactly what we send to Vectorizer.ai (after the single JPEG encode).
        cleaned_image.save(job_dir / "cleaned.png", format="PNG", optimize=True)

        # --- Encode JPEG ONCE, immediately before the API call ---------------
        jpeg_bytes = intake.encode_jpeg(cleaned_image)
        (job_dir / "vectorize_input.jpg").write_bytes(jpeg_bytes)

        # --- Vectorize --------------------------------------------------------
        try:
            svg_bytes = await vectorize.vectorize(
                jpeg_bytes,
                filename="vectorize_input.jpg",
                content_type="image/jpeg",
            )
        except VectorizerError as exc:
            log.error("Job %s vectorizer failure: %s", job_id, exc)
            package.cleanup(job_dir)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Vectorization failed — open in Photopea to process manually.",
                    "detail": exc.message,
                    "photopea_url": exc.photopea_url,
                },
            )

        (job_dir / "vectorized.svg").write_bytes(svg_bytes)

        # --- Separate colors --------------------------------------------------
        try:
            separation = separate.extract_colors(svg_bytes)
        except NoColorsDetected as exc:
            log.warning("Job %s no colors: %s", job_id, exc)
            package.cleanup(job_dir)
            return JSONResponse(
                status_code=422,
                content={"error": "No colors detected — try a higher resolution image."},
            )

        layers = separation.layers
        log.info(
            "Job %s colors=%s only_one=%s complex=%s gradients=%s stroke_only=%s multipage=%s",
            job_id, len(layers),
            separation.only_one_color, separation.complex_design,
            separation.gradients_detected, separation.stroke_only_fallback,
            intake_result.multipage_pdf,
        )

        # --- Render per-color PDFs in parallel -------------------------------
        dpi = config.get_output_dpi()

        def _render_one(layer: separate.ColorLayer) -> tuple[separate.ColorLayer, Path]:
            pdf_path = job_dir / package.pdf_filename(layer)
            render_separation_pdf(
                svg_bytes=svg_bytes,
                target_hex=layer.hex,
                output_path=pdf_path,
                dpi=dpi,
                bleed_inches=0.125,
            )
            return layer, pdf_path

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=_PDF_THREAD_POOL_SIZE) as pool:
            color_pdfs = list(
                await asyncio.gather(
                    *(loop.run_in_executor(pool, _render_one, layer) for layer in layers)
                )
            )
        # Preserve coverage-sorted order from `layers`.
        order_index = {layer.hex: i for i, layer in enumerate(layers)}
        color_pdfs.sort(key=lambda entry: order_index[entry[0].hex])

        # --- Package ZIP ------------------------------------------------------
        zip_path = package.build_zip(job_dir=job_dir, color_pdfs=color_pdfs, job_name=job_name)
        log.info("Job %s zip=%s", job_id, zip_path.name)

        headers = {
            "X-Color-Count": str(len(layers)),
            "X-Color-Names": ",".join(layer.name for layer in layers),
            "X-Color-Hexes": ",".join(layer.hex for layer in layers),
        }
        if separation.only_one_color:
            headers["X-Warning-Only-One-Color"] = "1"
        if separation.complex_design:
            headers["X-Warning-Complex-Design"] = "1"
        if separation.gradients_detected:
            headers["X-Warning-Gradients"] = "1"
        if separation.stroke_only_fallback:
            headers["X-Warning-StrokeOnly"] = "1"
        if intake_result.multipage_pdf:
            headers["X-Warning-MultiPage"] = "1"

        if intake_result.low_resolution != "none":
            ow, oh = intake_result.original_size
            headers["X-Warning-LowResolution"] = f"{ow}x{oh}"
        if was_upscaled:
            uw, uh = working_image.size
            headers["X-Stage-Upscaled"] = f"{uw}x{uh}"
        elif (
            upscale.should_upscale(intake_result.image.size)
            and config.get_replicate_token()
            and config.get_upscale_enabled()
        ):
            # Preconditions for upscale were met but it didn't happen — it failed.
            headers["X-Warning-UpscaleSkipped"] = "1"
        if qc_verdict.is_photo_of_object:
            headers["X-Warning-PhotoOfObject"] = "1"
        if qc_verdict.has_illegible_text:
            headers["X-Warning-IllegibleText"] = "1"
        if qc_verdict.model_used:
            headers["X-QC-Model"] = qc_verdict.model_used

        # Single per-job cost line — rough but useful for spotting runaway jobs.
        cost = _COST_VECTORIZER_CREDIT
        if was_upscaled:
            cost += _COST_REPLICATE_UPSCALE
        cost += _estimate_qc_cost(qc_verdict.model_used)
        log.info("Job %s cost_estimate=$%.4f", job_id, cost)

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=zip_path.name,
            headers=headers,
            background=BackgroundTask(package.cleanup, job_dir),
        )

    except HTTPException:
        package.cleanup(job_dir)
        raise
    except Exception:  # pragma: no cover — catch-all safety net
        log.error("Job %s unhandled exception:\n%s", job_id, traceback.format_exc())
        package.cleanup(job_dir)
        return JSONResponse(
            status_code=500,
            content={
                "error": f"Something went wrong — text Anthony at {_SUPPORT_PHONE}.",
                "photopea_url": PHOTOPEA_FALLBACK_URL,
            },
        )
