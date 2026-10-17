from unittest.mock import patch

from odoo import Command
from odoo.tests.common import TransactionCase

from ..services.client import ProviderClient


class TestIbtAiEmbedding(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Embedding provider",
            "base_url": "https://embedding.example.com/v1",
            "model_ids": [Command.create({
                "name": "company-embedding-model",
                "model_type": "embedding",
            })],
        })
        cls.embedding_model = cls.provider.model_ids

    @patch.object(ProviderClient, "embeddings", return_value={"embeddings": [[0.0] * 1536]})
    def test_requested_model_selects_its_provider(self, embeddings):
        result = self.env["ai.embedding"]._get_embeddings(
            [{"content": "text"}], self.embedding_model.name, "document",
        )

        self.assertEqual(len(result["embeddings"][0]), 1536)
        self.assertEqual(embeddings.call_args.kwargs["model"], self.embedding_model.name)
        self.assertEqual(embeddings.call_args.args[0], [{"content": "text"}])

    def test_supported_models_include_custom_models(self):
        supported = self.env["ai.embedding"]._get_supported_embedding_models()

        self.assertIn(self.embedding_model.name, supported)


class TestIbtAiEmbeddingSwitch(TransactionCase):
    """With a custom embedding model, agents and sources leave Odoo's IAP models."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Company provider",
            "base_url": "https://ai.example.com/v1",
            "model_ids": [
                Command.create({"name": "company-chat-model", "model_type": "chat"}),
                Command.create({"name": "company-embedding-model", "model_type": "embedding"}),
            ],
        })
        chat, embedding = cls.provider.model_ids.sorted("id")
        cls.env.company.write({
            "ibt_ai_enabled": True,
            "ibt_ai_provider_id": cls.provider.id,
            "ibt_ai_chat_model_id": chat.id,
            "ibt_ai_embedding_model_id": embedding.id,
        })
        cls.agent = cls.env["ai.agent"].create({"name": "Agent with an old model"})
        cls.agent.embedding_model = "gemini-embedding-2"
        cls.embedding = cls.env["ai.embedding"].create({
            "res_model": "res.partner", "res_id": cls.env.user.partner_id.id, "content": "chunk",
            "embedding_model": "gemini-embedding-2", "embedding_vector": [0.1] * 1536,
        })

    def test_only_custom_models_are_supported(self):
        with patch("odoo.addons.ai.models.ai_embedding.call_odoo_ai") as call_odoo_ai:
            supported = self.env["ai.embedding"]._get_supported_embedding_models()
        self.assertEqual(supported, ["company-embedding-model"])
        call_odoo_ai.assert_not_called()

    def test_deprecated_model_cron_moves_agents_and_sources(self):
        self.assertTrue(self.env["ai.embedding"]._ibt_has_foreign_embedding_models())
        # The cron commits its progress; that is Odoo's code, not under test here.
        with patch.object(type(self.env["ir.cron"]), "_commit_progress", return_value=True):
            self.env["ai.embedding"]._cron_update_deprecated_embedding_models()
        self.assertEqual(self.agent.embedding_model, "company-embedding-model")
        self.assertEqual(self.embedding.embedding_model, "company-embedding-model")
        self.assertFalse(self.embedding.embedding_vector)
        self.assertFalse(self.env["ai.embedding"]._ibt_has_foreign_embedding_models())

    def test_saving_settings_starts_the_switch(self):
        cron = self.env.ref("ai.ir_cron_update_deprecated_embedding_models")
        triggered = []
        original = type(cron)._trigger

        def record(crons, *args, **kwargs):
            triggered.extend(crons.ids)
            return original(crons, *args, **kwargs)

        with patch.object(type(cron), "_trigger", record):
            self.env["res.config.settings"].create({}).execute()
        self.assertIn(cron.id, triggered)

    def test_disabled_provider_keeps_odoo_models(self):
        self.env.company.ibt_ai_enabled = False
        supported = self.env["ai.embedding"]._get_supported_embedding_models()
        # Odoo's own list (a placeholder during tests) is offered again.
        self.assertIn("dummy", supported)
        self.assertIn("company-embedding-model", supported)
