import base64
import json
from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.web.tests.test_js import HootCommon, unit_test_error_checker

from ..services import speech
from ..services.client import ProviderClient, ProviderError

REQUEST = "odoo.addons.ibt_custom_ai_provider.services.client.ProviderClient._request"
AUDIO = b"ID3 fake mp3 audio"
CONFIG = {
    "transport": "openai",
    "base_url": "https://speech.example.com/v1",
    "api_key": "gateway-key",
    "allow_private_network": True,
    "timeout": 120,
    "correction": "off",
}


def gateway_answer(raw="hola señor", corrected=None, status="disabled", vtt=False):
    raw_part = {"text": raw, "language": "es"}
    corrected_part = {"text": corrected} if corrected else None
    if vtt:
        raw_part["vtt"] = f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{raw}\n"
        if corrected_part:
            corrected_part["vtt"] = f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{corrected}\n"
    return {"raw": raw_part, "corrected": corrected_part, "changes": [], "correction_status": status}


def gateway_error(status, code=None):
    error = ProviderError("AI provider error")
    error.status_code = status
    error.error_code = code
    return error


class TestSpeechService(TransactionCase):

    def test_language_mapping(self):
        for code, expected in [("es_ES", "es"), ("es-MX", "es"), ("EN", "en"), ("sr@latin", "sr"),
                               ("", None), (None, None), ("x", None), ("es_419", "es")]:
            with self.subTest(code=code):
                self.assertEqual(speech.stt_language(code), expected)

    @patch(REQUEST)
    def test_transcribe_uses_the_correction_route(self, request):
        request.return_value = gateway_answer(corrected="Hola, señor.", status="applied")
        result = speech.transcribe(dict(CONFIG, correction="format"), AUDIO, "audio/mpeg", language="es_ES")

        method, path = request.call_args.args
        self.assertEqual((method, path), ("POST", "ibt/transcriptions"))
        data = dict(request.call_args.kwargs["data"])
        self.assertEqual(data, {"model": "whisper-1", "response_format": "json", "correction": "format", "language": "es"})
        name, content, mimetype = request.call_args.kwargs["files"]["file"]
        self.assertEqual((name, content, mimetype), ("audio.mp3", AUDIO, "audio/mpeg"))
        self.assertEqual(result["text"], "Hola, señor.")
        self.assertEqual(result["raw_text"], "hola señor")
        self.assertTrue(result["corrected"])

    @patch(REQUEST)
    def test_raw_text_without_correction(self, request):
        request.return_value = gateway_answer(status="no_changes")
        result = speech.transcribe(CONFIG, AUDIO, "audio/webm;codecs=opus")
        self.assertEqual(result["text"], "hola señor")
        self.assertFalse(result["corrected"])
        self.assertNotIn("language", dict(request.call_args.kwargs["data"]))
        self.assertEqual(request.call_args.kwargs["files"]["file"][0], "audio.webm")

    @patch(REQUEST)
    def test_vtt_keeps_raw_and_corrected(self, request):
        request.return_value = gateway_answer(corrected="Hola, señor.", status="applied", vtt=True)
        result = speech.transcribe(CONFIG, AUDIO, "audio/ogg", response_format="vtt")
        self.assertIn("Hola, señor.", result["vtt"])
        self.assertIn("hola señor", result["raw_vtt"])

    @patch(REQUEST)
    def test_unsupported_language_falls_back_to_auto_detection(self, request):
        request.side_effect = [gateway_error(400, "language_not_enabled"), gateway_answer()]
        with mute_logger("odoo.addons.ibt_custom_ai_provider.services.speech"):
            result = speech.transcribe(CONFIG, AUDIO, "audio/mpeg", language="fr_FR")
        self.assertEqual(result["text"], "hola señor")
        first, second = (dict(call.kwargs["data"]) for call in request.call_args_list)
        self.assertEqual(first["language"], "fr")
        self.assertNotIn("language", second)

    @patch(REQUEST)
    def test_other_errors_are_not_retried(self, request):
        for error in (gateway_error(400, "invalid_parameter"), gateway_error(401, "invalid_api_key"), gateway_error(502)):
            with self.subTest(status=error.status_code):
                request.reset_mock()
                request.side_effect = error
                with self.assertRaises(ProviderError):
                    speech.transcribe(CONFIG, AUDIO, "audio/mpeg", language="es")
                self.assertEqual(request.call_count, 1)

    @patch(REQUEST)
    def test_audio_too_large(self, request):
        with self.assertRaises(ProviderError) as ctx:
            speech.transcribe(CONFIG, b"x" * (speech.MAX_AUDIO_BYTES + 1), "audio/mpeg")
        self.assertEqual(ctx.exception.status_code, 413)
        request.assert_not_called()

    def test_user_messages(self):
        for status, fragment in [(401, "credentials"), (403, "credentials"), (413, "too long"),
                                 (415, "audio format"), (502, "not available"), (None, "not available")]:
            with self.subTest(status=status):
                self.assertIn(fragment, speech.user_message(self.env, gateway_error(status)))

    @patch("odoo.addons.ibt_custom_ai_provider.services.client.requests.Session")
    @patch("odoo.addons.ibt_custom_ai_provider.services.client.socket.getaddrinfo")
    def test_multipart_request_and_error_codes(self, getaddrinfo, Session):
        getaddrinfo.return_value = [(2, 1, 6, "", ("192.168.10.12", 443))]
        response = MagicMock(ok=False, is_redirect=False, status_code=400, reason="Bad Request")
        response.json.return_value = {"error": {"message": "Language 'fr' is not enabled.", "code": "language_not_enabled"}}
        session = Session.return_value.__enter__.return_value
        session.request.return_value = response

        with self.assertRaises(ProviderError) as ctx:
            ProviderClient(CONFIG)._request("POST", "ibt/transcriptions", files={"file": ("a.mp3", AUDIO, "audio/mpeg")},
                                            data=[("model", "whisper-1")])

        self.assertEqual((ctx.exception.status_code, ctx.exception.error_code), (400, "language_not_enabled"))
        kwargs = session.request.call_args.kwargs
        self.assertNotIn("Content-Type", kwargs["headers"])  # requests sets the multipart boundary
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer gateway-key")
        self.assertEqual(session.request.call_args.args[1], "https://192.168.10.12/v1/ibt/transcriptions")


class SpeechSetupMixin:

    @classmethod
    def _create_speech_setup(cls):
        cls.speech_provider = cls.env["ibt.ai.provider"].create({
            "name": "Speech gateway",
            "provider_kind": "speech_gateway",
            "base_url": "https://speech.example.com/v1",
            "api_key": "gateway-key",
            "allow_private_network": True,
        })
        cls.chat_provider = cls.env["ibt.ai.provider"].create({
            "name": "Chat provider",
            "base_url": "https://ai.example.com/v1",
            "model_ids": [Command.create({"name": "chat-model", "model_type": "chat"})],
        })
        cls.company = cls.env.company
        cls.company.write({"ibt_speech_enabled": True, "ibt_speech_provider_id": cls.speech_provider.id})


class TestSpeechConfig(SpeechSetupMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._create_speech_setup()
        cls.user = new_test_user(cls.env, login="ibt-speech-user", groups="base.group_user")

    def test_regular_user_gets_a_snapshot_with_correction(self):
        self.company.ibt_speech_correction = "format"
        config = self.company.with_user(self.user)._ibt_speech_config()
        self.assertEqual(config["base_url"], "https://speech.example.com/v1")
        self.assertEqual(config["api_key"], "gateway-key")
        self.assertEqual(config["correction"], "format")

    def test_disabled_means_no_snapshot(self):
        self.company.ibt_speech_enabled = False
        self.assertIsNone(self.company._ibt_speech_config())

    def test_speech_provider_must_be_a_speech_gateway(self):
        with self.assertRaises(ValidationError):
            self.company.ibt_speech_provider_id = self.chat_provider
        with self.assertRaises(ValidationError):
            self.company.write({"ibt_speech_provider_id": False})

    def test_speech_gateway_is_not_a_chat_provider(self):
        with self.assertRaises(ValidationError):
            self.company.write({
                "ibt_ai_enabled": True,
                "ibt_ai_provider_id": self.speech_provider.id,
                "ibt_ai_chat_model_id": self.chat_provider.model_ids.id,
            })

    def test_selected_gateway_stays_a_gateway_and_active(self):
        with self.assertRaises(ValidationError):
            self.speech_provider.provider_kind = "generic"
        with self.assertRaises(ValidationError):
            self.speech_provider.active = False

    def test_regular_user_cannot_read_the_gateway_key(self):
        with self.assertRaises(AccessError):
            self.speech_provider.with_user(self.user).read(["api_key"])


@tagged("post_install", "-at_install")
class TestSpeechController(SpeechSetupMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._create_speech_setup()
        cls.user = new_test_user(cls.env, login="ibt-speech-http", password="ibt-speech-http-pw",
                                 groups="base.group_user", company_id=cls.company.id)

    def setUp(self):
        super().setUp()
        self.authenticate("ibt-speech-http", "ibt-speech-http-pw")

    def transcribe(self, audio=AUDIO, language="es"):
        return self.make_jsonrpc_request("/ai/transcription", {
            "audio_base64": base64.b64encode(audio).decode() if isinstance(audio, bytes) else audio,
            "language": language,
        })

    @patch(REQUEST)
    def test_voice_message_uses_the_gateway(self, request):
        request.return_value = gateway_answer(raw="hola, señor")
        self.assertEqual(self.transcribe(), {"status": "success", "text": "hola, señor"})
        self.assertEqual(dict(request.call_args.kwargs["data"])["language"], "es")

    @patch(REQUEST)
    def test_corrected_text_is_returned(self, request):
        self.company.ibt_speech_correction = "format"
        request.return_value = gateway_answer(corrected="Hola, señor.", status="applied")
        self.assertEqual(self.transcribe()["text"], "Hola, señor.")
        self.assertEqual(dict(request.call_args.kwargs["data"])["correction"], "format")

    @patch("odoo.addons.ai.controllers.ai.call_odoo_ai")
    @patch(REQUEST)
    def test_disabled_gateway_keeps_odoo_iap(self, request, call_odoo_ai):
        self.company.ibt_speech_enabled = False
        call_odoo_ai.return_value = {"text": "from iap"}
        self.assertEqual(self.transcribe(), {"status": "success", "text": "from iap"})
        request.assert_not_called()

    @mute_logger("odoo.http", "odoo.addons.ibt_custom_ai_provider.controllers.ai")
    @patch(REQUEST)
    def test_gateway_errors_become_user_errors(self, request):
        request.side_effect = gateway_error(401, "invalid_api_key")
        with self.assertRaises(JsonRpcException) as ctx:
            self.transcribe()
        self.assertEqual(ctx.exception.args[0], "odoo.exceptions.UserError")

    @mute_logger("odoo.http")
    @patch(REQUEST)
    def test_invalid_audio_is_rejected(self, request):
        with self.assertRaises(JsonRpcException):
            self.transcribe(audio="not base64!")
        request.assert_not_called()


class TestSpeechCallArtifact(SpeechSetupMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._create_speech_setup()
        channel = cls.env["discuss.channel"].create({"name": "IBT speech channel"})
        cls.call = cls.env["discuss.call.history"].create({"start_dt": "2026-10-16 10:00:00", "channel_id": channel.id})

    def _pending_artifact(self):
        artifact = self.env["mail.call.artifact"].create({
            "discuss_call_history_id": self.call.id,
            "start_ms": 0,
            "end_ms": 1000,
            "is_stt": True,
            "transcription_state": "pending",
        })
        self.env["ir.attachment"].create({
            "name": "transcription.webm",
            "raw": AUDIO,
            "res_model": artifact._name,
            "res_id": artifact.id,
            "mimetype": "audio/webm",
        })
        artifact.invalidate_recordset(["media_id"])
        [(record_id, lease_until)] = artifact._claim_records_to_transcribe()
        return artifact.browse(record_id), lease_until

    @patch(REQUEST)
    def test_recording_is_transcribed_by_the_gateway(self, request):
        request.return_value = gateway_answer(raw="hola señor", vtt=True)
        artifact, lease_until = self._pending_artifact()
        result = artifact._transcribe_claimed_record(lease_until)

        self.assertEqual(result["status"], "ok")
        data = dict(request.call_args.kwargs["data"])
        self.assertEqual(data["response_format"], "vtt")
        self.assertNotIn("language", data)
        self.assertEqual(request.call_args.kwargs["files"]["file"][2], "audio/webm")
        self.assertTrue(artifact._finalize_transcription(lease_until, result))
        self.assertEqual(artifact.transcription_state, "done")
        self.assertIn("hola señor", artifact.transcript)
        self.assertFalse(artifact.ibt_transcript_raw)

    @patch(REQUEST)
    def test_corrected_recording_keeps_the_raw_transcript(self, request):
        self.company.ibt_speech_correction = "glossary"
        request.return_value = gateway_answer(raw="hola señor", corrected="Hola, señor.", status="applied", vtt=True)
        artifact, lease_until = self._pending_artifact()
        artifact._finalize_transcription(lease_until, artifact._transcribe_claimed_record(lease_until))
        self.assertIn("Hola, señor.", artifact.transcript)
        self.assertIn("hola señor", artifact.ibt_transcript_raw)

    @mute_logger("odoo.addons.ibt_custom_ai_provider.models.mail_call_artifact")
    @patch(REQUEST)
    def test_gateway_error_is_retried_later(self, request):
        request.side_effect = gateway_error(502)
        artifact, lease_until = self._pending_artifact()
        result = artifact._transcribe_claimed_record(lease_until)
        self.assertEqual(result, {"status": "err"})
        artifact._finalize_transcription(lease_until, result)
        self.assertEqual(artifact.transcription_state, "pending")
        self.assertEqual(artifact.transcription_error_count, 1)

    @patch("odoo.addons.ai.models.mail_call_artifact.call_odoo_ai")
    @patch(REQUEST)
    def test_disabled_gateway_keeps_odoo_iap(self, request, call_odoo_ai):
        self.company.ibt_speech_enabled = False
        call_odoo_ai.return_value = {"text": "WEBVTT\n"}
        artifact, lease_until = self._pending_artifact()
        self.assertEqual(artifact._transcribe_claimed_record(lease_until), {"status": "ok", "transcript": "WEBVTT\n"})
        request.assert_not_called()


@tagged("post_install", "-at_install")
class TestRealtimeSession(SpeechSetupMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._create_speech_setup()
        cls.company.ibt_speech_realtime = True
        new_test_user(cls.env, login="ibt-realtime-http", password="ibt-realtime-http-pw",
                      groups="base.group_user", company_id=cls.company.id)

    def setUp(self):
        super().setUp()
        self.authenticate("ibt-realtime-http", "ibt-realtime-http-pw")

    def request_session(self, language="es", prompt="Notas"):
        return self.make_jsonrpc_request("/ai/transcription/session", {"language": language, "prompt": prompt})

    @staticmethod
    def decode(session_token):
        prefix, _, encoded = session_token.partition(".")
        return prefix, json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    @patch(REQUEST)
    def test_session_token_points_the_browser_to_the_gateway(self, request):
        request.return_value = {"client_secret": {"value": "ek_secret", "expires_at": 1}}
        result = self.request_session(language="es_ES", prompt="x" * 3000)

        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["iap_transaction_token"])
        prefix, payload = self.decode(result["session_token"])
        self.assertEqual(prefix, "ibt")
        self.assertEqual(payload, {"url": "wss://speech.example.com/v1/realtime", "secret": "ek_secret"})
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ("POST", "realtime/transcription_sessions"))
        transcription = body["input_audio_transcription"]
        self.assertEqual((transcription["model"], transcription["language"]), ("whisper-1", "es"))
        self.assertEqual(len(transcription["prompt"]), 2000)

    @patch(REQUEST)
    def test_unsupported_language_falls_back_to_auto_detection(self, request):
        request.side_effect = [gateway_error(400, "language_not_enabled"),
                               {"client_secret": {"value": "ek_auto", "expires_at": 1}}]
        with mute_logger("odoo.addons.ibt_custom_ai_provider.services.speech"):
            result = self.request_session(language="fr")
        self.assertEqual(self.decode(result["session_token"])[1]["secret"], "ek_auto")
        self.assertNotIn("language", request.call_args.args[2]["input_audio_transcription"])

    @patch("odoo.addons.ai.controllers.ai.call_odoo_ai")
    @patch(REQUEST)
    def test_realtime_off_keeps_odoo_iap(self, request, call_odoo_ai):
        self.company.ibt_speech_realtime = False
        call_odoo_ai.return_value = {"session_token": "openai-token", "iap_transaction_token": "iap"}
        self.assertEqual(self.request_session(), {"status": "success", "session_token": "openai-token",
                                          "iap_transaction_token": "iap"})
        request.assert_not_called()

    @mute_logger("odoo.http", "odoo.addons.ibt_custom_ai_provider.controllers.ai")
    @patch(REQUEST)
    def test_gateway_errors_become_user_errors(self, request):
        for error in (gateway_error(403, "route_not_allowed"), gateway_error(502)):
            with self.subTest(status=error.status_code):
                request.side_effect = error
                with self.assertRaises(JsonRpcException) as ctx:
                    self.request_session()
                self.assertEqual(ctx.exception.args[0], "odoo.exceptions.UserError")

    def test_realtime_url(self):
        for base_url, expected in [("https://speech.example.com/v1", "wss://speech.example.com/v1/realtime"),
                                   ("http://192.168.10.25:8080/v1/", "ws://192.168.10.25:8080/v1/realtime")]:
            with self.subTest(base_url=base_url):
                self.assertEqual(speech.realtime_url({"base_url": base_url}), expected)


@tagged("post_install", "-at_install")
class TestRealtimeClientPatch(HttpCase):
    """Runs static/tests (hoot) of this module in headless Chrome."""

    def test_hoot(self):
        new_test_user(self.env, login="ibt_hoot", password="ibt_hoot", groups="base.group_user")
        module_id = HootCommon._generate_hash(self, "@ibt_custom_ai_provider")
        self.browser_js(
            f"/web/tests?headless&loglevel=2&preset=desktop&timeout=15000&id={module_id}", "", "",
            login="ibt_hoot", timeout=600, success_signal="[HOOT] Test suite succeeded",
            error_checker=unit_test_error_checker,
        )
