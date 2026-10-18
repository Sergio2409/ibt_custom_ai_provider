import io
import json
import time
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase

from ..services import client as client_module
from ..services.client import ProviderClient, ProviderError, _to_anthropic_messages, _to_openai_messages, validate_endpoint

SESSION_REQUEST = "odoo.addons.ibt_custom_ai_provider.services.client.requests.Session.request"
SLEEP = "odoo.addons.ibt_custom_ai_provider.services.client.time.sleep"


def http_response(status=200, body=None, headers=None):
    response = requests.Response()
    response.status_code = status
    response.reason = "Test"
    response.headers.update(headers or {})
    response.raw = io.BytesIO(json.dumps({"data": []} if body is None else body).encode())
    return response


class TestProviderClient(TransactionCase):
    def test_openai_tool_round_trip_mapping(self):
        messages = [
            {"role": "assistant", "content": [{"type": "tool_call", "name": "find_record", "args": {"id": 7}, "call_id": "call_1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call_1", "success": True, "result": [{"type": "text", "text": "Found"}]}]},
        ]
        converted = _to_openai_messages(messages, "system")
        self.assertEqual(converted[1]["content"], "")
        self.assertEqual(converted[1]["tool_calls"][0]["function"]["name"], "find_record")
        self.assertEqual(converted[2]["tool_call_id"], "call_1")

    def test_anthropic_tool_round_trip_mapping(self):
        messages = [{"role": "assistant", "content": [{"type": "tool_call", "name": "find_record", "args": {"id": 7}, "call_id": "call_1"}]}]
        converted = _to_anthropic_messages(messages)
        self.assertEqual(converted[0]["content"][0]["type"], "tool_use")

    @patch("odoo.addons.ibt_custom_ai_provider.services.client.socket.getaddrinfo")
    def test_private_endpoint_requires_opt_in(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("192.168.1.10", 11434))]
        with self.assertRaises(ProviderError):
            validate_endpoint("http://ollama.local:11434/v1", allow_private=False)

    def test_invalid_endpoint_port_is_reported_as_provider_error(self):
        with self.assertRaisesRegex(ProviderError, "invalid port"):
            validate_endpoint("https://ai.example.com:invalid/v1", resolve=False)

    @patch("odoo.addons.ibt_custom_ai_provider.services.client.socket.getaddrinfo")
    def test_non_global_endpoint_requires_opt_in(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("100.64.0.1", 443))]
        with self.assertRaises(ProviderError):
            validate_endpoint("https://provider.example.com/v1")

    @patch("odoo.addons.ibt_custom_ai_provider.services.client.requests.Session")
    @patch("odoo.addons.ibt_custom_ai_provider.services.client.socket.getaddrinfo")
    def test_request_connects_to_validated_address(self, getaddrinfo, Session):
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        response = MagicMock(ok=True, is_redirect=False)
        response.json.return_value = {"data": "ok"}
        session = Session.return_value.__enter__.return_value
        session.request.return_value = response
        client = ProviderClient({
            "transport": "openai",
            "base_url": "https://provider.example.com/v1",
        })

        client._request("GET", "models")

        request = session.request.call_args
        self.assertEqual(request.args[:2], ("GET", "https://93.184.216.34/v1/models"))
        self.assertEqual(request.kwargs["headers"]["Host"], "provider.example.com")
        self.assertFalse(session.trust_env)

    @patch("odoo.addons.ibt_custom_ai_provider.services.client.requests.Session")
    @patch("odoo.addons.ibt_custom_ai_provider.services.client.socket.getaddrinfo")
    @patch(SLEEP)
    def test_proxy_error_page_becomes_a_clear_message(self, sleep, getaddrinfo, Session):
        getaddrinfo.return_value = [(2, 1, 6, "", ("192.168.10.12", 443))]
        response = MagicMock(ok=False, is_redirect=False, status_code=502, reason="Bad Gateway",
                             headers={"Content-Type": "text/html"},
                             text="<html><head><title>502 Bad Gateway</title></head></html>")
        response.json.side_effect = ValueError
        Session.return_value.__enter__.return_value.request.return_value = response
        client = ProviderClient({
            "transport": "openai",
            "base_url": "https://speech.example.com/v1",
            "allow_private_network": True,
        })
        with self.assertRaises(ProviderError) as ctx:
            client.test_connection()
        message = str(ctx.exception)
        self.assertIn("not responding (HTTP 502: Bad Gateway)", message)
        self.assertNotIn("<html>", message)
        self.assertEqual(ctx.exception.status_code, 502)

    @patch.object(ProviderClient, "_request")
    def test_ollama_model_discovery(self, request):
        request.side_effect = [
            {"models": [
                {"model": "qwen3:8b", "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"}},
                {"model": "embedding-model:latest", "details": {}},
            ]},
            {"capabilities": ["completion", "tools"]},
            {"capabilities": ["embedding"], "model_info": {"bert.embedding_length": 1536}},
        ]
        client = ProviderClient({
            "provider_kind": "ollama",
            "base_url": "http://localhost:11434/v1",
            "allow_private_network": True,
        })
        models = client.discover_ollama_models()
        self.assertEqual(models[0]["model_type"], "chat")
        self.assertIn("tools", models[0]["capabilities"])
        self.assertEqual(models[1]["model_type"], "embedding")
        self.assertEqual(models[1]["embedding_dimensions"], 1536)

    @patch.object(ProviderClient, "discover_ollama_models")
    def test_discovery_imports_models_and_skips_incompatible_embeddings(self, discover):
        discover.return_value = [
            {"name": "qwen3:8b", "label": "qwen3:8b · 8B", "model_type": "chat", "capabilities": ["completion", "tools"]},
            {"name": "nomic-embed-text", "label": "nomic-embed-text", "model_type": "embedding", "embedding_dimensions": 768, "capabilities": ["embedding"]},
        ]
        provider = self.env["ibt.ai.provider"].create({
            "name": "Test Ollama",
            "transport": "openai",
            "provider_kind": "ollama",
            "base_url": "http://localhost:11434/v1",
            "allow_private_network": True,
        })
        action = provider.action_discover_models()
        model = provider.model_ids
        self.assertEqual(model.name, "qwen3:8b")
        self.assertTrue(model.supports_tools)
        self.assertIn("1 incompatible embedding model", action["params"]["message"])



@patch(SLEEP)
class TestProviderClientResilience(TransactionCase):

    def setUp(self):
        super().setUp()
        self.client = ProviderClient({
            "transport": "openai",
            "base_url": "http://127.0.0.1:9/v1",
            "allow_private_network": True,
            "chat_model": "chat-model",
        })

    def test_connection_failure_is_retried(self, sleep):
        with patch(SESSION_REQUEST, side_effect=[requests.ConnectionError("refused"), http_response()]) as request:
            self.assertEqual(self.client._request("GET", "models"), {"data": []})
        self.assertEqual(request.call_count, 2)

    def test_retry_after_is_honored_and_retries_are_bounded(self, sleep):
        busy = [http_response(429, {"error": "busy"}, {"Retry-After": "7"}) for _ in range(3)]
        with patch(SESSION_REQUEST, side_effect=busy) as request, self.assertRaises(ProviderError) as ctx:
            self.client._request("GET", "models")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(ctx.exception.category, "throttled")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [7.0, 7.0])

    def test_completion_is_not_resent_once_accepted(self, sleep):
        for failure in (http_response(504, {"error": "gateway timeout"}), requests.ReadTimeout("slow")):
            with self.subTest(failure=failure), patch(SESSION_REQUEST, side_effect=[failure]) as request, \
                    self.assertRaises(ProviderError) as ctx:
                self.client.completions([], "")
            self.assertEqual(request.call_count, 1)
            self.assertTrue(ctx.exception.transient, "the queue retries it on a later run")
        with patch(SESSION_REQUEST, return_value=http_response(401, {"error": "bad key"})), \
                self.assertRaises(ProviderError) as ctx:
            self.client.completions([], "")
        self.assertFalse(ctx.exception.transient)

    def test_completion_rejected_by_the_provider_is_resent(self, sleep):
        answer = {"choices": [{"message": {"content": "Hello"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        responses = [http_response(503, {"error": "loading model"}), http_response(body=answer, headers={"x-request-id": "req-7"})]
        with patch(SESSION_REQUEST, side_effect=responses):
            self.assertIn("Hello", str(self.client.completions([], "")))
        self.assertEqual((self.client.request_id, self.client.usage["prompt_tokens"]), ("req-7", 7))

    def test_redirect_is_refused(self, sleep):
        redirect = http_response(302, {}, {"Location": "http://169.254.169.254/latest/meta-data"})
        with patch(SESSION_REQUEST, return_value=redirect) as request, self.assertRaisesRegex(ProviderError, "redirect"):
            self.client._request("GET", "models")
        self.assertEqual(request.call_count, 1)

    def test_response_size_is_capped(self, sleep):
        with patch.object(client_module, "MAX_BODY_BYTES", 10):
            for headers in ({"Content-Length": "11"}, {}):
                with self.subTest(headers=headers), patch(SESSION_REQUEST, return_value=http_response(headers=headers)), \
                        self.assertRaisesRegex(ProviderError, "too large"):
                    self.client._request("GET", "models")

    def test_deadline_bounds_requests_and_retries(self, sleep):
        self.client.config["deadline"] = time.monotonic() + 1.5
        with patch(SESSION_REQUEST, return_value=http_response(503, {"error": "busy"}, {"Retry-After": "5"})) as request, \
                self.assertRaises(ProviderError):
            self.client._request("GET", "models")
        self.assertEqual((request.call_count, sleep.call_count), (1, 0))
        self.assertLessEqual(request.call_args.kwargs["timeout"][1], 1.5)
        self.client.config["deadline"] = time.monotonic() - 1
        with patch(SESSION_REQUEST) as request, self.assertRaises(ProviderError):
            self.client._request("GET", "models")
        request.assert_not_called()


class TestModelCapabilities(TransactionCase):

    def _client(self, transport="openai", **supports):
        return ProviderClient({
            "transport": transport,
            "base_url": "http://127.0.0.1:9/v1",
            "allow_private_network": True,
            "chat_model": "small-model",
            "supports": dict({"tools": True, "vision": True, "pdf": True}, **supports),
        })

    def _ask(self, mimetype, data="UERG"):
        return [{"role": "user", "content": [{"type": "inline_data", "mimetype": mimetype, "data": data}]}]

    def test_unsupported_requests_are_rejected_before_sending(self):
        cases = [
            (self._client(tools=False), [], [{"name": "find"}], "tools"),
            (self._client(vision=False), self._ask("image/png"), None, "images"),
            (self._client(pdf=False), self._ask("application/pdf"), None, "PDF files"),
            (self._client(), self._ask("application/zip"), None, "application/zip files"),
            (self._client(), self._ask("application/pdf", "A" * 28_000_000), None, "too large"),
        ]
        for client, messages, tools, message in cases:
            with self.subTest(message=message), patch(SESSION_REQUEST) as request, \
                    self.assertRaisesRegex(client_module.CapabilityError, message):
                client.completions(messages, "", tools)
            request.assert_not_called()

    def test_pdf_reaches_both_transports(self):
        answers = {"openai": {"choices": [{"message": {"content": "ok"}}]}, "anthropic": {"content": [{"type": "text", "text": "ok"}]}}
        for transport, answer in answers.items():
            with self.subTest(transport=transport), patch(SESSION_REQUEST, return_value=http_response(body=answer)) as request:
                self._client(transport).completions(self._ask("application/pdf"), "")
            part = request.call_args.kwargs["json"]["messages"][-1]["content"][0]
            self.assertIn(part["type"], ("file", "document"))

    def test_empty_answer_is_retried_later(self):
        empty = {"choices": [{"message": {"content": "", "reasoning": "long thoughts"}, "finish_reason": "stop"}]}
        with patch(SESSION_REQUEST, return_value=http_response(body=empty)), \
                self.assertRaisesRegex(ProviderError, "empty answer") as ctx:
            self._client().completions([], "")
        self.assertEqual((ctx.exception.transient, ctx.exception.category), (True, "invalid_response"))
        tool_only = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "find", "arguments": "{}"}}]}}]}
        with patch(SESSION_REQUEST, return_value=http_response(body=tool_only)):
            self.assertEqual(self._client().completions([], "", [{"name": "find"}])["result"]["content"][-1]["type"], "tool_call")

    def test_structured_answer_is_validated(self):
        schema = {"type": "object", "properties": {"title": {"type": "string"}, "note": {"type": "string"}},
                  "required": ["title"], "additionalProperties": False}
        for text, valid in [
            ('```json\n{"title": "Hi", "note": null}\n```', True),
            ('{"note": "no title"}', False),
            ('{"title": 3}', False),
            ('{"title": "Hi", "extra": 1}', False),
            ("not json", False),
        ]:
            answer = {"choices": [{"message": {"content": text}}]}
            with self.subTest(text=text), patch(SESSION_REQUEST, return_value=http_response(body=answer)):
                if valid:
                    result = self._client().completions([], "", schema=schema)
                    self.assertEqual(result["result"]["content"], [{"type": "text", "text": '{"title": "Hi", "note": null}'}])
                else:
                    with self.assertRaisesRegex(ProviderError, "requested structure"):
                        self._client().completions([], "", schema=schema)
