import ipaddress
import json
import logging
import math
import random
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

_logger = logging.getLogger(__name__)
_ = LazyTranslate(__name__, default_lang="en_US")
EMBEDDING_DIMENSIONS = 1536
MAX_ERROR_LENGTH = 800
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_RETRIES = 2
MAX_INLINE_BYTES = 20 * 1024 * 1024
JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
RETRY_STATUSES = {429, 502, 503, 504}
# The provider refused these without working on them: resending a completion
# cannot produce a second answer.
REJECTED_STATUSES = {429, 503}


class ProviderError(UserError):
    # Set when the provider answered with an HTTP error: its status and, for
    # OpenAI-style bodies, the error code (e.g. "language_not_enabled").
    status_code = None
    error_code = None
    retry_after = None
    # Connection refused, reset or unreachable (not a read timeout): safe to
    # resend at once.
    retryable = False
    # Network, time or 429/5xx failure that may succeed later (queue retry).
    transient = False


class CapabilityError(ProviderError):
    """The selected model cannot handle this request; shown to the user as is."""


def _transient(error):
    error.transient = True
    return error


def _resolve_endpoint(url, allow_private=False, resolve=True):
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
        return parsed, ()
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
        if blocked or (not allow_private and not ip.is_global):
            raise ProviderError(_("The provider URL resolves to a network address that is not allowed."))
    return parsed, tuple(sorted(addresses))


def validate_endpoint(url, allow_private=False, resolve=True):
    parsed, _addresses = _resolve_endpoint(url, allow_private, resolve)
    return parsed


class _PinnedHTTPSAdapter(HTTPAdapter):
    """Connect to a validated IP while verifying TLS for the configured host."""

    def __init__(self, hostname, *args, **kwargs):
        self.hostname = hostname
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs.update(assert_hostname=self.hostname, server_hostname=self.hostname)
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _pinned_url(parsed, address):
    host = f"[{address}]" if ":" in address else address
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host).geturl()


def _host_header(parsed):
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.hostname}:{parsed.port}" if parsed.port and parsed.port != default_port else parsed.hostname


def _read_body(response, deadline):
    """Load the streamed body into the response, bounded in size and time."""
    length = response.headers.get("Content-Length") or ""
    if length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise ProviderError(_("The AI provider response is too large."))
    body = bytearray()
    try:
        for chunk in response.iter_content(64 * 1024):
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise ProviderError(_("The AI provider response is too large."))
            if time.monotonic() > deadline:
                raise _transient(ProviderError(_("The AI provider did not answer in time.")))
    except requests.RequestException as error:
        raise _transient(ProviderError(_("The AI provider connection failed during the answer: %s", str(error)))) from error
    response._content = bytes(body)


def _json_error_code(response):
    try:
        error = response.json().get("error")
    except (ValueError, TypeError, AttributeError):
        return None
    return error.get("code") if isinstance(error, dict) else None


def _json_error(response):
    try:
        body = response.json()
        error = body.get("error", body)
        if isinstance(error, dict):
            return error.get("message") or json.dumps(error)
        return str(error)
    except (ValueError, TypeError):
        # A proxy's HTML error page (e.g. 502 from Nginx) says nothing useful.
        if "html" in response.headers.get("Content-Type", "") or response.text.lstrip().startswith("<"):
            return response.reason
        return response.text


class ProviderClient:
    def __init__(self, config):
        self.config = config

    def _request(self, method, path, payload=None, files=None, data=None, raw=False, idempotent=True):
        """JSON request, or multipart/form-data when `files` is given.

        raw=True returns the response body as bytes (e.g. generated audio).
        Transient failures are retried within one deadline: the provider
        timeout, or the caller's config["deadline"] (time.monotonic()) if
        sooner. idempotent=False (completions) is not resent once the
        provider may be working on it (read timeout, 502, 504).
        """
        deadline = min(self.config.get("deadline", math.inf), time.monotonic() + self.config.get("timeout", 120))
        statuses = RETRY_STATUSES if idempotent else REJECTED_STATUSES
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._send(method, path, payload, files, data, raw, deadline)
            except ProviderError as error:
                try:
                    wait = float(error.retry_after)
                except (TypeError, ValueError):
                    wait = 2 ** attempt + random.random()
                if (attempt == MAX_RETRIES or not (error.retryable or error.status_code in statuses)
                        or time.monotonic() + wait >= deadline):
                    raise
                _logger.info("AI provider request failed (%s); retrying in %.1f s", error.status_code or "connection", wait)
                time.sleep(wait)

    def _send(self, method, path, payload, files, data, raw, deadline):
        url = urljoin(self.config["base_url"].rstrip("/") + "/", path.lstrip("/"))
        parsed, addresses = _resolve_endpoint(url, self.config.get("allow_private_network", False))
        headers = {
            "Accept": "application/json",
            "Host": _host_header(parsed),
        }
        if files is None:
            headers["Content-Type"] = "application/json"
        key = self.config.get("api_key")
        if self.config["transport"] == "anthropic":
            headers.update({"anthropic-version": "2023-06-01", "x-api-key": key})
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        _logger.info("AI provider request: %s %s", method, url)
        response = None
        last_error = None
        with requests.Session() as session:
            session.trust_env = False
            if parsed.scheme == "https":
                session.mount("https://", _PinnedHTTPSAdapter(parsed.hostname))
            for address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    response = session.request(
                        method,
                        _pinned_url(parsed, address),
                        headers=headers,
                        json=payload if files is None else None,
                        files=files,
                        data=data,
                        timeout=(min(10, remaining), remaining),
                        verify=self.config.get("verify_ssl", True),
                        allow_redirects=False,
                        stream=True,
                    )
                    break
                except requests.RequestException as error:
                    last_error = error
            if response is None:
                error = _transient(ProviderError(_("The AI provider could not be reached: %s", str(last_error or "timeout"))))
                # A read timeout means the provider may be working on it.
                error.retryable = isinstance(last_error, requests.ConnectionError)
                raise error from last_error
            with response:
                _read_body(response, deadline)
        if response.is_redirect:
            raise ProviderError(_("The AI provider attempted an HTTP redirect, which is blocked for security."))
        if not response.ok:
            detail = (_json_error(response) or response.reason)[:MAX_ERROR_LENGTH]
            if response.status_code in (502, 503, 504):
                error = ProviderError(_(
                    "The AI provider is not responding (HTTP %(status)s: %(detail)s). "
                    "Check that the service is running and reachable from its proxy.",
                    status=response.status_code, detail=detail,
                ))
            else:
                error = ProviderError(_("AI provider error %(status)s: %(detail)s", status=response.status_code, detail=detail))
            error.status_code = response.status_code
            error.error_code = _json_error_code(response)
            error.retry_after = response.headers.get("Retry-After")
            error.transient = response.status_code in RETRY_STATUSES
            raise error
        if raw:
            return response.content
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
        self._check_capabilities(messages, tools)
        if self.config["transport"] == "anthropic":
            result = self._anthropic_completion(messages, instructions, tools, options)
        else:
            result = self._openai_completion(messages, instructions, tools, options)
        content = result["result"]["content"]
        if options.get("schema") and not any(part.get("type") == "tool_call" for part in content):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", "".join(part.get("text", "") for part in content).strip())
            try:
                valid = _matches(json.loads(text), options["schema"])
            except ValueError:
                valid = False
            if not valid:
                raise ProviderError(_("The AI provider returned an answer that does not match the requested structure."))
            result["result"]["content"] = [{"type": "text", "text": text}]
        return result

    def _check_capabilities(self, messages, tools):
        supports = self.config.get("supports") or {}
        parts = [part for message in messages for part in message.get("content") or [] if part.get("type") == "inline_data"]
        needs = {"tools": _("tools")} if tools else {}
        for part in parts:
            mimetype = part.get("mimetype") or ""
            kind = "vision" if mimetype.startswith("image/") else "pdf" if mimetype == "application/pdf" else mimetype
            needs[kind] = {"vision": _("images"), "pdf": _("PDF files")}.get(kind) or _("%s files", mimetype)
        for kind, what in needs.items():
            # Other file types are never sent; without flags (no chat model
            # record) the known capabilities count as supported.
            if not supports.get(kind, kind in ("tools", "vision", "pdf")):
                raise CapabilityError(_(
                    "The AI model %(model)s cannot handle %(what)s. Ask an administrator to choose another model "
                    "or to enable this capability on the model.", model=self.config["chat_model"], what=what,
                ))
        if sum(len(part.get("data") or "") for part in parts) * 3 // 4 > MAX_INLINE_BYTES:
            raise CapabilityError(_("The attached files are too large for the AI model (20 MB at most)."))

    def embeddings(self, inputs, model=None):
        if self.config["transport"] == "anthropic":
            raise ProviderError(_("Anthropic does not provide an embeddings API. Configure another provider for embeddings."))
        model = model or self.config.get("embedding_model")
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
        data = self._request("POST", "chat/completions", payload, idempotent=False)
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
        data = self._request("POST", "messages", payload, idempotent=False)
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
    return {"type": "file", "file": {"filename": "document.pdf", "file_data": data}}


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
            elif kind == "inline_data":
                block = "image" if part["mimetype"].startswith("image/") else "document"
                content.append({"type": block, "source": {"type": "base64", "media_type": part["mimetype"], "data": part["data"]}})
        if content:
            result.append({"role": message["role"], "content": content})
    return result


def _matches(value, schema):
    """Subset of JSON Schema (type, enum, anyOf, properties, required,
    additionalProperties, items); unknown keywords are accepted."""
    if "anyOf" in schema:
        return any(_matches(value, option) for option in schema["anyOf"])
    types = schema.get("type") or []
    if types and not any(
        isinstance(value, JSON_TYPES.get(name, object)) and not (isinstance(value, bool) and name in ("integer", "number"))
        for name in (types if isinstance(types, list) else [types])
    ):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties, required = schema.get("properties", {}), schema.get("required", [])
        if any(name not in value for name in required) or (
            schema.get("additionalProperties") is False and set(value) - set(properties)
        ):
            return False
        # Optional fields may be null: strict mode makes them nullable.
        return all(_matches(item, properties[name]) for name, item in value.items()
                   if name in properties and not (item is None and name not in required))
    if isinstance(value, list) and "items" in schema:
        return all(_matches(item, schema["items"]) for item in value)
    return True


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
