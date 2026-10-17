"""Speech-to-text through the IBT AI Speech Gateway.

The gateway (ibt_ai_speech_gateway) is an OpenAI-compatible Speaches server
with an extra route, POST /v1/ibt/transcriptions, that always answers JSON
with the raw transcript and, when correction is on, a corrected one. This
module calls that route through ProviderClient, so the provider's endpoint
validation (DNS pinning, no redirects, private-network opt-in) applies.

Never logs audio, transcripts or keys.
"""

import base64
import json
import logging
import re
from urllib.parse import urlparse

from odoo.tools import html2plaintext

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


def realtime_url(config):
    """WebSocket URL of the gateway: https://host/v1 -> wss://host/v1/realtime."""
    parsed = urlparse(config["base_url"].rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return parsed._replace(scheme=scheme, path=f"{parsed.path}/realtime").geturl()


def realtime_session(config, language=None, prompt=None):
    """Ask the gateway for a single-use realtime secret for a browser.

    :return: the session token Odoo's RealtimeClient receives: "ibt." plus the
        base64url JSON {"url": <gateway WebSocket URL>, "secret": <secret>};
        the JS patch (realtime_client_patch.js) connects there.
    :raises ProviderError: like transcribe()
    """
    language = stt_language(language)
    transcription = {"model": "whisper-1", "prompt": (prompt or "")[:2000]}
    client = ProviderClient(config)

    def request(transcription):
        return client._request("POST", "realtime/transcription_sessions",
                               {"input_audio_transcription": transcription})

    try:
        body = request(dict(transcription, language=language) if language else transcription)
    except ProviderError as error:
        if not (language and error.status_code == 400 and error.error_code in LANGUAGE_ERRORS):
            raise
        _logger.info("Speech gateway does not accept language %s for realtime; using auto-detection", language)
        body = request(transcription)
    secret = (body.get("client_secret") or {}).get("value")
    if not secret:
        raise ProviderError("The speech gateway returned no realtime secret")
    payload = json.dumps({"url": realtime_url(config), "secret": secret}).encode()
    return "ibt." + base64.urlsafe_b64encode(payload).decode().rstrip("=")


# --- Text-to-speech (voice replies) -------------------------------------------------------

VOICE_RE = re.compile(r"^[a-z]{1,2}[fm]_[a-z0-9]+$")
FIRST_PART_CHARS = 220   # short first part: playback starts quickly
PART_CHARS = 700
MAX_PARTS = 40
CODE_BLOCK_RE = re.compile(r"<pre\b.*?</pre>", re.IGNORECASE | re.DOTALL)
URL_RE = re.compile(r"https?://\S+")
MARKUP_RE = re.compile(r"[*_#`>|~\[\]]+")
SENTENCE_RE = re.compile(r"(?<=[.!?\u2026:;])\s+")


def speech_text(html):
    """Plain text worth reading aloud from a message body (no code, links or markup)."""
    text = html2plaintext(CODE_BLOCK_RE.sub(" ", str(html or "")), include_references=False)
    text = MARKUP_RE.sub(" ", URL_RE.sub(" ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _split_long(sentence, limit):
    """Cut a sentence longer than `limit` at commas or spaces."""
    pieces, current = [], ""
    for word in re.split(r"(?<=,)\s+|\s+", sentence):
        if current and len(current) + len(word) + 1 > limit:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return pieces + ([current] if current else [])


def speech_parts(text):
    """Split text into parts for synthesis: a short first part, then up to PART_CHARS each."""
    sentences = []
    for line in text.splitlines():
        for sentence in SENTENCE_RE.split(line):
            sentences.extend(_split_long(sentence.strip(), PART_CHARS) if sentence.strip() else [])
    parts, current = [], ""
    for sentence in sentences:
        limit = FIRST_PART_CHARS if not parts else PART_CHARS
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts[:MAX_PARTS]


def list_voices(config):
    """Voices the gateway offers: [{"name", "language", "gender"}] (needs the key's tts route)."""
    body = ProviderClient(config)._request("GET", "audio/voices")
    voices = []
    for voice in body.get("voices") or []:
        name = voice.get("id") or voice.get("name")
        if isinstance(name, str) and VOICE_RE.match(name):
            gender = voice.get("gender")
            voices.append({
                "name": name,
                "language": str(voice.get("language") or "")[:16],
                "gender": gender if gender in ("female", "male") else False,
            })
    return voices


def voice_for(config, lang_code):
    """The configured voice for an Odoo language (Spanish voice for es_*, else English)."""
    return config["voice_es"] if stt_language(lang_code) == "es" else config["voice_en"]


def synthesize(config, text, voice):
    """MP3 bytes of `text` spoken by `voice`, generated by the gateway (nothing stored)."""
    audio = ProviderClient(config)._request(
        "POST", "audio/speech",
        {"model": "tts-1", "voice": voice, "input": text, "response_format": "mp3"},
        raw=True,
    )
    _logger.info("Speech gateway text-to-speech: %d characters, voice %s -> %d bytes", len(text), voice, len(audio))
    return audio


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
