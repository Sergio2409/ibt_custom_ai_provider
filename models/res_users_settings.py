from odoo import fields, models


class ResUsersSettings(models.Model):
    _inherit = "res.users.settings"

    ibt_voice_mode = fields.Boolean(
        string="Voice Mode", default=True,
        help="Read answers to voice messages aloud in AI chats, when the company allows voice mode.",
    )

    def _store_settings_fields(self, res):
        super()._store_settings_fields(res)
        res.extend(["ibt_voice_mode"])
