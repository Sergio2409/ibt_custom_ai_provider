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
