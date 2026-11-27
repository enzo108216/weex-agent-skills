#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_profile_store as store  # noqa: E402


@unittest.skipUnless(platform.system() == "Linux", "Linux vault tests require Linux")
class LinuxVaultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.prev_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
        os.environ["WEEX_TRADER_SKILL_HOME"] = self.tempdir.name

    def tearDown(self) -> None:
        if self.prev_home is None:
            os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
        else:
            os.environ["WEEX_TRADER_SKILL_HOME"] = self.prev_home

    def run_profiles_cli(self, *args: str, input_text: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "weex_profiles_en.py"), *args],
            cwd=ROOT,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_vault_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "weex_vault_en.py"), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_manual_once_vault_requires_unlock_then_can_save_and_load(self) -> None:
        self.assertEqual(store.secure_store_backend_name(), "Application Vault (setup required)")
        store.setup_linux_vault(mode="manual_once", passphrase="vault-pass", unlock=False)

        with self.assertRaises(store.ProfileError):
            store.upsert_profile(
                name="main",
                description="Main account",
                api_key="key-1234",
                api_secret="secret-1234",
                api_passphrase="pass-1234",
                set_default=True,
            )

        store.unlock_linux_vault("vault-pass")
        profile = store.upsert_profile(
            name="main",
            description="Main account",
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )

        self.assertEqual(store.secure_store_backend_name(), "Application Vault (manual_once)")
        self.assertTrue(store.profile_has_credentials_by_id(profile.profile_id))

        creds = store.load_profile_credentials("main")
        self.assertEqual(creds.api_key, "key-1234")
        self.assertEqual(creds.api_secret, "secret-1234")
        self.assertEqual(creds.api_passphrase, "pass-1234")

        store.lock_linux_vault()
        with self.assertRaises(store.ProfileError):
            store.load_profile_credentials("main")

    def test_setup_linux_vault_rejects_auto_unlock_mode(self) -> None:
        with self.assertRaises(store.ProfileError):
            store.setup_linux_vault(mode="auto_unlock", passphrase="vault-pass", unlock=False)

    def test_linux_backend_never_falls_back_to_secret_tool(self) -> None:
        self.assertEqual(store.secure_store_backend_name(), "Application Vault (setup required)")
        with self.assertRaises(store.ProfileError):
            store.save_profile_credentials(
                "main",
                store.ProfileCredentials(
                    api_key="key",
                    api_secret="secret",
                    api_passphrase="pass",
                ),
            )

    def test_session_key_path_is_scoped_to_config_home(self) -> None:
        first = store.vault_session_path()
        other_home = tempfile.TemporaryDirectory()
        self.addCleanup(other_home.cleanup)
        os.environ["WEEX_TRADER_SKILL_HOME"] = other_home.name
        second = store.vault_session_path()
        self.assertNotEqual(first, second)

    def test_stale_manual_once_session_descriptor_recovers_as_locked(self) -> None:
        store.setup_linux_vault(mode="manual_once", passphrase="vault-pass", unlock=False)
        store.vault_session_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "host": "127.0.0.1",
                    "port": 65500,
                    "token": "stale-token",
                    "pid": 999999,
                }
            ),
            encoding="utf-8",
        )

        status = store.vault_status()
        self.assertEqual(status["state"], "locked")
        self.assertEqual(status["action_required"], "unlock")

    def test_metadata_store_permissions_are_private(self) -> None:
        store.setup_linux_vault(
            mode="manual_once",
            passphrase="vault-pass",
            unlock=True,
        )
        store.upsert_profile(
            name="main",
            description="Main account",
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )

        self.assertEqual(stat.S_IMODE(store.metadata_path().stat().st_mode), 0o600)

    def test_locked_manual_once_list_and_show_return_metadata(self) -> None:
        os.environ["BOOTSTRAP_PASS"] = "manual-pass"
        setup = self.run_vault_cli("setup", "--mode", "manual_once", "--password-env", "BOOTSTRAP_PASS")
        self.assertEqual(setup.returncode, 0, setup.stderr)

        save = self.run_profiles_cli(
            "save",
            "--profile",
            "main",
            "--api-key",
            "key-1234",
            "--api-secret",
            "secret-1234",
            "--api-passphrase",
            "pass-1234",
            "--set-default",
        )
        self.assertEqual(save.returncode, 0, save.stderr)

        lock = self.run_vault_cli("lock")
        self.assertEqual(lock.returncode, 0, lock.stderr)

        listed = self.run_profiles_cli("list", "--pretty")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        listed_payload = json.loads(listed.stdout)
        self.assertEqual(listed_payload["profiles"][0]["name"], "main")
        self.assertIsNone(listed_payload["profiles"][0]["has_credentials"])
        self.assertEqual(listed_payload["profiles"][0]["credentials_status"], "unknown_locked")

        shown = self.run_profiles_cli("show", "--profile", "main", "--pretty")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        shown_payload = json.loads(shown.stdout)
        self.assertEqual(shown_payload["profile"]["name"], "main")
        self.assertIsNone(shown_payload["profile"]["has_credentials"])
        self.assertEqual(shown_payload["profile"]["credentials_status"], "unknown_locked")

    def test_locked_manual_once_delete_keeps_metadata_until_vault_unlocks(self) -> None:
        os.environ["BOOTSTRAP_PASS"] = "manual-pass"
        setup = self.run_vault_cli("setup", "--mode", "manual_once", "--password-env", "BOOTSTRAP_PASS")
        self.assertEqual(setup.returncode, 0, setup.stderr)

        save = self.run_profiles_cli(
            "save",
            "--profile",
            "main",
            "--api-key",
            "key-1234",
            "--api-secret",
            "secret-1234",
            "--api-passphrase",
            "pass-1234",
            "--set-default",
            "--pretty",
        )
        self.assertEqual(save.returncode, 0, save.stderr)
        saved_payload = json.loads(save.stdout)
        profile_id = saved_payload["profile"]["id"]

        lock = self.run_vault_cli("lock")
        self.assertEqual(lock.returncode, 0, lock.stderr)

        deleted = self.run_profiles_cli("delete", "--profile", "main", "--pretty")
        self.assertNotEqual(deleted.returncode, 0)
        self.assertIn("Vault is locked", deleted.stderr)

        listed = self.run_profiles_cli("list", "--pretty")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        listed_payload = json.loads(listed.stdout)
        self.assertEqual(listed_payload["default_profile_id"], profile_id)
        self.assertEqual(len(listed_payload["profiles"]), 1)
        self.assertEqual(listed_payload["profiles"][0]["id"], profile_id)
        self.assertEqual(listed_payload["profiles"][0]["name"], "main")

    def test_cli_save_accepts_stdin_json_secrets(self) -> None:
        os.environ["BOOTSTRAP_PASS"] = "env-pass"
        setup = self.run_vault_cli("setup", "--mode", "manual_once", "--password-env", "BOOTSTRAP_PASS")
        self.assertEqual(setup.returncode, 0, setup.stderr)

        payload = json.dumps(
            {
                "api_key": "key-5678",
                "api_secret": "secret-5678",
                "api_passphrase": "pass-5678",
            }
        )
        saved = self.run_profiles_cli(
            "save",
            "--profile",
            "stdin-main",
            "--secrets-stdin-json",
            "--set-default",
            "--pretty",
            input_text=payload,
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        saved_payload = json.loads(saved.stdout)
        self.assertEqual(saved_payload["profile"]["name"], "stdin-main")
        self.assertTrue(saved_payload["profile"]["has_credentials"])

    def test_vault_cli_accepts_password_file_for_setup_and_unlock(self) -> None:
        password_file = Path(self.tempdir.name) / "vault-pass.txt"
        password_file.write_text("vault-pass\n", encoding="utf-8")

        setup = self.run_vault_cli(
            "setup",
            "--mode",
            "manual_once",
            "--password-file",
            str(password_file),
            "--no-unlock",
            "--pretty",
        )
        self.assertEqual(setup.returncode, 0, setup.stderr)

        status = self.run_vault_cli("status", "--pretty")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["mode"], "manual_once")
        self.assertEqual(status_payload["state"], "locked")

        unlock = self.run_vault_cli("unlock", "--password-file", str(password_file), "--pretty")
        self.assertEqual(unlock.returncode, 0, unlock.stderr)
        unlock_payload = json.loads(unlock.stdout)
        self.assertEqual(unlock_payload["state"], "unlocked")

    def test_vault_cli_can_change_password_for_manual_once(self) -> None:
        env = {
            "CURRENT_PASS": "vault-pass",
            "NEW_PASS": "vault-pass-2",
        }
        setup = self.run_vault_cli("setup", "--mode", "manual_once", "--password-env", "CURRENT_PASS", extra_env=env)
        self.assertEqual(setup.returncode, 0, setup.stderr)

        save = self.run_profiles_cli(
            "save",
            "--profile",
            "main",
            "--api-key",
            "key-1234",
            "--api-secret",
            "secret-1234",
            "--api-passphrase",
            "pass-1234",
            "--set-default",
            extra_env=env,
        )
        self.assertEqual(save.returncode, 0, save.stderr)

        changed = self.run_vault_cli(
            "change-password",
            "--current-password-env",
            "CURRENT_PASS",
            "--new-password-env",
            "NEW_PASS",
            "--pretty",
            extra_env=env,
        )
        self.assertEqual(changed.returncode, 0, changed.stderr)
        changed_payload = json.loads(changed.stdout)
        self.assertEqual(changed_payload["mode"], "manual_once")

        lock = self.run_vault_cli("lock", extra_env=env)
        self.assertEqual(lock.returncode, 0, lock.stderr)

        old_unlock = self.run_vault_cli("unlock", "--password-env", "CURRENT_PASS", extra_env=env)
        self.assertNotEqual(old_unlock.returncode, 0)

        new_unlock = self.run_vault_cli("unlock", "--password-env", "NEW_PASS", "--pretty", extra_env=env)
        self.assertEqual(new_unlock.returncode, 0, new_unlock.stderr)
        new_unlock_payload = json.loads(new_unlock.stdout)
        self.assertEqual(new_unlock_payload["state"], "unlocked")

    def test_vault_cli_rejects_auto_unlock_mode(self) -> None:
        env = {"BOOTSTRAP_PASS": "vault-pass"}

        setup = self.run_vault_cli("setup", "--mode", "auto_unlock", "--password-env", "BOOTSTRAP_PASS", extra_env=env)
        self.assertNotEqual(setup.returncode, 0)

    def test_vault_cli_rejects_env_var_option_for_change_password(self) -> None:
        env = {
            "CURRENT_PASS": "vault-pass",
            "NEW_PASS": "vault-pass-2",
        }
        setup = self.run_vault_cli("setup", "--mode", "manual_once", "--password-env", "CURRENT_PASS", extra_env=env)
        self.assertEqual(setup.returncode, 0, setup.stderr)

        changed = self.run_vault_cli(
            "change-password",
            "--current-password-env",
            "CURRENT_PASS",
            "--new-password-env",
            "NEW_PASS",
            "--env-var",
            "IGNORED",
            "--pretty",
            extra_env=env,
        )
        self.assertNotEqual(changed.returncode, 0)

    def test_concurrent_metadata_saves_do_not_clobber_profiles(self) -> None:
        original_load = store.load_metadata_store
        load_count = 0
        load_count_lock = threading.Lock()

        def delayed_load() -> dict[str, object]:
            nonlocal load_count
            result = original_load()
            with load_count_lock:
                load_count += 1
                current = load_count
            if current <= 2:
                time.sleep(0.2)
            return result

        first = store.ProfileMetadata(profile_id="profile-alpha", name="alpha")
        second = store.ProfileMetadata(profile_id="profile-beta", name="beta")

        with mock.patch.object(store, "load_metadata_store", side_effect=delayed_load):
            threads = [
                threading.Thread(target=store.save_profile_metadata, args=(first,)),
                threading.Thread(target=store.save_profile_metadata, args=(second,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(
            sorted(profile.name for profile in store.list_profiles()),
            ["alpha", "beta"],
        )

    def test_force_setup_resets_stale_metadata(self) -> None:
        store.setup_linux_vault(mode="manual_once", passphrase="vault-pass", unlock=True)
        store.upsert_profile(
            name="main",
            description="Main account",
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )
        self.assertEqual(len(store.list_profiles()), 1)

        store.setup_linux_vault(mode="manual_once", passphrase="other-pass", unlock=False, force=True)

        self.assertEqual(store.list_profiles(), [])
        self.assertIsNone(store.get_default_profile_id())
        self.assertIsNone(store.get_default_profile_name())

    def test_reset_linux_vault_returns_to_uninitialized_and_clears_profiles(self) -> None:
        store.setup_linux_vault(mode="manual_once", passphrase="vault-pass", unlock=True)
        store.upsert_profile(
            name="main",
            description="Main account",
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )

        status = store.reset_linux_vault()

        self.assertEqual(status["state"], "uninitialized")
        self.assertEqual(store.list_profiles(), [])
        self.assertIsNone(store.get_default_profile_id())
        self.assertFalse(store.vault_config_path().exists())
        self.assertFalse(store.vault_data_path().exists())


class ApplicationVaultApprovalKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.prev_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
        os.environ["WEEX_TRADER_SKILL_HOME"] = self.tempdir.name
        self.session_key: bytes | None = None

        self.read_session_patch = mock.patch.object(
            store,
            "_read_vault_session_key",
            side_effect=lambda: self.session_key,
        )
        self.write_session_patch = mock.patch.object(
            store,
            "_write_vault_session_key",
            side_effect=self._remember_session_key,
        )
        self.delete_session_patch = mock.patch.object(
            store,
            "_delete_vault_session_key",
            side_effect=self._forget_session_key,
        )
        self.read_session_patch.start()
        self.write_session_patch.start()
        self.delete_session_patch.start()

    def tearDown(self) -> None:
        self.delete_session_patch.stop()
        self.write_session_patch.stop()
        self.read_session_patch.stop()
        store._MANUAL_SESSION_PROCESSES.clear()
        if self.prev_home is None:
            os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
        else:
            os.environ["WEEX_TRADER_SKILL_HOME"] = self.prev_home

    def _remember_session_key(self, key: bytes) -> None:
        self.session_key = key

    def _forget_session_key(self) -> None:
        self.session_key = None

    def _setup(self, *, passphrase: str = "vault-pass", force: bool = False) -> None:
        store.setup_linux_vault(
            mode="manual_once",
            passphrase=passphrase,
            unlock=True,
            force=force,
        )
        self.assertIsNotNone(self.session_key)

    def _payload(self) -> dict[str, object]:
        self.assertIsNotNone(self.session_key)
        return store._decrypt_vault_payload(store._load_vault_file(), self.session_key)

    def _rewrite_payload(self, payload: dict[str, object]) -> None:
        self.assertIsNotNone(self.session_key)
        raw_vault = store._load_vault_file()
        config = store._require_vault_config()
        salt = store.b64decode(raw_vault["kdf"]["salt"])
        store._save_vault_file(
            store._encrypt_vault_payload(payload, self.session_key, config, salt)
        )

    def _create_profile(self, *, name: str = "main") -> store.ProfileMetadata:
        return store.upsert_profile(
            name=name,
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )

    def test_approval_key_is_lazily_created_once_and_stable_across_reloads(self) -> None:
        self._setup()
        self.assertEqual(self._payload()["application_secrets"], {})

        first = store.get_or_create_local_mcp_approval_key()
        second = store.get_or_create_local_mcp_approval_key()

        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first.encode("utf-8")), 32)
        self.assertEqual(
            self._payload()["application_secrets"]["local_mcp_approval_key"],
            first,
        )

    def test_binding_key_is_separate_from_legacy_approval_key_and_rotates_independently(self) -> None:
        self._setup()
        profile = self._create_profile()
        approval_key = store.get_or_create_local_mcp_approval_key()

        first = store.get_or_create_local_mcp_binding_key()
        second = store.rotate_local_mcp_binding_key()

        self.assertNotEqual(first, approval_key)
        self.assertNotEqual(second, first)
        self.assertEqual(store.get_or_create_local_mcp_binding_key(), second)
        self.assertEqual(store.get_or_create_local_mcp_approval_key(), approval_key)
        application_secrets = self._payload()["application_secrets"]
        self.assertEqual(application_secrets["local_mcp_approval_key"], approval_key)
        self.assertEqual(application_secrets["local_mcp_binding_key"], second)
        self.assertEqual(
            store.load_profile_credentials(profile.profile_id).api_secret,
            "secret-1234",
        )

    def test_binding_key_first_provision_is_atomic_and_status_is_non_mutating(self) -> None:
        self._setup()
        before = store.vault_data_path().read_bytes()
        self.assertEqual(
            store.local_mcp_binding_key_status(),
            {"present": False, "valid": False, "length": 0, "fp_sha256": None},
        )
        self.assertEqual(store.vault_data_path().read_bytes(), before)

        results: list[str] = []
        errors: list[BaseException] = []
        original_save = store._save_vault_file

        def worker() -> None:
            try:
                results.append(store.get_or_create_local_mcp_binding_key())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with mock.patch.object(store, "_save_vault_file", wraps=original_save) as save_mock:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(save_mock.call_count, 1)
        binding_key = results[0]
        status = store.local_mcp_binding_key_status()
        self.assertEqual(status["present"], True)
        self.assertEqual(status["valid"], True)
        self.assertEqual(status["length"], len(binding_key.encode("utf-8")))
        self.assertEqual(
            status["fp_sha256"],
            hashlib.sha256(binding_key.encode("utf-8")).hexdigest()[:16],
        )
        self.assertNotIn(binding_key, json.dumps(status, sort_keys=True))

    def test_concurrent_first_provision_returns_one_key_and_writes_once(self) -> None:
        self._setup()
        results: list[str] = []
        errors: list[BaseException] = []
        original_save = store._save_vault_file

        def worker() -> None:
            try:
                results.append(store.get_or_create_local_mcp_approval_key())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with mock.patch.object(store, "_save_vault_file", wraps=original_save) as save_mock:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(save_mock.call_count, 1)

    def test_locked_vault_refuses_approval_key_without_writing(self) -> None:
        store.setup_linux_vault(
            mode="manual_once",
            passphrase="vault-pass",
            unlock=False,
        )
        before = store.vault_data_path().read_bytes()

        with self.assertRaisesRegex(store.ProfileError, "locked"):
            store.get_or_create_local_mcp_approval_key()

        self.assertEqual(store.vault_data_path().read_bytes(), before)

    def test_locked_vault_refuses_binding_key_operations_without_writing(self) -> None:
        store.setup_linux_vault(
            mode="manual_once",
            passphrase="vault-pass",
            unlock=False,
        )
        before = store.vault_data_path().read_bytes()

        for operation in (
            store.get_or_create_local_mcp_binding_key,
            store.rotate_local_mcp_binding_key,
            store.local_mcp_binding_key_status,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(store.ProfileError, "locked"):
                    operation()
                self.assertEqual(store.vault_data_path().read_bytes(), before)

    def test_invalid_application_secret_payload_fails_closed_without_rewrite(self) -> None:
        self._setup()
        invalid_values: tuple[object, ...] = (
            "not-an-object",
            {"local_mcp_approval_key": 123},
            {"local_mcp_approval_key": "short"},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                payload = self._payload()
                payload["application_secrets"] = value
                self._rewrite_payload(payload)
                before = store.vault_data_path().read_bytes()
                for operation in (
                    store.get_or_create_local_mcp_approval_key,
                    store.rotate_local_mcp_approval_key,
                    store.local_mcp_approval_key_status,
                ):
                    with self.assertRaises(store.ProfileError):
                        operation()
                self.assertEqual(store.vault_data_path().read_bytes(), before)

    def test_invalid_binding_key_payload_fails_closed_without_rewrite(self) -> None:
        self._setup()
        for value in (123, "short"):
            with self.subTest(value=value):
                payload = self._payload()
                payload["application_secrets"] = {
                    "local_mcp_binding_key": value,
                }
                self._rewrite_payload(payload)
                before = store.vault_data_path().read_bytes()
                for operation in (
                    store.get_or_create_local_mcp_binding_key,
                    store.rotate_local_mcp_binding_key,
                    store.local_mcp_binding_key_status,
                ):
                    with self.assertRaises(store.ProfileError):
                        operation()
                self.assertEqual(store.vault_data_path().read_bytes(), before)

    def test_old_vault_upgrade_preserves_profile_credentials_and_unknown_fields(self) -> None:
        self._setup()
        profile = self._create_profile()
        payload = self._payload()
        payload.pop("application_secrets", None)
        payload["future_top_level"] = {"sentinel": "keep"}
        self._rewrite_payload(payload)

        approval_key = store.get_or_create_local_mcp_approval_key()

        upgraded = self._payload()
        self.assertEqual(upgraded["future_top_level"], {"sentinel": "keep"})
        self.assertEqual(
            upgraded["application_secrets"]["local_mcp_approval_key"],
            approval_key,
        )
        self.assertEqual(store.load_profile_credentials(profile.profile_id).api_key, "key-1234")

    def test_profile_save_update_delete_and_rename_preserve_application_secrets(self) -> None:
        self._setup()
        profile = self._create_profile()
        approval_key = store.get_or_create_local_mcp_approval_key()
        payload = self._payload()
        payload["version"] = 7
        payload["future_top_level"] = {"sentinel": "keep"}
        self._rewrite_payload(payload)

        store._store_secret_linux(profile.profile_id, "api_key", "key-updated")
        renamed = store.upsert_profile(name="renamed", profile_id=profile.profile_id)
        self.assertEqual(renamed.profile_id, profile.profile_id)
        self.assertEqual(store.get_default_profile_id(), profile.profile_id)
        self.assertEqual(store.load_profile_credentials("renamed").api_key, "key-updated")
        self.assertEqual(store.get_or_create_local_mcp_approval_key(), approval_key)
        after_store = self._payload()
        self.assertEqual(after_store["version"], 7)
        self.assertEqual(after_store["future_top_level"], {"sentinel": "keep"})
        self.assertEqual(
            after_store["application_secrets"]["local_mcp_approval_key"],
            approval_key,
        )

        self.assertTrue(store.delete_profile_credentials("renamed"))
        after_delete = self._payload()
        self.assertEqual(after_delete["version"], 7)
        self.assertEqual(
            after_delete["application_secrets"]["local_mcp_approval_key"],
            approval_key,
        )
        self.assertEqual(after_delete["future_top_level"], {"sentinel": "keep"})

    def test_password_change_preserves_approval_key(self) -> None:
        self._setup()
        self._create_profile()
        approval_key = store.get_or_create_local_mcp_approval_key()

        store.change_linux_vault_password("vault-pass", "vault-pass-2")

        self.assertEqual(store.get_or_create_local_mcp_approval_key(), approval_key)
        store.lock_linux_vault()
        with self.assertRaises(store.ProfileError):
            store.unlock_linux_vault("vault-pass")
        store.unlock_linux_vault("vault-pass-2")
        self.assertEqual(store.get_or_create_local_mcp_approval_key(), approval_key)

    def test_explicit_rotation_changes_only_key_and_preserves_profiles(self) -> None:
        self._setup()
        profile = self._create_profile()
        first = store.get_or_create_local_mcp_approval_key()

        second = store.rotate_local_mcp_approval_key()

        self.assertNotEqual(first, second)
        self.assertEqual(store.get_or_create_local_mcp_approval_key(), second)
        self.assertEqual(store.resolve_profile("main").profile_id, profile.profile_id)
        self.assertEqual(store.load_profile_credentials(profile.profile_id).api_secret, "secret-1234")

    def test_reset_and_force_setup_create_new_approval_keys(self) -> None:
        self._setup()
        first = store.get_or_create_local_mcp_approval_key()
        first_binding = store.get_or_create_local_mcp_binding_key()

        store.reset_linux_vault()
        self._setup(passphrase="vault-pass-2")
        second = store.get_or_create_local_mcp_approval_key()
        second_binding = store.get_or_create_local_mcp_binding_key()
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_binding, second_binding)

        self._setup(passphrase="vault-pass-3", force=True)
        third = store.get_or_create_local_mcp_approval_key()
        third_binding = store.get_or_create_local_mcp_binding_key()
        self.assertNotEqual(second, third)
        self.assertNotEqual(second_binding, third_binding)

    def test_simulated_old_writer_drop_regenerates_key_and_keeps_profiles(self) -> None:
        self._setup()
        profile = self._create_profile()
        first = store.get_or_create_local_mcp_approval_key()
        current = self._payload()
        legacy_payload = {
            "version": current["version"],
            "profiles": current["profiles"],
        }
        self._rewrite_payload(legacy_payload)

        second = store.get_or_create_local_mcp_approval_key()

        self.assertNotEqual(first, second)
        self.assertEqual(store.load_profile_credentials(profile.profile_id).api_passphrase, "pass-1234")

    def test_full_file_backup_restore_preserves_key_profile_default_and_credentials(self) -> None:
        self._setup()
        profile = self._create_profile()
        approval_key = store.get_or_create_local_mcp_approval_key()
        binding_key = store.get_or_create_local_mcp_binding_key()
        paths = (store.vault_config_path(), store.vault_data_path(), store.metadata_path())
        backup = {path.name: path.read_text(encoding="utf-8") for path in paths}

        store.reset_linux_vault()
        for path in paths:
            store._atomic_write_text(path, backup[path.name], 0o600)
        store.unlock_linux_vault("vault-pass")

        self.assertEqual(store.get_or_create_local_mcp_approval_key(), approval_key)
        self.assertEqual(store.get_or_create_local_mcp_binding_key(), binding_key)
        self.assertEqual(store.get_default_profile_id(), profile.profile_id)
        self.assertEqual(store.resolve_profile("main").profile_id, profile.profile_id)
        self.assertEqual(store.load_profile_credentials(profile.profile_id).api_key, "key-1234")

    def test_status_is_non_mutating_and_never_returns_the_secret(self) -> None:
        self._setup()
        before = store.vault_data_path().read_bytes()
        absent = store.local_mcp_approval_key_status()
        self.assertEqual(
            absent,
            {"present": False, "valid": False, "length": 0, "fp_sha256": None},
        )
        self.assertEqual(store.vault_data_path().read_bytes(), before)

        approval_key = store.get_or_create_local_mcp_approval_key()
        status = store.local_mcp_approval_key_status()
        self.assertEqual(status["present"], True)
        self.assertEqual(status["valid"], True)
        self.assertEqual(status["length"], len(approval_key.encode("utf-8")))
        self.assertEqual(
            status["fp_sha256"],
            hashlib.sha256(approval_key.encode("utf-8")).hexdigest()[:16],
        )
        self.assertNotIn(approval_key, json.dumps(status, sort_keys=True))

    def test_file_lock_uses_posix_or_windows_and_fails_without_backend(self) -> None:
        lock_path = Path(self.tempdir.name) / "portable.lock"

        posix = mock.Mock()
        posix.LOCK_EX = 1
        posix.LOCK_UN = 2
        with mock.patch.object(store, "fcntl", posix), mock.patch.object(
            store,
            "msvcrt",
            None,
            create=True,
        ):
            with store._file_lock(lock_path):
                pass
        self.assertEqual(
            [call.args[1] for call in posix.flock.call_args_list],
            [posix.LOCK_EX, posix.LOCK_UN],
        )

        windows = mock.Mock()
        windows.LK_LOCK = 3
        windows.LK_UNLCK = 4
        with mock.patch.object(store, "fcntl", None), mock.patch.object(
            store,
            "msvcrt",
            windows,
            create=True,
        ):
            with store._file_lock(lock_path):
                pass
        self.assertEqual(
            [(call.args[1], call.args[2]) for call in windows.locking.call_args_list],
            [(windows.LK_LOCK, 1), (windows.LK_UNLCK, 1)],
        )
        self.assertGreaterEqual(lock_path.stat().st_size, 1)

        with mock.patch.object(store, "fcntl", None), mock.patch.object(
            store,
            "msvcrt",
            None,
            create=True,
        ):
            with self.assertRaisesRegex(store.ProfileError, "locking"):
                with store._file_lock(lock_path):
                    pass


class ProfileStoreCredentialBackendTests(unittest.TestCase):
    def test_upsert_profile_rejects_non_weex_custom_base_url_before_saving(self) -> None:
        with mock.patch.object(store, "get_profile", return_value=None), mock.patch.object(
            store,
            "save_profile_credentials",
        ) as save_credentials_mock, mock.patch.object(store, "save_profile_metadata") as save_metadata_mock:
            with self.assertRaises(store.ProfileError) as exc_info:
                store.upsert_profile(
                    name="main",
                    description="Main account",
                    contract_base_url="https://contract.example.test",
                    api_key="key-1234",
                    api_secret="secret-1234",
                    api_passphrase="pass-1234",
                    set_default=True,
                )

        self.assertIn("contract_base_url", str(exc_info.exception))
        save_credentials_mock.assert_not_called()
        save_metadata_mock.assert_not_called()

    def test_save_profile_credentials_uses_atomic_vault_update_for_encrypted_file_backend(self) -> None:
        credentials = store.ProfileCredentials(
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
        )

        with mock.patch.object(store, "_active_linux_backend", return_value="encrypted_file"), mock.patch.object(
            store,
            "_update_vault_profile_credentials",
        ) as update_mock, mock.patch.object(store, "_store_secret") as store_secret_mock:
            store.save_profile_credentials("demo-profile", credentials)

        update_mock.assert_called_once_with("demo-profile", credentials)
        store_secret_mock.assert_not_called()

    def test_delete_profile_credentials_uses_atomic_vault_delete_for_encrypted_file_backend(self) -> None:
        profile = store.ProfileMetadata(profile_id="profile-1234", name="demo-profile")

        with mock.patch.object(store, "get_profile", return_value=profile), mock.patch.object(
            store,
            "_active_linux_backend",
            return_value="encrypted_file",
        ), mock.patch.object(store, "_remove_vault_profile_credentials", return_value=True) as remove_mock:
            deleted = store.delete_profile_credentials("demo-profile")

        self.assertTrue(deleted)
        remove_mock.assert_called_once_with("profile-1234")


class ProfileStoreInterpreterSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.prev_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
        self.prev_python = os.environ.get("PYTHON")
        os.environ["WEEX_TRADER_SKILL_HOME"] = self.tempdir.name

    def tearDown(self) -> None:
        store._MANUAL_SESSION_PROCESSES.clear()
        if self.prev_home is None:
            os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
        else:
            os.environ["WEEX_TRADER_SKILL_HOME"] = self.prev_home
        if self.prev_python is None:
            os.environ.pop("PYTHON", None)
        else:
            os.environ["PYTHON"] = self.prev_python

    def test_vault_agent_launch_uses_current_interpreter_instead_of_python_env(self) -> None:
        os.environ["PYTHON"] = "C:\\fake\\python.exe"
        fake_session_path = Path(self.tempdir.name) / "vault.session.json"
        process = mock.Mock()
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        process.poll.return_value = None

        with mock.patch.object(store, "vault_session_path", return_value=fake_session_path), mock.patch.object(
            store.subprocess,
            "Popen",
            return_value=process,
        ) as popen_mock, mock.patch.object(store, "_request_vault_session", return_value={"ok": True}):
            store._write_vault_session_key(b"\x01" * 32)

        argv = popen_mock.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertNotEqual(argv[0], "C:\\fake\\python.exe")

    def test_write_vault_session_key_replaces_existing_managed_agent(self) -> None:
        fake_session_path = Path(self.tempdir.name) / "vault.session.json"
        old_process = mock.Mock()
        old_process.stdout = mock.Mock()
        old_process.stderr = mock.Mock()
        new_process = mock.Mock()
        new_process.stdin = mock.Mock()
        new_process.stdout = mock.Mock()
        new_process.stderr = mock.Mock()
        new_process.poll.return_value = None

        requests: list[str] = []

        def fake_request(action: str, **_kwargs: object) -> dict[str, object]:
            requests.append(action)
            return {"ok": True}

        store._MANUAL_SESSION_PROCESSES[str(fake_session_path)] = old_process
        with mock.patch.object(store, "vault_session_path", return_value=fake_session_path), mock.patch.object(
            store.subprocess,
            "Popen",
            return_value=new_process,
        ), mock.patch.object(store, "_request_vault_session", side_effect=fake_request):
            store._write_vault_session_key(b"\x01" * 32)

        old_process.wait.assert_called_once_with(timeout=1.0)
        old_process.stdout.close.assert_called_once()
        old_process.stderr.close.assert_called_once()
        self.assertEqual(requests[:2], ["shutdown", "ping"])
        self.assertIs(store._MANUAL_SESSION_PROCESSES[str(fake_session_path)], new_process)

    def test_vault_key_derivation_falls_back_to_cryptography_scrypt(self) -> None:
        class FakeScrypt:
            def __init__(self, *, salt: bytes, length: int, n: int, r: int, p: int) -> None:
                self.params = {
                    "salt": salt,
                    "length": length,
                    "n": n,
                    "r": r,
                    "p": p,
                }

            def derive(self, value: bytes) -> bytes:
                self.params["value"] = value
                return b"k" * 32

        captured: dict[str, object] = {}

        def build_fake_scrypt(*, salt: bytes, length: int, n: int, r: int, p: int) -> FakeScrypt:
            instance = FakeScrypt(salt=salt, length=length, n=n, r=r, p=p)
            captured.update(instance.params)
            return instance

        config = {
            "kdf": {
                "n": 32,
                "r": 4,
                "p": 2,
                "dklen": 32,
            }
        }

        with mock.patch.object(store, "_hashlib_scrypt", None), mock.patch.object(
            store,
            "_CryptographyScrypt",
            side_effect=build_fake_scrypt,
        ), mock.patch.object(store, "CRYPTOGRAPHY_RUNTIME_AVAILABLE", True):
            key = store._derive_vault_key("vault-pass", b"salt-bytes", config)

        self.assertEqual(key, b"k" * 32)
        self.assertEqual(captured["salt"], b"salt-bytes")
        self.assertEqual(captured["length"], 32)
        self.assertEqual(captured["n"], 32)
        self.assertEqual(captured["r"], 4)
        self.assertEqual(captured["p"], 2)


@unittest.skipUnless(platform.system() == "Windows", "Windows metadata tests require Windows")
class WindowsMetadataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.prev_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
        os.environ["WEEX_TRADER_SKILL_HOME"] = self.tempdir.name

    def tearDown(self) -> None:
        if self.prev_home is None:
            os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
        else:
            os.environ["WEEX_TRADER_SKILL_HOME"] = self.prev_home

    def run_profiles_cli(self, *args: str, input_text: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "weex_profiles_en.py"), *args],
            cwd=ROOT,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_vault_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "weex_vault_en.py"), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_save_profile_metadata_works_on_windows(self) -> None:
        profile = store.ProfileMetadata(profile_id="profile-main", name="main")

        store.save_profile_metadata(profile, set_default=True)

        self.assertEqual([item.name for item in store.list_profiles()], ["main"])
        self.assertEqual(store.get_default_profile_id(), "profile-main")

    def test_windows_backend_uses_application_vault(self) -> None:
        self.assertEqual(store.secure_store_backend_name(), "Application Vault (setup required)")

    def test_windows_manual_once_vault_requires_unlock_then_can_save_and_load(self) -> None:
        self.assertEqual(store.secure_store_backend_name(), "Application Vault (setup required)")
        store.setup_linux_vault(mode="manual_once", passphrase="vault-pass", unlock=False)

        with self.assertRaises(store.ProfileError):
            store.upsert_profile(
                name="main",
                description="Main account",
                api_key="key-1234",
                api_secret="secret-1234",
                api_passphrase="pass-1234",
                set_default=True,
            )

        store.unlock_linux_vault("vault-pass")
        profile = store.upsert_profile(
            name="main",
            description="Main account",
            api_key="key-1234",
            api_secret="secret-1234",
            api_passphrase="pass-1234",
            set_default=True,
        )

        self.assertEqual(store.secure_store_backend_name(), "Application Vault (manual_once)")
        self.assertTrue(store.profile_has_credentials_by_id(profile.profile_id))

        creds = store.load_profile_credentials("main")
        self.assertEqual(creds.api_key, "key-1234")
        self.assertEqual(creds.api_secret, "secret-1234")
        self.assertEqual(creds.api_passphrase, "pass-1234")

        store.lock_linux_vault()
        with self.assertRaises(store.ProfileError):
            store.load_profile_credentials("main")

    def test_vault_cli_status_reports_setup_required_on_windows(self) -> None:
        completed = self.run_vault_cli("status", "--pretty")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["backend"], "Application Vault (setup required)")
        self.assertEqual(payload["state"], "uninitialized")


if __name__ == "__main__":
    unittest.main()
