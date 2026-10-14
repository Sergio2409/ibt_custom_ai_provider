import ipaddress
import json
import logging
import socket
from urllib.parse import urljoin, urlparse

import requests

from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_logger = logging.getLogger(__name__)
_ = LazyTranslate(__name__, default_lang="en_US")
EMBEDDING_DIMENSIONS = 1536
MAX_ERROR_LENGTH = 800


class ProviderError(UserError):
    pass


def validate_endpoint(url, allow_private=False, resolve=True):
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderError(_("The provider URL must be a valid HTTP or HTTPS URL."))
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ProviderError(_("The provider URL cannot contain credentials, a query string, or a fragment."))
    if parsed.scheme == "http" and not allow_private:
        raise ProviderError(_("Public provider endpoints must use HTTPS."))
    try:
        port = parsed.port
    except ValueError as error:
        raise ProviderError(_("The provider URL contains an invalid port.")) from error
    if not resolve:
        return parsed
    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(
                parsed.hostname,
                port or (443 if parsed.scheme == "https" else 80),
            )
        }
    except (OSError, UnicodeError) as error:
        raise ProviderError(_("The provider hostname cannot be resolved.")) from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        blocked = ip.is_link_local or ip.is_multicast or ip.is_unspecified
        if blocked or (not allow_private and (ip.is_private or ip.is_loopback or ip.is_reserved)):
            raise ProviderError(_("The provider URL resolves to a network address that is not allowed."))
    return parsed


def _json_error(response):
    try:
        body = response.json()
        error = body.get("error", body)
        if isinstance(error, dict):
            return error.get("message") or json.dumps(error)
        return str(error)
    except (ValueError, TypeError):
        return response.text


class ProviderClient:
    def __init__(self, config):
        self.config = config

    def _request(self, method, path, payload=None):
        validate_endpoint(self.config["base_url"], self.config.get("allow_private_network", False))
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        key = self.config.get("api_key")
        if self.config["transport"] == "anthropic":
            headers.update({"anthropic-version": "2023-06-01", "x-api-key": key})
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        url = urljoin(self.config["base_url"].rstrip("/") + "/", path.lstrip("/"))
        _logger.info("AI provider request: %s %s", method, url)
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=(10, self.config.get("timeout", 120)),
                verify=self.config.get("verify_ssl", True),
                allow_redirects=False,
            )
        except requests.RequestException as error:
            raise ProviderError(_("The AI provider could not be reached: %s", str(error))) from error
        if response.is_redirect:
            raise ProviderError(_("The AI provider attempted an HTTP redirect, which is blocked for security."))
        if not response.ok:
            detail = (_json_error(response) or response.reason)[:MAX_ERROR_LENGTH]
            raise ProviderError(_("AI provider error %(status)s: %(detail)s", status=response.status_code, detail=detail))
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError(_("The AI provider returned an invalid JSON response.")) from error

    def test_connection(self):
        if self.config["transport"] == "anthropic":
            if not self.config.get("api_key"):
                raise ProviderError(_("Anthropic requires an API key."))
            self._request("GET", "models")
        else:
            self._request("GET", "models")

    def discover_ollama_models(self):
        if self.config.get("provider_kind") != "ollama":
            raise ProviderError(_("This provider is not configured as Ollama."))
        parsed = validate_endpoint(
            self.config["base_url"], self.config.get("allow_private_network", False),
        )
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        native_url = parsed._replace(path=path or "/", params="", query="", fragment="").geturl().rstrip("/")
        native_config = dict(self.config, base_url=native_url)
        native_client = ProviderClient(native_config)
        data = native_client._request("GET", "api/tags")
        result = []
        for summary in data.get("models") or []:
            name = summary.get("model") or summary.get("name")
            if not name:
                continue
            details = native_client._request("POST", "api/show", {"model": name, "verbose": False})
            capabilities = details.get("capabilities") or []
            model_type = "embedding" if "embedding" in capabilities and "completion" not in capabilities else "chat"
            dimensions = _ollama_embedding_dimensions(details) if model_type == "embedding" else None
            metadata = summary.get("details") or {}
            label_parts = [name, metadata.get("parameter_size"), metadata.get("quantization_level")]
            result.append({
                "name": name,
                "label": " · ".join(part for part in label_parts if part),
                "model_type": model_type,
                "embedding_dimensions": dimensions,
                "capabilities": capabilities,
            })
        return result

    def completions(self, messages, instructions, tools=None, **options):
        if options.get("image_generation"):
            raise ProviderError(_("Image generation is not supported by this provider adapter."))
        if not self.config.get("chat_model"):
            raise ProviderError(_("No chat model is configured for this company."))
        if self.config["transport"] == "anthropic":
            return self._anthropic_completion(messages, instructions, tools, options)
        return self._openai_completion(messages, instructions, tools, options)

    def embeddings(self, inputs):
        if self.config["transport"] == "anthropic":
            raise ProviderError(_("Anthropic does not provide an embeddings API. Configure another provider for embeddings."))
        model = self.config.get("embedding_model")
        if not model:
            raise ProviderError(_("No embedding model is configured for this company."))
        texts = [f"{item['title']}\n\n{item.get('content', '')}" if item.get("title") else item.get("content", "") for item in inputs]
        payload = {"model": model, "input": texts, "dimensions": EMBEDDING_DIMENSIONS}
        if not self.config.get("send_embedding_dimensions", True):
            payload.pop("dimensions")
        data = self._request("POST", "embeddings", payload)
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        vectors = [row.get("embedding", []) for row in rows]
        if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
            raise ProviderError(_("The provider must return one 1536-dimensional embedding for each input."))
        return {"embeddings": vectors}

    def _openai_completion(self, messages, instructions, tools, options):
        payload = {
            "model": self.config["chat_model"],
            "messages": _to_openai_messages(messages, instructions),
        }
        if tools:
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("instructions", ""),
                    "parameters": tool.get("schema") or {"type": "object", "properties": {}},
                },
            } for tool in tools]
        if options.get("schema"):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "odoo_response", "strict": True, "schema": _strict_schema(options["schema"])},
            }
        data = self._request("POST", "chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(_("The AI provider returned no completion."))
        message = choices[0].get("message") or {}
        content = []
        text = message.get("content")
        if isinstance(text, list):
            text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
        if text is not None:
            content.append({"type": "text", "text": text})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError) as error:
                raise ProviderError(_("The provider returned invalid tool arguments.")) from error
            content.append({"type": "tool_call", "name": function.get("name"), "args": arguments, "call_id": call.get("id")})
        return _odoo_result(content, data.get("model"), data.get("usage"), "openai")

    def _anthropic_completion(self, messages, instructions, tools, options):
        payload = {
            "model": self.config["chat_model"],
            "max_tokens": options.get("max_tokens") or 4096,
            "messages": _to_anthropic_messages(messages),
        }
        if instructions:
            payload["system"] = instructions
        if tools:
            payload["tools"] = [{
                "name": tool["name"],
                "description": tool.get("instructions", ""),
                "input_schema": tool.get("schema") or {"type": "object", "properties": {}},
            } for tool in tools]
        if options.get("schema"):
            payload["system"] = (payload.get("system", "") + "\nReturn valid JSON matching this schema: " + json.dumps(options["schema"])).strip()
        data = self._request("POST", "messages", payload)
        content = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                content.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "tool_use":
                content.append({"type": "tool_call", "name": block.get("name"), "args": block.get("input") or {}, "call_id": block.get("id")})
        return _odoo_result(content, data.get("model"), data.get("usage"), "anthropic")


def _odoo_result(content, model, usage, provider):
    return {"result": {"role": "assistant", "content": content or [{"type": "text", "text": ""}], "provider_metadata": {"provider": provider, "model": model, "usage": usage}}}


def _inline_openai(part):
    mimetype = part.get("mimetype") or "application/octet-stream"
    data = f"data:{mimetype};base64,{part.get('data', '')}"
    if mimetype.startswith("image/"):
        return {"type": "image_url", "image_url": {"url": data}}
    return {"type": "text", "text": f"[Unsupported attachment: {mimetype}]"}


def _to_openai_messages(messages, instructions):
    result = [{"role": "system", "content": instructions}] if instructions else []
    for message in messages:
        if message["role"] == "assistant":
            text = []
            calls = []
            for part in message.get("content", []):
                if part.get("type") == "text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "tool_call":
                    calls.append({"id": str(part["call_id"]), "type": "function", "function": {"name": part["name"], "arguments": json.dumps(part.get("args") or {})}})
            # Ollama's OpenAI-compatible endpoint rejects JSON null here when
            # replaying an assistant tool call in the following model round.
            item = {"role": "assistant", "content": "\n".join(text)}
            if calls:
                item["tool_calls"] = calls
            result.append(item)
            continue
        user_content = []
        for part in message.get("content", []):
            if part.get("type") == "tool_result":
                value = "\n".join(p.get("text", "") for p in part.get("result", []) if p.get("type") == "text")
                result.append({"role": "tool", "tool_call_id": str(part["tool_call_id"]), "content": value or "OK"})
            elif part.get("type") == "text":
                user_content.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "inline_data":
                user_content.append(_inline_openai(part))
        if user_content:
            result.append({"role": "user", "content": user_content})
    return result


def _to_anthropic_messages(messages):
    result = []
    for message in messages:
        content = []
        for part in message.get("content", []):
            kind = part.get("type")
            if kind == "text":
                content.append({"type": "text", "text": part.get("text", "")})
            elif kind == "tool_call":
                content.append({"type": "tool_use", "id": str(part["call_id"]), "name": part["name"], "input": part.get("args") or {}})
            elif kind == "tool_result":
                blocks = [{"type": "text", "text": p.get("text", "")} for p in part.get("result", []) if p.get("type") == "text"]
                content.append({"type": "tool_result", "tool_use_id": str(part["tool_call_id"]), "content": blocks or "OK", "is_error": not part.get("success", True)})
            elif kind == "inline_data" and (part.get("mimetype") or "").startswith("image/"):
                content.append({"type": "image", "source": {"type": "base64", "media_type": part["mimetype"], "data": part["data"]}})
        if content:
            result.append({"role": message["role"], "content": content})
    return result


def _strict_schema(value):
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(item) for key, item in value.items()}
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        originally_required = set(result.get("required", []))
        for name, schema in properties.items():
            if name not in originally_required:
                properties[name] = _nullable_schema(schema)
        result["properties"] = properties
        result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


def _nullable_schema(schema):
    schema = dict(schema)
    value_type = schema.get("type")
    if isinstance(value_type, str) and value_type != "null":
        schema["type"] = [value_type, "null"]
    elif isinstance(value_type, list) and "null" not in value_type:
        schema["type"] = [*value_type, "null"]
    elif value_type is None:
        schema["anyOf"] = [*schema.get("anyOf", []), {"type": "null"}]
    return schema


def _ollama_embedding_dimensions(details):
    for key, value in (details.get("model_info") or {}).items():
        if key.endswith(".embedding_length") and isinstance(value, int):
            return value
    return None
