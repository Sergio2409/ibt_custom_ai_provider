import logging
import time
from datetime import timedelta

from odoo import api, fields, models

from ..services.client import ProviderError

_logger = logging.getLogger(__name__)
RETENTION = timedelta(days=90)


class IbtAiRequestLog(models.Model):
    """One row per call to a provider (chat, embedding, speech gateway):
    metadata only, never prompts, answers, audio, transcripts, keys or error
    texts."""
    _name = "ibt.ai.request.log"
    _description = "IBT AI Request Log"
    _order = "id desc"

    kind = fields.Selection([
        ("chat", "Chat"),
        ("embedding", "Embedding"),
        ("transcription", "Transcription"),
        ("speech", "Text-to-speech"),
        ("realtime", "Realtime session"),
    ], required=True, readonly=True)
    company_id = fields.Many2one("res.company", readonly=True, index=True)
    provider_id = fields.Many2one("ibt.ai.provider", readonly=True, index=True, ondelete="cascade")
    model_name = fields.Char(string="Model", readonly=True)
    user_id = fields.Many2one("res.users", readonly=True, ondelete="set null")
    session_id = fields.Many2one("ai.session", readonly=True, ondelete="set null", index="btree_not_null")
    agent_id = fields.Many2one("ai.agent", readonly=True, ondelete="set null")
    request_uuid = fields.Char(string="Request", readonly=True, index="btree_not_null")
    request_round = fields.Integer(string="Round", readonly=True)
    attempt = fields.Integer(readonly=True, aggregator="max")
    outcome = fields.Selection(
        [("success", "Success"), ("retrying", "Retrying"), ("failed", "Failed")], required=True, readonly=True,
    )
    category = fields.Selection([
        ("configuration", "Configuration"),
        ("capability", "Model capability"),
        ("throttled", "Throttled (429)"),
        ("unavailable", "Unavailable"),
        ("invalid_response", "Invalid response"),
        ("expired", "Waited too long"),
        ("unexpected", "Unexpected"),
    ], readonly=True)
    status_code = fields.Integer(string="HTTP Status", readonly=True, aggregator=False)
    provider_request_id = fields.Char(readonly=True)
    duration = fields.Float(string="Duration (s)", digits=(16, 2), readonly=True, aggregator="avg")
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    audio_seconds = fields.Float(string="Audio (s)", digits=(16, 1), readonly=True)
    characters = fields.Integer(readonly=True)
    estimated_cost = fields.Float(
        string="Estimated Cost (USD)", digits=(16, 6), readonly=True,
        help="From the token counts and the model's prices; not an invoice.",
    )

    @api.model
    def _ibt_record(self, kind, provider, model_name, *, client=None, start=None, error=None,
                    outcome=None, category=None, session=None, attempt=1, audio_seconds=0, characters=0,
                    own_transaction=False):
        """Store one row and log it as one line. The row is part of the current
        transaction (a caller that rolls back loses it) unless own_transaction,
        for HTTP routes that roll back on errors or are read-only; the line
        always stays in the server log."""
        usage = (client and client.usage) or {}
        audio_seconds = round(audio_seconds or 0, 1)  # as stored, so the cost matches it
        input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        model = provider.model_ids.filtered(lambda m: m.name == model_name and m.model_type == kind)[:1]
        values = {
            "kind": kind,
            "company_id": (session.ibt_ai_company_id if session else self.env.company).id,
            "provider_id": provider.id,
            "model_name": model_name,
            "user_id": (session.request_user_id if session else self.env.user).id,
            "session_id": session.id if session else False,
            "agent_id": session.agent_id.id if session else False,
            "request_uuid": session.request_uuid if session else False,
            "request_round": session.request_round if session else 0,
            "attempt": attempt,
            "outcome": outcome or ("failed" if error else "success"),
            "category": category or error and (error.category if isinstance(error, ProviderError) else "unexpected"),
            "status_code": getattr(error, "status_code", None) or 0,
            "provider_request_id": client and client.request_id,
            "duration": time.monotonic() - start if start else 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "audio_seconds": audio_seconds,
            "characters": characters,
            "estimated_cost": (input_tokens * model.price_input + output_tokens * model.price_output) / 1e6
                              + audio_seconds / 60 * provider.price_audio_minute
                              + characters / 1e6 * provider.price_speech,
        }
        (_logger.info if values["outcome"] == "success" else _logger.warning)(
            "ibt_ai kind=%s request=%s company=%s provider=%s model=%s attempt=%s outcome=%s category=%s "
            "status=%s duration=%.2fs tokens=%s/%s audio=%.1fs characters=%s", kind, values["request_uuid"] or "-", values["company_id"],
            provider.id, model_name, attempt, values["outcome"], values["category"] or "-",
            values["status_code"] or "-", values["duration"], input_tokens, output_tokens,
            values["audio_seconds"], characters,
        )
        if not own_transaction:
            return self.sudo().create(values)
        with self.env.registry.cursor() as cr:
            self.with_env(self.env(cr=cr, su=True)).create(values)

    @api.autovacuum
    def _gc_old_rows(self):
        self.search([("create_date", "<", fields.Datetime.now() - RETENTION)]).unlink()
