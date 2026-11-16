import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from store_db import StoreError

try:
    import discord
    from discord.ext import commands
    from store import Store, order_embed, payment_message
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "Install requirements.txt for Discord command tests")
class StoreCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATABASE_URL": "", "STORE_BACKEND": "sqlite", "PAYMENT_PROVIDER": "manual", "STORE_DB_PATH": str(Path(self.temp.name) / "test.db")})
        self.env.start()
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        await self.bot.__aenter__()
        await self.bot.load_extension("store")
        self.store = self.bot.get_cog("Store")
        self.store.db.add_product(1, 9, "TEST", "Test", "", 10000, 5)

    async def asyncTearDown(self):
        await self.bot.close()
        self.env.stop()
        self.temp.cleanup()

    async def test_slash_commands_register_without_connecting_discord(self):
        command = self.bot.tree.get_command("store")
        names = {c.name for c in command.commands}
        self.assertTrue({"buy", "products", "panel", "configure", "order", "payment", "complete"} <= names)
        payload = command.to_dict(self.bot.tree)
        self.assertEqual(payload["name"], "store")
        self.assertLessEqual(len(payload["options"]), 25)

    async def test_buyer_cannot_read_other_buyers_order(self):
        order = self.store.db.reserve(1, 10, "TEST", 1, "manual")
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=11))
        with patch.object(self.store, "staff", new=AsyncMock(return_value=False)):
            with self.assertRaises(StoreError):
                await self.store.owned_order(ctx, str(order["code"]))

    async def test_other_server_cannot_read_order_even_as_staff(self):
        order = self.store.db.reserve(1, 10, "TEST", 1, "manual")
        ctx = SimpleNamespace(guild=SimpleNamespace(id=2), author=SimpleNamespace(id=10))
        with self.assertRaises(StoreError):
            await self.store.owned_order(ctx, str(order["code"]))

    async def test_manual_order_returns_no_fake_qr(self):
        order, warning = await self.store.place_order(1, 10, "TEST", 1)
        message = payment_message(order)
        self.assertNotIn("file", message)
        self.assertNotIn("view", message)
        self.assertEqual(order["status"], "pending")
        self.assertTrue(warning)


if __name__ == "__main__":
    unittest.main()
