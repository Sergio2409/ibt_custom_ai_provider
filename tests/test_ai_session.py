import math
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user

from ..services.client import CapabilityError, ProviderClient, ProviderError


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

    def test_changed_company_access_finishes_request_without_raising(self):
        request_user = new_test_user(
            self.env,
            login="restricted_ai_user",
            groups="base.group_user",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        other_company = self.env["res.company"].create({"name": "Former company"})
        session = self._make_session("waiting_model")
        session.write({
            "request_user_id": request_user.id,
            "request_context": {"allowed_company_ids": [other_company.id]},
        })
        completion = {"kind": "success", "message": {"role": "assistant", "content": []}}

        with patch.object(type(session), "_finish_exchange") as finish_exchange:
            session._deliver_ibt_ai_result(completion)

        finish_exchange.assert_called_once()
        self.assertIn("company access changed", finish_exchange.call_args.kwargs["content"])


class TestIbtAiQueue(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Queue provider",
            "base_url": "https://ai.example.com/v1",
            "model_ids": [Command.create({"name": "chat-model", "model_type": "chat"})],
        })
        cls.env.company.write({
            "ibt_ai_enabled": True,
            "ibt_ai_provider_id": cls.provider.id,
            "ibt_ai_chat_model_id": cls.provider.model_ids.id,
        })
        cls.session = cls.env["ai.session"].create({
            "state": {"callback_type": "agent_loop"},
            "loop_state": "waiting_model",
            "request_uuid": "queue-request",
            "request_webhook_secret": "queue-secret",
            "request_payload": {"messages": []},
            "request_round": 1,
            "request_round_limit": 30,
            "request_user_id": cls.env.user.id,
            "request_context": {},
            "ibt_ai_provider_id": cls.provider.id,
            "ibt_ai_company_id": cls.env.company.id,
            "ibt_ai_queued_at": fields.Datetime.now(),
        })

    def _run_cron(self, budget=math.inf):
        with patch.object(type(self.env["ir.cron"]), "_commit_progress", return_value=budget):
            self.env["ai.session"]._cron_process_ibt_ai_requests()

    def _failure(self, status):
        error = ProviderError("provider down")
        error.status_code = status
        error.transient = status == 503
        error.category = {503: "unavailable", 429: "throttled"}.get(status, "configuration")
        return error

    def test_transient_failure_is_retried_on_a_later_run(self):
        with patch.object(ProviderClient, "completions", side_effect=self._failure(503)), \
                patch.object(type(self.session), "_finish_exchange") as finish:
            self._run_cron()
        finish.assert_not_called()
        self.assertEqual(self.session.loop_state, "waiting_model")
        self.assertEqual(self.session.ibt_ai_provider_id, self.provider)
        self.assertEqual(self.session.ibt_ai_attempts, 1)
        self.assertGreater(self.session.ibt_ai_next_attempt, fields.Datetime.now())
        with patch.object(ProviderClient, "completions") as completions:
            self._run_cron()
        completions.assert_not_called()

    def test_cancelled_request_is_not_sent(self):
        self.session.loop_state = "ready"
        with patch.object(ProviderClient, "completions") as completions:
            self._run_cron()
        completions.assert_not_called()

    def test_permanent_failure_fails_at_once(self):
        with patch.object(ProviderClient, "completions", side_effect=self._failure(401)), \
                patch.object(type(self.session), "_finish_exchange") as finish:
            self._run_cron()
        self.assertEqual(finish.call_args.args, ("failed",))

    def test_request_waiting_too_long_fails_without_calling_the_provider(self):
        self.session.ibt_ai_queued_at = fields.Datetime.now() - timedelta(minutes=11)
        with patch.object(ProviderClient, "completions") as completions, \
                patch.object(type(self.session), "_finish_exchange") as finish:
            self._run_cron()
        completions.assert_not_called()
        self.assertEqual(finish.call_args.args, ("failed",))

    def test_cron_budget_bounds_the_request_and_the_batch(self):
        answer = {"result": {"role": "assistant", "content": []}}
        with patch.object(ProviderClient, "completions", autospec=True, return_value=answer) as completions, \
                patch.object(type(self.session), "_continue_agent_loop"):
            self._run_cron(budget=20)
            completions.assert_not_called()
            self._run_cron(budget=200)
        deadline = completions.call_args.args[0].config["deadline"]
        self.assertLessEqual(deadline, time.monotonic() + 170)

    def test_unsupported_request_tells_the_user_why(self):
        with patch.object(ProviderClient, "completions", side_effect=CapabilityError("The AI model x cannot handle images.")), \
                patch.object(type(self.session), "_finish_exchange") as finish:
            self._run_cron()
        self.assertEqual(finish.call_args.kwargs["content"], "The AI model x cannot handle images.")
        self.assertEqual(self.session.ibt_ai_attempts, 0)

    def _answer(self, client, *args, **kwargs):
        client.usage, client.request_id = {"prompt_tokens": 1000, "completion_tokens": 500}, "req-42"
        return {"result": {"role": "assistant", "content": []}}

    def _rows(self):
        return self.env["ibt.ai.request.log"].search([("request_uuid", "=", "queue-request")], order="id")

    def test_request_log_records_each_attempt_without_content(self):
        self.provider.model_ids.write({"price_input": 2.0, "price_output": 10.0})
        with patch.object(ProviderClient, "completions", side_effect=self._failure(503)):
            self._run_cron()
        self.session.ibt_ai_next_attempt = False
        with patch.object(ProviderClient, "completions", autospec=True, side_effect=self._answer), \
                patch.object(type(self.session), "_continue_agent_loop"):
            self._run_cron()
        retry, success = self._rows()
        self.assertEqual((retry.outcome, retry.category, retry.status_code, retry.attempt), ("retrying", "unavailable", 503, 1))
        self.assertEqual((success.outcome, success.attempt, success.provider_request_id), ("success", 2, "req-42"))
        self.assertEqual((success.input_tokens, success.output_tokens, success.estimated_cost), (1000, 500, 0.007))
        self.assertEqual((success.company_id, success.user_id, success.model_name),
                         (self.env.company, self.env.user, "chat-model"))
        self.assertNotIn("provider down", str(retry.read()))
        self.assertEqual(self.provider.last_success_at, success.create_date)

    def test_request_log_records_failures_and_give_up(self):
        with patch.object(ProviderClient, "completions", side_effect=self._failure(401)), \
                patch.object(type(self.session), "_finish_exchange"):
            self._run_cron()
        self.assertEqual(self._rows().mapped("category"), ["configuration"])
        self.session.write({"loop_state": "waiting_model", "ibt_ai_provider_id": self.provider.id,
                            "ibt_ai_queued_at": fields.Datetime.now() - timedelta(minutes=11)})
        with patch.object(type(self.session), "_finish_exchange"):
            self._run_cron()
        self.assertEqual(self._rows().mapped("category"), ["configuration", "expired"])
        self.assertEqual(set(self._rows().mapped("outcome")), {"failed"})

    def test_request_log_covers_embeddings(self):
        model = self.env["ibt.ai.provider.model"].create({
            "provider_id": self.provider.id, "name": "embed-model", "model_type": "embedding",
        })
        Embedding, Log = self.env["ai.embedding"], self.env["ibt.ai.request.log"]
        with patch.object(ProviderClient, "embeddings", return_value=[[0.0] * 1536]):
            Embedding._get_embeddings(["text"], model.name, "query")
        self.assertEqual(Log.search([("kind", "=", "embedding")]).mapped("outcome"), ["success"])
        # A caller that rolls back loses the failure's row, never its log line.
        with patch.object(ProviderClient, "embeddings", side_effect=self._failure(429)), \
                self.assertLogs("odoo.addons.ibt_custom_ai_provider.models.ai_request_log", "WARNING") as logs, \
                self.assertRaises(ProviderError):
            Embedding._get_embeddings(["text"], model.name, "query")
        self.assertIn("kind=embedding", logs.output[0])
        self.assertIn("outcome=failed category=throttled status=429", logs.output[0])

    def test_request_log_is_for_administrators_and_kept_90_days(self):
        Log = self.env["ibt.ai.request.log"]
        old, recent = (Log.create({"kind": "chat", "provider_id": self.provider.id, "outcome": "success"}) for _ in range(2))
        self.env.cr.execute("UPDATE ibt_ai_request_log SET create_date = now() - interval '91 days' WHERE id = %s", [old.id])
        old.invalidate_recordset()
        Log._gc_old_rows()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        user = new_test_user(self.env, login="ibt-log-user", groups="base.group_user")
        with self.assertRaises(AccessError):
            recent.with_user(user).read(["outcome"])
