from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import discord

from store import panel_permissions


class PanelPermissionTests(unittest.TestCase):
    def channel(self, channel_type, **flags):
        channel = Mock(spec=channel_type)
        channel.permissions_for.return_value = SimpleNamespace(**flags)
        return channel

    def test_normal_channel_reports_missing_send_and_embed(self):
        channel = self.channel(discord.TextChannel, view_channel=True, send_messages=False, embed_links=False)
        missing = panel_permissions(channel, object())
        self.assertEqual(len(missing), 2)
        self.assertTrue(any('Embed Links' in item for item in missing))
        self.assertTrue(any('Send Messages' in item for item in missing))

    def test_allowed_channel_needs_no_admin_permission(self):
        channel = self.channel(discord.TextChannel, view_channel=True, send_messages=True, embed_links=True)
        self.assertEqual(panel_permissions(channel, object()), [])

    def test_thread_checks_send_in_threads(self):
        channel = self.channel(discord.Thread, view_channel=True, send_messages=True,
                               send_messages_in_threads=False, embed_links=True)
        self.assertEqual(panel_permissions(channel, object()),
                         ['Send Messages in Threads (Gửi tin trong luồng)'])
