from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestIbtAiSession(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Test provider",
            "base_url": "https://ai.example.com/v1",
        })

    def _make_session(self, loop_state):
        return self.env["ai.session"].create({
            "state": {"callback_type": "agent_loop"},
            "loop_state": loop_state,
            "request_uuid": "test-request",
            "request_webhook_secret": "test-secret",
            "request_payload": {"messages": []},
            "request_round": 1,
            "request_round_limit": 30,
            "request_user_id": self.env.user.id,
            "request_context": {},
            "ibt_ai_provider_id": self.provider.id,
            "ibt_ai_company_id": self.env.company.id,
        })

    def test_follow_up_model_round_keeps_provider(self):
        session = self._make_session("waiting_model")
        completion = {"kind": "success", "message": {"role": "assistant", "content": []}}

        with patch.object(type(session), "_continue_agent_loop"):
            session._deliver_ibt_ai_result(completion)

        self.assertEqual(session.ibt_ai_provider_id, self.provider)

    def test_finished_agent_round_releases_provider(self):
        session = self._make_session("ready")
        completion = {"kind": "success", "message": {"role": "assistant", "content": []}}

        with patch.object(type(session), "_continue_agent_loop"):
            session._deliver_ibt_ai_result(completion)

        self.assertFalse(session.ibt_ai_provider_id)

    def test_provider_failure_does_not_expose_connection_details(self):
        session = self._make_session("waiting_model")
        completion = {"kind": "failure", "code": "provider_request_failed"}
        technical_error = "HTTPConnectionPool(host='internal.example', port=11434)"

        with patch.object(type(session), "_finish_exchange") as finish_exchange:
            session._deliver_ibt_ai_result(completion, technical_error)

        user_message = finish_exchange.call_args.kwargs["content"]
        self.assertNotIn("internal.example", user_message)
        self.assertNotIn("11434", user_message)
        self.assertIn("temporarily unavailable", user_message)
