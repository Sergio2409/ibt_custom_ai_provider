import time

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
        custom_models = self.env["ibt.ai.provider.model"].sudo().search([
            ("model_type", "=", "embedding"),
            ("active", "=", True),
            ("provider_id.active", "=", True),
        ])
        if self.env.company._ibt_ai_embedding_config():
            # Odoo's models (e.g. gemini-embedding-2) would embed every
            # question through IAP; listing only the custom models lets
            # Odoo's deprecated-model cron move agents and sources to the
            # company's model, and avoids the IAP call.
            return custom_models.mapped("name")
        supported = super()._get_supported_embedding_models()
        if isinstance(supported, str):
            supported = [supported]
        return list(dict.fromkeys([*supported, *custom_models.mapped("name")]))

    @api.model
    def _ibt_has_foreign_embedding_models(self):
        """True if an agent or a source still uses a model other than the company's custom one."""
        config = self.env.company._ibt_ai_embedding_config()
        if not config:
            return False
        model = config["embedding_model"]
        return bool(
            self.env["ai.agent"].sudo().with_context(active_test=False).search_count(
                [("embedding_model", "!=", model)], limit=1)
            or self.sudo().search_count([("embedding_model", "!=", model)], limit=1)
        )

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
        client, start, error = ProviderClient(config), time.monotonic(), None
        try:
            return client.embeddings(input, model=model)
        except Exception as failure:
            error = failure
            raise
        finally:
            self.env["ibt.ai.request.log"]._ibt_record(
                "embedding", provider_model.provider_id, model, client=client, start=start, error=error,
            )
