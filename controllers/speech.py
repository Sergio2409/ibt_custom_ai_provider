import base64
import logging

from werkzeug.exceptions import BadRequest, NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..services import speech
from ..services.client import ProviderError

_logger = logging.getLogger(__name__)


class IbtVoiceReplyController(http.Controller):

    @http.route("/ibt_ai/speech", type="jsonrpc", auth="user", methods=["POST"], readonly=True)
    def ibt_speech(self, message_id, part=0):
        """One part of an AI chat message read aloud by the speech gateway.

        Only messages of AI chats the user can read; the text comes from the
        message, never from the client. The audio is generated on the fly and
        not stored. Returns the number of parts so the client can fetch the
        next one while this one plays.
        """
        config = request.env.company._ibt_speech_config()
        if not config or not config["voice_replies"]:
            raise NotFound()
        try:
            message_id, part = int(message_id), int(part)
        except (TypeError, ValueError):
            raise BadRequest()
        # search() applies the user's access rules: unreadable messages are not found.
        message = request.env["mail.message"].search([
            ("id", "=", message_id), ("model", "=", "discuss.channel"),
        ], limit=1)
        if not message or not request.env["discuss.channel"].search_count([
            ("id", "=", message.res_id), ("channel_type", "=", "ai_chat"),
        ], limit=1):
            raise NotFound()
        parts = speech.speech_parts(speech.speech_text(message.body))
        if not parts:
            return {"parts": 0}
        if not 0 <= part < len(parts):
            raise BadRequest()
        voice = speech.voice_for(config, request.env.user.lang)
        try:
            audio = speech.synthesize(config, parts[part], voice)
        except ProviderError as error:
            _logger.warning(
                "Speech gateway text-to-speech failed: HTTP %s, %s",
                error.status_code or "-", error.error_code or "no response",
            )
            raise UserError(speech.user_message(request.env, error)) from error
        return {
            "parts": len(parts),
            "part": part,
            "mimetype": "audio/mpeg",
            "audio": base64.b64encode(audio).decode(),
        }
