"""DJ's Art Engine — FastAPI application entry point."""
from __future__ import annotations

import base64
import logging
import secrets
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from pipeline import cleanup, config, intake, package, separate, vectorize
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
        # --- Intake: any supported format -> clean JPEG ----------------------
        try:
            jpeg_bytes = intake.to_jpeg(
                raw_bytes,
                filename=file.filename,
                content_type=content_type,
            )
        except IntakeError as exc:
            log.warning("Job %s intake failure: %s", job_id, exc)
            package.cleanup(job_dir)
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        (job_dir / "intake.jpg").write_bytes(jpeg_bytes)

        # --- Pre-vectorization cleanup ---------------------------------------
        cleaned_bytes = cleanup.clean_image(jpeg_bytes)
        (job_dir / "cleaned.jpg").write_bytes(cleaned_bytes)

        # --- Vectorize --------------------------------------------------------
        try:
            svg_bytes = await vectorize.vectorize(
                cleaned_bytes,
                filename="cleaned.jpg",
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

        # Persist a copy of the SVG for debugging (cleaned up with job_dir).
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
            "Job %s colors=%s only_one=%s complex=%s",
            job_id, len(layers), separation.only_one_color, separation.complex_design,
        )

        # --- Render per-color PDFs -------------------------------------------
        dpi = config.get_output_dpi()
        color_pdfs: list[tuple[separate.ColorLayer, Path]] = []
        for layer in layers:
            pdf_path = job_dir / package.pdf_filename(layer)
            render_separation_pdf(
                svg_bytes=svg_bytes,
                target_hex=layer.hex,
                output_path=pdf_path,
                dpi=dpi,
                bleed_inches=0.125,
            )
            color_pdfs.append((layer, pdf_path))

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
                "error": "Something went wrong — text Anthony.",
                "photopea_url": PHOTOPEA_FALLBACK_URL,
            },
        )
