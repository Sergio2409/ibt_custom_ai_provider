# SOP: IBT Custom AI Provider (Odoo 20)

Updated 2026-10-17 for version 20.0.1.4.0 · owner: Sergio Valdes

## 1. Purpose and scope

This SOP is how an Odoo administrator sets up, tests and maintains the **IBT Custom AI Provider** module (`ibt_custom_ai_provider`, version 20.0.1.4.0 or later) on Odoo 20 Enterprise. With it, Odoo's AI features run on providers the company chooses instead of Odoo's IAP service.

| Feature | What the module does | Where users see it |
| --- | --- | --- |
| AI chat, reasoning and embeddings | Sends Odoo AI requests to OpenAI, Anthropic, Ollama or any OpenAI-compatible provider | AI agents, AI chats, document search (embeddings) |
| Speech-to-text | Sends voice messages and call recordings to the company's **IBT Speech Gateway** (Whisper on the company GPU) | Microphone button in AI chats; Discuss call transcripts |
| Transcript correction | Lets the gateway fix punctuation, capitals and accents (optionally a glossary) with a local language model; the raw transcript is kept | Voice messages and call transcripts |
| Realtime dictation | Streams the microphone live to the gateway and writes text while the user speaks | Voice transcription command in HTML fields (Knowledge, notes, tasks) |
| Voice replies | Reads AI agent answers aloud with the gateway's voices; voice mode does it automatically after a voice message. Nothing is stored | Speaker button on agent answers in AI chats |

Out of scope: installing the IBT Speech Gateway itself and running the GPU server (see the gateway's documentation), and Odoo's own AI configuration (agents, prompts, sources).

## 2. Roles and prerequisites

Two administrators are involved: the **Odoo administrator** configures Odoo, the **gateway administrator** (Unraid) issues keys and allows Odoo's address. Users only use the features.

| Role | Responsibilities |
| --- | --- |
| Odoo administrator (Settings rights) | Installs and upgrades the module, sets the master key, creates providers, enters keys, sets company settings, runs the checks in section 9 |
| Gateway administrator | Creates and revokes gateway keys (`ibt-admin keys`), sets `REALTIME_ALLOWED_ORIGINS`, keeps transcript correction configured |
| Users | Record voice messages, use realtime dictation, review transcripts |

Before you start, check:

- [ ] Odoo 20 Enterprise with the `ai` module installed, and `ibt_custom_ai_provider` 20.0.1.4.0 or later in the addons path, and its master key (section 3).
- [ ] For speech: the IBT Speech Gateway at `v2026.10.16-10` or later, reachable at `https://speech.ibt-homelab.cloud`.
- [ ] The Odoo server resolves the gateway name to the proxy: `getent hosts speech.ibt-homelab.cloud` prints `192.168.10.12`. If not, use the Pi-hole as DNS or add an `/etc/hosts` line.
- [ ] A gateway key for this Odoo, with the routes it needs: `stt` for speech-to-text, plus `realtime` for live dictation and `tts` for voice replies. Get it from the gateway administrator, delivered through a password manager, never by chat or e-mail.
- [ ] For realtime dictation: users open Odoo through **HTTPS** (or `http://localhost` on a developer machine). Browsers block the microphone on other `http://` pages.
- [ ] For chat providers: an API key from the provider (OpenAI, Anthropic), or the address of an Ollama server reachable from the Odoo server.

## 3. Procedure 1: install or upgrade the module

Upgrade the module on every database after pulling a new version; a restart alone does not add new fields or settings.

**Master key first.** From version 20.0.1.2.0, provider API keys are stored encrypted with a master key kept outside the database, so a database backup alone does not expose them. Set the master key before upgrading: without it, an upgrade of a database that holds keys stops with an error and changes nothing.

Generate it once per Odoo server (never reuse the development key in production), with the Odoo server's Python, and store it in the password manager, apart from database backups:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add it to the Odoo configuration file (mode 600, never committed), or set it as the `IBT_AI_SECRET_KEYS` environment variable (for example from a Docker secret). The configuration file wins when both are set. Odoo then logs at startup that `ibt_ai_secret_keys` is an unknown option; that is expected, and the value is never logged.

```ini
[options]
ibt_ai_secret_keys = <master key>
```

1. Pull the module's latest code into the addons path (repository `ibt_custom_ai_provider`, branch `master`).
2. Upgrade the database from the Odoo source folder, with Odoo's virtual environment active:

   ```bash
   python odoo-bin -c <config file> -d <database> -u ibt_custom_ai_provider --stop-after-init
   ```

   For a first installation use `-i ibt_custom_ai_provider` instead of `-u`.
3. Start Odoo again as usual.
4. Check: **Apps** → search *IBT Custom AI Provider* → the module shows as installed, and **Settings** shows the *IBT AI Provider* block below *Integrations*.

The module creates three providers on installation (*OpenAI*, *Anthropic Claude*, *Ollama*); they do nothing until a company selects them. With all its settings off, Odoo keeps using IAP exactly as before.

Upgrading to 20.0.1.4.0 also turns the new model capability flags (section 4) on for models created by hand, so existing setups keep working; discovered Ollama models keep their detected flags, with PDF off.

## 4. Procedure 2: AI chat, reasoning and embedding providers

Create one provider per AI service, list its models, then select the provider and models per company.

**A. Create or edit the provider**

1. Activate developer mode (**Settings** → *Activate the developer mode*).
2. **Settings → Technical → IBT AI Providers** → open the provider (or **New**) and fill in:

| Field | OpenAI / compatible | Anthropic | Ollama (e.g. the DGX Spark) |
| --- | --- | --- | --- |
| Transport | OpenAI compatible | Anthropic | OpenAI compatible |
| Provider kind | Generic | Generic | Ollama |
| Base URL | `https://api.openai.com/v1` | `https://api.anthropic.com/v1` | `https://nvspark.ibt-homelab.cloud/v1` |
| API key | provider key | provider key | the key checked by Nginx Proxy Manager |
| Allow private network | off | off | **on** (the name resolves to a LAN address) |
| Verify SSL | on | on | on |
| Timeout (s) | 120 | 120 | 120, more for slow reasoning models |

3. **Models** tab: add each model ID with its type, *Chat* or *Embedding*. For Ollama, click **Discover Models** instead: it imports the installed models and their capabilities and never deletes models you added by hand.
4. Check the capability columns of each chat model (show them from the list's optional columns): *Supports Tools*, *Supports Images*, *Supports PDF*. Discovery fills them for Ollama (PDF always off, Ollama cannot read PDF files) and refreshes them on every run; models added by hand start with all three on. Untick what an OpenAI-compatible server cannot do.
5. Click **Test Connection** → *Connection successful*.

Rules the module enforces: embedding models must return **1536 dimensions** (Odoo's storage size), an embedding model ID must be unique across providers, Anthropic cannot provide embeddings, and agents need a tool-capable chat model. A request that needs a capability the model lacks (tools, images, PDF files), any other file type, or more than 20 MB of attachments is refused before it is sent, and the user sees a message naming the model.

**B. Select it for the company**

1. **Settings → IBT AI Provider → Custom AI provider**: tick it, then choose *AI Provider*, *Chat Model*, optionally a *Reasoning Model*, an *Embedding Provider* and an *Embedding Model*.
2. **Save.** Provider and models are saved together and checked as a whole, so switching to another provider with its models works in one save.
3. Requests are processed in the background by the scheduled action *AI: Process custom provider requests* (every minute, woken immediately on each request); keep it active.

How requests behave when a provider has trouble:

- A short failure (connection refused, HTTP 429 or 5xx) is retried within the request, at most twice, honoring the provider's `Retry-After`.
- If the provider is still unavailable, the request stays queued and is retried after 30 s, 60 s, 120 s and so on; the user keeps seeing the answer as pending.
- A request still pending 10 minutes after it was sent fails with *The configured AI provider is temporarily unavailable*. A wrong key or model fails at once.
- Each request has one deadline: the provider timeout, and never more than the time the background action has left (`limit_time_real_cron`); a request that cannot finish waits for the next run.

## 5. Procedure 3: speech-to-text with the IBT Speech Gateway

With speech-to-text on, AI chat voice messages and Discuss call recordings are transcribed by the company's gateway instead of Odoo's IAP service.

**A. Create the gateway provider** (developer mode, once per database)

1. **Settings → Technical → IBT AI Providers → New**:

| Field | Value |
| --- | --- |
| Name | `IBT Speech Gateway` |
| Transport | OpenAI compatible |
| Provider kind | **IBT Speech Gateway** |
| Base URL | `https://speech.ibt-homelab.cloud/v1` |
| API key | this Odoo's gateway key, the key alone without `Bearer` |
| Allow private network | on |
| Verify SSL | on |
| Timeout (s) | 120 |

2. **Save → Test Connection** → *Connection successful*. The *Models* tab is hidden for this kind: a gateway needs no models.

**B. Turn it on for the company**

1. **Settings → IBT AI Provider → Speech-to-text**: tick it, set *Speech Gateway* to the provider from step A.
2. Leave *Transcript Correction* on *Off* for now (section 6) and **Save**.

What changes:

- **Voice messages in AI chats**: the user's language is sent (`es_ES` becomes `es`). A language the gateway does not accept is retried once with automatic detection.
- **Call recordings**: transcribed as subtitles (VTT) by Odoo's background queue after the call. A failed attempt is retried up to 5 times.
- **Limit**: recordings above 24 MB are not sent (the call shows *File too large*).

A company that is switched on without a gateway selected is treated as off. With several companies, repeat step B in each.

## 6. Procedure 4: transcript correction

Correction is off by default; turn it on only where Whisper's text needs it, because Whisper usually punctuates clean speech correctly already.

| Mode | What the gateway changes | When to use |
| --- | --- | --- |
| Off | Nothing: Whisper's text as is | Default; clear recordings |
| Punctuation, capitals and accents | Punctuation, capital letters, accents (`garcia` → `García`) | Noisy or fast speech, lower-case output |
| Punctuation, capitals, accents and glossary | The above, then exact replacements from the gateway's glossary (e.g. `45-12` → `4512`, `odoo` → `Odoo`) | Recurring company terms, product names, invoice numbers |

1. Confirm with the gateway administrator that correction is configured on the gateway (`ibt-admin correction` shows *applied* or *no_changes*); for the glossary mode, that `/config/glossary.yaml` holds your terms.
2. **Settings → IBT AI Provider → Speech-to-text → Transcript Correction**: choose the mode → **Save**.

Safeguards (enforced by the gateway, not by the prompt): a correction that changes a number, name, code, e-mail, URL or negation, translates, or rewrites more than 15% of the words is rejected, and the raw transcript is used. If the language model is down or slow, the raw transcript is used too. For call recordings, Whisper's original stays in the call artifact's field *Raw Transcript (VTT)*.

Correction applies to voice messages and call recordings, not to realtime dictation.

## 7. Procedure 5: realtime dictation

Realtime dictation streams the user's microphone to the gateway and writes text after each pause; the gateway key never reaches the browser.

How a session starts:

1. The user clicks **Record**; the browser asks Odoo for a session.
2. The Odoo server asks the gateway, with this Odoo's key, for a single-use secret.
3. Odoo gives the browser only that secret and the gateway's `wss://` address.
4. The browser connects to the gateway with the secret and streams audio; the gateway returns text after each pause.

Steps 1 to 4 take under a second when the user clicks Record; the secret works once, so a copied secret is useless.

**A. On the gateway** (gateway administrator)

1. Give this Odoo's key the realtime route: `docker exec ibt-ai-speech-gateway ibt-admin keys routes <key name> stt,tts,realtime`.
2. Add this Odoo's exact address to *Realtime allowed origins* (`REALTIME_ALLOWED_ORIGINS`), comma-separated, as `scheme://host[:port]`: for example `https://master-odoo20.ibt-homelab.cloud`, plus `http://localhost:8069` for a developer laptop. **Apply**.

**B. In Odoo** (Odoo administrator)

1. Speech-to-text must already be on (section 5).
2. **Settings → IBT AI Provider → Speech-to-text → Realtime Dictation**: tick it → **Save**.
3. Users reload the page (Ctrl+F5) so the browser loads the new code.

**C. Using it** (users)

1. Open an HTML field, such as a Knowledge article, a note or a task description.
2. Type `/` and choose the voice transcription command, then click **Record**.
3. Allow the microphone when the browser asks. Speak in sentences; text appears after each pause.
4. **Stop** ends the session and creates the summary.

Limits: up to 4 simultaneous sessions per key and 30 minutes per session (gateway settings). The widget's language selector sets the language; the notes typed above the transcript are not used as a hint by the gateway. With *Realtime Dictation* off, the widget keeps using Odoo's IAP service.

## 8. Procedure 6: voice replies and voice mode

Voice replies read the AI agent's answers aloud with the gateway's voices, and voice mode does it automatically after a voice message; the audio is generated on demand and never stored.

**A. On the gateway** (gateway administrator)

1. Text-to-speech must be on: *Text-to-speech model* (`TTS_MODEL`) set, `speaches-ai/Kokoro-82M-v1.0-ONNX` by default.
2. Give this Odoo's key the `tts` route: `docker exec ibt-ai-speech-gateway ibt-admin keys routes <key name> stt,tts,realtime`.

**B. In Odoo** (Odoo administrator)

1. Speech-to-text must already be on (section 5).
2. **Settings → IBT AI Provider → Speech-to-text**:

| Setting | Value |
| --- | --- |
| Voice Replies | ticked: a speaker button on agent answers |
| Voice Mode | ticked: answers to voice messages are read aloud automatically |
| Spanish Voice | pick from the list (Spanish voices only), e.g. `ef_dora (es, Female)` or `em_alex (es, Male)` |
| Other Languages Voice | pick from the list (all voices), e.g. `af_heart (en-us, Female)` or `am_michael (en-us, Male)` |

The voice lists come from the gateway itself. Odoo loads them when you click **Test Connection** on the speech gateway provider, **Load voices from the gateway** under the voice fields, or **Discover Voices** on the provider (its **Voices** tab shows them). The first load selects `ef_dora` and `af_heart`; a voice the gateway no longer offers is archived and replaced by the default. Loading the list needs the key's `tts` route (step A2).

When **Voice Mode** is on for the company, each user can switch it off or on for themselves in any AI chat: **More Actions (⋯) → Voice Mode**. The choice is saved on the user and applies on every device; it is on by default.

With Voice Mode on, AI chats open in **Voice Avatar** by default: the chat's messages are replaced by the agent's picture, which shows when it is listening, thinking or speaking. Tap the microphone, speak, and tap it again: the message is sent automatically and every answer is read aloud. **Show chat** (or **More Actions (⋯) → Voice Avatar**) returns to the messages and is remembered for that user; switching Voice Mode off also turns the avatar off. The conversation is kept as usual.

3. **Save**; users reload the page (Ctrl+F5).

**C. Using it** (users)

1. In a chat with an AI agent, click the **speaker** button on an answer to hear it; click it again (**Stop reading**) to stop.
2. Voice mode: hold the microphone button, speak, send. When the agent's answer arrives (within 10 minutes), it is read aloud.

The voice follows the user's language: Spanish users hear the Spanish voice, everyone else the other voice. Code blocks, links and formatting symbols are skipped; long answers play in parts, the first one starting after about a second.

## 9. Verification

Run these checks after every installation, upgrade or key change; each takes under a minute.

- [ ] **Providers**: every provider in use answers **Test Connection** with *Connection successful*.
- [ ] **AI chat**: ask an AI agent a question; the answer arrives (within about a minute, through the background action).
- [ ] **Attachments**: attach an image to a question for a model with *Supports Images*; the answer refers to it. With an Ollama model, a PDF is refused with a message naming the model.
- [ ] **Voice message**: in a chat with an AI agent, hold the microphone button, say a sentence with a name and a number, release; the text appears in the input box with correct accents.
- [ ] **Realtime dictation**: in a Knowledge article, start the voice transcription, speak two sentences with a pause; both appear while you speak.
- [ ] **Voice replies**: click the speaker button on an agent answer; it is read aloud in the user's language. With voice mode on, send a voice message: the answer is read aloud when it arrives.
- [ ] **Call recording** (when used): after a recorded Discuss call, the transcript appears on the call.
- [ ] **Gateway side** (gateway administrator), within 10 minutes of the tests:

  ```bash
  docker logs --since 10m ibt-ai-speech-gateway 2>&1 | grep '"client":"<key name>"' | tail -5
  ```

  Expect `ibt-transcriptions` lines with `"status":200` and their `correction_status`, and for dictation a `realtime-session` line followed by a `realtime` line with `"status":101`.

Record the date, Odoo database, module version and result of each check in the change log of the affected database.

Before a release, developers run `ci/run-tests.sh` (README, "Tests and CI"): a clean install, an upgrade from the version in production with a stored key, the module's tests both times, and an uninstall.

## 10. Keys, security and maintenance

Every Odoo database gets its own gateway key, so one can be revoked without touching the others.

| Task | How |
| --- | --- |
| New key for an Odoo | Gateway: `ibt-admin keys add <name> stt,tts,realtime` (printed once) → password manager → provider's *API key* field |
| Rotate a key (yearly, or when someone with access leaves) | Gateway: `ibt-admin keys rotate <name>` → paste the new key into the provider → **Test Connection**. The old key stops working at once |
| Revoke a key (database retired, key exposed) | Gateway: `ibt-admin keys revoke <name>` |
| Check which key Odoo holds | Gateway: `ibt-admin keys list` shows a fingerprint per key; compare with `printf %s '<key>' \| sha256sum \| cut -c1-12` |
| Rotate an AI provider key (OpenAI, Anthropic, Ollama proxy) | Issue the new key at the provider, paste it into the provider record, **Test Connection**, then revoke the old one |
| Set, replace or remove a key in Odoo | Provider form: type the key in API key and save; the provider and its models stay. The key is stored encrypted and never shown again, even to administrators; the form only says that a key is stored. "remove it" deletes the stored key |
| Rotate the master key (yearly, or when someone with access to the server configuration leaves) | Generate a new master key (section 3) → `ibt_ai_secret_keys = <new>,<old>` → restart Odoo → `odoo-bin shell -c <config> -d <database>`, then `env["ibt.ai.provider"]._ibt_rotate_api_keys()` and `env.cr.commit()` → remove `<old>` → restart Odoo |
| Master key lost | Stored keys cannot be recovered: set a new master key, restart, and type every provider key again |
| Read two databases with different master keys on one server | List both: `ibt_ai_secret_keys = <key A>,<key B>`; the first encrypts new keys |

Rules:

- Keys travel only through a password manager: never by chat, e-mail, tickets or screenshots. A key that was exposed is rotated the same day. The master key is kept the same way and never stored with database backups; neutralized copies of a database (staging, tests) lose their provider keys.
- Only Settings administrators can change providers and replace their keys; nobody, administrators included, can read a stored key back, and ordinary users use the features without access to them.
- Odoo and the gateway log metadata only (sizes, language, status, timings), never audio, transcripts or keys.
- Audio goes only to the company gateway; with correction on, the transcript text (never audio) goes to the company's own language model server. Nothing is sent to an external provider unless a chat provider such as OpenAI or Anthropic is selected.
- After each module upgrade, run section 9.

## 11. Troubleshooting

Start with **Test Connection** and the gateway log line for the key; the gateway names the reason for every refusal.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| *Test Connection*: hostname cannot be resolved / could not be reached | The Odoo server does not resolve the name, or the Base URL is wrong | `getent hosts speech.ibt-homelab.cloud` on the Odoo server; use the Pi-hole or add `/etc/hosts`; Base URL without a port, ending in `/v1` |
| *Test Connection*: resolves to an address that is not allowed | *Allow private network* is off | Tick it for LAN services (gateway, Ollama) |
| *Test Connection*: error 401 | Wrong key, or `Bearer` typed in front of it | Paste the key alone; check its fingerprint (section 10) |
| *Test Connection*: voices could not be loaded | The key lacks the `tts` route | `ibt-admin keys routes <name> stt,tts,realtime` |
| Voice message: *Transcription failed* | Gateway unreachable, or the key lacks the `stt` route | Server log line *Speech gateway transcription failed: HTTP \<code>*; 401/403 = key or route |
| Dictation does not start; gateway log `origin_not_allowed` | The Odoo address is not in *Realtime allowed origins* | Add the exact `scheme://host[:port]` the browser shows |
| Dictation refused with *rejected Odoo's credentials* | The key lacks the `realtime` route | `ibt-admin keys routes <name> stt,tts,realtime` |
| Microphone blocked by the browser | Odoo opened over plain `http://` (not localhost) | Open Odoo through its HTTPS address |
| Dictation still goes to OpenAI | The browser runs old code, or *Realtime Dictation* is off | Ctrl+F5; check the setting |
| Transcripts never corrected | Gateway correction not configured, or text already correct | Gateway log `correction_status`: `unavailable` = no correction URL, `ollama_error` = language model unreachable, `no_changes` = nothing to fix |
| Call transcript missing, shows *File too large* | Recording above 24 MB | Shorter recordings; the limit is the gateway's upload size |
| AI answers never arrive | The background action is inactive | **Settings → Technical → Scheduled Actions** → *AI: Process custom provider requests* active |
| AI answer pending for minutes, then *temporarily unavailable* | The provider was unreachable or kept failing for 10 minutes | Server log lines *AI provider request ... failed* (each retry) and *gave up*; check the provider with **Test Connection** |
| *The AI model X cannot handle tools / images / PDF files* | The selected chat model lacks that capability | Choose another chat model in Settings, or tick the capability on the model if the server supports it |
| *The AI provider returned an answer that does not match the requested structure* | The model ignored the JSON schema an Odoo feature asked for | Use a model with structured output support; retry the action |
| No speaker button on agent answers | Voice Replies off, or the page still runs old code | Tick Voice Replies (section 8); Ctrl+F5 |
| "The answer could not be read aloud" | The key lacks the tts route, or text-to-speech is off on the gateway | Server log: Speech gateway text-to-speech failed: HTTP \<code>; `ibt-admin keys routes <name> stt,tts,realtime` |
| Voice mode does not read the answer | Voice Mode off, the chat window was closed when the answer arrived, or more than 10 minutes passed | Keep the chat open; use the speaker button |
| Upgrade, Test Connection or a request: "No master key is configured" | `ibt_ai_secret_keys` missing, or Odoo not restarted after setting it | Set the master key (section 3) and restart Odoo |
| "A provider API key cannot be decrypted" | Master key changed or lost, or the database comes from another server | Put the old master key back (after the new one), or type the provider key again |

## 12. References and revision history

**References** (repositories, private):

- `ibt_custom_ai_provider`: `README.md` (providers, models, keys, tests and CI), `PRODUCTION_READINESS.md` (open work before production), `docs/SOP.md` (this document).
- `ibt_ai_speech_gateway`: `docs/odoo-integration.md` (Odoo setup and request contract), `docs/clients.md` (keys and routes), `docs/realtime.md` (live dictation), `docs/transcript-correction.md` (correction and glossary), `docs/operations.md` (gateway troubleshooting).

| Date | Version | Change |
| --- | --- | --- |
| 2026-10-17 | 20.0.1.4.0 | Model capability flags (tools, images, PDF) checked before sending; PDF files sent to Anthropic and OpenAI-compatible APIs; structured answers validated |
| 2026-10-17 | 20.0.1.3.0 | Queued requests retried after transient failures, failed after 10 minutes |
| 2026-10-17 | 20.0.1.2.0 | Provider keys encrypted with a master key; Settings saves provider and models together; bounded, retried provider requests |
| 2026-10-17 | 20.0.1.1.0 | First version: chat and embedding providers, speech-to-text, transcript correction, realtime dictation |
