from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..services.client import ProviderClient, ProviderError, _to_anthropic_messages, _to_openai_messages, validate_endpoint


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

