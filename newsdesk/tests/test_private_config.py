import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from newsdesk import private_config, service
from newsdesk.evaluation import AIHubPricePredictor


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "private"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "aihub.env"
        patch = mock.patch.object(private_config, "DEFAULT_CONFIG_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, content=None):
        content = content if content is not None else "AIHUB_API_KEY=fixture-private-key\nAIHUB_BASE_URL=http://127.0.0.1:8765/v1\n"
        self.path.write_text(content, encoding="utf-8")
        self.path.chmod(0o600)

    def test_owner_only_configuration_loads_without_returning_values(self):
        self.write()
        environment = {}
        names = private_config.load_private_aihub_config(environ=environment)
        self.assertEqual(set(names), {"AIHUB_API_KEY", "AIHUB_BASE_URL"})
        self.assertEqual(environment["AIHUB_API_KEY"], "fixture-private-key")
        self.assertNotIn("fixture-private-key", repr(names))

    def test_missing_file_preserves_environment_and_creates_nothing(self):
        environment = {"UNRELATED": "unchanged"}
        self.assertEqual(private_config.load_private_aihub_config(environ=environment), ())
        self.assertEqual(environment, {"UNRELATED": "unchanged"})
        self.assertFalse(self.path.exists())

    def test_nonempty_environment_key_overrides_even_an_unrelated_bad_file(self):
        self.write()
        self.path.chmod(0o644)
        environment = {"AIHUB_API_KEY": "fixture-process-key", "AIHUB_BASE_URL": "http://localhost:9999/v1"}
        self.assertEqual(private_config.load_private_aihub_config(environ=environment), ())
        self.assertEqual(environment["AIHUB_API_KEY"], "fixture-process-key")

    def test_existing_base_url_wins_and_empty_key_is_filled(self):
        self.write()
        environment = {"AIHUB_API_KEY": "", "AIHUB_BASE_URL": "http://localhost:9999/v1"}
        self.assertEqual(private_config.load_private_aihub_config(environ=environment), ("AIHUB_API_KEY",))
        self.assertEqual(environment["AIHUB_BASE_URL"], "http://localhost:9999/v1")
        self.assertEqual(environment["AIHUB_API_KEY"], "fixture-private-key")

    def test_file_and_parent_directory_permissions_fail_with_safe_diagnostics(self):
        for target, mode, required in (("file", 0o644, "0600"), ("directory", 0o755, "0700")):
            with self.subTest(target=target):
                self.directory.chmod(0o700)
                self.write()
                (self.path if target == "file" else self.directory).chmod(mode)
                with self.assertRaises(private_config.PrivateConfigError) as raised:
                    private_config.load_private_aihub_config(environ={})
                self.assertIn(required, str(raised.exception))
                self.assertNotIn("fixture-private-key", str(raised.exception))

    def test_foreign_owner_and_symbolic_links_are_rejected(self):
        self.write()
        current_uid = os.geteuid()
        with mock.patch.object(private_config.os, "geteuid", return_value=current_uid + 1):
            with self.assertRaisesRegex(private_config.PrivateConfigError, "当前用户"):
                private_config.load_private_aihub_config(environ={})
        target = self.directory / "original.env"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(private_config.PrivateConfigError):
            private_config.load_private_aihub_config(environ={})
        self.path.unlink()
        self.path = Path(self.temporary.name) / "linked" / "aihub.env"
        (Path(self.temporary.name) / "linked").symlink_to(self.directory, target_is_directory=True)
        target.rename(self.directory / "aihub.env")
        with self.assertRaises(private_config.PrivateConfigError):
            private_config.load_private_aihub_config(path=self.path, environ={})

    def test_only_allowlisted_assignments_are_parsed_atomically(self):
        invalid = ["PATH=fixture-private-key", "source fixture-private-key", "AIHUB_API_KEY='fixture-private-key",
                   "AIHUB_API_KEY=", "AIHUB_API_KEY=fixture-private-key\nAIHUB_API_KEY=duplicate"]
        for content in invalid:
            with self.subTest(content_kind=content.split("=", 1)[0]):
                self.write("AIHUB_BASE_URL=http://127.0.0.1/v1\n" + content + "\n")
                environment = {}
                with self.assertRaises(private_config.PrivateConfigError) as raised:
                    private_config.load_private_aihub_config(environ=environment)
                self.assertEqual(environment, {})
                self.assertNotIn("fixture-private-key", str(raised.exception))

    def test_quoted_values_are_literal_without_shell_expansion(self):
        self.write("# owner-only configuration\nexport AIHUB_API_KEY='$(must_not_execute)'\n"
                   'AIHUB_BASE_URL="http://127.0.0.1:8765/v1"\n')
        environment = {}
        private_config.load_private_aihub_config(environ=environment)
        self.assertEqual(environment["AIHUB_API_KEY"], "$(must_not_execute)")

    def test_predictor_initialization_uses_private_configuration(self):
        self.write()
        with mock.patch.dict(os.environ, {}, clear=True):
            predictor = AIHubPricePredictor("deepseek-v4-pro")
            self.assertEqual(predictor.api_key, "fixture-private-key")
            self.assertEqual(predictor.base_url, "http://127.0.0.1:8765/v1")

    def test_explicit_predictor_credentials_skip_private_file(self):
        self.write()
        self.path.chmod(0o644)
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("newsdesk.evaluation.load_private_aihub_config") as load:
            predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture-explicit-key", base_url="http://localhost/v1")
            self.assertEqual(predictor.api_key, "fixture-explicit-key")
            load.assert_not_called()

    def test_service_loads_configuration_before_starting_children(self):
        with mock.patch.object(service, "load_private_aihub_config", side_effect=private_config.PrivateConfigError("private permissions")), \
                mock.patch.object(service.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(private_config.PrivateConfigError, "private permissions"):
                service.main()
            spawn.assert_not_called()

    def test_server_reports_configuration_error_without_constructing_server(self):
        from newsdesk import server
        error_output = io.StringIO()
        with mock.patch.object(server, "load_private_aihub_config", side_effect=private_config.PrivateConfigError("private permissions")), \
                mock.patch.object(server, "create_server") as create, mock.patch("sys.argv", ["newsdesk.server"]), \
                contextlib.redirect_stderr(error_output):
            with self.assertRaises(SystemExit):
                server.main()
            create.assert_not_called()
        self.assertIn("private permissions", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
