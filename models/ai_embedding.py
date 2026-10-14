from odoo import api, models

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
        config = self.env.company._ibt_ai_embedding_config()
        if config and config.get("embedding_model"):
            return [config["embedding_model"]]
        return super()._get_supported_embedding_models()

    @api.model
    def _get_embeddings(self, input, model, mode):
        config = self.env.company._ibt_ai_embedding_config()
        if not config:
            return super()._get_embeddings(input, model, mode)
        return ProviderClient(config).embeddings(input)
