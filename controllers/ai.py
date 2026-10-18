import base64
import binascii
import logging
import time

from werkzeug.exceptions import BadRequest

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from odoo.addons.ai.controllers.ai import AIController

from ..services import speech
from ..services.client import ProviderError

_logger = logging.getLogger(__name__)


def log_speech(kind, config, model_name, start, error=None, **usage):
    """One request log row for a speech gateway call, kept even when the
    request is rolled back (errors) or read-only."""
    request.env["ibt.ai.request.log"]._ibt_record(
        kind, request.env["ibt.ai.provider"].sudo().browse(config["id"]), model_name,
        start=start, error=error, own_transaction=True, **usage,
    )


def check_employee():
    """The speech gateway serves employees only, not portal users (customers)."""
    if not request.env.user._is_internal():
        raise AccessError(request.env._("Speech features are available to employees only."))


class IbtSpeechAIController(AIController):

    @http.route()
    def transcribe(self, audio_base64, language=None):
        """Voice messages in AI chats: the company's speech gateway instead of IAP."""
        config = request.env.company._ibt_speech_config()
        if not config:
            return super().transcribe(audio_base64, language=language)
        check_employee()
        if not audio_base64:
            raise BadRequest("No audio data provided")
        try:
            audio = base64.b64decode(audio_base64, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("Invalid audio data")
        start = time.monotonic()
        try:
            # The voice recorder produces MP3 (see ai/static/src/discuss/composer_patch.js).
            result = speech.transcribe(config, audio, "audio/mpeg", language=language or request.env.lang)
        except ProviderError as error:
            log_speech("transcription", config, "whisper-1", start, error)
            _logger.warning(
                "Speech gateway transcription failed: HTTP %s, %s",
                error.status_code or "-", error.error_code or "no response",
            )
            raise UserError(speech.user_message(request.env, error)) from error
        log_speech("transcription", config, "whisper-1", start, audio_seconds=result["duration"])
        return {"status": "success", "text": result["text"]}

    # Not read-only like the parent route: it writes a request log row.
    @http.route(readonly=False)
    def get_session_token(self, language: str, prompt: str):
        """Realtime dictation: a single-use secret from the speech gateway instead of IAP."""
        config = request.env.company._ibt_speech_config()
        if not config or not config["realtime"]:
            return super().get_session_token(language, prompt)
        check_employee()
        start = time.monotonic()
        try:
            session_token = speech.realtime_session(config, language=language, prompt=prompt)
        except ProviderError as error:
            log_speech("realtime", config, "whisper-1", start, error)
            _logger.warning(
                "Speech gateway realtime session failed: HTTP %s, %s",
                error.status_code or "-", error.error_code or "no response",
            )
            raise UserError(speech.user_message(request.env, error)) from error
        log_speech("realtime", config, "whisper-1", start)
        # No IAP transaction: the widget then skips its usage report to IAP.
        return {"status": "success", "session_token": session_token, "iap_transaction_token": None}
