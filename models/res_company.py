from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    ibt_ai_enabled = fields.Boolean(string="Use IBT AI Provider")
    ibt_ai_provider_id = fields.Many2one("ibt.ai.provider", string="AI Provider", ondelete="restrict")
    ibt_ai_chat_model_id = fields.Many2one("ibt.ai.provider.model", string="Chat Model", ondelete="restrict")
    ibt_ai_reasoning_model_id = fields.Many2one("ibt.ai.provider.model", string="Reasoning Model", ondelete="restrict")
    ibt_ai_embedding_provider_id = fields.Many2one("ibt.ai.provider", string="Embedding Provider", ondelete="restrict")
    ibt_ai_embedding_model_id = fields.Many2one("ibt.ai.provider.model", string="Embedding Model", ondelete="restrict")

    def _ibt_ai_config(self, boost_reasoning=False):
        self.ensure_one()
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
        provider = (self.ibt_ai_embedding_provider_id or self.ibt_ai_provider_id).sudo()
        if not self.ibt_ai_enabled or not provider or not self.ibt_ai_embedding_model_id:
            return None
        return provider._snapshot(embedding_model=self.ibt_ai_embedding_model_id.sudo())

    @api.constrains(
        "ibt_ai_enabled", "ibt_ai_provider_id", "ibt_ai_chat_model_id",
        "ibt_ai_reasoning_model_id", "ibt_ai_embedding_provider_id", "ibt_ai_embedding_model_id",
    )
    def _check_ibt_ai_models(self):
        for company in self:
            if company.ibt_ai_enabled and (not company.ibt_ai_provider_id or not company.ibt_ai_chat_model_id):
                raise ValidationError(_("Select a provider and chat model before enabling the custom AI provider."))
            if company.ibt_ai_chat_model_id and company.ibt_ai_chat_model_id.provider_id != company.ibt_ai_provider_id:
                raise ValidationError(_("The chat model must belong to the selected chat provider."))
            if company.ibt_ai_reasoning_model_id and company.ibt_ai_reasoning_model_id.provider_id != company.ibt_ai_provider_id:
                raise ValidationError(_("The reasoning model must belong to the selected chat provider."))
            embedding_provider = company.ibt_ai_embedding_provider_id or company.ibt_ai_provider_id
            if company.ibt_ai_embedding_model_id and company.ibt_ai_embedding_model_id.provider_id != embedding_provider:
                raise ValidationError(_("The embedding model must belong to the selected embedding provider."))
