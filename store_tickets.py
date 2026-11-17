"""Private, persistent order tickets backed by the existing store orders."""
import asyncio
import logging

import discord

from store_db import StoreError

log = logging.getLogger(__name__)


def ticket_overwrites(guild, buyer, staff, bot):
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        buyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                           attach_files=True),
        staff: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                           attach_files=True),
        bot: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                         manage_channels=True, manage_roles=True, embed_links=True, attach_files=True),
    }


def ticket_private(channel, buyer_id, staff_id, bot_id):
    # Include explicit member overrides, and roles granted visibility after ticket creation.
    for role in channel.guild.roles:
        if role.permissions.administrator or role.id == staff_id:
            continue
        if role.tags and role.tags.bot_id == bot_id:
            continue
        if channel.permissions_for(role).view_channel:
            return False
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role) and overwrite.view_channel is True:
            if target.id in (buyer_id, bot_id):
                continue
            if not isinstance(target, discord.Member):
                return False
            if not target.guild_permissions.administrator and not any(r.id == staff_id for r in target.roles):
                return False
    return True


class TicketControls(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    async def run(self, interaction, action):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.manager.current(interaction)
            cog = self.manager.cog
            order = await asyncio.to_thread(cog.db.order, row["order_code"], interaction.guild_id)
            if action == "close":
                if not await cog.staff(interaction.guild, interaction.user.id):
                    raise StoreError("Chỉ nhân viên hoặc admin được đóng ticket.")
                await self.manager.close(interaction.guild, interaction.channel, interaction.user.id)
                return await interaction.followup.send("Đã đóng ticket và giữ lại lịch sử trao đổi.", ephemeral=True)
            if action == "sent":
                if interaction.user.id != row["buyer_id"]:
                    raise StoreError("Chỉ khách mua đơn này được báo đã chuyển tiền.")
                if order["status"] != "pending":
                    raise StoreError("Đơn này không còn chờ thanh toán.")
                now = asyncio.get_running_loop().time()
                previous = self.manager.reminders.get(row["order_code"], -100)
                if now - previous < 60:
                    raise StoreError("Đã báo nhân viên. Vui lòng chờ ít nhất 60 giây trước khi báo lại.")
                self.manager.reminders[row["order_code"]] = now
                await interaction.channel.send(
                    "Khách báo đã chuyển tiền. Nhân viên kiểm tra giao dịch ngân hàng rồi dùng "
                    f"`/store paid code: {order.get('description') or order['code']}` nếu đúng tiền. "
                    "Thông báo này chưa xác nhận thanh toán.")
                return await interaction.followup.send("Đã báo trong ticket để nhân viên kiểm tra.", ephemeral=True)
            from store import payment_message
            await interaction.followup.send(**payment_message(order), ephemeral=True)
        except (StoreError, discord.Forbidden) as exc:
            text = str(exc) if isinstance(exc, StoreError) else "Bot thiếu quyền thao tác trong ticket."
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            log.exception("Ticket action failed")
            await interaction.followup.send("Có lỗi xử lý ticket. Dùng /store order để kiểm tra đơn.", ephemeral=True)

    @discord.ui.button(label="Xem đơn / QR", custom_id="manageandstore:ticket:refresh:v1")
    async def refresh(self, interaction, button):
        await self.run(interaction, "refresh")

    @discord.ui.button(label="Tôi đã chuyển tiền", style=discord.ButtonStyle.green,
                       custom_id="manageandstore:ticket:sent:v1")
    async def sent(self, interaction, button):
        await self.run(interaction, "sent")

    @discord.ui.button(label="Đóng ticket", style=discord.ButtonStyle.secondary,
                       custom_id="manageandstore:ticket:close:v1")
    async def close(self, interaction, button):
        await self.run(interaction, "close")


class TicketManager:
    def __init__(self, cog):
        self.cog = cog
        self.lock = asyncio.Lock()
        self.controls = TicketControls(self)
        self.reminders = {}

    async def current(self, interaction):
        if not interaction.guild:
            raise StoreError("Dùng ticket trong server.")
        row = await asyncio.to_thread(self.cog.db.ticket, interaction.guild_id, channel=interaction.channel_id)
        if not row or row["closed"]:
            raise StoreError("Ticket không tồn tại hoặc đã đóng.")
        if interaction.user.id != row["buyer_id"] and not await self.cog.staff(interaction.guild, interaction.user.id):
            raise StoreError("Bạn không có quyền thao tác ticket này.")
        await self.check_private(interaction.channel, row)
        return row

    async def check_private(self, channel, row):
        settings = await asyncio.to_thread(self.cog.db.settings, row["guild_id"])
        if not ticket_private(channel, row["buyer_id"], settings.get("staff_role_id"), self.cog.bot.user.id):
            raise StoreError("Quyền ticket đã thay đổi và không còn riêng tư. Nhờ admin kiểm tra quyền kênh.")

    async def open(self, guild, order):
        async with self.lock:
            existing = await asyncio.to_thread(self.cog.db.ticket, guild.id, code=order["code"])
            if existing:
                if existing["closed"]:
                    raise StoreError("Ticket của đơn đã đóng; bạn vẫn xem đơn bằng /store order.")
                try:
                    channel = await guild.fetch_channel(existing["channel_id"])
                except discord.NotFound:
                    await asyncio.to_thread(self.cog.db.forget_missing_ticket, guild.id, existing["channel_id"])
                else:
                    await self.check_private(channel, existing)
                    return channel
            if order["status"] in ("completed", "cancelled", "expired"):
                raise StoreError("Không mở ticket mới cho đơn đã kết thúc.")
            category_id = await asyncio.to_thread(self.cog.db.ticket_category, guild.id)
            settings = await asyncio.to_thread(self.cog.db.settings, guild.id)
            if not category_id:
                raise StoreError("Admin cần chạy /store ticketsetup trước.")
            category = await guild.fetch_channel(category_id)
            roles = await guild.fetch_roles()
            staff = discord.utils.get(roles, id=settings.get("staff_role_id"))
            if not isinstance(category, discord.CategoryChannel) or not staff or staff.is_default() or staff.managed:
                raise StoreError("Category hoặc role nhân viên không hợp lệ. Admin kiểm tra cấu hình store.")
            me = await guild.fetch_member(self.cog.bot.user.id)
            perms = category.permissions_for(me)
            if not all((perms.manage_channels, perms.manage_roles, perms.view_channel, perms.send_messages, perms.embed_links, perms.attach_files)):
                raise StoreError("Bot cần Manage Channels, Manage Roles, View Channel, Send Messages, Embed Links, Attach Files tại category.")
            buyer = await guild.fetch_member(order["buyer_id"])
            name = f"mua-{(order.get('description') or str(order['code'])).lower()}"
            channel = await guild.create_text_channel(name, category=category,
                overwrites=ticket_overwrites(guild, buyer, staff, me),
                topic=f"Store order {order['code']} | buyer {buyer.id}", reason=f"Purchase ticket {order['code']}")
            try:
                await self.check_private(channel, {"guild_id": guild.id, "buyer_id": buyer.id})
                await self.welcome(channel, buyer, order)
                await asyncio.to_thread(self.cog.db.bind_ticket, order["code"], guild.id, buyer.id, channel.id)
            except Exception:
                await channel.delete(reason="Could not register private store ticket")
                raise
            return channel

    async def welcome(self, channel, buyer, order):
        from store import payment_message
        kwargs = payment_message(order)
        kwargs["view"] = self.controls
        await channel.send(
            f"{buyer.mention} — ticket mua hàng riêng của bạn. Trao đổi và gửi bằng chứng tại đây.\n"
            "Quét QR, giữ đúng số tiền/nội dung. Nút **Tôi đã chuyển tiền** chỉ báo nhân viên kiểm tra.\n"
            f"Nhân viên: `/store paid code: {order.get('description') or order['code']}` → "
            "`/store process` → giao hàng → `/store complete`. Đóng ticket sau khi xử lý xong.",
            **kwargs, allowed_mentions=discord.AllowedMentions(users=[buyer]))

    async def close(self, guild, channel, actor):
        async with self.lock:
            row = await asyncio.to_thread(self.cog.db.ticket, guild.id, channel=channel.id)
            if not row or row["closed"]:
                raise StoreError("Ticket không tồn tại hoặc đã đóng.")
            await self.check_private(channel, row)
            order = await asyncio.to_thread(self.cog.db.order, row["order_code"], guild.id)
            if order["status"] not in ("completed", "cancelled", "expired"):
                raise StoreError("Hoàn thành hoặc hủy đơn trước khi đóng ticket; đóng ticket không hủy đơn.")
            try:
                buyer = await guild.fetch_member(row["buyer_id"])
            except discord.NotFound:
                buyer = await self.cog.bot.fetch_user(row["buyer_id"])
            overwrite = channel.overwrites_for(buyer)
            overwrite.send_messages = False
            overwrite.add_reactions = False
            overwrite.create_public_threads = False
            overwrite.create_private_threads = False
            overwrite.send_messages_in_threads = False
            await channel.set_permissions(buyer, overwrite=overwrite, reason=f"Store ticket closed by {actor}")
            await asyncio.to_thread(self.cog.db.close_ticket, guild.id, channel.id, actor)
            await channel.send("Ticket đã đóng. Lịch sử được giữ lại; đơn hàng và thanh toán không bị xóa.")
