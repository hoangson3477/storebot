import asyncio
import io
import logging
import os
import time

import discord
from discord.ext import commands, tasks

from payments import PaymentService
from store_db import open_store, StoreError, DatabaseUnavailable
from network_health import BackgroundBackoff, temporary_dns_error
from manual_payment import BankDetails
from store_tickets import TicketManager
from diagnostics import begin_command, interaction_context, unwrap_error

log = logging.getLogger(__name__)
STATUS = {"pending": "Chờ thanh toán", "paid": "Đã thanh toán", "fulfilling": "Đang xử lý",
          "completed": "Hoàn thành", "cancelled": "Đã hủy", "expired": "Hết hạn", "review": "Cần đối soát"}


def money(value):
    return f"{value:,}".replace(",", ".") + " đ"


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def panel_permissions(channel, member):
    perms = channel.permissions_for(member)
    required = [("view_channel", "View Channel (Xem kênh)"),
                ("embed_links", "Embed Links (Nhúng liên kết)")]
    if isinstance(channel, discord.Thread):
        required.append(("send_messages_in_threads", "Send Messages in Threads (Gửi tin trong luồng)"))
    else:
        required.append(("send_messages", "Send Messages (Gửi tin nhắn)"))
    return [label for flag, label in required if not getattr(perms, flag)]


def private_log(channel, staff_role_id, bot_id):
    """Check role and member overrides, not just @everyone visibility."""
    for role in channel.guild.roles:
        if role.permissions.administrator or role.id == staff_role_id:
            continue
        if role.tags and role.tags.bot_id == bot_id:
            continue
        if channel.permissions_for(role).view_channel:
            return False
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role) and overwrite.view_channel is True:
            if target.id == bot_id:
                continue
            if not isinstance(target, discord.Member):
                return False
            if not target.guild_permissions.administrator and not any(r.id == staff_role_id for r in target.roles):
                return False
    return True


def order_embed(order):
    embed = discord.Embed(title=f"Đơn #{order['code']}", color=discord.Color.blue())
    embed.add_field(name="Sản phẩm", value=safe(order["name"]), inline=False)
    embed.add_field(name="Số lượng", value=str(order["quantity"]))
    embed.add_field(name="Tổng tiền", value=money(order["total"]))
    embed.add_field(name="Trạng thái", value=STATUS[order["status"]], inline=False)
    if order["status"] == "pending":
        embed.add_field(name="Hạn thanh toán", value=f"<t:{order['expires_at']}:R>")
    if order.get("description"):
        embed.add_field(name="Nội dung chuyển khoản", value=safe(order["description"]), inline=False)
    if order.get("account_number"):
        embed.add_field(name="Người nhận", value=safe(order["account_name"] + " — " + order["account_number"]), inline=False)
    if order.get("review_reason"):
        embed.add_field(name="Cần nhân viên kiểm tra", value=safe(order["review_reason"]), inline=False)
    embed.set_footer(text="Chỉ giao hàng khi đơn đã thanh toán. Dùng /store order để xem trạng thái mới nhất.")
    return embed


def payment_message(order):
    kwargs = {"embed": order_embed(order)}
    if order["provider"] == "manual":
        if order["status"] == "pending" and order.get("checkout_url"):
            kwargs["embed"].set_image(url=order["checkout_url"])
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Mở QR chuyển khoản", url=order["checkout_url"]))
            kwargs["view"] = view
        return kwargs
    if order["status"] == "pending" and order.get("checkout_url"):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Mở trang thanh toán", url=order["checkout_url"]))
        kwargs["view"] = view
        if order.get("qr_code"):
            import qrcode

            image = qrcode.make(order["qr_code"])
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            stream.seek(0)
            kwargs["file"] = discord.File(stream, filename="payment-qr.png")
            kwargs["embed"].set_image(url="attachment://payment-qr.png")
    return kwargs


class BuyModal(discord.ui.Modal, title="Đặt hàng"):
    sku = discord.ui.TextInput(label="Mã SKU", max_length=32)
    quantity = discord.ui.TextInput(label="Số lượng", default="1", max_length=3)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        interaction_context(interaction)
        log.info("purchase.modal_submitted")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.guild_id:
                raise StoreError("Hãy đặt hàng trong server.")
            try:
                quantity = int(self.quantity.value)
            except ValueError as exc:
                raise StoreError("Số lượng phải là số nguyên.") from exc
            order, warning = await self.cog.place_order(interaction.guild_id, interaction.user.id,
                                                       self.sku.value, quantity)
            warning += await self.cog.ticket_delivery(interaction.guild, order)
            await interaction.followup.send(content=warning or None, **payment_message(order), ephemeral=True)
        except StoreError as exc:
            log.warning("purchase.rejected reason=%s", exc)
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            log.exception("Buy modal failed")
            await interaction.followup.send("Có lỗi. Dùng /store orders kiểm tra trước khi đặt lại.", ephemeral=True)


class Storefront(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Mua sản phẩm", style=discord.ButtonStyle.green, custom_id="manageandstore:buy:v1")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        interaction_context(interaction)
        log.info("purchase.panel_clicked")
        await interaction.response.send_modal(BuyModal(self.cog))


class Store(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = open_store()
        self.db.init_tickets()
        self.payments = PaymentService(self.db)
        self.buy_cooldowns = {}
        self.maintenance_backoff = BackgroundBackoff(30)
        self.notify_backoff = BackgroundBackoff(15)
        self.front = Storefront(self)
        self.tickets = TicketManager(self)
        log.info("store.initialized backend=%s payment_provider=%s", type(self.db).__name__, self.payments.mode)

    async def cog_load(self):
        await self.payments.start()
        self.bot.add_view(self.front)
        self.bot.add_view(self.tickets.controls)
        self.maintenance.start()
        self.notify.start()
        log.info("store.loaded persistent_views=2 maintenance_interval=30 notification_interval=15")

    async def cog_unload(self):
        self.maintenance.cancel()
        self.notify.cancel()
        self.bot.remove_view(self.front)
        self.bot.remove_view(self.tickets.controls)
        await self.payments.close()
        log.info("store.unloaded")

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Lệnh cửa hàng chỉ dùng trong server.")
        return True

    async def staff(self, guild, user_id):
        member = await guild.fetch_member(user_id)
        if member.guild_permissions.administrator:
            return True
        settings = await asyncio.to_thread(self.db.settings, guild.id)
        return any(role.id == settings.get("staff_role_id") for role in member.roles)

    async def require_staff(self, ctx):
        if not await self.staff(ctx.guild, ctx.author.id):
            raise StoreError("Bạn cần role nhân viên cửa hàng hoặc Administrator.")

    async def require_admin(self, ctx):
        member = await ctx.guild.fetch_member(ctx.author.id)
        if not member.guild_permissions.administrator:
            raise StoreError("Chỉ Administrator được cấu hình cửa hàng.")

    async def owned_order(self, ctx, code):
        if len(code.strip()) == 6 and code.strip().isascii() and code.strip().isalnum():
            order = await asyncio.to_thread(self.db.transfer_order, code.strip(), ctx.guild.id)
            if order["buyer_id"] != ctx.author.id and not await self.staff(ctx.guild, ctx.author.id):
                raise StoreError("Bạn không có quyền xem đơn này.")
            return order
        try:
            number = int(code)
        except ValueError as exc:
            raise StoreError("Mã đơn phải là số.") from exc
        if not 1_000_000_000_000 <= number < 9_000_000_000_000:
            raise StoreError("Mã đơn không hợp lệ.")
        order = await asyncio.to_thread(self.db.order, number, ctx.guild.id)
        if order["buyer_id"] != ctx.author.id and not await self.staff(ctx.guild, ctx.author.id):
            raise StoreError("Bạn không có quyền xem đơn này.")
        return order

    async def private(self, ctx, content=None, **kwargs):
        if ctx.interaction:
            return await ctx.send(content, **kwargs, ephemeral=True)
        try:
            return await ctx.author.send(content, **kwargs)
        except discord.Forbidden as exc:
            raise StoreError("Hãy bật DM từ server hoặc dùng slash command để nhận thông tin riêng.") from exc

    async def cog_before_invoke(self, ctx):
        if ctx.interaction:
            await ctx.defer(ephemeral=True)

    async def cog_command_error(self, ctx, error):
        begin_command(ctx)
        original = unwrap_error(error)
        if isinstance(original, StoreError):
            message = str(original)
            log.warning("command.rejected command=%s reason=%s", ctx.command, original,
                        exc_info=(type(original), original, original.__traceback__) if original.__cause__ else None)
        elif isinstance(original, discord.Forbidden):
            message = ("Bot thiếu quyền Discord tại kênh đang dùng. Kiểm tra View Channel, "
                       "Send Messages và Embed Links trong quyền kênh/category; "
                       "nếu dùng luồng, cần Send Messages in Threads. Quyền ghi đè của kênh cũng phải cho phép bot.")
            log.warning("Store command %s denied by Discord in channel %s (code=%s)",
                        ctx.command, ctx.channel.id, original.code,
                        exc_info=(type(original), original, original.__traceback__))
        elif isinstance(error, commands.NoPrivateMessage):
            log.warning("command.rejected command=%s reason=guild_only", ctx.command)
            message = "Lệnh cửa hàng chỉ dùng trong server."
        elif isinstance(error, commands.UserInputError):
            log.warning("command.rejected command=%s error_type=%s", ctx.command, type(error).__name__)
            message = "Thiếu hoặc sai tham số. Dùng /store help để xem hướng dẫn."
        elif isinstance(error, (commands.CommandOnCooldown, commands.MaxConcurrencyReached)):
            log.warning("command.throttled command=%s error_type=%s", ctx.command, type(error).__name__)
            message = "Bạn thao tác quá nhanh. Hãy chờ vài giây."
        else:
            log.error("command.failed command=%s error_type=%s", ctx.command, type(original).__name__,
                      exc_info=(type(original), original, original.__traceback__))
            message = f"Không xử lý được. Mã tra log: {ctx.log_reference}. Dùng /store orders kiểm tra đơn trước khi thử lại."
        # Errors never disclose order details in a public prefix response.
        await ctx.send(message, ephemeral=True)

    async def place_order(self, guild, buyer, sku, quantity):
        log.info("purchase.started guild=%s buyer=%s quantity=%s provider=%s", guild, buyer, quantity, self.payments.mode)
        now = time.monotonic()
        key = (guild, buyer)
        if now - self.buy_cooldowns.get(key, -100) < 10:
            raise StoreError("Chờ 10 giây trước khi đặt đơn tiếp theo.")
        self.buy_cooldowns[key] = now
        if len(self.buy_cooldowns) > 10000:
            self.buy_cooldowns = {k: t for k, t in self.buy_cooldowns.items() if now - t < 60}
        bank, ttl = None, 900
        if self.payments.mode == "manual":
            try:
                bank = BankDetails.from_env()
            except ValueError as exc:
                raise StoreError(str(exc)) from exc
            ttl = 86400
        order = await asyncio.to_thread(self.db.reserve, guild, buyer, sku, quantity, self.payments.mode, ttl, bank)
        log.info("purchase.reserved order=%s guild=%s buyer=%s total=%s expires_at=%s", order["code"], guild,
                 buyer, order["total"], order["expires_at"])
        if order["provider"] == "manual":
            log.info("payment.manual_qr_ready order=%s status=pending", order["code"])
            return order, (f"Chuyển đúng {money(order['total'])}, nội dung chính xác **{order['description']}**. "
                           "Người bán kiểm tra tiền về và xác nhận đơn. Đã chuyển thì không hủy hoặc chuyển lần nữa.")
        try:
            order = await self.payments.ensure_payment(order)
            return order, "Quét QR hoặc mở trang payOS. Giữ nguyên số tiền và nội dung do payOS cung cấp."
        except Exception:
            log.exception("Payment creation failed for order %s; keeping reservation", order["code"])
            return order, ("Đã lưu đơn nhưng chưa lấy được QR. Dùng /store payment với mã đơn này để thử lại; "
                           "không cần đặt đơn mới. Kho vẫn được giữ để tránh bán trùng.")

    async def ticket_delivery(self, guild, order):
        if not await asyncio.to_thread(self.db.ticket_category, guild.id):
            log.debug("ticket.skipped order=%s reason=not_configured", order["code"])
            return ""
        try:
            channel = await self.tickets.open(guild, order)
            return f"\nTicket mua hàng: {channel.mention}"
        except Exception:
            log.exception("Could not open ticket for order %s", order["code"])
            return ("\nĐơn đã lưu nhưng chưa mở được ticket. Bạn vẫn dùng QR bên dưới; "
                    f"nhờ admin kiểm tra quyền rồi dùng `/store ticket code: {order['code']}`. Không cần đặt lại.")

    @commands.hybrid_group(name="store", description="Cửa hàng và quản lý đơn", invoke_without_command=True)
    @commands.guild_only()
    async def store(self, ctx):
        await self.private(ctx, "Dùng /store products để xem hàng, /store buy để mua, /store help để xem các lệnh.")

    @store.command(name="help", description="Hướng dẫn cửa hàng")
    async def help_command(self, ctx):
        from help_content import build_help
        await self.private(ctx, embed=build_help("store"))

    @store.command(name="configure", description="Chọn role nhân viên và kênh log riêng tư")
    async def configure(self, ctx, staff_role: discord.Role, log_channel: discord.TextChannel):
        await self.require_admin(ctx)
        if staff_role.is_default() or staff_role.managed:
            raise StoreError("Chọn role nhân viên riêng, không dùng everyone hoặc role của bot.")
        # Refuse a public log channel, including access granted via another ordinary role.
        if not private_log(log_channel, staff_role.id, self.bot.user.id):
            raise StoreError("Kênh log phải riêng tư: tắt View Channel cho everyone và các role ngoài nhân viên.")
        me = ctx.guild.me
        perms = log_channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise StoreError("Bot cần xem kênh, gửi tin và Embed Links trong kênh log.")
        await asyncio.to_thread(self.db.configure, ctx.guild.id, ctx.author.id, staff_role.id, log_channel.id)
        await self.private(ctx, "Đã lưu role nhân viên và kênh log cửa hàng.")

    @store.command(name="add", description="Thêm sản phẩm; giá nguyên VND")
    async def add(self, ctx, sku: str, price: int, stock: int, name: str, description: str = ""):
        await self.require_staff(ctx)
        await asyncio.to_thread(self.db.add_product, ctx.guild.id, ctx.author.id, sku, name, description, price, stock)
        await self.private(ctx, f"Đã thêm SKU {safe(sku.upper())}.")

    @store.command(name="edit", description="Đổi giá, tăng/giảm tồn kho, bật/tắt sản phẩm")
    async def edit(self, ctx, sku: str, price: int | None = None, stock_delta: int = 0, active: bool | None = None):
        await self.require_staff(ctx)
        await asyncio.to_thread(self.db.edit_product, ctx.guild.id, ctx.author.id, sku, price, stock_delta, active)
        await self.private(ctx, "Đã cập nhật sản phẩm. Đơn đã tạo giữ nguyên giá.")

    @store.command(name="products", description="Xem catalog và đặt hàng")
    async def products(self, ctx, page: int = 1):
        if not 1 <= page <= 10000:
            raise StoreError("Trang từ 1 đến 10000.")
        rows = await asyncio.to_thread(self.db.products, ctx.guild.id, page)
        embed = discord.Embed(title=f"Cửa hàng — trang {page}", color=discord.Color.green())
        for row in rows:
            embed.add_field(name=f"{row['sku']} · {safe(row['name'])}"[:256],
                value=f"{money(row['price'])} · Còn {row['stock']}\n{safe(row['description'][:180])}", inline=False)
        if not rows:
            embed.description = "Chưa có sản phẩm ở trang này."
        await ctx.send(embed=embed, view=Storefront(self), ephemeral=True)

    @store.command(name="panel", description="Đăng catalog có nút mua công khai tại kênh hiện tại")
    async def panel(self, ctx, page: int = 1):
        await self.require_staff(ctx)
        me = ctx.guild.me or await ctx.guild.fetch_member(self.bot.user.id)
        missing = panel_permissions(ctx.channel, me)
        if missing:
            log.warning("panel.permissions_missing channel=%s permissions=%s", ctx.channel.id, missing)
            raise StoreError("Chưa đăng được panel. Bot thiếu: " + ", ".join(missing) +
                             ". Vào Chỉnh sửa kênh → Quyền → chọn bot và bật các quyền trên, rồi chạy lại /store panel.")
        rows = await asyncio.to_thread(self.db.products, ctx.guild.id, max(1, min(page, 10000)))
        ticket_enabled = await asyncio.to_thread(self.db.ticket_category, ctx.guild.id)
        description = "Bấm **Mua sản phẩm**, nhập mã SKU và số lượng để đặt hàng."
        if ticket_enabled:
            description += " Bot mở **ticket mua hàng riêng** kèm QR để bạn trao đổi với nhân viên."
        else:
            description += " Bot gửi thông tin thanh toán riêng cho bạn."
        embed = discord.Embed(title=f"Cửa hàng — trang {page}", description=description, color=discord.Color.green())
        for row in rows:
            embed.add_field(name=f"{row['sku']} · {safe(row['name'])}"[:256],
                            value=f"{money(row['price'])} · Còn {row['stock']}", inline=False)
        embed.set_footer(text="Giá/tồn kho trên bảng là lúc đăng. Bot kiểm tra lại khi đặt hàng.")
        try:
            log.info("panel.publishing channel=%s page=%s products=%s ticket_enabled=%s", ctx.channel.id,
                     page, len(rows), bool(ticket_enabled))
            await ctx.channel.send(embed=embed, view=Storefront(self))
        except discord.Forbidden as exc:
            raise StoreError("Discord từ chối đăng panel (50013). Kiểm tra quyền ghi đè của bot tại kênh/category: "
                             "View Channel, Send Messages, Embed Links; nếu là luồng, kiểm tra quyền gửi và trạng thái khóa luồng.") from exc
        log.info("panel.published channel=%s products=%s", ctx.channel.id, len(rows))
        await self.private(ctx, "Đã đăng bảng bán hàng công khai.")

    @store.command(name="ticketsetup", description="Admin chọn category tạo ticket mua hàng riêng")
    async def ticketsetup(self, ctx, category: discord.CategoryChannel):
        await self.require_admin(ctx)
        settings = await asyncio.to_thread(self.db.settings, ctx.guild.id)
        role = ctx.guild.get_role(settings.get("staff_role_id"))
        if not role or role.is_default() or role.managed:
            raise StoreError("Chạy /store configure để chọn role nhân viên trước.")
        perms = category.permissions_for(ctx.guild.me)
        if not all((perms.manage_channels, perms.manage_roles, perms.view_channel, perms.send_messages, perms.embed_links, perms.attach_files)):
            raise StoreError("Bot cần Manage Channels, Manage Roles, View Channel, Send Messages, Embed Links, Attach Files tại category.")
        await asyncio.to_thread(self.db.configure_tickets, ctx.guild.id, ctx.author.id, category.id)
        await self.private(ctx, "Đã bật ticket mua hàng. Khách mua từ panel hoặc /store buy sẽ có ticket riêng kèm QR.")

    @store.command(name="ticket", description="Mở hoặc lấy lại ticket cho đơn hàng có sẵn")
    async def ticket(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        channel = await self.tickets.open(ctx.guild, order)
        await self.private(ctx, f"Ticket của đơn: {channel.mention}")

    @store.command(name="ticketclose", description="Nhân viên đóng ticket hiện tại sau khi xử lý xong đơn")
    async def ticketclose(self, ctx):
        await self.require_staff(ctx)
        await self.tickets.close(ctx.guild, ctx.channel, ctx.author.id)
        await self.private(ctx, "Đã đóng ticket, giữ lại lịch sử và thông tin đơn.")

    @store.command(name="buy", description="Đặt sản phẩm và nhận thanh toán riêng tư")
    async def buy(self, ctx, sku: str, quantity: int = 1):
        if not ctx.interaction:
            await self.private(ctx, "Đang tạo đơn hàng của bạn…")  # Verify DM delivery before reserving stock.
        order, message = await self.place_order(ctx.guild.id, ctx.author.id, sku, quantity)
        message += await self.ticket_delivery(ctx.guild, order)
        await self.private(ctx, message, **payment_message(order))

    @store.command(name="orders", description="Xem các đơn của bạn")
    async def orders(self, ctx, page: int = 1):
        rows = await asyncio.to_thread(self.db.list_orders, ctx.guild.id, ctx.author.id, max(1, min(page, 10000)))
        await self.private(ctx, self.order_lines(rows))

    @store.command(name="allorders", description="Nhân viên xem tất cả đơn trong server")
    async def allorders(self, ctx, page: int = 1):
        await self.require_staff(ctx)
        rows = await asyncio.to_thread(self.db.list_orders, ctx.guild.id, None, max(1, min(page, 10000)))
        await self.private(ctx, self.order_lines(rows))

    @staticmethod
    def order_lines(rows):
        return "\n".join(f"`{r['code']}` · {safe(r['name'][:40])} ×{r['quantity']} · "
                         f"{money(r['total'])} · {STATUS[r['status']]} | CK: {safe(r.get('description') or '-')}" for r in rows) or "Chưa có đơn."

    @store.command(name="order", description="Xem và đối soát trạng thái đơn")
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def order(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        warning = None
        if order["provider"] == "payos" and order["status"] in ("pending", "review"):
            try:
                order = await self.payments.sync(order)
            except Exception:
                log.exception("payment.order_reconcile_failed order=%s", order["code"])
                warning = "Chưa kết nối được payOS; dưới đây là trạng thái đang lưu."
        await self.private(ctx, warning, **payment_message(order))

    @store.command(name="payment", description="Lấy lại QR/link của cùng một đơn")
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def payment(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        if order["provider"] != "payos":
            return await self.private(ctx, "Người bán xác nhận sau khi kiểm tra tiền về.", **payment_message(order))
        try:
            order = await self.payments.sync(order)
        except Exception:
            # A previous create may not have reached payOS; retry uses the same unique order code.
            log.exception("payment.sync_failed_before_retry order=%s", order["code"])
        if order["status"] == "pending":
            order = await self.payments.ensure_payment(order)
        await self.private(ctx, **payment_message(order))

    @store.command(name="cancel", description="Hủy đơn chưa thanh toán và trả kho khi xác minh an toàn")
    async def cancel(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        if order["provider"] == "manual":
            await asyncio.to_thread(self.db.manual_cancel, order["code"], ctx.guild.id, ctx.author.id)
        else:
            await self.payments.cancel(order)
        order = await asyncio.to_thread(self.db.order, order["code"])
        await self.private(ctx, embed=order_embed(order))

    @store.command(name="paid", description="Nhân viên xác nhận đã nhận tiền cho đơn manual")
    async def paid(self, ctx, code: str):
        await self.require_staff(ctx)
        order = await self.owned_order(ctx, code)
        await asyncio.to_thread(self.db.manual_paid, order["code"], ctx.guild.id, ctx.author.id)
        await self.private(ctx, "Đã xác nhận thanh toán thủ công, có ghi audit người thực hiện.")

    @store.command(name="process", description="Nhận xử lý đơn đã thanh toán")
    async def process(self, ctx, code: str):
        await self.require_staff(ctx)
        order = await self.owned_order(ctx, code)
        await asyncio.to_thread(self.db.advance, order["code"], ctx.guild.id, ctx.author.id, "fulfilling")
        await self.private(ctx, "Đã chuyển sang Đang xử lý.")

    @store.command(name="complete", description="Xác nhận đã giao hàng/xong dịch vụ")
    async def complete(self, ctx, code: str):
        await self.require_staff(ctx)
        order = await self.owned_order(ctx, code)
        await asyncio.to_thread(self.db.advance, order["code"], ctx.guild.id, ctx.author.id, "completed")
        await self.private(ctx, "Đã hoàn thành đơn.")

    @store.command(name="revenue", description="Tổng tiền nhận và đơn hoàn thành")
    async def revenue(self, ctx):
        await self.require_staff(ctx)
        data = await asyncio.to_thread(self.db.revenue, ctx.guild.id)
        await self.private(ctx, f"Đơn đã thanh toán: {data['orders']}\nTổng tiền ghi nhận: {money(data['collected'])}\n"
                           f"Giá trị đơn hoàn thành: {money(data['completed'])}\nChưa trừ phí, chi phí hoặc hoàn tiền.")

    @store.command(name="resolve", description="Admin đóng đơn lỗi quá hạn sau khi xác nhận không nhận tiền")
    async def resolve(self, ctx, code: str, *, note: str):
        await self.require_admin(ctx)
        order = await self.owned_order(ctx, code)
        await asyncio.to_thread(self.db.resolve_unpaid, order["code"], ctx.guild.id, ctx.author.id, note)
        await self.private(ctx, "Đã đóng đơn và trả kho, có ghi audit. Nếu tiền đến muộn, bot sẽ đánh dấu cần đối soát.")

    @tasks.loop(seconds=30)
    async def maintenance(self):
        try:
            rows = await asyncio.to_thread(self.db.maintenance)
            if self.payments.client:
                for row in rows:
                    try:
                        await self.payments.sync(row)
                    except Exception:
                        log.exception("maintenance.reconcile_failed order=%s stock_retained=true", row["code"])
            if self.maintenance_backoff.recovered():
                self.maintenance.change_interval(seconds=30)
                log.info("maintenance.recovered interval_seconds=30")
        except Exception as exc:
            if isinstance(exc, DatabaseUnavailable) or temporary_dns_error(exc):
                delay = self.maintenance_backoff.failed()
                self.maintenance.change_interval(seconds=delay)
                log.warning("maintenance.dns_backoff failures=%s next_run_seconds=%s", self.maintenance_backoff.failures, delay)
            log.exception("Store maintenance failed")

    @maintenance.before_loop
    async def maintenance_ready(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def notify(self):
        if not self.bot.is_ready():
            log.debug("notification.skipped reason=gateway_not_ready")
            return
        try:
            notices = await asyncio.to_thread(self.db.notifications)
            for row in notices:
                # The event keeps its state even if the order has since advanced.
                embed = discord.Embed(title=f"Đơn #{row['code']}: {STATUS[row['notice_status']]}",
                    description=f"{safe(row['name'])} ×{row['quantity']} · {money(row['total'])}")
                if row.get("description"):
                    embed.add_field(name="Mã chuyển khoản", value=safe(row["description"]))
                ok = True
                try:
                    user = self.bot.get_user(row["buyer_id"]) or await self.bot.fetch_user(row["buyer_id"])
                    await user.send(embed=embed)
                except discord.Forbidden:
                    log.warning("notification.dm_blocked order=%s buyer=%s", row["code"], row["buyer_id"])
                    pass  # User can still inspect persistent state via /store orders.
                except discord.HTTPException:
                    log.exception("notification.dm_failed order=%s buyer=%s", row["code"], row["buyer_id"])
                    ok = False
                settings = await asyncio.to_thread(self.db.settings, row["guild_id"])
                ticket = await asyncio.to_thread(self.db.ticket, row["guild_id"], code=row["code"])
                if ticket and not ticket["closed"]:
                    try:
                        channel = self.bot.get_channel(ticket["channel_id"]) or await self.bot.fetch_channel(ticket["channel_id"])
                        if channel.guild.id == row["guild_id"]:
                            await self.tickets.check_private(channel, ticket)
                            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    except discord.NotFound:
                        log.warning("notification.ticket_missing order=%s channel=%s", row["code"], ticket["channel_id"])
                        pass  # An externally deleted ticket does not block other notices.
                    except StoreError:
                        log.warning("Skipping non-private ticket %s", ticket["channel_id"])
                    except discord.HTTPException:
                        log.exception("notification.ticket_failed order=%s channel=%s", row["code"], ticket["channel_id"])
                        ok = False
                if settings.get("log_channel_id"):
                    try:
                        channel = self.bot.get_channel(settings["log_channel_id"]) or await self.bot.fetch_channel(settings["log_channel_id"])
                        # Recheck privacy: permissions may have changed since configuration.
                        guild = channel.guild
                        if channel.guild.id == row["guild_id"] and private_log(channel, settings.get("staff_role_id"), self.bot.user.id):
                            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                        else:
                            log.warning("Skipping non-private store log channel for guild %s", row["guild_id"])
                    except discord.HTTPException:
                        log.exception("notification.staff_log_failed order=%s channel=%s", row["code"], settings["log_channel_id"])
                        ok = False
                await asyncio.to_thread(self.db.notification_done, row["notice_id"], ok)
                log.debug("notification.finished order=%s event=%s ok=%s attempt=%s", row["code"],
                          row["notice_status"], ok, row["attempts"] + 1)
            if self.notify_backoff.recovered():
                self.notify.change_interval(seconds=15)
                log.info("notification.recovered interval_seconds=15")
        except Exception as exc:
            if isinstance(exc, DatabaseUnavailable) or temporary_dns_error(exc):
                delay = self.notify_backoff.failed()
                self.notify.change_interval(seconds=delay)
                log.warning("notification.dns_backoff failures=%s next_run_seconds=%s", self.notify_backoff.failures, delay)
            log.exception("Store notifications failed")

    @notify.before_loop
    async def notify_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Store(bot))
