from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        info = super().session_info()
        config = self.env.company._ibt_speech_config() if self.env.user._is_internal() else None
        # Only switches, never the gateway address or key.
        info["ibt_voice_replies"] = bool(config and config["voice_replies"])
        info["ibt_voice_mode"] = bool(config and config["voice_mode"])
        return info
