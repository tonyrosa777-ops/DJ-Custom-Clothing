"""Configuration loader — all env access routes through this module.

CLAUDE.md rule: never access os.environ directly elsewhere in the codebase.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_MAX_FILE_MB = 20
_DEFAULT_OUTPUT_DPI = 300
_DEFAULT_QC_MODEL = "claude-haiku-4-5"


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def get_api_credentials() -> tuple[str, str]:
    """Return (api_id, api_token) — may be empty strings if unset."""
    return _env("VECTORIZER_API_ID"), _env("VECTORIZER_API_TOKEN")


def get_basic_auth() -> tuple[str, str]:
    """Return (user, password) for basic-auth gate. Empty strings = auth disabled."""
    return _env("DJ_BASIC_USER"), _env("DJ_BASIC_PASS")


def get_vectorizer_mode() -> str:
    """Return Vectorizer.ai mode: 'production' (default), 'preview', or 'test'.

    'test' is free but watermarks the output. Use during dev when the API key
    doesn't yet have a paid production-mode subscription. Set in .env via
    VECTORIZER_MODE=test.
    """
    raw = _env("VECTORIZER_MODE").lower()
    return raw if raw in ("production", "preview", "test") else "production"


def get_max_file_mb() -> int:
    raw = _env("MAX_FILE_SIZE_MB")
    if not raw:
        return _DEFAULT_MAX_FILE_MB
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_FILE_MB


def get_output_dpi() -> int:
    raw = _env("OUTPUT_DPI")
    if not raw:
        return _DEFAULT_OUTPUT_DPI
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_OUTPUT_DPI


def get_replicate_token() -> str:
    """Return Replicate API token. Empty string = upscale stage disabled."""
    return _env("REPLICATE_API_TOKEN")


def get_anthropic_key() -> str:
    """Return Anthropic API key. Empty string = quality-check stage disabled."""
    return _env("ANTHROPIC_API_KEY")


def get_qc_model() -> str:
    """Claude model ID for the quality-check call. Default = Haiku 4.5."""
    return _env("QC_MODEL") or _DEFAULT_QC_MODEL


def get_upscale_enabled() -> bool:
    """Kill-switch to skip Replicate calls without unsetting the token. Default: true."""
    return _env_bool("UPSCALE_ENABLED", True)


def get_temp_dir() -> Path:
    """Resolve temp dir. Falls back to system temp if env value is unset or unwritable."""
    raw = _env("TEMP_DIR")
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(Path(tempfile.gettempdir()) / "djs-art-engine")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_file = candidate / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            return candidate
        except OSError:
            continue

    # Final fallback — raise only if nothing worked.
    raise RuntimeError("No writable temp directory available.")
