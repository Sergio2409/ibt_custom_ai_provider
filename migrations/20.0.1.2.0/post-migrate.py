from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ibt.ai.provider"]._ibt_encrypt_plain_api_keys()
