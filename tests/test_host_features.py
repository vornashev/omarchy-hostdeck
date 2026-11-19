"""System information and file apps: synthetic data and fake processes only."""
import io
import json
import os
import signal
import subprocess
import sys
from unittest.mock import patch

from test_helper import h, Isolated, profile, ID, REAL_POPEN


def fastfetch_bytes():
    return json.dumps([
        {"type": "Host", "result": {"vendor": "Example", "name": "Virtual machine", "serial": "PRIVATE-SERIAL"}},
        {"type": "CPU", "result": {"cpu": "Example CPU", "cores": {"online": 8}}},
        {"type": "Memory", "result": {"total": 8192, "used": 2048}},
        {"type": "Disk", "result": [{"mountpoint": "/", "bytes": {"total": 16384, "free": 8192}, "serial": "PRIVATE-DISK"}]},
        {"type": "OS", "result": {"prettyName": "Example Linux"}},
        {"type": "Kernel", "result": {"name": "Linux", "release": "6.0", "architecture": "x86_64"}},
    ]).encode()


class FileAppTests(Isolated):
    def settings(self, **changes):
        return h.global_settings({"version": 1, "defaultTerminal": "ghostty", **changes})

    def test_old_settings_keep_nautilus_and_new_settings_round_trip_without_export(self):
        self.assertEqual(self.store.load_settings()["fileManager"], "nautilus")
        settings = self.settings(fileManager="custom", fileExecutable="example-files",
                                 fileArguments=["--new-window", "{target}"], fileMode="uri", systemFields=["os", "cpu"])
        with patch.object(h.shutil, "which", return_value="/synthetic/app"):
            self.call(op="saveSettings", settings=settings)
        self.assertEqual(h.Store(self.store.root).load_settings(), settings)
        exported = h.export_document(self.store.load())
        self.assertNotIn("fileExecutable", exported)
        self.assertNotIn("systemFields", exported)

    def test_invalid_settings_do_not_replace_saved_data(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/app"):
            self.call(op="saveSettings", settings=self.settings())
        before = self.store.settings_path.read_bytes()
        for changes in (
            {"fileManager": "unknown"}, {"fileManager": []}, {"fileMode": "shell"},
            {"fileMode": []}, {"fileManager": "custom"}, {"fileExecutable": "sh -c"},
            {"fileExecutable": "/tmp/app"}, {"fileArguments": []}, {"fileArguments": ["{password}"]},
            {"fileArguments": ["--path={target}"]}, {"fileArguments": ["{target}", "{target}"]},
            {"fileArguments": ["{target}", "a\nb"]}, {"fileArguments": ["{target}"] * 17},
            {"fileArguments": ["{target}", "x" * 513]}, {"systemFields": ["os", "os"]},
            {"systemFields": ["serial"]}, {"systemFields": "os"}, {"systemFields": [[]]},
            {"password": "synthetic-secret"},
        ):
            with self.subTest(changes=changes), self.assertRaises(h.Error):
                self.call(op="saveSettings", settings={**self.settings(), **changes})
        self.assertEqual(self.store.settings_path.read_bytes(), before)

    def test_strata_and_custom_app_open_existing_mount_without_remount_or_secret(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
             patch.object(h.shutil, "which", return_value="/synthetic/app"), \
             patch.object(h, "sftp_mount_kind", return_value="mounted"), \
             patch.object(h, "secret") as secret, patch.object(h.subprocess, "run") as run, \
             patch.object(h.subprocess, "Popen") as spawn:
            point = str(h.sftp_mountpoint(profile()))
            h.mount_sftp(profile(), self.settings(fileManager="strata"))
            self.assertEqual(spawn.call_args.args[0], ["strata", point])
            h.mount_sftp(profile(), self.settings(fileManager="custom", fileExecutable="example-files",
                                                 fileArguments=["--window", "{target}", "literal $(id)"]))
            self.assertEqual(spawn.call_args.args[0], ["example-files", "--window", point, "literal $(id)"])
            self.assertFalse(spawn.call_args.kwargs.get("shell", False))
        secret.assert_not_called()
        run.assert_not_called()

    def test_saved_file_manager_is_used_when_opening_files_from_catalog(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/app"):
            options = self.call(op="getSettings")["fileManagers"]
            self.assertIn({"value": "flea", "label": "Flea", "available": True}, options)
            self.call(op="saveSettings", settings=self.settings(fileManager="flea"))
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
             patch.object(h.shutil, "which", return_value="/synthetic/app"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "sftp_mount_kind", return_value="mounted"), \
             patch.object(h.subprocess, "Popen") as spawn:
            result = self.call(op="launch", id=ID, client="sftp")
            self.assertEqual(spawn.call_args.args[0], ["flea", str(h.sftp_mountpoint(profile()))])
        self.assertEqual(result["sftpStates"][ID]["state"], "mounted")

    def test_missing_app_cannot_mount_or_launch_fallback(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
             patch.object(h.shutil, "which", side_effect=lambda n: None if n == "strata" else "/synthetic/tool"), \
             patch.object(h, "secret") as secret, patch.object(h.subprocess, "run") as run, \
             patch.object(h.subprocess, "Popen") as spawn, self.assertRaises(h.Error):
            h.mount_sftp(profile(), self.settings(fileManager="strata"))
        secret.assert_not_called()
        run.assert_not_called()
        spawn.assert_not_called()

    def test_app_start_failure_preserves_existing_mount(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
             patch.object(h.shutil, "which", return_value="/synthetic/app"), \
             patch.object(h, "sftp_mount_kind", return_value="mounted"), \
             patch.object(h.subprocess, "Popen", side_effect=OSError("synthetic-secret")), \
             patch.object(h, "unmount_sftp") as unmount, self.assertRaises(h.Error) as raised:
            h.mount_sftp(profile(), self.settings(fileManager="strata"))
        self.assertNotIn("synthetic-secret", str(raised.exception))
        unmount.assert_not_called()

    def test_direct_uri_encodes_ipv6_port_and_path_without_secrets_or_mounting(self):
        d = profile(host="2001:db8::1", remoteFolder="/srv/My Files", auth="password")
        with patch.object(h, "require_sftp_disconnected"), \
             patch.object(h.shutil, "which", return_value="/synthetic/app"), \
             patch.object(h.subprocess, "Popen") as spawn, patch.object(h, "secret") as secret, \
             patch.object(h, "sftp_mountpoint") as point:
            h.mount_sftp(d, self.settings(fileManager="strata", fileMode="uri"))
        self.assertEqual(spawn.call_args.args[0], ["strata", "sftp://tema@[2001:db8::1]:2222/srv/My%20Files"])
        secret.assert_not_called()
        point.assert_not_called()

    def test_direct_uri_does_not_hide_an_owned_or_foreign_mount(self):
        for kind in ("mounted", "foreign"):
            with self.subTest(kind=kind), patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
                 patch.object(h, "sftp_mount_kind", return_value=kind), \
                 patch.object(h.subprocess, "Popen") as spawn, self.assertRaises(h.Error):
                h.mount_sftp(profile(), self.settings(fileMode="uri"))
            spawn.assert_not_called()

    def test_direct_uri_still_requires_hostkey_review_and_is_not_a_managed_mount(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/app"):
            self.call(op="saveSettings", settings=self.settings(fileManager="strata", fileMode="uri"))
        preview = {"hostKeys": {"state": "new", "deviceId": ID}}
        with patch.object(h, "has_known_host_entry", return_value=False), \
             patch.object(h, "host_key_preview", return_value=preview), \
             patch.object(h.subprocess, "Popen") as spawn:
            self.assertEqual(self.call(op="launch", id=ID, client="sftp"), preview)
        spawn.assert_not_called()
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h.shutil, "which", return_value="/synthetic/app"), \
             patch.object(h, "sftp_mount_kind", return_value="unmounted"), \
             patch.object(h, "secret") as secret, patch.object(h.subprocess, "Popen") as spawn:
            result = self.call(op="launch", id=ID, client="sftp")
        self.assertEqual(spawn.call_args.args[0], ["strata", "sftp://tema@example.test:2222/"])
        self.assertEqual(result["sftpStates"][ID]["state"], "unmounted")
        secret.assert_not_called()


class SystemInfoTests(Isolated):
    def fake_remote_program(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_install_is_offered_only_for_confirmed_missing_fastfetch(self):
        (self.root / "sh").symlink_to("/bin/sh")

        def fake_ssh(argv, **kwargs):
            kwargs["env"] = {"PATH": str(self.root)}
            return REAL_POPEN(["/bin/sh", "-c", argv[-1]], **kwargs)

        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h.subprocess, "Popen", side_effect=fake_ssh):
            missing = self.call(op="systemInfo", id=ID)["systemInfo"]
            self.assertTrue(missing["fastfetchInstallToken"])
            self.assertEqual(missing["binding"], h.system_binding(profile()))
            self.assertNotIn("fields", missing)
            self.fake_remote_program("fastfetch", "exit 127")
            with self.assertRaises(h.Error):
                self.call(op="systemInfo", id=ID)

    def test_install_requires_current_profile_and_verified_key_at_launch_and_terminal(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/bin"), \
             patch.object(h, "has_known_host_entry", return_value=True) as known, \
             patch.object(h, "bounded_system_query", return_value=None), \
             patch.object(h.subprocess, "Popen") as spawn:
            token = self.call(op="systemInfo", id=ID)["systemInfo"]["fastfetchInstallToken"]
            request = {"op": "installFastfetch", "id": ID, "token": token}
            with self.assertRaises(h.Error):
                self.call(**request, command="id")
            known.return_value = False
            with self.assertRaises(h.Error):
                self.call(**request)
            spawn.assert_not_called()
            known.return_value = True
            self.call(op="saveSettings", settings={"version": 1, "defaultTerminal": "foot"})
            self.call(**request)
            args = spawn.call_args.args[0]
            self.assertEqual(args[0], "foot")
            self.assertEqual(args[-3:], ["--install-fastfetch", ID, token])

            with self.store.locked():
                self.data["devices"][0]["host"] = "changed.example.test"
                self.store.save(self.data)
            with self.assertRaises(h.Error):
                self.call(**request)
            with patch.object(h, "Store", return_value=self.store), self.assertRaises(h.Error):
                h.terminal_install_fastfetch(ID, token, "en")
            spawn.assert_called_once()

    def test_terminal_installs_with_strict_ssh_and_the_remote_package_manager(self):
        (self.root / "sh").symlink_to("/bin/sh")
        (self.root / "ln").symlink_to("/usr/bin/ln")
        self.fake_remote_program("id", "printf '1000\\n'")
        self.fake_remote_program("sudo", 'printf "%s\\n" "$@" > "$TRACE"\nexec "$@"')
        installed = self.fake_remote_program("installed", "exit 0")
        trace = self.root / "trace"

        def fake_ssh(argv, **kwargs):
            self.assertEqual(argv[:3], ["ssh", "-F", "/dev/null"])
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertNotIn("StrictHostKeyChecking=ask", argv)
            self.assertIn("ClearAllForwardings=yes", argv)
            self.assertIn("PermitLocalCommand=no", argv)
            self.assertIn("-t", argv)
            self.assertEqual(argv[-2], profile()["host"])
            self.assertNotIn("stdin", kwargs)  # Inherit the terminal for sudo prompts.
            env = {"PATH": str(self.root), "TRACE": str(trace), "INSTALLED": str(installed)}
            with REAL_POPEN(["/bin/sh", "-c", argv[-1]], env=env) as child:
                return subprocess.CompletedProcess(argv, child.wait())

        with patch.object(h, "Store", return_value=self.store), \
             patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "bounded_system_query", return_value=None), \
             patch.object(h.subprocess, "run", side_effect=fake_ssh), \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            token = self.call(op="systemInfo", id=ID)["systemInfo"]["fastfetchInstallToken"]
            for manager, args in (("apt-get", ["install"]), ("dnf", ["install"]),
                                  ("pacman", ["-S", "--needed"]), ("zypper", ["install"]), ("apk", ["add"])):
                with self.subTest(manager=manager):
                    package = self.fake_remote_program(manager, 'ln -s "$INSTALLED" "${INSTALLED%/*}/fastfetch"')
                    self.assertEqual(h.terminal_install_fastfetch(ID, token, "en"), 0)
                    self.assertEqual(trace.read_text().splitlines(), [manager, *args, "fastfetch"])
                    trace.unlink()
                    self.assertEqual(h.terminal_install_fastfetch(ID, token, "en"), 0)
                    self.assertFalse(trace.exists())
                    (self.root / "fastfetch").unlink()
                    package.unlink()
            self.assertNotEqual(h.terminal_install_fastfetch(ID, token, "en"), 0)
            self.assertIn("No supported package manager", errors.getvalue())
            self.fake_remote_program("apt-get", "exit 100")
            self.assertNotEqual(h.terminal_install_fastfetch(ID, token, "en"), 0)
            self.assertIn("Could not install Fastfetch", errors.getvalue())
            self.assertIn("Refresh system details", output.getvalue())

    def test_normalizes_selected_data_without_serials_or_raw_results(self):
        info = h.parse_system_info(fastfetch_bytes())
        self.assertEqual(info["unavailable"], [])
        self.assertEqual(info["fields"]["cpu"], {"text": "Example CPU", "threads": 8})
        self.assertEqual(info["fields"]["disk"], [{"mountpoint": "/", "total": 16384, "used": 8192}])
        self.assertNotIn("PRIVATE", json.dumps(info))

    def test_older_disk_schema_partial_modules_and_plain_text(self):
        info = h.parse_system_info(json.dumps([
            {"type": "CPU", "result": {"name": '<img src="https://example.invalid/a">\n', "cores": {"online": True}}},
            {"type": "OS", "error": "synthetic-secret"},
            {"type": "Memory", "result": {"used": 2, "total": 0}},
            {"type": "Disk", "result": [{"mountpoint": "/", "bytes": {"used": 20, "total": 100}}]},
            {"type": "Unknown", "result": "synthetic-secret"},
        ]).encode())
        self.assertEqual(info["fields"]["cpu"]["text"], '<img src="https://example.invalid/a">')
        self.assertNotIn("threads", info["fields"]["cpu"])
        self.assertEqual(info["fields"]["disk"][0]["used"], 20)
        self.assertIn("memory", info["unavailable"])
        self.assertNotIn("synthetic-secret", json.dumps(info))

    def test_rejects_malformed_oversized_duplicate_and_unusable_results(self):
        for raw in (b"not-json", b"\xff", b"{}", b"[]", b"[1]", b"[{}]", b" " * (h.MAX_SYSTEM_BYTES + 1),
                    b'[{"type":"OS","result":{"name":"Example"}},{"type":"OS"}]',
                    b'[{"type":"Memory","result":{"total":100,"used":101}}]',
                    json.dumps([{"type": "Disk", "result": [{}] * 129}]).encode()):
            with self.subTest(raw=raw[:80]), self.assertRaises(h.Error):
                h.parse_system_info(raw)

    def test_untrusted_text_is_bounded_and_invalid_numbers_are_unavailable(self):
        info = h.parse_system_info(json.dumps([
            {"type": "OS", "result": {"prettyName": "x" * 400}},
            {"type": "Memory", "result": {"total": 2**60, "used": 1}},
            {"type": "Disk", "result": [{"mountpoint": "/", "bytes": {"total": 100, "free": 101}}]},
        ]).encode())
        self.assertEqual(len(info["fields"]["os"]["text"]), 256)
        self.assertIn("memory", info["unavailable"])
        self.assertIn("disk", info["unavailable"])

    def test_collection_requires_verified_key_and_accepts_no_remote_command(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=False), \
             patch.object(h, "bounded_system_query") as query, self.assertRaises(h.Error):
            self.call(op="systemInfo", id=ID)
        query.assert_not_called()
        with self.assertRaises(h.Error):
            self.call(op="systemInfo", id=ID, command="id")

    def test_collection_persists_snapshot_separately_from_profile_and_settings(self):
        before = self.store.path.read_bytes()
        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "bounded_system_query", return_value=fastfetch_bytes()):
            result = self.call(op="systemInfo", id=ID)["systemInfo"]
        self.assertEqual(result["id"], ID)
        self.assertEqual(result["binding"], h.system_binding(profile()))
        self.assertIn("fetchedAt", result)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.settings_path.exists())

    def catalog(self):
        with patch.object(h, "cleanup_sftp_runtime"), patch.object(h, "sftp_states", return_value={}):
            return h.dispatch({"op": "list"}, h.Store(self.store.root))

    def test_snapshot_survives_restart_and_error_and_is_removed_on_rebind(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "bounded_system_query", return_value=fastfetch_bytes()) as query:
            saved = self.call(op="systemInfo", id=ID)["systemInfo"]
            query.side_effect = h.Error("failed")
            with self.assertRaises(h.Error):
                self.call(op="systemInfo", id=ID)
        self.assertEqual(self.catalog()["systemCache"], {ID: saved})
        with self.store.locked():
            self.data["devices"][0]["port"] += 1
            self.store.save(self.data)
        self.assertEqual(self.catalog()["systemCache"], {})
        with self.assertRaises(h.Error):
            self.call(op="systemInfo", id=ID, binding=saved["binding"])

    def test_corrupt_snapshot_does_not_leak_raw_data_or_block_listing(self):
        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "bounded_system_query", return_value=fastfetch_bytes()):
            saved = self.call(op="systemInfo", id=ID)["systemInfo"]
        path = self.store.system_path(profile(), "snapshot")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved["fields"]["secret"] = "synthetic-private"
        path.write_text(json.dumps(saved))
        catalog = self.catalog()
        self.assertEqual(catalog["systemCache"], {})
        self.assertNotIn("synthetic-private", json.dumps(catalog))

    def test_preferences_patch_inherit_and_delete(self):
        request = {"op": "setSystemPreference", "id": ID, "binding": h.system_binding(profile())}
        for change in ({"mode": "schedule"}, {"fields": []}, {"minutes": 30}):
            self.call(**request, change=change)
        self.assertEqual(self.catalog()["systemPreferences"][ID], {"mode": "schedule", "minutes": 30, "fields": []})
        for change in ({"mode": "other"}, {"minutes": 1}, {"fields": ["password"]}, {"secret": "bad"}):
            with self.assertRaises(h.Error):
                self.call(**request, change=change)
        self.call(**request, change={"fields": None})
        self.assertIsNone(self.catalog()["systemPreferences"][ID]["fields"])
        with patch.object(h, "require_sftp_disconnected"):
            self.call(op="deleteDevice", id=ID)
        self.assertEqual(self.catalog()["systemPreferences"], {})

    def test_changed_profile_during_collection_cannot_return_stale_data(self):
        def change_profile(d):
            with self.store.locked():
                self.data["devices"][0]["host"] = "changed.example.test"
                self.store.save(self.data)
            return fastfetch_bytes()

        with patch.object(h.shutil, "which", return_value="/synthetic/ssh"), \
             patch.object(h, "has_known_host_entry", return_value=True), \
             patch.object(h, "bounded_system_query", side_effect=change_profile), self.assertRaises(h.Error):
            self.call(op="systemInfo", id=ID)

    def test_collection_uses_strict_noninteractive_isolated_ssh(self):
        for d in (profile(), profile(auth="password"), profile(keyPath="/tmp/synthetic-key", saveKeyPassphrase=True)):
            with self.subTest(auth=d["auth"]):
                args = h.system_argv(d)
                self.assertEqual(args[:3], ["ssh", "-F", "/dev/null"])
                self.assertIn("StrictHostKeyChecking=yes", args)
                self.assertNotIn("StrictHostKeyChecking=ask", args)
                self.assertIn("PermitLocalCommand=no", args)
                self.assertIn("ClearAllForwardings=yes", args)
                self.assertIn("-T", args)
                self.assertEqual(args[-2:], [d["host"], h.SYSTEM_COMMAND])
                self.assertEqual("BatchMode=yes" in args, d["auth"] != "password" and not d["saveKeyPassphrase"])

    def fake_query(self, code):
        children = []

        def spawn(argv, **kwargs):
            process = REAL_POPEN([sys.executable, "-c", code], **kwargs)
            children.append(process)
            return process

        with patch.object(h.subprocess, "Popen", side_effect=spawn):
            try:
                return h.bounded_system_query(profile())
            finally:
                self.assertTrue(children)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdout.closed)

    def test_bounded_reader_accepts_synthetic_output_and_never_forwards_stderr(self):
        self.assertEqual(self.fake_query("import sys; sys.stderr.write('SYNTHETIC-secret'); print('[]')"), b"[]\n")

    def test_bounded_reader_kills_noisy_and_slow_children_and_hides_errors(self):
        for code in (
            "import os; os.write(1, b'x' * 300000)",
            "import sys; print('SYNTHETIC-secret'); sys.exit(1)",
            "import sys; sys.exit(127)",
        ):
            with self.subTest(code=code), self.assertRaises(h.Error) as raised:
                self.fake_query(code)
            self.assertNotIn("SYNTHETIC-secret", str(raised.exception))
        with patch.object(h, "SYSTEM_TIMEOUT", 0.05), self.assertRaises(h.Error):
            self.fake_query("import time; time.sleep(5)")

    def test_helper_termination_cleans_up_child_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(h.select, "select", side_effect=lambda *args: signal.raise_signal(signal.SIGTERM)), \
             self.assertRaises(h.Error):
            self.fake_query("import time; time.sleep(5)")
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
