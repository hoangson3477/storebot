import io
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diagnostics import SafeFormatter, configure_logging, context, db_operation, unwrap_error


class DiagnosticsTests(unittest.TestCase):
    def format(self, message, exc_info=None):
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, message, (), exc_info)
        return SafeFormatter("%(levelname)s [%(trace)s] %(message)s").format(record)

    def test_redacts_env_secrets_and_encoded_values(self):
        with patch.dict(os.environ, {"AI_API_KEY": "secret/key+123", "DISCORD_TOKEN": "discord-secret-123"}):
            text = self.format("secret/key+123 secret%2Fkey%2B123 discord-secret-123")
        self.assertNotIn("secret/key", text)
        self.assertNotIn("secret%2F", text)
        self.assertNotIn("discord-secret", text)

    def test_redacts_credentials_in_exception_traceback(self):
        try:
            raise RuntimeError("postgresql://user:password123@db.invalid/test?sslmode=require Bearer bearer-test-value")
        except RuntimeError:
            import sys
            text = self.format("operation failed", sys.exc_info())
        self.assertIn("RuntimeError", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("password123", text)
        self.assertNotIn("bearer-test-value", text)

    def test_redacts_password_extracted_from_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:a%2Fb%2Bc@db.invalid/test"}):
            text = self.format('a/b+c password="other password" ?key=unknown-key&ok=1')
        self.assertNotIn("a/b+c", text)
        self.assertNotIn("other password", text)
        self.assertNotIn("unknown-key", text)

    def test_context_is_present_and_original_record_is_not_mutated(self):
        token = context.set("req=123 guild=456")
        try:
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
            output = SafeFormatter("[%(trace)s] %(message)s").format(record)
            self.assertIn("req=123 guild=456", output)
            self.assertFalse(hasattr(record, "trace"))
        finally:
            context.reset(token)

    def test_nested_command_errors_unwrap(self):
        cause = ValueError("expected")
        outer = RuntimeError()
        middle = RuntimeError()
        middle.original = cause
        outer.original = middle
        self.assertIs(unwrap_error(outer), cause)
        cause.original = outer
        self.assertIn(unwrap_error(outer), (outer, middle, cause))

    def test_db_wrapper_preserves_result_and_exception_without_logging_args(self):
        @db_operation
        def reserve(guild, payload):
            if guild == 2:
                raise RuntimeError("fake database failure")
            return 10
        with self.assertLogs("store.db", logging.DEBUG) as records:
            self.assertEqual(reserve(1, "PRIVATE-DATA"), 10)
            with self.assertRaises(RuntimeError):
                reserve(2, "PRIVATE-DATA")
        output = "\n".join(records.output)
        self.assertIn("guild=1", output)
        self.assertIn("db.failed", output)
        self.assertNotIn("PRIVATE-DATA", output)

    def test_console_and_rotating_file_redact_the_same_exception(self):
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        # Detach test runner handlers so configure_logging does not close them.
        root.handlers = []
        names = ("discord", "aiohttp", "httpx", "httpcore", "payos", "psycopg")
        previous = {name: logging.getLogger(name).level for name in names}
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "bot.log"
                console = io.StringIO()
                with patch.dict(os.environ, {"LOG_FILE": str(path), "LOG_LEVEL": "DEBUG", "AI_API_KEY": "file-secret-123"}), patch('sys.stderr', console):
                    configure_logging()
                    rotating = [handler for handler in root.handlers if hasattr(handler, 'backupCount')][0]
                    self.assertEqual(rotating.maxBytes, 5 * 1024 * 1024)
                    self.assertEqual(rotating.backupCount, 5)
                    logger = logging.getLogger("diagnostics.test")
                    try:
                        raise RuntimeError("file-secret-123")
                    except RuntimeError:
                        logger.exception("test.failure")
                    for handler in root.handlers:
                        handler.flush()
                    file_text = path.read_text(encoding="utf-8")
                    self.assertIn("test.failure", file_text)
                    self.assertIn("[REDACTED]", file_text)
                    self.assertNotIn("file-secret-123", file_text)
                    self.assertNotIn("file-secret-123", console.getvalue())
                for handler in root.handlers:
                    handler.close()
                root.handlers = []
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = old_handlers
            root.setLevel(old_level)
            for name, level in previous.items():
                logging.getLogger(name).setLevel(level)
