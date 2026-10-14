from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.client import ProviderClient, ProviderError, validate_endpoint


class IbtAiProvider(models.Model):
    _name = "ibt.ai.provider"
    _description = "IBT AI Provider"
    _order = "sequence, name, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    transport = fields.Selection(
        [("openai", "OpenAI compatible"), ("anthropic", "Anthropic")],
        required=True,
        default="openai",
    )
    provider_kind = fields.Selection(
        [("generic", "Generic"), ("ollama", "Ollama")],
        required=True,
        default="generic",
        help="Select Ollama to enable discovery of locally installed models.",
    )
    base_url = fields.Char(required=True)
    api_key = fields.Char(groups="base.group_system", copy=False)
    allow_private_network = fields.Boolean(
        groups="base.group_system",
        help="Allow RFC1918 and loopback endpoints. Enable only for trusted local services such as Ollama.",
    )
    verify_ssl = fields.Boolean(default=True, groups="base.group_system")
    timeout = fields.Integer(default=120, required=True, groups="base.group_system")
    send_embedding_dimensions = fields.Boolean(
        default=True,
        help="Request 1536 dimensions from compatible embedding APIs. Disable when the API rejects this parameter.",
    )
    model_ids = fields.One2many("ibt.ai.provider.model", "provider_id", context={"active_test": False})
    note = fields.Text()

    @api.constrains("active", "base_url", "allow_private_network", "timeout", "provider_kind", "transport")
    def _check_connection_settings(self):
        for provider in self:
            if not 1 <= provider.timeout <= 1800:
                raise ValidationError(self.env._("Timeout must be between 1 and 1800 seconds."))
            if provider.provider_kind == "ollama" and provider.transport != "openai":
                raise ValidationError(self.env._("Ollama providers must use the OpenAI-compatible transport."))
            if provider.transport == "anthropic" and self.env["res.company"].sudo().search_count([
                ("ibt_ai_embedding_provider_id", "=", provider.id),
                ("ibt_ai_embedding_model_id", "!=", False),
            ], limit=1):
                raise ValidationError(self.env._("Anthropic providers cannot be used for embeddings."))
            company_domain = fields.Domain("ibt_ai_enabled", "=", True) & (
                fields.Domain("ibt_ai_provider_id", "=", provider.id)
                | fields.Domain("ibt_ai_embedding_provider_id", "=", provider.id)
            )
            if not provider.active and self.env["res.company"].sudo().search_count(company_domain, limit=1):
                raise ValidationError(self.env._(
                    "An AI provider selected by an enabled company cannot be archived."
                ))
            try:
                validate_endpoint(provider.base_url, provider.allow_private_network, resolve=False)
            except ProviderError as error:
                raise ValidationError(str(error)) from error

    def _snapshot(self, chat_model=None, embedding_model=None):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "provider_kind": self.provider_kind,
            "base_url": self.base_url.rstrip("/"),
            "api_key": self.api_key or "",
            "allow_private_network": self.allow_private_network,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "send_embedding_dimensions": self.send_embedding_dimensions,
            "chat_model": chat_model.name if chat_model else "",
            "embedding_model": embedding_model.name if embedding_model else "",
        }

    def action_test_connection(self):
        self.ensure_one()
        chat_model = self.model_ids.filtered(lambda model: model.model_type == "chat")[:1]
        ProviderClient(self._snapshot(chat_model=chat_model)).test_connection()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Connection successful"),
                "message": self.env._("The AI provider is reachable."),
                "type": "success",
            },
        }

    def action_discover_models(self):
        self.ensure_one()
        if self.provider_kind != "ollama":
            raise ValidationError(self.env._("Automatic model discovery is currently available only for Ollama providers."))
        discovered = ProviderClient(self._snapshot()).discover_ollama_models()
        Model = self.env["ibt.ai.provider.model"]
        created = updated = skipped = 0
        for item in discovered:
            model_type = item["model_type"]
            dimensions = item.get("embedding_dimensions")
            if model_type == "embedding" and dimensions != 1536:
                skipped += 1
                continue
            model = Model.with_context(active_test=False).search([
                ("provider_id", "=", self.id),
                ("name", "=", item["name"]),
                ("model_type", "=", model_type),
            ], limit=1)
            values = {
                "label": item.get("label") or item["name"],
                "active": True,
                "capabilities": ", ".join(item.get("capabilities", [])),
                "supports_tools": "tools" in item.get("capabilities", []),
                "supports_vision": "vision" in item.get("capabilities", []),
            }
            if model:
                model.write(values)
                updated += 1
            else:
                values.update({
                    "provider_id": self.id,
                    "name": item["name"],
                    "model_type": model_type,
                    "embedding_dimensions": dimensions or 1536,
                })
                Model.create(values)
                created += 1
        message = self.env._(
            "Discovered %(total)s Ollama models: %(created)s added, %(updated)s updated.",
            total=len(discovered),
            created=created,
            updated=updated,
        )
        if skipped:
            message += " " + self.env._(
                "%(count)s incompatible embedding model(s) were skipped because Odoo requires 1536 dimensions.",
                count=skipped,
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Ollama model discovery"),
                "message": message,
                "type": "success",
                "sticky": bool(skipped),
            },
        }


class IbtAiProviderModel(models.Model):
    _name = "ibt.ai.provider.model"
    _description = "IBT AI Provider Model"
    _order = "provider_id, model_type, sequence, name"

    name = fields.Char(string="Model ID", required=True)
    label = fields.Char()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    provider_id = fields.Many2one("ibt.ai.provider", required=True, ondelete="cascade", index=True)
    model_type = fields.Selection([("chat", "Chat"), ("embedding", "Embedding")], required=True, default="chat")
    embedding_dimensions = fields.Integer(default=1536)
    capabilities = fields.Char(readonly=True)
    supports_tools = fields.Boolean(readonly=True)
    supports_vision = fields.Boolean(readonly=True)

    _provider_type_name_unique = models.Constraint(
        "UNIQUE(provider_id, model_type, name)",
        "The model ID must be unique per provider and model type.",
    )
    _embedding_name_unique = models.UniqueIndex("(name) WHERE model_type = 'embedding'")

    @api.constrains("active", "embedding_dimensions", "model_type", "provider_id")
    def _check_embedding_dimensions(self):
        for model in self:
            if model.model_type == "embedding" and model.embedding_dimensions != 1536:
                raise ValidationError(self.env._(
                    "Odoo AI requires embedding models that return exactly 1536 dimensions."
                ))
            company_domain = fields.Domain("ibt_ai_enabled", "=", True) & (
                fields.Domain("ibt_ai_chat_model_id", "=", model.id)
                | fields.Domain("ibt_ai_reasoning_model_id", "=", model.id)
                | fields.Domain("ibt_ai_embedding_model_id", "=", model.id)
            )
            if not model.active and self.env["res.company"].sudo().search_count(company_domain, limit=1):
                raise ValidationError(self.env._(
                    "An AI model selected by an enabled company cannot be archived."
                ))
            chat_companies = self.env["res.company"].sudo().search([
                "|",
                ("ibt_ai_chat_model_id", "=", model.id),
                ("ibt_ai_reasoning_model_id", "=", model.id),
            ])
            if chat_companies and (
                model.model_type != "chat"
                or any(company.ibt_ai_provider_id != model.provider_id for company in chat_companies)
            ):
                raise ValidationError(self.env._(
                    "A selected chat or reasoning model must remain a chat model of its configured provider."
                ))
            embedding_companies = self.env["res.company"].sudo().search([
                ("ibt_ai_embedding_model_id", "=", model.id),
            ])
            if embedding_companies and (
                model.model_type != "embedding"
                or any(
                    (company.ibt_ai_embedding_provider_id or company.ibt_ai_provider_id) != model.provider_id
                    for company in embedding_companies
                )
            ):
                raise ValidationError(self.env._(
                    "A selected embedding model must remain an embedding model of its configured provider."
                ))

    @api.depends("name", "label")
    def _compute_display_name(self):
        for model in self:
            model.display_name = f"{model.label} ({model.name})" if model.label else model.name
