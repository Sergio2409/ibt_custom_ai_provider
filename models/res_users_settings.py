from odoo import fields, models


class ResUsersSettings(models.Model):
    _inherit = "res.users.settings"

    ibt_voice_mode = fields.Boolean(
        string="Voice Mode", default=True,
        help="Read answers to voice messages aloud in AI chats, when the company allows voice mode.",
    )
    ibt_voice_avatar = fields.Boolean(
        string="Voice Avatar",
        help="Show a talking avatar instead of the AI chat's messages; every answer is read aloud.",
    )

    def _store_settings_fields(self, res):
        super()._store_settings_fields(res)
        res.extend(["ibt_voice_mode", "ibt_voice_avatar"])
