"""Speech-to-text through the IBT AI Speech Gateway.

The gateway (ibt_ai_speech_gateway) is an OpenAI-compatible Speaches server
with an extra route, POST /v1/ibt/transcriptions, that always answers JSON
with the raw transcript and, when correction is on, a corrected one. This
module calls that route through ProviderClient, so the provider's endpoint
validation (DNS pinning, no redirects, private-network opt-in) applies.

Never logs audio, transcripts or keys.
"""

import logging
import re

from .client import ProviderClient, ProviderError

_logger = logging.getLogger(__name__)

# The gateway's MAX_UPLOAD_SIZE is 25 MB (decimal); keep room for the
# multipart envelope.
MAX_AUDIO_BYTES = 24 * 1000 * 1000
LANGUAGE_ERRORS = {"language_not_enabled", "invalid_language"}
CORRECTION_MODES = ("off", "format", "glossary")
EXTENSIONS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/flac": "flac",
}


def stt_language(lang_code):
    """Odoo language code (es_ES, es-MX, en) -> ISO-639 code for Whisper, or None."""
    short = re.split(r"[_@-]", (lang_code or "").strip().lower(), maxsplit=1)[0]
    return short if re.fullmatch(r"[a-z]{2,3}", short) else None


def _filename(mimetype):
    base = (mimetype or "").split(";", 1)[0].strip().lower()
    return f"audio.{EXTENSIONS.get(base, 'bin')}"


def transcribe(config, audio, mimetype, language=None, response_format="json"):
    """Transcribe `audio` (bytes) with the speech gateway described by `config`.

    :param dict config: provider snapshot plus "correction" (off/format/glossary),
        see res.company._ibt_speech_config()
    :param str language: Odoo or ISO language code; None = auto-detect. A
        language the gateway does not accept falls back to auto-detection.
    :param str response_format: "json" (text only) or "vtt" (also cues)
    :return: dict with "text" (the corrected text if a correction was applied,
        else the raw one), "raw_text", "vtt" / "raw_vtt" (vtt format only),
        "corrected" (bool) and "correction_status"
    :raises ProviderError: the gateway is unreachable or rejected the request;
        status_code / error_code tell why
    """
    if len(audio) > MAX_AUDIO_BYTES:
        error = ProviderError("Audio too large for the speech gateway")
        error.status_code = 413
        raise error
    correction = config.get("correction") or "off"
    language = stt_language(language)
    fields = [("model", "whisper-1"), ("response_format", response_format), ("correction", correction)]
    files = {"file": (_filename(mimetype), audio, mimetype or "application/octet-stream")}
    client = ProviderClient(config)
    try:
        body = client._request("POST", "ibt/transcriptions", files=files,
                               data=fields + ([("language", language)] if language else []))
    except ProviderError as error:
        if not (language and error.status_code == 400 and error.error_code in LANGUAGE_ERRORS):
            raise
        # The user's language is not enabled on the gateway: let Whisper detect it.
        _logger.info("Speech gateway does not accept language %s; retrying with auto-detection", language)
        language = None
        body = client._request("POST", "ibt/transcriptions", files=files, data=fields)

    raw = body.get("raw") or {}
    corrected = body.get("corrected") or None
    result = {
        "raw_text": raw.get("text") or "",
        "text": (corrected or raw).get("text") or "",
        "corrected": corrected is not None,
        "correction_status": body.get("correction_status"),
    }
    if response_format == "vtt":
        result["raw_vtt"] = raw.get("vtt") or ""
        result["vtt"] = (corrected or raw).get("vtt") or ""
    _logger.info(
        "Speech gateway transcription: %d bytes, language %s, format %s, correction %s -> %s",
        len(audio), language or "auto", response_format, correction, result["correction_status"],
    )
    return result


def user_message(env, error):
    """A message for the user of a failed transcription (no technical details)."""
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return env._("The speech service rejected Odoo's credentials. Ask an administrator to check the speech gateway settings.")
    if status == 413:
        return env._("The recording is too long to be transcribed.")
    if status == 415:
        return env._("This audio format cannot be transcribed.")
    return env._("The speech service is not available right now. Please try again later.")
