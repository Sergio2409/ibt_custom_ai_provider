from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ibt_ai_enabled = fields.Boolean(related="company_id.ibt_ai_enabled", readonly=False)
    ibt_ai_provider_id = fields.Many2one(related="company_id.ibt_ai_provider_id", readonly=False)
    ibt_ai_chat_model_id = fields.Many2one(
        related="company_id.ibt_ai_chat_model_id",
        readonly=False,
        domain="[('provider_id', '=', ibt_ai_provider_id), ('model_type', '=', 'chat')]",
    )
    ibt_ai_reasoning_model_id = fields.Many2one(
        related="company_id.ibt_ai_reasoning_model_id",
        readonly=False,
        domain="[('provider_id', '=', ibt_ai_provider_id), ('model_type', '=', 'chat')]",
    )
    ibt_ai_embedding_provider_id = fields.Many2one(related="company_id.ibt_ai_embedding_provider_id", readonly=False)
    ibt_ai_embedding_model_id = fields.Many2one(
        related="company_id.ibt_ai_embedding_model_id",
        readonly=False,
        domain="[('provider_id', '=', ibt_ai_embedding_provider_id), ('model_type', '=', 'embedding')]",
    )

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

    def set_values(self):
        companies = self.company_id
        previous = companies.ibt_ai_embedding_model_id
        result = super().set_values()
        if previous != companies.ibt_ai_embedding_model_id:
            cron = self.env.ref("ai.ir_cron_update_deprecated_embedding_models", raise_if_not_found=False)
            if cron:
                cron._trigger()
        return result
