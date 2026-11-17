from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import discord

from store_db import StoreDB, StoreError
from store_tickets import ticket_private, ticket_overwrites


class TicketDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store.db"
        self.db = StoreDB(self.path)
        self.db.init_tickets()
        self.db.configure(1, 99, 50, 60)
        self.db.configure_tickets(1, 99, 70)
        self.db.add_product(1, 99, "KEY", "Game", "", 10000, 5)
        self.order = self.db.reserve(1, 10, "KEY", 1, "manual", ttl=86400)

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, channel=100):
        self.db.bind_ticket(self.order["code"], 1, 10, channel)

    def test_persists_after_restart_without_changing_order_or_stock(self):
        self.bind()
        reopened = StoreDB(self.path)
        reopened.init_tickets()
        self.assertEqual(reopened.ticket_category(1), 70)
        self.assertEqual(reopened.settings(1)["staff_role_id"], 50)
        self.assertEqual(reopened.ticket(1, code=self.order["code"])["channel_id"], 100)
        self.assertEqual(reopened.order(self.order["code"]), self.order)
        self.assertEqual(reopened.products(1)[0]["stock"], 4)

    def test_rejects_wrong_buyer_and_wrong_server(self):
        for guild, buyer in ((2, 10), (1, 11)):
            with self.assertRaises(StoreError):
                self.db.bind_ticket(self.order["code"], guild, buyer, 100)
        self.bind()
        self.assertIsNone(self.db.ticket(2, channel=100))
        self.assertIsNone(self.db.ticket(2, code=self.order["code"]))

    def test_concurrent_requests_cannot_bind_two_tickets(self):
        def bind(channel):
            try:
                self.bind(channel)
                return True
            except StoreError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(bind, (100, 101))), 1)

    def test_cannot_close_pending_or_paid_order(self):
        self.bind()
        for paid in (False, True):
            if paid:
                self.db.manual_paid(self.order["code"], 1, 99)
            with self.assertRaises(StoreError):
                self.db.close_ticket(1, 100, 99)
            self.assertEqual(self.db.ticket(1, channel=100)["closed"], 0)

    def test_close_completed_ticket_keeps_money_stock_and_history(self):
        self.bind()
        self.db.manual_paid(self.order["code"], 1, 99)
        self.db.advance(self.order["code"], 1, 99, "fulfilling")
        self.db.advance(self.order["code"], 1, 99, "completed")
        before = self.db.revenue(1)
        self.db.close_ticket(1, 100, 99)
        self.db.forget_missing_ticket(1, 100)
        self.assertEqual(self.db.ticket(1, channel=100)["closed"], 1)
        self.assertEqual(self.db.revenue(1), before)
        self.assertEqual(self.db.products(1)[0]["stock"], 4)
        with self.assertRaises(StoreError):
            self.db.close_ticket(1, 100, 99)

    def test_cancel_then_close_does_not_release_stock_twice(self):
        self.bind()
        self.db.manual_cancel(self.order["code"], 1, 10)
        self.db.close_ticket(1, 100, 99)
        self.assertEqual(self.db.products(1)[0]["stock"], 5)

    def test_deleted_open_ticket_can_be_rebound_to_same_order(self):
        self.bind()
        self.db.forget_missing_ticket(2, 100)
        self.assertIsNotNone(self.db.ticket(1, channel=100))
        self.db.forget_missing_ticket(1, 100)
        self.bind(101)
        self.assertEqual(self.db.ticket(1, code=self.order["code"])["channel_id"], 101)
        self.assertEqual(self.db.order(self.order["code"])["description"], self.order["description"])


class TicketPrivacyTests(unittest.TestCase):
    @staticmethod
    def role(role_id, admin=False):
        role = Mock(spec=discord.Role)
        role.id = role_id
        role.permissions = discord.Permissions(administrator=admin)
        role.tags = None
        return role

    def test_no_inherited_public_access_and_buyer_override_is_allowed(self):
        everyone, staff, buyer, bot = (self.role(x) for x in (1, 50, 10, 99))
        guild = SimpleNamespace(default_role=everyone)
        overwrites = ticket_overwrites(guild, buyer, staff, bot)
        self.assertFalse(overwrites[everyone].view_channel)
        self.assertTrue(overwrites[buyer].view_channel)
        self.assertTrue(overwrites[staff].send_messages)

    def test_public_role_or_unrelated_member_is_rejected(self):
        everyone, staff, other = (self.role(x) for x in (1, 50, 60))
        channel = SimpleNamespace(guild=SimpleNamespace(roles=[everyone, staff, other]), overwrites={},
            permissions_for=lambda role: SimpleNamespace(view_channel=role.id in (50, 60)))
        self.assertFalse(ticket_private(channel, 10, 50, 99))
        channel.permissions_for = lambda role: SimpleNamespace(view_channel=role.id == 50)
        self.assertTrue(ticket_private(channel, 10, 50, 99))
        member = Mock(spec=discord.Member)
        member.id = 11
        member.roles = []
        member.guild_permissions = discord.Permissions.none()
        channel.overwrites = {member: discord.PermissionOverwrite(view_channel=True)}
        self.assertFalse(ticket_private(channel, 10, 50, 99))
        member.id = 10
        self.assertTrue(ticket_private(channel, 10, 50, 99))


if __name__ == "__main__":
    unittest.main()
