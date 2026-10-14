from odoo import Command
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
