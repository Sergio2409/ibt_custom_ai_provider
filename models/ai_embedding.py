from odoo import api, models
from odoo.exceptions import UserError

from ..services.client import ProviderClient


class AiEmbedding(models.Model):
    _inherit = "ai.embedding"

    @api.model
    def _get_default_embedding_model(self):
        config = self.env.company._ibt_ai_embedding_config()
        if config and config.get("embedding_model"):
            return config["embedding_model"]
        return super()._get_default_embedding_model()

    @api.model
    def _get_supported_embedding_models(self):
        supported = super()._get_supported_embedding_models()
        if isinstance(supported, str):
            supported = [supported]
        custom_models = self.env["ibt.ai.provider.model"].sudo().search([
            ("model_type", "=", "embedding"),
            ("active", "=", True),
            ("provider_id.active", "=", True),
        ])
        return list(dict.fromkeys([*supported, *custom_models.mapped("name")]))

    @api.model
    def _get_embeddings(self, input, model, mode):
        provider_model = self.env["ibt.ai.provider.model"].sudo().with_context(active_test=False).search([
            ("name", "=", model),
            ("model_type", "=", "embedding"),
        ], limit=1)
        if not provider_model:
            return super()._get_embeddings(input, model, mode)
        if not provider_model.active or not provider_model.provider_id.active:
            raise UserError(self.env._(
                "The custom embedding model %s is archived. Select an active model before generating embeddings.",
                model,
            ))
        config = provider_model.provider_id._snapshot(embedding_model=provider_model)
        return ProviderClient(config).embeddings(input, model=model)
