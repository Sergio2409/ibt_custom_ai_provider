from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestIbtAiCompanyConfig(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Restricted provider",
            "base_url": "https://ai.example.com/v1",
            "model_ids": [
                Command.create({"name": "chat-model", "model_type": "chat"}),
                Command.create({"name": "embedding-model", "model_type": "embedding"}),
            ],
        })
        cls.chat_model = cls.provider.model_ids.filtered(lambda model: model.model_type == "chat")
        cls.embedding_model = cls.provider.model_ids.filtered(lambda model: model.model_type == "embedding")
        cls.company = cls.env.company
        cls.company.write({
            "ibt_ai_enabled": True,
            "ibt_ai_provider_id": cls.provider.id,
            "ibt_ai_chat_model_id": cls.chat_model.id,
            "ibt_ai_embedding_provider_id": cls.provider.id,
            "ibt_ai_embedding_model_id": cls.embedding_model.id,
        })
        cls.ai_user = cls.env["res.users"].create({
            "name": "AI user without provider administration",
            "login": "ibt-ai-user",
            "company_id": cls.company.id,
            "company_ids": [Command.set(cls.company.ids)],
            "group_ids": [Command.set([cls.env.ref("base.group_user").id])],
        })

    def test_regular_user_can_build_chat_provider_snapshot(self):
        config = self.company.with_user(self.ai_user)._ibt_ai_config()

        self.assertEqual(config["id"], self.provider.id)
        self.assertEqual(config["chat_model"], self.chat_model.name)
        self.assertNotIn(self.env.ref("base.group_system"), self.ai_user.group_ids)

    def test_regular_user_can_build_embedding_provider_snapshot(self):
        config = self.company.with_user(self.ai_user)._ibt_ai_embedding_config()

        self.assertEqual(config["id"], self.provider.id)
        self.assertEqual(config["embedding_model"], self.embedding_model.name)

    def test_chat_field_rejects_embedding_model(self):
        with self.assertRaisesRegex(ValidationError, "must be a chat model"):
            with self.env.cr.savepoint():
                self.company.ibt_ai_chat_model_id = self.embedding_model

    def test_embedding_field_rejects_chat_model(self):
        with self.assertRaisesRegex(ValidationError, "must be an embedding model"):
            with self.env.cr.savepoint():
                self.company.ibt_ai_embedding_model_id = self.chat_model

    def test_anthropic_provider_cannot_supply_embeddings(self):
        provider = self.env["ibt.ai.provider"].create({
            "name": "Anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "transport": "anthropic",
            "model_ids": [Command.create({
                "name": "invalid-anthropic-embedding",
                "model_type": "embedding",
            })],
        })
        with self.assertRaisesRegex(ValidationError, "cannot be used for embeddings"):
            with self.env.cr.savepoint():
                self.company.write({
                    "ibt_ai_embedding_provider_id": provider.id,
                    "ibt_ai_embedding_model_id": provider.model_ids.id,
                })

    def test_ollama_requires_openai_transport(self):
        with self.assertRaisesRegex(ValidationError, "OpenAI-compatible transport"):
            with self.env.cr.savepoint():
                self.env["ibt.ai.provider"].create({
                    "name": "Invalid Ollama",
                    "base_url": "https://ollama.example.com/v1",
                    "provider_kind": "ollama",
                    "transport": "anthropic",
                })

    def test_selected_provider_cannot_be_archived(self):
        with self.assertRaisesRegex(ValidationError, "cannot be archived"):
            with self.env.cr.savepoint():
                self.provider.active = False

    def _save_settings(self, **values):
        self.env["res.config.settings"].create(values).execute()

    def test_settings_page_enables_provider_and_models_at_once(self):
        self.company.write({field: False for field in (
            "ibt_ai_enabled", "ibt_ai_provider_id", "ibt_ai_chat_model_id",
            "ibt_ai_embedding_provider_id", "ibt_ai_embedding_model_id",
        )})
        self._save_settings(
            ibt_ai_enabled=True,
            ibt_ai_provider_id=self.provider.id,
            ibt_ai_chat_model_id=self.chat_model.id,
            ibt_ai_reasoning_model_id=self.chat_model.id,
            ibt_ai_embedding_provider_id=self.provider.id,
            ibt_ai_embedding_model_id=self.embedding_model.id,
        )
        self.assertEqual(self.company._ibt_ai_config()["chat_model"], "chat-model")
        self.assertEqual(self.company._ibt_ai_embedding_config()["embedding_model"], "embedding-model")

    def test_settings_page_switches_provider_and_models_at_once(self):
        other = self.env["ibt.ai.provider"].create({
            "name": "Other provider",
            "base_url": "https://other.example.com/v1",
            "model_ids": [
                Command.create({"name": "other-chat", "model_type": "chat"}),
                Command.create({"name": "other-embedding", "model_type": "embedding"}),
            ],
        })
        other_chat = other.model_ids.filtered(lambda model: model.model_type == "chat")
        other_embedding = other.model_ids.filtered(lambda model: model.model_type == "embedding")
        self._save_settings(
            ibt_ai_enabled=True,
            ibt_ai_provider_id=other.id,
            ibt_ai_chat_model_id=other_chat.id,
            ibt_ai_embedding_provider_id=other.id,
            ibt_ai_embedding_model_id=other_embedding.id,
        )
        self.assertEqual(self.company.ibt_ai_chat_model_id, other_chat)
        self.assertEqual(self.company.ibt_ai_embedding_model_id, other_embedding)

    def test_settings_page_keeps_values_it_does_not_send(self):
        self._save_settings(ibt_ai_reasoning_model_id=self.chat_model.id)
        self.assertEqual(self.company.ibt_ai_provider_id, self.provider)
        self.assertEqual(self.company.ibt_ai_reasoning_model_id, self.chat_model)
        self.assertEqual(self.company.ibt_ai_embedding_model_id, self.embedding_model)

    def test_settings_page_still_rejects_a_foreign_model(self):
        other = self.env["ibt.ai.provider"].create({
            "name": "Other provider",
            "base_url": "https://other.example.com/v1",
        })
        with self.assertRaisesRegex(ValidationError, "must belong to the selected chat provider"):
            with self.env.cr.savepoint():
                self._save_settings(ibt_ai_provider_id=other.id, ibt_ai_chat_model_id=self.chat_model.id)

    def test_selected_model_cannot_be_archived(self):
        with self.assertRaisesRegex(ValidationError, "cannot be archived"):
            with self.env.cr.savepoint():
                self.chat_model.active = False
