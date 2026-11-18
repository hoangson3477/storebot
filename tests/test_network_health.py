import socket
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from network_health import BackgroundBackoff, temporary_dns_error
from postgres_db import PostgresStoreDB, connect_with_dns_retry
from store_db import DatabaseUnavailable


class DNSRetryTests(unittest.TestCase):
    def test_recovers_after_two_dns_failures(self):
        connection = object()
        connect = Mock(side_effect=[socket.gaierror(socket.EAI_AGAIN, 'temporary failure'),
                                   RuntimeError('Temporary failure in name resolution'), connection])
        with patch('postgres_db.time.sleep') as sleep, self.assertLogs('postgres_db') as logs:
            self.assertIs(connect_with_dns_retry(connect), connection)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])
        self.assertIn('db.dns_recovered', '\n'.join(logs.output))

    def test_exhausted_dns_is_a_friendly_error_and_preserves_cause(self):
        error = RuntimeError('Temporary failure in name resolution')
        connect = Mock(side_effect=error)
        with patch('postgres_db.time.sleep'), self.assertLogs('postgres_db'), self.assertRaises(DatabaseUnavailable) as caught:
            connect_with_dns_retry(connect)
        self.assertEqual(connect.call_count, 3)
        self.assertIs(caught.exception.__cause__, error)

    def test_auth_and_permanent_dns_errors_are_not_retried(self):
        for error in (RuntimeError('password authentication failed'),
                      socket.gaierror(socket.EAI_NONAME, 'Name or service not known')):
            connect = Mock(side_effect=error)
            with patch('postgres_db.time.sleep') as sleep, self.assertRaises(type(error)):
                connect_with_dns_retry(connect)
            connect.assert_called_once()
            sleep.assert_not_called()

    def database(self):
        db = PostgresStoreDB.__new__(PostgresStoreDB)
        db.url = 'postgresql://test.invalid/example'
        db.sslmode = 'require'
        return db

    def test_transaction_failure_does_not_reconnect_or_replay(self):
        connection = MagicMock()
        with patch('psycopg.connect', return_value=connection) as connect, patch('postgres_db.time.sleep') as sleep:
            with self.assertRaises(socket.gaierror):
                with self.database().connect(True):
                    raise socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_commit_failure_does_not_reconnect_or_replay(self):
        connection = MagicMock()
        connection.__exit__.side_effect = socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')
        with patch('psycopg.connect', return_value=connection) as connect, patch('postgres_db.time.sleep') as sleep:
            with self.assertRaises(socket.gaierror):
                with self.database().connect(True):
                    pass
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_dns_classifier_handles_nested_cause_and_cycles(self):
        outer = RuntimeError('connector failed')
        inner = socket.gaierror(socket.EAI_AGAIN, 'temporary failure')
        outer.__cause__ = inner
        self.assertTrue(temporary_dns_error(outer))
        a, b = RuntimeError('one'), RuntimeError('two')
        a.__cause__, b.__cause__ = b, a
        self.assertFalse(temporary_dns_error(a))


class BackgroundBackoffTests(unittest.TestCase):
    def test_intervals_cap_at_five_minutes_and_reset_after_success(self):
        backoff = BackgroundBackoff(30)
        self.assertEqual([backoff.failed() for _ in range(6)], [60, 120, 240, 300, 300, 300])
        self.assertEqual(backoff.recovered(), 6)
        self.assertEqual(backoff.recovered(), 0)
        self.assertEqual(backoff.failed(), 60)
