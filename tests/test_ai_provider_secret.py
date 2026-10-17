import os
from contextlib import contextmanager
from unittest.mock import patch

import requests
from cryptography.fernet import Fernet

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import SQL, config, sql

from ..services.client import ProviderClient, ProviderError

MASTER_KEY = Fernet.generate_key().decode()
API_KEY = "sk-test-0123456789abcdef"


@contextmanager
def master_keys(*keys):
    """Set the master keys of both sources: the configuration option and the environment."""
    with patch.dict(config.options, {"ibt_ai_secret_keys": ",".join(keys)}), \
            patch.dict(os.environ, {"IBT_AI_SECRET_KEYS": ""}):
        yield


class TestProviderApiKey(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(master_keys(MASTER_KEY))
        cls.provider = cls.env["ibt.ai.provider"].create({
            "name": "Keyed provider",
            "base_url": "http://127.0.0.1:9/v1",
            "allow_private_network": True,
            "api_key": API_KEY,
            "model_ids": [Command.create({"name": "chat-model", "model_type": "chat"})],
        })
        cls.env.company.write({
            "ibt_ai_enabled": True,
            "ibt_ai_provider_id": cls.provider.id,
            "ibt_ai_chat_model_id": cls.provider.model_ids.id,
        })
        cls.user = new_test_user(cls.env, login="ibt-key-user", groups="base.group_user,base.group_allow_export")
        cls.portal = new_test_user(cls.env, login="ibt-key-portal", groups="base.group_portal")

    def _stored(self, provider=None):
        self.env.cr.execute(SQL(
            "SELECT api_key_secret FROM ibt_ai_provider WHERE id = %s", (provider or self.provider).id,
        ))
        return self.env.cr.fetchone()[0]

    def _add_plain_key_column(self, key):
        self.env.cr.execute("ALTER TABLE ibt_ai_provider ADD COLUMN api_key varchar")
        self.env.cr.execute(SQL(
            "UPDATE ibt_ai_provider SET api_key = %s, api_key_secret = NULL WHERE id = %s", key, self.provider.id,
        ))

    def test_key_is_stored_encrypted_and_never_read_back(self):
        self.assertTrue(self._stored())
        self.assertNotIn(API_KEY, self._stored())
        self.provider.invalidate_recordset()
        self.assertFalse(self.provider.api_key)
        self.assertTrue(self.provider.api_key_set)
        fields = [name for name, field in self.provider._fields.items() if field.exportable]
        self.assertNotIn(API_KEY, str(self.provider.read()))
        self.assertNotIn(API_KEY, str(self.provider.export_data(fields)))
        self.assertEqual(self.provider._snapshot()["api_key"], API_KEY)

    def test_saved_form_does_not_return_the_key(self):
        result = self.provider.web_save({"api_key": "sk-typed-in-form"}, {"api_key": {}, "api_key_set": {}})
        self.assertEqual((result[0]["api_key"], result[0]["api_key_set"]), (False, True))
        self.assertEqual(self.provider._snapshot()["api_key"], "sk-typed-in-form")

    def test_other_users_cannot_read_the_key(self):
        for user in (self.user, self.portal):
            with self.subTest(user=user.login):
                provider = self.provider.with_user(user)
                with self.assertRaises(AccessError):
                    provider.read(["api_key"])
                with self.assertRaises(AccessError):
                    provider.search_read([], ["name"])
                # Portal users have no export right at all.
                with self.assertRaises(AccessError if user == self.user else UserError):
                    provider.export_data(["name"])
                with self.assertRaises(AccessError):
                    self.env.company.with_user(user).ibt_ai_provider_id.api_key  # noqa: B018

    def test_regular_user_snapshot_carries_the_key(self):
        config = self.env.company.with_user(self.user)._ibt_ai_config()
        self.assertEqual(config["api_key"], API_KEY)

    def test_new_key_replaces_without_recreating_and_remove_clears(self):
        models = self.provider.model_ids
        self.provider.api_key = "sk-replaced"
        self.assertEqual(self.provider._snapshot()["api_key"], "sk-replaced")
        self.assertEqual(self.provider.model_ids, models)
        self.provider.action_remove_api_key()
        self.assertFalse(self.provider.api_key_set)
        self.assertIsNone(self._stored())
        self.assertEqual(self.provider._snapshot()["api_key"], "")

    def test_copy_has_no_key(self):
        copy = self.provider.copy()
        self.assertFalse(copy.api_key_set)
        self.assertEqual(copy._snapshot()["api_key"], "")

    def test_missing_or_wrong_master_key(self):
        with master_keys(), self.assertRaisesRegex(ProviderError, "No master key") as missing:
            self.provider._snapshot()
        with master_keys(Fernet.generate_key().decode()), \
                self.assertRaisesRegex(ProviderError, "cannot be decrypted") as wrong:
            self.provider._snapshot()
        with master_keys("not-a-fernet-key"), self.assertRaisesRegex(ProviderError, "valid Fernet keys"):
            self.provider._snapshot()
        with master_keys(), self.assertRaisesRegex(ProviderError, "No master key"), self.env.cr.savepoint():
            self.provider.api_key = "sk-not-stored"
        for error in (missing.exception, wrong.exception):
            self.assertNotIn(API_KEY, str(error))
        self.assertIsNotNone(self._stored())
        with master_keys():
            keyless = self.provider.copy()
            self.assertEqual(keyless._snapshot()["api_key"], "")

    def test_master_key_rotation(self):
        # Rotation fails on any key it cannot decrypt: keep only this test's key.
        self.env.cr.execute(SQL(
            "UPDATE ibt_ai_provider SET api_key_secret = NULL WHERE id != %s", self.provider.id,
        ))
        new_master_key = Fernet.generate_key().decode()
        before = self._stored()
        with master_keys(new_master_key, MASTER_KEY):
            self.assertEqual(self.env["ibt.ai.provider"]._ibt_rotate_api_keys(), 1)
        self.assertNotEqual(self._stored(), before)
        with master_keys(new_master_key):
            self.assertEqual(self.provider._snapshot()["api_key"], API_KEY)

    def test_upgrade_encrypts_plain_keys(self):
        self._add_plain_key_column("sk-from-older-version")
        self.env["ibt.ai.provider"]._ibt_encrypt_plain_api_keys()
        self.assertFalse(sql.column_exists(self.env.cr, "ibt_ai_provider", "api_key"))
        self.assertNotIn("sk-from-older-version", self._stored())
        self.assertEqual(self.provider._snapshot()["api_key"], "sk-from-older-version")

    def test_upgrade_without_master_key_stops(self):
        self._add_plain_key_column("sk-from-older-version")
        with master_keys(), self.assertRaisesRegex(ProviderError, "No master key"), self.env.cr.savepoint():
            self.env["ibt.ai.provider"]._ibt_encrypt_plain_api_keys()
        self.assertTrue(sql.column_exists(self.env.cr, "ibt_ai_provider", "api_key"))

    def test_key_is_not_logged_or_shown_in_errors(self):
        unreachable = requests.ConnectionError("connection refused")
        with self.assertLogs("odoo.addons.ibt_custom_ai_provider", "INFO") as logs, \
                patch.object(requests.Session, "request", side_effect=unreachable):
            self.provider.api_key = "sk-must-not-be-logged"
            with self.assertRaises(ProviderError) as failure:
                ProviderClient(self.provider._snapshot()).test_connection()
        self.assertNotIn("sk-must-not-be-logged", "\n".join(logs.output))
        self.assertNotIn("sk-must-not-be-logged", str(failure.exception))
