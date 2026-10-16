from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    ibt_ai_enabled = fields.Boolean(string="Use IBT AI Provider")
    ibt_ai_provider_id = fields.Many2one("ibt.ai.provider", string="AI Provider", ondelete="restrict")
    ibt_ai_chat_model_id = fields.Many2one("ibt.ai.provider.model", string="Chat Model", ondelete="restrict")
    ibt_ai_reasoning_model_id = fields.Many2one("ibt.ai.provider.model", string="Reasoning Model", ondelete="restrict")
    ibt_ai_embedding_provider_id = fields.Many2one("ibt.ai.provider", string="Embedding Provider", ondelete="restrict")
    ibt_ai_embedding_model_id = fields.Many2one("ibt.ai.provider.model", string="Embedding Model", ondelete="restrict")
    ibt_speech_enabled = fields.Boolean(string="Use IBT Speech Gateway")
    ibt_speech_provider_id = fields.Many2one(
        "ibt.ai.provider", string="Speech Gateway", ondelete="restrict",
        domain="[('provider_kind', '=', 'speech_gateway')]",
    )
    ibt_speech_correction = fields.Selection(
        [
            ("off", "Off"),
            ("format", "Punctuation, capitals and accents"),
            ("glossary", "Punctuation, capitals, accents and glossary"),
        ],
        string="Transcript Correction",
        default="off",
        required=True,
        help="Optional correction of transcripts by the gateway's local language model. "
             "The gateway always keeps the raw transcript; it must have correction configured.",
    )

    def _ibt_ai_config(self, boost_reasoning=False):
        self.ensure_one()
        # Provider credentials are administrator-only, but every AI user must
        # be able to consume the company-selected configuration.
        provider = self.ibt_ai_provider_id.sudo()
        if not self.ibt_ai_enabled or not provider:
            return None
        chat_model = (
            self.ibt_ai_reasoning_model_id
            if boost_reasoning and self.ibt_ai_reasoning_model_id
            else self.ibt_ai_chat_model_id
        ).sudo()
        return provider._snapshot(chat_model=chat_model)

    def _ibt_ai_embedding_config(self):
        self.ensure_one()
        # See _ibt_ai_config: callers need a bounded snapshot, not access to
        # the provider record itself.
        provider = (self.ibt_ai_embedding_provider_id or self.ibt_ai_provider_id).sudo()
        if not self.ibt_ai_enabled or not provider or not self.ibt_ai_embedding_model_id:
            return None
        return provider._snapshot(embedding_model=self.ibt_ai_embedding_model_id.sudo())

    def _ibt_speech_config(self):
        """Snapshot of the speech gateway for transcription, or None when disabled."""
        self.ensure_one()
        # Like _ibt_ai_config: every user may transcribe, only administrators
        # may read the provider and its key.
        provider = self.ibt_speech_provider_id.sudo()
        if not self.ibt_speech_enabled or not provider:
            return None
        return dict(provider._snapshot(), correction=self.ibt_speech_correction)

    @api.constrains("ibt_speech_enabled", "ibt_speech_provider_id")
    def _check_ibt_speech_settings(self):
        for company in self:
            provider = company.ibt_speech_provider_id
            if provider and provider.provider_kind != "speech_gateway":
                raise ValidationError(self.env._("The speech gateway must be a provider of kind IBT Speech Gateway."))
            if company.ibt_speech_enabled and not provider:
                raise ValidationError(self.env._("Select a speech gateway before enabling it."))
            if company.ibt_speech_enabled and not provider.active:
                raise ValidationError(self.env._("The selected speech gateway must be active."))

    @api.constrains(
        "ibt_ai_enabled", "ibt_ai_provider_id", "ibt_ai_chat_model_id",
        "ibt_ai_reasoning_model_id", "ibt_ai_embedding_provider_id", "ibt_ai_embedding_model_id",
    )
    def _check_ibt_ai_models(self):
        for company in self:
            if company.ibt_ai_enabled and (not company.ibt_ai_provider_id or not company.ibt_ai_chat_model_id):
                raise ValidationError(self.env._("Select a provider and chat model before enabling the custom AI provider."))
            if company.ibt_ai_enabled and not company.ibt_ai_provider_id.active:
                raise ValidationError(self.env._("The selected chat provider must be active."))
            if "speech_gateway" in (company.ibt_ai_provider_id | company.ibt_ai_embedding_provider_id).mapped("provider_kind"):
                raise ValidationError(self.env._("A speech gateway cannot be used for chat or embeddings."))
            if company.ibt_ai_chat_model_id and company.ibt_ai_chat_model_id.provider_id != company.ibt_ai_provider_id:
                raise ValidationError(self.env._("The chat model must belong to the selected chat provider."))
            if company.ibt_ai_chat_model_id and company.ibt_ai_chat_model_id.model_type != "chat":
                raise ValidationError(self.env._("The selected chat model must be a chat model."))
            if company.ibt_ai_enabled and company.ibt_ai_chat_model_id and not company.ibt_ai_chat_model_id.active:
                raise ValidationError(self.env._("The selected chat model must be active."))
            if company.ibt_ai_reasoning_model_id and company.ibt_ai_reasoning_model_id.provider_id != company.ibt_ai_provider_id:
                raise ValidationError(self.env._("The reasoning model must belong to the selected chat provider."))
            if company.ibt_ai_reasoning_model_id and company.ibt_ai_reasoning_model_id.model_type != "chat":
                raise ValidationError(self.env._("The selected reasoning model must be a chat model."))
            if company.ibt_ai_reasoning_model_id and not company.ibt_ai_reasoning_model_id.active:
                raise ValidationError(self.env._("The selected reasoning model must be active."))
            embedding_provider = company.ibt_ai_embedding_provider_id or company.ibt_ai_provider_id
            if company.ibt_ai_embedding_model_id and company.ibt_ai_embedding_model_id.model_type != "embedding":
                raise ValidationError(self.env._("The selected embedding model must be an embedding model."))
            if company.ibt_ai_embedding_model_id and company.ibt_ai_embedding_model_id.provider_id != embedding_provider:
                raise ValidationError(self.env._("The embedding model must belong to the selected embedding provider."))
            if company.ibt_ai_embedding_model_id and not company.ibt_ai_embedding_model_id.active:
                raise ValidationError(self.env._("The selected embedding model must be active."))
            if company.ibt_ai_embedding_model_id and not embedding_provider.active:
                raise ValidationError(self.env._("The selected embedding provider must be active."))
            if company.ibt_ai_embedding_model_id and embedding_provider.transport == "anthropic":
                raise ValidationError(self.env._("Anthropic providers cannot be used for embeddings."))
