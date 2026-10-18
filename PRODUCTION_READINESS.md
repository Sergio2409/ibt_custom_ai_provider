# Production-readiness roadmap

This document tracks work that remains after the initial provider routing and
security hardening. Items are ordered by release priority. A production release
should complete every P0 item; P1 items may be deferred only with an explicit
operational decision and compensating controls.

## P0 — release blockers

### 1. Protect provider credentials at rest

Done in 20.0.1.2.0: keys are encrypted with a master key kept outside the
database (`ibt_ai_secret_keys`), write-only, removed from neutralized copies,
and replaceable or rotatable without recreating providers; see the README.

- Replace directly stored API keys with an encrypted secret field or a reference
  to an external secret supplied at runtime.
- Keep secrets out of ORM exports, chatter, audit messages, logs, exceptions and
  database copies used outside production.
- Add key replacement and revocation instructions. Changing a key must not
  require recreating the provider or its models.
- Add tests proving that non-system users cannot read secrets through `read`,
  `search_read`, related fields, exports or provider snapshots.

Acceptance: a stolen ordinary database backup does not expose usable provider
credentials without access to the separately managed encryption key or secret
store.

### 2. Add resilient, bounded HTTP behavior

Done, trimmed to the deployment: every request has one total
deadline (the provider timeout, and the cron run's remaining time minus 30 s
for queued requests); bodies are streamed and capped at 32 MiB; connection
failures and HTTP 429/502/503/504 are retried twice with backoff and jitter,
honoring `Retry-After` within the deadline; completions are resent only after
429/503 or a failed connection, never after a read timeout, 502 or 504.
Deliberately not done: per-provider configurable policies (fixed constants
suffice), connection-pool limits (each request uses its own session) and
idempotency keys (OpenAI chat and Anthropic do not support them). User-facing
messages were already generic; `ProviderError.retryable` is left for item 3.

- Add configurable retry policies for connection failures, HTTP 429 and
  transient 5xx responses, respecting `Retry-After` where available.
- Use exponential backoff with jitter and a strict total request deadline so
  retries cannot exceed the queue worker's execution budget.
- Cap response-body size before parsing JSON, including error responses.
- Set connection-pool limits and close/discard failed responses deterministically.
- Classify errors as retryable, permanent configuration errors, invalid provider
  responses, or user cancellations.
- Do not retry completion requests unless the provider supports an idempotency
  key or the request is known not to have been accepted.

Acceptance: provider throttling or an outage cannot tie up all cron workers,
create an unbounded response in memory, or silently produce duplicate model
responses.

### 3. Complete queue lifecycle and recovery

Done in 20.0.1.3.0, trimmed: queued sessions record when they were queued,
their retries and the next attempt. A transient failure (network, timeout,
429/5xx) is retried on a later cron run after 30 s, 60 s, 120 s, ...; a
permanent one fails at once. A request still pending 10 minutes after it was
queued, including one whose worker was stopped mid-call (its transaction is
rolled back, so it stays queued), fails visibly instead of being retried
forever. Already covered by Odoo: a deleted channel deletes its sessions
(cascade), a cancelled or declined exchange returns the session to `ready`
and the cron skips it, a user with sessions cannot be deleted. Concurrency:
one cron job runs in one worker at a time; the per-session advisory lock and
the `waiting_model` re-check guard other paths. Not done: a separate request
model and a failed-requests view (item 6).

- Persist attempt count, next-attempt time, last error category and terminal
  failure time on queued sessions or a dedicated request model.
- Add bounded retries and a dead-letter/failed state instead of immediately
  converting every transient failure into a conversation failure.
- Detect and recover requests left in `waiting_model` after worker termination.
- Add cancellation handling and prevent delivery after the initiating session,
  user or channel has been deleted or cancelled.
- Document and test the concurrency contract for multiple cron workers,
  advisory locks, commits, savepoints and tool-call follow-up rounds.

Acceptance: every submitted request eventually succeeds, reaches a visible
terminal failure, or is explicitly cancelled; no request remains pending
indefinitely.

### 4. Enforce model capabilities before dispatch

Done in 20.0.1.4.0, trimmed: each model has editable flags for tools, images
and PDF files (Ollama discovery sets them, PDF off; models created by hand
start with all on). A request needing a missing capability, any other file
type, or more than 20 MB of attachments is refused before it is sent, with a
message that names the model. PDF files reach Anthropic as documents and
OpenAI-compatible APIs as file parts instead of a placeholder text. Structured
answers are parsed (Markdown fences removed) and checked against the requested
schema with a built-in subset validator (type, enum, anyOf, properties,
required, additionalProperties, items). Not done: per-transport defaults and
re-asking the model after an invalid structured answer.

- Validate tool calls, vision input and structured output against model
  capabilities instead of relying on provider failures.
- Store explicit capability flags for all provider kinds, with an administrator
  override for generic OpenAI-compatible endpoints.
- Validate attachment MIME types and decoded sizes before creating inline
  provider payloads. Reject unsupported files rather than converting them into
  ambiguous prompt text.
- Define the behavior for providers that do not support strict JSON Schema and
  validate structured responses locally before returning them to Odoo.

Acceptance: an unsupported feature is rejected with a clear, safe error before
the request is sent, and structured responses always satisfy the requested
schema.

### 5. Establish release-grade integration tests

Done, trimmed (Odoo 20 Enterprise only; the module extends `ai`):
`ci/run-tests.sh` installs on an empty PostgreSQL 16 + pgvector database,
upgrades from a released revision with a stored key (checked afterwards),
runs the suite both times and uninstalls; `.github/workflows/ci.yml` runs it
on a self-hosted runner. Contract tests cover OpenAI, Anthropic and Ollama
mappings including tool follow-up rounds, structured output, 401, 429, 502,
503, 504, timeouts, oversized bodies, redirects and the queue (retries,
expiry, cron budget, cancelled requests). Not covered: vLLM and
llama.cpp fixtures (they use the OpenAI mapping), TLS failure and DNS
rebinding tests, concurrent cron workers.

- Add contract fixtures for OpenAI chat completions, Anthropic Messages,
  Ollama, vLLM and llama.cpp, including tool-result follow-up rounds.
- Cover structured output, malformed/partial JSON, empty responses, usage
  metadata, multimodal input, embedding ordering and dimension errors.
- Test 401/403, 404, 408, 409, 429 and representative 5xx behavior, timeouts,
  TLS failures, redirects, DNS rebinding and multiple resolved addresses.
- Add cron tests for retries, concurrent workers, stale sessions, configuration
  changes, deleted users/companies/channels and transaction rollback isolation.
- Add install, upgrade and uninstall tests on a database with `pgvector` already
  provisioned.
- Run the module suite in CI against the Odoo 20 Enterprise revision used for
  deployment.

Acceptance: CI installs the module from a clean database, upgrades from the
previous released version, and passes without real provider credentials.

## P1 — production hardening

### 6. Add observability and administrator diagnostics

Done in 20.0.1.5.0 and 20.0.1.6.0, trimmed: `ibt.ai.request.log` keeps one row
per chat, embedding or speech gateway call (transcription, text-to-speech,
realtime session) for 90 days (company, provider, model, user, agent, request,
round, attempt, outcome, category, HTTP status, provider request ID,
duration, tokens, audio seconds, characters, estimated cost from optional
model and gateway prices), never prompts, answers, audio, transcripts,
attachments, keys or error texts, readable by administrators only.
Each call is also one structured `ibt_ai ...` log line. Settings > Technical
offers the request log (list, graph, pivot, filters) and the live request
queue; providers show their last success and failure and a Requests button.
Graphs and pivots can be added to an Odoo Dashboards spreadsheet. Not done:
latency percentiles, a metrics endpoint, a content-logging diagnostic mode
and realtime session durations (only the gateway sees the audio).

- Record redacted request diagnostics: provider, model, company, duration,
  outcome, retry count, provider request ID, token usage and error category.
- Never record prompts, tool arguments, attachment data or credentials by
  default. Make content logging an explicit, time-limited diagnostic mode.
- Provide queue-health views for pending, retrying, failed and oldest requests.
- Add health checks and last successful connection timestamps per provider.
- Expose metrics suitable for alerting on queue age, error rate, latency,
  throttling and token consumption.

Acceptance: operators can diagnose an outage and identify the affected
provider/company without enabling debug logging or inspecting raw database
payloads.

### 7. Strengthen network and TLS policy

- Replace the broad private-network switch with optional hostname/CIDR
  allowlists for local providers.
- Add support for a custom CA bundle and, if required by deployments, mutual TLS.
- Warn prominently when TLS verification is disabled and optionally prohibit it
  through a system parameter.
- Revalidate every address used for a connection and retain hostname-based
  certificate verification for IPv4 and IPv6.
- Document the recommended outbound firewall policy and proxy behavior.

Acceptance: administrators can grant access only to the intended local service
without allowing arbitrary private-network destinations.

### 8. Add quotas and cost controls

- Support per-company request, token and concurrency limits.
- Add provider/model rate limits and maximum input/output token settings.
- Capture normalized usage and optional price tables for budget reporting.
- Stop or queue requests predictably when a limit is reached.

Acceptance: one company or user cannot consume all worker capacity or exceed a
configured budget unnoticed.

### 9. Define data-governance controls

- Document which conversation, attachment and tool data leaves Odoo for each
  configured provider.
- Add optional provider-level warnings/consent text and company policy fields.
- Define retention for queued payloads and diagnostic metadata; clear request
  payloads and secrets as soon as they are no longer needed.
- Provide a deletion/anonymization path compatible with Odoo user and channel
  deletion.

Acceptance: administrators can state what is transmitted, how long request data
is retained, and how it is removed.

### 10. Add upgrade and configuration safety

- Add explicit migration scripts whenever stored fields, constraints or XML IDs
  change.
- Preflight embedding-model uniqueness before installing the partial unique
  index and provide an actionable remediation message for conflicting data.
- Add a configuration export/import format that excludes credentials.
- Document backup, restore and rollback procedures (provider-key rotation is
  in the README).

Acceptance: upgrades fail safely with actionable diagnostics and never expose or
silently discard configuration.

## P2 — supported enhancements

### 11. Broaden provider feature coverage

- Optional streaming responses when the Odoo AI session API supports safe
  incremental delivery.
- Provider-specific parameters such as temperature, maximum tokens and thinking
  budgets, controlled by administrator-defined profiles rather than arbitrary
  end-user values.
- Model discovery for APIs that expose a reliable models endpoint, while
  retaining manual configuration for vLLM and llama.cpp deployments.
- Optional failover provider/model chains with explicit rules for data residency
  and retry safety.
- Image generation only after defining storage, moderation, size and retention
  behavior.

### 12. Improve administration UX

- Capability badges and compatibility warnings in provider/model views.
- A connection test that separately reports DNS, TCP, TLS, authentication and
  model-access status without disclosing sensitive details.
- Provider duplication with credentials omitted.
- Company-configuration overview showing which provider/model is active for
  chat, reasoning and embeddings.

## Release gates

Before declaring a version production ready:

- All P0 items are complete and covered by automated tests.
- No credentials or prompt content appear in normal logs or exports.
- Security review covers ACLs, `sudo()` boundaries, SSRF, TLS and queued-context
  restoration.
- CI passes install, upgrade and module tests on the deployment's Odoo 20 build.
- Load testing demonstrates acceptable queue latency and recovery during 429 and
  5xx provider failures.
- Operations documentation includes monitoring, alerting, backup/restore,
  secret rotation, incident response and rollback.

