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

## Provider API keys

API keys are stored encrypted with a master key that is kept outside the
database, so a database backup alone does not expose them. The key field is
write-only: once saved, a key is never displayed, read, exported or copied
again, even by administrators; the form only shows that a key is stored.
Keys never appear in logs or error messages, and neutralized database copies
(staging, tests) have their keys removed.

**Master key.** Generate one with the Odoo server's Python and keep it with
the server configuration, not with database backups:

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Put it in the Odoo configuration file (Odoo logs at startup that the option
is unknown to it; the value is not logged), or set it as the
`IBT_AI_SECRET_KEYS` environment variable, for example from a Docker secret:

    [options]
    ibt_ai_secret_keys = <master key>

Restart Odoo after changing it. Providers without a key (Ollama) work without
a master key; storing or using a key requires it. Upgrading from a version
before 20.0.1.2.0 encrypts the stored keys and stops with an error, leaving
everything unchanged, when no master key is configured.

**Replace or revoke a provider key.** Open the provider, type the new key and
save: the stored key is replaced, the provider and its models stay. *Remove
it* deletes the stored key. Revoke the old key at the provider (for the speech
gateway, `ibt-admin keys rotate <name>` issues the new key and stops the old
one; `ibt-admin keys revoke <name>` only stops it).

**Rotate the master key.** Put a new master key first and keep the old one
after it (`ibt_ai_secret_keys = <new>,<old>`), restart, re-encrypt, then
remove the old key and restart again:

    odoo-bin shell -c <config> -d <database>
    >>> env["ibt.ai.provider"]._ibt_rotate_api_keys()
    >>> env.cr.commit()

**Lost master key.** Stored keys cannot be recovered: configure a new master
key and type every provider key again.

## Network access

Restrict provider administration to trusted users. This module deliberately blocks loopback,
link-local and private destinations unless the provider explicitly allows
private-network access; cloud endpoints should keep that option disabled.

## Request log and queue

**Settings > Technical > IBT AI Request Log** lists every chat, embedding and
speech gateway call for 90 days: provider, model or voice, company, user,
outcome, error category, HTTP status, duration, tokens, audio seconds,
characters read aloud and an estimated cost (prices per million tokens on
models; per audio minute and per million characters on the speech gateway).
Realtime dictation sessions are counted, not timed: their audio goes straight
from the browser to the gateway. Prompts, answers, audio, transcripts,
attachments, keys and error texts are never stored. Graph and pivot
views show volume, failures and usage; *Insert in spreadsheet* adds them to
an Odoo Dashboards dashboard. **IBT AI Request Queue** shows the requests
waiting or retrying, oldest first. Each call is also logged as one
`ibt_ai kind=... outcome=... category=...` line for log-based alerts.

## Tests and CI

The module supports Odoo 20 Enterprise only (it extends the `ai` module).
`ci/run-tests.sh` starts a throwaway PostgreSQL 16 + pgvector container,
installs the module on an empty database and runs its tests, then installs
the released revision `FROM_REF`, stores a provider key, upgrades to the
current checkout, runs the tests again, checks the key still works and
uninstalls the module:

    ODOO_SRC=~/src/odoo ENTERPRISE_SRC=~/src/enterprise PYTHON=~/venv/odoo20/bin/python \
        FROM_REF=<released commit> ci/run-tests.sh

The JS tests also need Chrome and `websocket-client` (on `PYTHONPATH`).
Odoo Enterprise is private, so `.github/workflows/ci.yml` runs the script on
a self-hosted GitHub runner labelled `odoo20` with those variables in the
runner's `.env` file; pushes upgrade from the previous commit, pull requests
from their base.

The administrator procedure (setup, checks, keys, troubleshooting) is in
[docs/SOP.md](docs/SOP.md).

See [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for the prioritized work
and release gates that remain before treating the module as production ready.

