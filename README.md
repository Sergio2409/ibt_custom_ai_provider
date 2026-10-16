# IBT Custom AI Provider

Routes Odoo 20 AI completion and embedding calls to a provider selected per
company. Supported transports:

- OpenAI and OpenAI-compatible APIs, including Ollama, vLLM and llama.cpp
- Anthropic Messages API (chat and tool calling; no embeddings)

Install the module, open **Settings → Integrations → IBT AI Provider**, create
a provider, add its model identifiers, test the connection, and enable it for
the current company. When disabled, Odoo's standard IAP path remains active.

For Ollama, use an address reachable from the Odoo server, such as
`http://host.docker.internal:11434/v1`, enable private-network access, and use
a tool-capable model for Odoo agents. Odoo currently stores 1536-value
embeddings, so select a model/API capable of returning that dimension or leave
the embedding model empty.

For an Ollama provider, use **Discover Models** to import the models currently
installed on that Ollama server. Discovery reads `/api/tags` and `/api/show`,
updates capability metadata, and never deletes manually configured models.
Embedding-only models are imported only when Ollama reports the 1536 dimensions
required by Odoo. Embedding model IDs must be unique across providers because
Odoo stores the model identifier with each embedding and uses it to route
future regeneration requests.

API keys are readable only by Settings administrators, are never returned by
the connection-test action, and are omitted from logs. Restrict provider
administration to trusted users. This module deliberately blocks loopback,
link-local and private destinations unless the provider explicitly allows
private-network access; cloud endpoints should keep that option disabled.

See [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for the prioritized work
and release gates that remain before treating the module as production ready.

