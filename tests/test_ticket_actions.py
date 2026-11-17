from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from store_db import StoreError
from store_tickets import TicketManager


class TicketActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Mock()
        self.db.ticket.return_value = {"order_code": 1000000000001, "guild_id": 1,
                                       "channel_id": 100, "buyer_id": 10, "closed": 0}
        self.db.order.return_value = {"code": 1000000000001, "status": "pending", "description": "ABC234"}
        self.cog = SimpleNamespace(db=self.db, staff=AsyncMock(return_value=False),
                                   bot=SimpleNamespace(user=SimpleNamespace(id=99)))
        self.manager = TicketManager(self.cog)
        self.manager.check_private = AsyncMock()
        self.guild = SimpleNamespace(id=1)
        self.channel = SimpleNamespace(id=100, send=AsyncMock(), set_permissions=AsyncMock())
        self.interaction = SimpleNamespace(guild=self.guild, guild_id=1, channel=self.channel,
            channel_id=100, user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def test_reported_transfer_never_marks_paid_and_has_cooldown(self):
        await self.manager.controls.run(self.interaction, "sent")
        self.channel.send.assert_awaited_once()
        self.db.manual_paid.assert_not_called()
        self.db.advance.assert_not_called()
        await self.manager.controls.run(self.interaction, "sent")
        self.channel.send.assert_awaited_once()

    async def test_other_buyer_cannot_use_ticket_controls(self):
        self.interaction.user.id = 11
        with self.assertRaises(StoreError):
            await self.manager.current(self.interaction)
        self.manager.check_private.assert_not_awaited()

    async def test_buyer_cannot_close_ticket(self):
        await self.manager.controls.run(self.interaction, "close")
        self.db.close_ticket.assert_not_called()
        self.channel.set_permissions.assert_not_awaited()

    async def test_close_pending_ticket_does_not_change_permissions_or_stock(self):
        with self.assertRaises(StoreError):
            await self.manager.close(self.guild, self.channel, 99)
        self.channel.set_permissions.assert_not_awaited()
        self.db.close_ticket.assert_not_called()
        self.db.manual_cancel.assert_not_called()
