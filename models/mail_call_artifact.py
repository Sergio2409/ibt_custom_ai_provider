import logging

from odoo import fields, models

from odoo.addons.ai.models.mail_call_artifact import TRANSCRIPTION_MAX_FILE_SIZE

from ..services import speech
from ..services.client import ProviderError

_logger = logging.getLogger(__name__)


class MailCallArtifact(models.Model):
    _inherit = "mail.call.artifact"

    ibt_transcript_raw = fields.Text(
        string="Raw Transcript (VTT)",
        copy=False,
        help="Whisper's transcript before correction; "
             "set only when the speech gateway corrected the transcript.",
    )

    def _transcribe_claimed_record(self, lease_until):
        """Call recordings: the company's speech gateway instead of IAP.

        Same contract as the parent: no database writes (phase 2), and a
        result dict that _finalize_transcription understands.
        """
        config = self.env.company._ibt_speech_config()
        if not config:
            return super()._transcribe_claimed_record(lease_until)
        self.ensure_one()
        if not self._is_lease_valid(lease_until):
            return {"status": "skip"}
        if self.media_id.file_size > min(TRANSCRIPTION_MAX_FILE_SIZE, speech.MAX_AUDIO_BYTES):
            return {"status": "file_too_large"}
        data = self.media_id.raw
        if not data:
            return {"status": "no_media"}
        # Odoo 20 returns a lazily loaded BinaryValue, not bytes.
        audio = data.content if hasattr(data, "content") else data
        try:
            result = speech.transcribe(config, audio, self.media_id.mimetype, response_format="vtt")
        except ProviderError as error:
            _logger.error(
                "Speech gateway transcription failed for call.artifact %s: HTTP %s, %s",
                self.id, error.status_code or "-", error.error_code or "no response",
            )
            return {"status": "err"}
        return {
            "status": "ok",
            "transcript": result["vtt"],
            "ibt_transcript_raw": result["raw_vtt"] if result["corrected"] else False,
        }

    def _finalize_transcription(self, lease_until, result, max_retries=5):
        finalized = super()._finalize_transcription(lease_until, result, max_retries=max_retries)
        if finalized and result.get("status") == "ok" and result.get("ibt_transcript_raw"):
            self.sudo().ibt_transcript_raw = result["ibt_transcript_raw"]
        return finalized
