import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL, sql

from ..services import secret, speech
from ..services.client import ProviderClient, ProviderError, validate_endpoint

_logger = logging.getLogger(__name__)

DEFAULT_VOICES = {"es": "ef_dora", "other": "af_heart"}


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
        [("generic", "Generic"), ("ollama", "Ollama"), ("speech_gateway", "IBT Speech Gateway")],
        required=True,
        default="generic",
        help="Select Ollama to enable discovery of locally installed models. "
             "Select IBT Speech Gateway for speech-to-text (ibt_ai_speech_gateway); "
             "such a provider is used only for transcription.",
    )
    base_url = fields.Char(required=True)
    # Write-only: the key is encrypted into the api_key_secret column, which
    # is not a field (see init()), so no read, export or copy can return it.
    api_key = fields.Char(
        compute="_compute_api_key", inverse="_inverse_api_key", groups="base.group_system", copy=False,
        help="Type a key to store it encrypted; a new key replaces the stored one. "
             "The key is never displayed again.",
    )
    api_key_set = fields.Boolean(string="API Key Stored", compute="_compute_api_key_set")
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
    voice_ids = fields.One2many("ibt.ai.provider.voice", "provider_id", context={"active_test": False})
    note = fields.Text()

    def init(self):
        super().init()
        self.env.cr.execute(SQL(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS api_key_secret varchar", SQL.identifier(self._table),
        ))

    def _compute_api_key(self):
        self.api_key = False

    def _inverse_api_key(self):
        for provider in self:
            provider._ibt_store_api_key(provider.api_key)

    def _compute_api_key_set(self):
        self.env.cr.execute(SQL(
            "SELECT id FROM %s WHERE id IN %s AND api_key_secret IS NOT NULL",
            SQL.identifier(self._table), tuple(self._origin.ids) or (None,),
        ))
        stored = {row[0] for row in self.env.cr.fetchall()}
        for provider in self:
            provider.api_key_set = provider._origin.id in stored

    def _ibt_store_api_key(self, key):
        self.ensure_one()
        self.env.cr.execute(SQL(
            "UPDATE %s SET api_key_secret = %s WHERE id = %s",
            SQL.identifier(self._table), secret.encrypt(key) if key else None, self.id,
        ))
        # Also drop the typed key from the cache, so that the saved record
        # read back by the form does not return it.
        self.invalidate_recordset(["api_key", "api_key_set"])
        _logger.info("API key %s for AI provider %s (#%s) by %s",
                     "stored" if key else "removed", self.name, self.id, self.env.user.login)

    def _ibt_api_key(self):
        self.ensure_one()
        self.env.cr.execute(SQL(
            "SELECT api_key_secret FROM %s WHERE id = %s", SQL.identifier(self._table), self.id,
        ))
        token = (self.env.cr.fetchone() or [None])[0]
        return secret.decrypt(token) if token else ""

    def action_remove_api_key(self):
        for provider in self:
            provider._ibt_store_api_key(False)

    @api.model
    def _ibt_rotate_api_keys(self):
        """Re-encrypt every stored key with the first master key (see README)."""
        self.env.cr.execute(SQL(
            "SELECT id, api_key_secret FROM %s WHERE api_key_secret IS NOT NULL", SQL.identifier(self._table),
        ))
        rows = self.env.cr.fetchall()
        for provider_id, token in rows:
            self.env.cr.execute(SQL(
                "UPDATE %s SET api_key_secret = %s WHERE id = %s",
                SQL.identifier(self._table), secret.rotate(token), provider_id,
            ))
        _logger.info("Re-encrypted %s AI provider API keys with the current master key", len(rows))
        return len(rows)

    @api.model
    def _ibt_encrypt_plain_api_keys(self):
        """Move keys stored in clear by versions before 20.0.1.2.0 into api_key_secret."""
        if not sql.column_exists(self.env.cr, self._table, "api_key"):
            return
        self.env.cr.execute(SQL(
            "SELECT id, api_key FROM %s WHERE api_key IS NOT NULL AND api_key != ''", SQL.identifier(self._table),
        ))
        # Fails (and so stops the upgrade) when no master key is configured:
        # keys are never left in clear silently.
        for provider_id, key in self.env.cr.fetchall():
            self.env.cr.execute(SQL(
                "UPDATE %s SET api_key_secret = %s WHERE id = %s",
                SQL.identifier(self._table), secret.encrypt(key), provider_id,
            ))
        self.env.cr.execute(SQL("ALTER TABLE %s DROP COLUMN api_key", SQL.identifier(self._table)))

    @api.constrains("active", "base_url", "allow_private_network", "timeout", "provider_kind", "transport")
    def _check_connection_settings(self):
        for provider in self:
            if not 1 <= provider.timeout <= 1800:
                raise ValidationError(self.env._("Timeout must be between 1 and 1800 seconds."))
            if provider.provider_kind == "ollama" and provider.transport != "openai":
                raise ValidationError(self.env._("Ollama providers must use the OpenAI-compatible transport."))
            if provider.provider_kind == "speech_gateway" and provider.transport != "openai":
                raise ValidationError(self.env._("Speech gateway providers must use the OpenAI-compatible transport."))
            Company = self.env["res.company"].sudo()
            if provider.provider_kind == "speech_gateway" and Company.search_count([
                "|", ("ibt_ai_provider_id", "=", provider.id), ("ibt_ai_embedding_provider_id", "=", provider.id),
            ], limit=1):
                raise ValidationError(self.env._("A speech gateway cannot be used for chat or embeddings."))
            speech_domain = fields.Domain("ibt_speech_enabled", "=", True) & fields.Domain("ibt_speech_provider_id", "=", provider.id)
            if provider.provider_kind != "speech_gateway" and Company.search_count(
                fields.Domain("ibt_speech_provider_id", "=", provider.id), limit=1,
            ):
                raise ValidationError(self.env._("A provider selected as speech gateway must stay of kind IBT Speech Gateway."))
            if not provider.active and Company.search_count(speech_domain, limit=1):
                raise ValidationError(self.env._("A speech gateway selected by a company cannot be archived."))
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
            "api_key": self._ibt_api_key(),
            "allow_private_network": self.allow_private_network,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "send_embedding_dimensions": self.send_embedding_dimensions,
            "chat_model": chat_model.name if chat_model else "",
            "supports": {kind: chat_model[f"supports_{kind}"] for kind in ("tools", "vision", "pdf")} if chat_model else {},
            "embedding_model": embedding_model.name if embedding_model else "",
        }

    def action_test_connection(self):
        self.ensure_one()
        chat_model = self.model_ids.filtered(lambda model: model.model_type == "chat")[:1]
        ProviderClient(self._snapshot(chat_model=chat_model)).test_connection()
        message = self.env._("The AI provider is reachable.")
        notification_type = "success"
        if self.provider_kind == "speech_gateway":
            # Keep the voice list current; voices need the key's tts route.
            try:
                result = self._ibt_sync_voices()
                message += " " + self.env._("%(total)s voices available.", total=result["total"])
            except ProviderError:
                message += " " + self.env._(
                    "Voices could not be loaded: the key needs the gateway's tts route for voice replies."
                )
                notification_type = "warning"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Connection successful"),
                "message": message,
                "type": notification_type,
            },
        }

    def action_discover_voices(self):
        self.ensure_one()
        if self.provider_kind != "speech_gateway":
            raise ValidationError(self.env._("Voices are available only for IBT Speech Gateway providers."))
        result = self._ibt_sync_voices()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Speech gateway voices"),
                "message": self.env._(
                    "%(total)s voices: %(created)s added, %(archived)s no longer offered (archived).",
                    **result,
                ),
                "type": "success",
            },
        }

    def _ibt_sync_voices(self):
        """Mirror the gateway's voices; pick the default voices for companies that have none."""
        self.ensure_one()
        offered = {voice["name"]: voice for voice in speech.list_voices(self._snapshot())}
        Voice = self.env["ibt.ai.provider.voice"].with_context(active_test=False)
        existing = {voice.name: voice for voice in Voice.search([("provider_id", "=", self.id)])}
        created = 0
        for name, values in offered.items():
            values = dict(values, active=True)
            if name in existing:
                existing[name].write(values)
            else:
                Voice.create(dict(values, provider_id=self.id))
                created += 1
        gone = Voice.browse([voice.id for name, voice in existing.items() if name not in offered and voice.active])
        gone.active = False
        voices = Voice.search([("provider_id", "=", self.id), ("active", "=", True)])
        by_name = {voice.name: voice for voice in voices}
        spanish = voices.filtered(lambda voice: voice.language.startswith("es"))
        for company in self.env["res.company"].sudo().search([("ibt_speech_provider_id", "=", self.id)]):
            values = {}
            if company.ibt_speech_voice_es_id not in spanish:
                values["ibt_speech_voice_es_id"] = (by_name.get(DEFAULT_VOICES["es"]) or spanish[:1]).id
            if company.ibt_speech_voice_en_id not in voices:
                values["ibt_speech_voice_en_id"] = (by_name.get(DEFAULT_VOICES["other"]) or voices[:1]).id
            if values:
                company.write(values)
        return {"total": len(offered), "created": created, "archived": len(gone)}

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
                "supports_pdf": False,
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


class IbtAiProviderVoice(models.Model):
    _name = "ibt.ai.provider.voice"
    _description = "IBT Speech Gateway Voice"
    _order = "provider_id, language, name"
    _rec_names_search = ["name", "language"]

    name = fields.Char(string="Voice", required=True, readonly=True)
    language = fields.Char(readonly=True, help="Language code reported by the gateway, e.g. es, en-us.")
    gender = fields.Selection([("female", "Female"), ("male", "Male")], readonly=True)
    active = fields.Boolean(default=True)
    provider_id = fields.Many2one("ibt.ai.provider", required=True, ondelete="cascade", index=True)

    _provider_name_unique = models.Constraint(
        "UNIQUE(provider_id, name)",
        "A voice is listed once per speech gateway.",
    )

    @api.depends("name", "language", "gender")
    def _compute_display_name(self):
        for voice in self:
            details = ", ".join(filter(None, [voice.language, voice.gender and dict(
                voice._fields["gender"]._description_selection(voice.env)).get(voice.gender)]))
            voice.display_name = f"{voice.name} ({details})" if details else voice.name


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
    # Checked before each request; Ollama discovery sets them, administrators
    # may override them (e.g. for a generic OpenAI-compatible endpoint).
    supports_tools = fields.Boolean(default=True)
    supports_vision = fields.Boolean(string="Supports Images", default=True)
    supports_pdf = fields.Boolean(string="Supports PDF", default=True)

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
