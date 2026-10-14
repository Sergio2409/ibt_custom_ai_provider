import copy
import logging
import uuid

from odoo import api, fields, models

from ..services.client import ProviderClient, ProviderError

_logger = logging.getLogger(__name__)
BATCH_SIZE = 4


class AiSession(models.Model):
    _inherit = "ai.session"

    ibt_ai_provider_id = fields.Many2one("ibt.ai.provider", copy=False, readonly=True)
    ibt_ai_company_id = fields.Many2one("res.company", copy=False, readonly=True)

    def _get_completions(self, messages, instructions, tools=None, **options):
        config = self.env.company._ibt_ai_config(boost_reasoning=options.get("boost_reasoning", False))
        if not config:
            return super()._get_completions(messages, instructions, tools, **options)
        return ProviderClient(config).completions(messages, instructions, tools, **options)

    def _save_and_submit_request(
        self,
        payload,
        *,
        callback_type,
        request_round,
        request_round_limit,
        state,
    ) -> None:
        config = self.env.company._ibt_ai_config(boost_reasoning=payload.get("boost_reasoning", False))
        if not config:
            return super()._save_and_submit_request(
                payload,
                callback_type=callback_type,
                request_round=request_round,
                request_round_limit=request_round_limit,
                state=state,
            )
        self.ensure_one()
        guest = self.env.context.get("guest")
        state = copy.deepcopy(state)
        state["callback_type"] = callback_type
        self.write({
            "loop_state": "waiting_model",
            "request_uuid": str(uuid.uuid4()),
            "request_webhook_secret": uuid.uuid4().hex,
            "request_round": request_round,
            "request_round_limit": request_round_limit,
            "request_payload": payload,
            "request_user_id": self.env.uid,
            "request_guest_id": guest.id if guest else False,
            "request_context": self._get_request_context_snapshot(),
            "resume_token": False,
            "pending_tool_call": False,
            "state": state,
            "ibt_ai_provider_id": config["id"],
            "ibt_ai_company_id": self.env.company.id,
        })
        self._publish_response_state()
        self.env.ref("ibt_custom_ai_provider.ir_cron_ibt_ai_requests")._trigger()

    @api.model
    def _cron_process_ibt_ai_requests(self):
        domain = [
            ("loop_state", "=", "waiting_model"),
            ("ibt_ai_provider_id", "!=", False),
        ]
        sessions = self.sudo().search(domain, order="write_date, id", limit=BATCH_SIZE)
        self.env["ir.cron"]._commit_progress(remaining=self.sudo().search_count(domain))
        for session in sessions:
            self.env.cr.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)",
                ["ibt_custom_ai_provider", session.id],
            )
            if not self.env.cr.fetchone()[0]:
                continue
            session.invalidate_recordset()
            if session.loop_state != "waiting_model" or not session.ibt_ai_provider_id:
                continue
            try:
                with self.env.cr.savepoint():
                    session._process_ibt_ai_request()
            except Exception:
                _logger.exception("Unexpected failure while processing queued AI session %s", session.id)
                try:
                    with self.env.cr.savepoint():
                        session._deliver_ibt_ai_result(
                            {"kind": "failure", "code": "provider_request_failed"},
                            "The queued AI request failed unexpectedly.",
                        )
                except Exception:
                    # Do not let one corrupt session roll back and starve the
                    # rest of the bounded cron batch.
                    _logger.exception("Unable to deliver failure for queued AI session %s", session.id)
                    session.write({"loop_state": "ready", "ibt_ai_provider_id": False})
            if not self.env["ir.cron"]._commit_progress(1):
                break

    def _process_ibt_ai_request(self):
        self.ensure_one()
        company = self.ibt_ai_company_id
        payload = copy.deepcopy(self.request_payload)
        config = company._ibt_ai_config(boost_reasoning=payload.get("boost_reasoning", False))
        if not config or config["id"] != self.ibt_ai_provider_id.id:
            return self._deliver_ibt_ai_result(
                {"kind": "failure", "code": "provider_configuration_changed"},
                "The AI provider configuration changed while this request was queued. Please try again.",
            )
        options = {key: value for key, value in payload.items() if key not in ("messages", "instructions", "tools")}
        try:
            result = ProviderClient(config).completions(
                payload["messages"], payload.get("instructions", ""), payload.get("tools"), **options,
            )
            completion_result = {"kind": "success", "message": result["result"]}
            error_message = None
        except ProviderError as error:
            _logger.warning("AI provider request %s failed: %s", self.request_uuid, error)
            completion_result = {"kind": "failure", "code": "provider_request_failed"}
            error_message = str(error)
        except Exception:
            _logger.exception("Unexpected AI provider failure for request %s", self.request_uuid)
            completion_result = {"kind": "failure", "code": "provider_request_failed"}
            error_message = "The AI provider request failed unexpectedly. Check the server log."
        return self._deliver_ibt_ai_result(completion_result, error_message)

    def _deliver_ibt_ai_result(self, completion_result, error_message=None):
        self.ensure_one()
        context = dict(self.request_context or {})
        base_user_env = api.Environment(self.env.cr, self.request_user_id.id, {})
        accessible_companies = set(base_user_env.user.company_ids.ids)
        company_access_changed = False
        for key in ("allowed_company_ids", "active_company_ids"):
            requested_ids = context.get(key, ())
            if any(company_id not in accessible_companies for company_id in requested_ids):
                company_access_changed = True
                context[key] = [company_id for company_id in requested_ids if company_id in accessible_companies]
                if not context[key]:
                    context[key] = [base_user_env.company.id]
        if company_access_changed:
            completion_result = {"kind": "failure", "code": "company_access_changed"}
            error_message = base_user_env._(
                "Your company access changed while this AI response was running. Please try again."
            )
        if self.request_guest_id:
            context["guest"] = self.env["mail.guest"].browse(self.request_guest_id.id)
        context["discuss_channel"] = self.channel_id
        user_env = api.Environment(self.env.cr, self.request_user_id.id, context)
        session = self.with_env(user_env).sudo()
        callback_type = session.state["callback_type"]
        if callback_type == "agent_loop":
            if completion_result["kind"] == "failure":
                if completion_result.get("code") == "provider_configuration_changed":
                    user_message = error_message or self.env._(
                        "The AI provider configuration changed while this request was queued. Please try again."
                    )
                elif completion_result.get("code") == "company_access_changed":
                    user_message = error_message or self.env._(
                        "Your company access changed while this AI response was running. Please try again."
                    )
                else:
                    # Detailed provider exceptions can contain internal hostnames,
                    # ports and API paths. They are logged by the caller and must
                    # not be exposed in the user-facing conversation.
                    user_message = self.env._(
                        "The configured AI provider is temporarily unavailable. "
                        "Please try again later or contact an administrator."
                    )
                session._finish_exchange("failed", content=user_message)
            else:
                session._continue_agent_loop(completion_result)
        elif callback_type == "channel_name":
            session._continue_channel_name(completion_result)
        # A successful tool call can immediately queue the next model round.
        # Keep the provider on that pending request so the cron can process it.
        if session.exists() and session.loop_state != "waiting_model":
            session.ibt_ai_provider_id = False
