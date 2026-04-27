#!/usr/bin/env python3
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_profile_manager_app as app  # noqa: E402


class FakeVar:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class FakeBoolVar(FakeVar):
    def __init__(self, value: bool = False) -> None:
        super().__init__(value)
        self.callbacks: list[object] = []

    def trace_add(self, _mode: str, callback: object) -> str:
        self.callbacks.append(callback)
        return f"trace-{len(self.callbacks)}"

    def set(self, value: object) -> None:
        super().set(value)
        for callback in list(self.callbacks):
            callback()


class FakeTextValue:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self, *_args: object, **_kwargs: object) -> str:
        return self.value


class ProfileManagerLayoutTests(unittest.TestCase):
    def test_main_bootstraps_agent_state_before_launching_ui(self) -> None:
        fake_root = types.SimpleNamespace(mainloop=mock.Mock())

        with tempfile.TemporaryDirectory() as tempdir:
            previous_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                with mock.patch.object(app, "_load_runtime_dependencies"):
                    with mock.patch.object(app, "ProfileManagerApp"):
                        with mock.patch.object(app, "maybe_detach_gui_entrypoint"):
                            with mock.patch.object(app, "maybe_reexec_under_managed_gui_runtime"):
                                with mock.patch.object(
                                    app,
                                    "tk",
                                    types.SimpleNamespace(Tk=mock.Mock(return_value=fake_root), TclError=RuntimeError),
                                ):
                                    exit_code = app.main("en", argv=[])
                self.assertEqual(exit_code, 0)
                self.assertTrue((Path(tempdir) / "agent-init.json").exists())
                self.assertTrue((Path(tempdir) / "agent-runtime.json").exists())
            finally:
                if previous_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = previous_home

    def test_main_checks_managed_gui_runtime_before_loading_tk(self) -> None:
        fake_root = types.SimpleNamespace(mainloop=mock.Mock())

        with mock.patch.object(app, "_load_runtime_dependencies"):
            with mock.patch.object(app, "ProfileManagerApp"):
                with mock.patch.object(app, "maybe_detach_gui_entrypoint"):
                    with mock.patch.object(app, "maybe_reexec_under_managed_gui_runtime") as bootstrap_mock:
                        with mock.patch.object(
                            app,
                            "tk",
                            types.SimpleNamespace(Tk=mock.Mock(return_value=fake_root), TclError=RuntimeError),
                        ):
                            exit_code = app.main("en", argv=[])

        self.assertEqual(exit_code, 0)
        bootstrap_mock.assert_called_once()
        bootstrap_kwargs = bootstrap_mock.call_args.kwargs
        self.assertEqual(bootstrap_kwargs["argv"], ["--language", "en"])
        self.assertTrue(str(bootstrap_kwargs["entrypoint_path"]).endswith("weex_profile_manager_app.py"))

    def test_prompt_vault_passphrase_uses_set_copy_during_initial_setup(self) -> None:
        prompts: list[tuple[str, str]] = []

        def fake_askstring(title: str, prompt: str, **kwargs) -> str:
            prompts.append((title, prompt))
            return "vault-passphrase"

        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.language = "en"
        profile_app.root = object()

        with mock.patch.object(app, "simpledialog", types.SimpleNamespace(askstring=fake_askstring)):
            with mock.patch.object(app, "messagebox", types.SimpleNamespace(showwarning=mock.Mock(), showerror=mock.Mock())):
                result = profile_app._prompt_vault_passphrase(confirm=True, setup_flow=True)

        self.assertEqual(result, "vault-passphrase")
        self.assertEqual(
            prompts,
            [
                ("Set Vault Passphrase", "Set the vault passphrase."),
                ("Confirm Vault Passphrase", "Re-enter the vault passphrase."),
            ],
        )

    def test_manage_vault_prompts_once_during_unlock(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.language = "en"
        profile_app.root = object()
        profile_app.current_profile_id = None
        profile_app.local_text = lambda en_text, _zh_text: en_text
        profile_app.refresh_vault_status = mock.Mock()
        profile_app.refresh_profiles = mock.Mock()

        with mock.patch.object(app, "vault_status", return_value={"state": "locked", "mode": "manual_once"}):
            with mock.patch.object(profile_app, "_prompt_vault_passphrase", return_value="vault-passphrase") as prompt_mock:
                with mock.patch.object(app, "unlock_linux_vault") as unlock_mock:
                    with mock.patch.object(app, "get_profile_by_id", return_value=None):
                        profile_app.manage_vault()

        prompt_mock.assert_called_once_with(confirm=False)
        unlock_mock.assert_called_once_with("vault-passphrase")

    def test_refresh_vault_status_shows_reset_button_only_when_locked(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.language = "en"
        profile_app.local_text = lambda en_text, zh_text: en_text
        profile_app.backend_var = FakeVar()
        profile_app.vault_state_var = FakeVar()
        profile_app.vault_guidance_var = FakeVar()
        profile_app.profile_actions_enabled = False
        profile_app.profile_action_hint_var = FakeVar()
        profile_app.header_var = FakeVar()
        profile_app.vault_action_button = types.SimpleNamespace(configure=mock.Mock())
        profile_app.vault_reset_button = types.SimpleNamespace(pack=mock.Mock(), pack_forget=mock.Mock())
        profile_app._sync_profile_action_controls = mock.Mock()
        profile_app._sync_account_surface_lock = mock.Mock()

        with mock.patch.object(app, "vault_status", return_value={"state": "locked", "mode": "manual_once"}):
            with mock.patch.object(app, "secure_store_backend_name", return_value="Application Vault (manual_once)"):
                profile_app.refresh_vault_status()

        self.assertTrue(profile_app.account_surface_locked)
        profile_app._sync_account_surface_lock.assert_called_once()
        profile_app.vault_reset_button.pack.assert_called_once()
        profile_app.vault_reset_button.pack_forget.assert_not_called()

    def test_refresh_vault_status_hides_overlay_when_unlocked(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.language = "en"
        profile_app.local_text = lambda en_text, zh_text: en_text
        profile_app.backend_var = FakeVar()
        profile_app.vault_state_var = FakeVar()
        profile_app.vault_guidance_var = FakeVar()
        profile_app.profile_actions_enabled = False
        profile_app.profile_action_hint_var = FakeVar()
        profile_app.header_var = FakeVar()
        profile_app.vault_action_button = types.SimpleNamespace(configure=mock.Mock())
        profile_app.vault_reset_button = types.SimpleNamespace(pack=mock.Mock(), pack_forget=mock.Mock())
        profile_app._sync_profile_action_controls = mock.Mock()
        profile_app._sync_account_surface_lock = mock.Mock()

        with mock.patch.object(app, "vault_status", return_value={"state": "unlocked", "mode": "manual_once"}):
            with mock.patch.object(app, "secure_store_backend_name", return_value="Application Vault (manual_once)"):
                profile_app.refresh_vault_status()

        self.assertFalse(profile_app.account_surface_locked)
        profile_app._sync_account_surface_lock.assert_called_once()
        profile_app.vault_reset_button.pack_forget.assert_called_once()

    def test_reset_vault_executes_destructive_reset_flow(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.language = "en"
        profile_app.root = object()
        profile_app.current_profile_id = "profile-main"
        profile_app.refresh_vault_status = mock.Mock()
        profile_app.refresh_profiles = mock.Mock()
        profile_app.reset_form = mock.Mock()
        profile_app._refresh_agent_cache = mock.Mock()
        profile_app.local_text = lambda en_text, zh_text: en_text
        profile_app._confirm_vault_reset = mock.Mock(return_value=True)

        fake_messagebox = types.SimpleNamespace(showinfo=mock.Mock(), showerror=mock.Mock())

        with mock.patch.object(app, "messagebox", fake_messagebox):
            with mock.patch.object(app, "reset_linux_vault", return_value={"state": "uninitialized"}) as reset_mock:
                profile_app.reset_vault()

        reset_mock.assert_called_once_with()
        profile_app.reset_form.assert_called_once()
        profile_app.refresh_vault_status.assert_called_once()
        profile_app.refresh_profiles.assert_called_once()
        profile_app._refresh_agent_cache.assert_called_once()

    def test_action_button_supports_keyboard_activation_and_invoke(self) -> None:
        class FakeWidget:
            def __init__(self, master=None, **kwargs):
                self.master = master
                self.kwargs = dict(kwargs)
                self.bindings: dict[str, object] = {}

            def bind(self, sequence: str, callback: object) -> None:
                self.bindings[sequence] = callback

            def configure(self, **kwargs) -> None:
                self.kwargs.update(kwargs)

            config = configure

            def cget(self, key: str) -> object:
                return self.kwargs.get(key)

            def pack(self, *args, **kwargs) -> None:
                return None

            def focus_set(self) -> None:
                self.kwargs["focused"] = True

        fake_tk = types.SimpleNamespace(
            Frame=FakeWidget,
            Label=FakeWidget,
            NORMAL="normal",
            DISABLED="disabled",
            BOTH="both",
        )
        previous_tk = app.tk
        app.tk = fake_tk
        try:
            calls: list[str] = []
            button = app.ActionButton(
                object(),
                text="Save",
                command=lambda: calls.append("called") or "ok",
                kind="primary",
                font=object(),
            )

            self.assertEqual(button.invoke(), "ok")
            self.assertEqual(button.widget.cget("highlightthickness"), 2)
            self.assertEqual(button.widget.cget("highlightbackground"), app.PALETTE["button_primary_bg"])
            self.assertEqual(button.widget.cget("highlightcolor"), app.PALETTE["button_primary_bg"])
            self.assertIn("<FocusIn>", button.widget.bindings)
            self.assertIn("<FocusOut>", button.widget.bindings)
            self.assertIn("<KeyPress-space>", button.widget.bindings)
            self.assertIn("<KeyRelease-space>", button.widget.bindings)
            self.assertIn("<Return>", button.widget.bindings)

            button.widget.bindings["<FocusIn>"](object())
            self.assertEqual(button.widget.cget("highlightbackground"), app.PALETTE["accent"])
            self.assertEqual(button.widget.cget("highlightcolor"), app.PALETTE["accent"])
            button.widget.bindings["<FocusOut>"](object())
            self.assertEqual(button.widget.cget("highlightbackground"), app.PALETTE["button_primary_bg"])
            self.assertEqual(button.widget.cget("highlightcolor"), app.PALETTE["button_primary_bg"])

            self.assertEqual(button.widget.bindings["<Return>"](object()), "break")
            self.assertEqual(button.widget.bindings["<KeyPress-space>"](object()), "break")
            self.assertEqual(button.widget.bindings["<KeyRelease-space>"](object()), "break")
            self.assertEqual(calls, ["called", "called", "called"])

            button.configure(state=fake_tk.DISABLED)
            button.widget.bindings["<FocusIn>"](object())
            self.assertIsNone(button.invoke())
            self.assertEqual(button.widget.cget("highlightbackground"), app.PALETTE["button_disabled_bg"])
            self.assertEqual(button.widget.cget("highlightcolor"), app.PALETTE["button_disabled_bg"])
            self.assertEqual(button.widget.bindings["<Return>"](object()), "break")
            self.assertEqual(button.widget.bindings["<KeyPress-space>"](object()), "break")
            self.assertEqual(button.widget.bindings["<KeyRelease-space>"](object()), "break")
            self.assertEqual(calls, ["called", "called", "called"])
        finally:
            app.tk = previous_tk

    def test_action_checkbox_supports_keyboard_activation_and_external_updates(self) -> None:
        class FakeWidget:
            def __init__(self, master=None, **kwargs):
                self.master = master
                self.kwargs = dict(kwargs)
                self.bindings: dict[str, object] = {}

            def bind(self, sequence: str, callback: object) -> None:
                self.bindings[sequence] = callback

            def configure(self, **kwargs) -> None:
                self.kwargs.update(kwargs)

            config = configure

            def cget(self, key: str) -> object:
                return self.kwargs.get(key)

            def pack(self, *args, **kwargs) -> None:
                return None

            def focus_set(self) -> None:
                self.kwargs["focused"] = True

        fake_tk = types.SimpleNamespace(
            Frame=FakeWidget,
            Label=FakeWidget,
            NORMAL="normal",
            DISABLED="disabled",
            FLAT="flat",
            LEFT="left",
        )
        previous_tk = app.tk
        app.tk = fake_tk
        try:
            variable = FakeBoolVar(False)
            checkbox = app.ActionCheckbox(
                FakeWidget(bg="white"),
                text="Default profile",
                variable=variable,
                font=object(),
            )

            self.assertEqual(checkbox._box.cget("highlightbackground"), app.PALETTE["checkbox_border"])
            self.assertEqual(checkbox._box.cget("text"), "")
            self.assertEqual(checkbox.invoke(), None)
            self.assertTrue(variable.get())
            self.assertEqual(checkbox._box.cget("text"), "✓")
            self.assertEqual(checkbox._box.cget("bg"), app.PALETTE["checkbox_checked_bg"])
            self.assertIn("<Return>", checkbox.widget.bindings)
            self.assertIn("<KeyPress-space>", checkbox.widget.bindings)
            self.assertIn("<KeyRelease-space>", checkbox.widget.bindings)

            variable.set(False)
            self.assertEqual(checkbox._box.cget("text"), "")
            self.assertEqual(checkbox._box.cget("bg"), app.PALETTE["checkbox_bg"])

            self.assertEqual(checkbox.widget.bindings["<Return>"](object()), "break")
            self.assertTrue(variable.get())
            self.assertEqual(checkbox.widget.bindings["<KeyPress-space>"](object()), "break")
            self.assertEqual(checkbox.widget.bindings["<KeyRelease-space>"](object()), "break")
            self.assertFalse(variable.get())

            checkbox.configure(state=fake_tk.DISABLED)
            self.assertEqual(checkbox._box.cget("bg"), app.PALETTE["checkbox_disabled_bg"])
            self.assertEqual(checkbox._label.cget("fg"), app.PALETTE["checkbox_disabled_text"])
            self.assertEqual(checkbox.widget.bindings["<Return>"](object()), "break")
            self.assertFalse(variable.get())
        finally:
            app.tk = previous_tk

    def test_form_inputs_no_longer_bind_page_mousewheel_handler(self) -> None:
        class FakeWidget:
            def __init__(self, master=None, **kwargs):
                self.master = master
                self.kwargs = kwargs
                self.bindings: dict[str, object] = {}

            def bind(self, sequence: str, callback: object) -> None:
                self.bindings[sequence] = callback

            def grid(self, *args, **kwargs) -> None:
                return None

            def pack(self, *args, **kwargs) -> None:
                return None

            def cget(self, key: str) -> object:
                return self.kwargs[key]

        class FakeEntry(FakeWidget):
            pass

        class FakeText(FakeWidget):
            pass

        class FakeLabel(FakeWidget):
            pass

        fake_tk = types.SimpleNamespace(
            Frame=FakeWidget,
            Entry=FakeEntry,
            Text=FakeText,
            Label=FakeLabel,
            FLAT="flat",
            EW="ew",
            LEFT="left",
            W="w",
            NW="nw",
        )
        previous_tk = app.tk
        app.tk = fake_tk
        try:
            profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
            profile_app.fonts = {"body": object(), "label": object(), "small": object()}
            profile_app._layout_metrics = app.compute_layout_metrics(viewport_width=1360, viewport_height=880)
            profile_app.section_wraplength = profile_app._layout_metrics["section_wraplength"]
            entry = profile_app._create_entry(FakeWidget(bg="white"), variable=object())
            self.assertNotIn("<MouseWheel>", entry.bindings)

            profile_app._add_text_row(FakeWidget(bg="white"), 0, "Description", "Help text")
            self.assertNotIn("<MouseWheel>", profile_app.description_text.bindings)
        finally:
            app.tk = previous_tk

    def test_compute_layout_metrics_scales_for_desktop_window(self) -> None:
        layout = app.compute_layout_metrics(
            viewport_width=1480,
            viewport_height=940,
        )

        self.assertGreater(layout["scale"], 1.0)
        self.assertEqual(layout["form_columns"], 2)
        self.assertGreaterEqual(layout["sidebar_width"], 300)
        self.assertIn("status_wraplength", layout)
        self.assertIn("workspace_gap", layout)

    def test_compute_layout_metrics_keeps_desktop_grid_at_minimum_window(self) -> None:
        layout = app.compute_layout_metrics(
            viewport_width=1280,
            viewport_height=820,
        )

        self.assertLess(layout["scale"], 1.0)
        self.assertEqual(layout["form_columns"], 2)
        self.assertLess(layout["page_pad_x"], 24)
        self.assertLess(layout["card_pad_x"], 16)
        self.assertGreaterEqual(layout["workspace_min_row_height"], 180)

    def test_build_layout_uses_direct_root_shell_without_page_canvas(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_layout)

        self.assertNotIn("self.main_canvas = tk.Canvas", source)
        self.assertNotIn("create_window", source)
        self.assertIn("self.layout_root = tk.Frame", source)
        self.assertIn("self.root.bind(\"<Configure>\", self._on_root_configure)", source)

    def test_sidebar_uses_existing_checkbox_label_pad_y_metric(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_sidebar)

        self.assertIn("checkbox_label_pad_y", source)
        self.assertNotIn("checkbox_label_pady", source)

    def test_content_layout_builds_fixed_workspace_grid_with_vault_section(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_content)

        self.assertNotIn("self._build_workspace_overview(shell)", source)
        self.assertIn("workspace.grid_columnconfigure(0, weight=1, uniform=\"workspace\")", source)
        self.assertIn("workspace.grid_rowconfigure(1, weight=1, uniform=\"workspace_rows\"", source)
        self.assertIn("self._build_workspace_vault_section", source)

    def test_profile_layout_routes_inline_help_through_shared_helpers(self) -> None:
        section_source = inspect.getsource(app.ProfileManagerApp._build_section_shell)
        entry_source = inspect.getsource(app.ProfileManagerApp._add_compact_entry_row)
        text_source = inspect.getsource(app.ProfileManagerApp._add_compact_text_row)
        sidebar_source = inspect.getsource(app.ProfileManagerApp._build_sidebar)
        action_source = inspect.getsource(app.ProfileManagerApp._build_workspace_vault_section)

        self.assertIn("_pack_title_with_help(", section_source)
        self.assertIn("_grid_label_with_help(", entry_source)
        self.assertIn("_grid_label_with_help(", text_source)
        self.assertIn("_pack_title_with_help(", sidebar_source)
        self.assertIn("_create_help_icon(", action_source)

    def test_profile_manager_defines_help_icon_and_bubble_helpers(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertIn("def _create_help_icon", source)
        self.assertIn("def _toggle_help_bubble", source)
        self.assertIn("def _pack_title_with_help", source)
        self.assertIn("def _grid_label_with_help", source)
        self.assertIn('text="?"', source)

    def test_layout_builds_lock_overlay_for_lower_account_surface(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._render_layout)

        self.assertIn("account_lock_overlay", source)
        self.assertIn("account_lock_title_var", source)
        self.assertIn("account_lock_body_var", source)
        self.assertIn("_sync_account_surface_lock", source)
        self.assertIn("self.manage_vault", source)
        self.assertIn("self.reset_vault", source)

    def test_compute_window_geometry_caps_requested_size_to_screen(self) -> None:
        geometry = app.compute_window_geometry(
            screen_width=1512,
            screen_height=982,
            requested_width=1480,
            requested_height=940,
        )

        self.assertEqual(geometry["width"], 1416)
        self.assertEqual(geometry["height"], 886)
        self.assertEqual(geometry["x"], 48)
        self.assertEqual(geometry["y"], 48)

    def test_init_uses_responsive_window_geometry_instead_of_fixed_size(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp.__init__)

        self.assertIn("_apply_window_geometry()", source)
        self.assertNotIn('self.root.geometry("1360x880")', source)

    def test_sidebar_layout_no_longer_uses_outer_scroll_canvas(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_sidebar)

        self.assertNotIn("self.sidebar_canvas = tk.Canvas", source)
        self.assertNotIn("sidebar_scrollbar", source)
        self.assertNotIn("create_window", source)

    def test_content_layout_no_longer_uses_outer_scroll_canvas(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_content)

        self.assertNotIn("self.content_canvas = tk.Canvas", source)
        self.assertNotIn("content_scrollbar", source)
        self.assertNotIn("create_window", source)

    def test_status_strip_owns_global_vault_controls_and_editor_context(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_hero)

        self.assertIn("editor_context_var", source)
        self.assertIn("manage_vault", source)
        self.assertIn("vault_state_var", source)
        self.assertIn("backend_var", source)
        self.assertIn("profile_action_hint_var", source)
        self.assertIn("credential_status_var", source)

    def test_workspace_vault_section_focuses_on_profile_specific_actions(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_workspace_vault_section)

        self.assertNotIn("vault_state_var", source)
        self.assertNotIn("manage_vault", source)
        self.assertNotIn("backend_var", source)
        self.assertIn("_create_checkbox", source)
        self.assertNotIn("profile_action_hint_var", source)
        self.assertNotIn("credential_status_var", source)
        self.assertIn("delete_current_profile", source)
        self.assertIn("save_profile", source)

    def test_security_section_no_longer_owns_vault_controls(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_security_section)

        self.assertNotIn("vault_state_var", source)
        self.assertNotIn("vault_action_button", source)
        self.assertNotIn("manage_vault", source)
        self.assertNotIn("tk.Checkbutton", source)
        self.assertNotIn("_create_checkbox", source)

    def test_security_copy_describes_shared_vault_storage(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp._build_security_section)

        self.assertIn("shared by all profiles", source.lower())

    def test_layout_related_methods_are_defined_once(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertEqual(source.count("def _build_layout"), 1)
        self.assertEqual(source.count("def _build_hero"), 1)
        self.assertEqual(source.count("def _build_workspace_vault_section"), 1)
        self.assertEqual(source.count("def refresh_vault_status"), 1)

    def test_zh_copy_describes_application_vault_instead_of_system_storage(self) -> None:
        zh_texts = app.TEXTS["zh"]

        self.assertIn("应用层 Vault", zh_texts["field_api_secret_help"])
        self.assertIn("应用层 Vault", zh_texts["field_api_passphrase_help"])
        self.assertIn("应用层 Vault", zh_texts["delete_confirm_message"])
        self.assertNotIn("系统安全存储", zh_texts["field_api_secret_help"])
        self.assertNotIn("系统安全存储", zh_texts["field_api_passphrase_help"])
        self.assertNotIn("系统安全存储", zh_texts["delete_confirm_message"])

    def test_refresh_vault_status_updates_lower_surface_lock_overlay(self) -> None:
        source = inspect.getsource(app.ProfileManagerApp.refresh_vault_status)

        self.assertIn("profile_actions_enabled", source)
        self.assertIn("account_surface_locked", source)
        self.assertIn("_sync_account_surface_lock()", source)

    def test_manage_vault_shows_error_when_status_lookup_fails(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.root = object()
        profile_app.local_text = lambda en_text, _zh_text: en_text

        fake_messagebox = types.SimpleNamespace(
            showwarning=mock.Mock(),
            showerror=mock.Mock(),
            showinfo=mock.Mock(),
        )

        with mock.patch.object(app, "messagebox", fake_messagebox):
            with mock.patch.object(app, "vault_status", side_effect=app.ProfileError("vault broke")):
                profile_app.manage_vault()

        fake_messagebox.showerror.assert_called_once_with(
            "Vault Error",
            "vault broke",
            parent=profile_app.root,
        )

    def test_save_profile_refreshes_agent_cache_after_success(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.profile_actions_enabled = True
        profile_app.root = object()
        profile_app.language = "en"
        profile_app.current_profile_id = None
        profile_app.name_var = FakeVar("main")
        profile_app.description_text = FakeTextValue("Main account")
        profile_app.contract_base_url_var = FakeVar("")
        profile_app.spot_base_url_var = FakeVar("")
        profile_app.api_key_var = FakeVar("key-1234")
        profile_app.api_secret_var = FakeVar("secret-1234")
        profile_app.api_passphrase_var = FakeVar("pass-1234")
        profile_app.default_var = FakeVar(True)
        profile_app.editor_context_var = FakeVar("")
        profile_app.local_text = lambda en_text, _zh_text: en_text
        profile_app.t = lambda key, **kwargs: key
        profile_app.refresh_profiles = mock.Mock()
        profile_app._set_mode_badge = mock.Mock()
        profile_app._update_profile_credential_status = mock.Mock()

        profile = types.SimpleNamespace(profile_id="profile-main", name="main", api_key_hint="***1234")

        with mock.patch.object(app, "tk", types.SimpleNamespace(END="end")):
            with mock.patch.object(app, "upsert_profile", return_value=profile):
                with mock.patch.object(app, "set_default_profile") as set_default_mock:
                    with mock.patch.object(app, "messagebox", types.SimpleNamespace(showwarning=mock.Mock(), showerror=mock.Mock(), showinfo=mock.Mock())):
                        with mock.patch.object(app, "refresh_agent_records") as refresh_agent_records_mock:
                            profile_app.save_profile()

        set_default_mock.assert_called_once_with("profile-main")
        refresh_agent_records_mock.assert_called_once()

    def test_delete_profile_refreshes_agent_cache_after_success(self) -> None:
        profile_app = app.ProfileManagerApp.__new__(app.ProfileManagerApp)
        profile_app.profile_actions_enabled = True
        profile_app.root = object()
        profile_app.language = "en"
        profile_app.current_profile_id = "profile-main"
        profile_app.name_var = FakeVar("")
        profile_app.local_text = lambda en_text, _zh_text: en_text
        profile_app.t = lambda key, **kwargs: key
        profile_app.refresh_profiles = mock.Mock()
        profile_app.reset_form = mock.Mock()

        profile = types.SimpleNamespace(profile_id="profile-main", name="main")

        with mock.patch.object(app, "get_profile_by_id", return_value=profile):
            with mock.patch.object(app, "delete_profile_by_id", return_value=True):
                with mock.patch.object(app, "messagebox", types.SimpleNamespace(showwarning=mock.Mock(), showerror=mock.Mock(), showinfo=mock.Mock(), askyesno=mock.Mock(return_value=True))):
                    with mock.patch.object(app, "refresh_agent_records") as refresh_agent_records_mock:
                        profile_app.delete_current_profile()

        refresh_agent_records_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
