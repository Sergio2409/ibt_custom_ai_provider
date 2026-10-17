from odoo import api, fields, models
from odoo.exceptions import ValidationError

# Not related fields: a related field writes the company once per field, and
# the company's cross-field checks then see half a configuration. get_values()
# loads them and set_values() writes them together.
IBT_AI_COMPANY_FIELDS = (
    "ibt_ai_enabled", "ibt_ai_provider_id", "ibt_ai_chat_model_id",
    "ibt_ai_reasoning_model_id", "ibt_ai_embedding_provider_id", "ibt_ai_embedding_model_id",
)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ibt_ai_enabled = fields.Boolean(string="Use IBT AI Provider")
    ibt_ai_provider_id = fields.Many2one(
        "ibt.ai.provider", string="AI Provider",
        domain="[('provider_kind', '!=', 'speech_gateway')]",
    )
    ibt_ai_chat_model_id = fields.Many2one(
        "ibt.ai.provider.model", string="Chat Model",
        domain="[('provider_id', '=', ibt_ai_provider_id), ('model_type', '=', 'chat')]",
    )
    ibt_ai_reasoning_model_id = fields.Many2one(
        "ibt.ai.provider.model", string="Reasoning Model",
        domain="[('provider_id', '=', ibt_ai_provider_id), ('model_type', '=', 'chat')]",
    )
    ibt_ai_embedding_provider_id = fields.Many2one(
        "ibt.ai.provider", string="Embedding Provider",
        domain="[('provider_kind', '!=', 'speech_gateway')]",
    )
    ibt_ai_embedding_model_id = fields.Many2one(
        "ibt.ai.provider.model", string="Embedding Model",
        domain="[('provider_id', '=', ibt_ai_embedding_provider_id), ('model_type', '=', 'embedding')]",
    )

    ibt_speech_enabled = fields.Boolean(related="company_id.ibt_speech_enabled", readonly=False)
    ibt_speech_provider_id = fields.Many2one(related="company_id.ibt_speech_provider_id", readonly=False)
    ibt_speech_correction = fields.Selection(related="company_id.ibt_speech_correction", readonly=False)
    ibt_speech_realtime = fields.Boolean(related="company_id.ibt_speech_realtime", readonly=False)
    ibt_speech_voice_replies = fields.Boolean(related="company_id.ibt_speech_voice_replies", readonly=False)
    ibt_speech_voice_mode = fields.Boolean(related="company_id.ibt_speech_voice_mode", readonly=False)
    ibt_speech_voice_es_id = fields.Many2one(
        related="company_id.ibt_speech_voice_es_id",
        readonly=False,
        domain="[('provider_id', '=', ibt_speech_provider_id), ('language', '=like', 'es%')]",
    )
    ibt_speech_voice_en_id = fields.Many2one(
        related="company_id.ibt_speech_voice_en_id",
        readonly=False,
        domain="[('provider_id', '=', ibt_speech_provider_id)]",
    )

    def get_values(self):
        values = super().get_values()
        company = self.env.company
        values.update({fname: company[fname].id if fname.endswith("_id") else company[fname]
                       for fname in IBT_AI_COMPANY_FIELDS})
        return values

    @api.onchange("ibt_ai_provider_id")
    def _onchange_ibt_ai_provider_id(self):
        provider = self.ibt_ai_provider_id
        if self.ibt_ai_chat_model_id.provider_id != provider:
            self.ibt_ai_chat_model_id = provider.model_ids.filtered(lambda model: model.model_type == "chat")[:1]
        if self.ibt_ai_reasoning_model_id.provider_id != provider:
            self.ibt_ai_reasoning_model_id = False
        if not self.ibt_ai_embedding_provider_id:
            self.ibt_ai_embedding_provider_id = provider

    @api.onchange("ibt_ai_embedding_provider_id")
    def _onchange_ibt_ai_embedding_provider_id(self):
        provider = self.ibt_ai_embedding_provider_id
        if self.ibt_ai_embedding_model_id.provider_id != provider:
            self.ibt_ai_embedding_model_id = provider.model_ids.filtered(lambda model: model.model_type == "embedding")[:1]

    @api.onchange("ibt_speech_provider_id")
    def _onchange_ibt_speech_provider_id(self):
        provider = self.ibt_speech_provider_id
        if self.ibt_speech_voice_es_id.provider_id != provider:
            self.ibt_speech_voice_es_id = False
        if self.ibt_speech_voice_en_id.provider_id != provider:
            self.ibt_speech_voice_en_id = False

    def action_ibt_discover_voices(self):
        self.ensure_one()
        if not self.ibt_speech_provider_id:
            raise ValidationError(self.env._("Select a speech gateway first."))
        return self.ibt_speech_provider_id.action_discover_voices()

    def set_values(self):
        companies = self.company_id
        previous = companies.ibt_ai_embedding_model_id
        result = super().set_values()
        for settings in self:
            settings.company_id.write({fname: settings[fname] for fname in IBT_AI_COMPANY_FIELDS})
        if previous != companies.ibt_ai_embedding_model_id or self.env["ai.embedding"]._ibt_has_foreign_embedding_models():
            cron = self.env.ref("ai.ir_cron_update_deprecated_embedding_models", raise_if_not_found=False)
            if cron:
                cron._trigger()
        return result
