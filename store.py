import asyncio
import io
import logging
import os
import time

import discord
from discord.ext import commands, tasks

from payments import PaymentService
from store_db import open_store, StoreError

log = logging.getLogger(__name__)
STATUS = {"pending": "Chờ thanh toán", "paid": "Đã thanh toán", "fulfilling": "Đang xử lý",
          "completed": "Hoàn thành", "cancelled": "Đã hủy", "expired": "Hết hạn", "review": "Cần đối soát"}


def money(value):
    return f"{value:,}".replace(",", ".") + " đ"


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


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
            await interaction.followup.send(content=warning or None, **payment_message(order), ephemeral=True)
        except StoreError as exc:
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
        await interaction.response.send_modal(BuyModal(self.cog))


class Store(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = open_store()
        self.payments = PaymentService(self.db)
        self.buy_cooldowns = {}
        self.front = Storefront(self)

    async def cog_load(self):
        await self.payments.start()
        self.bot.add_view(self.front)
        self.maintenance.start()
        self.notify.start()

    async def cog_unload(self):
        self.maintenance.cancel()
        self.notify.cancel()
        self.bot.remove_view(self.front)
        await self.payments.close()

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
        original = getattr(error, "original", error)
        if isinstance(original, StoreError):
            message = str(original)
        elif isinstance(error, commands.NoPrivateMessage):
            message = "Lệnh cửa hàng chỉ dùng trong server."
        elif isinstance(error, commands.UserInputError):
            message = "Thiếu hoặc sai tham số. Dùng /store help để xem hướng dẫn."
        elif isinstance(error, (commands.CommandOnCooldown, commands.MaxConcurrencyReached)):
            message = "Bạn thao tác quá nhanh. Hãy chờ vài giây."
        else:
            log.error("Store command failed", exc_info=(type(original), original, original.__traceback__))
            message = "Không xử lý được. Dùng /store orders kiểm tra đơn trước khi thử lại."
        # Errors never disclose order details in a public prefix response.
        await ctx.send(message, ephemeral=True)

    async def place_order(self, guild, buyer, sku, quantity):
        now = time.monotonic()
        key = (guild, buyer)
        if now - self.buy_cooldowns.get(key, -100) < 10:
            raise StoreError("Chờ 10 giây trước khi đặt đơn tiếp theo.")
        self.buy_cooldowns[key] = now
        if len(self.buy_cooldowns) > 10000:
            self.buy_cooldowns = {k: t for k, t in self.buy_cooldowns.items() if now - t < 60}
        order = await asyncio.to_thread(self.db.reserve, guild, buyer, sku, quantity, self.payments.mode)
        if order["provider"] == "manual":
            return order, "Đơn thanh toán thủ công: liên hệ nhân viên lấy thông tin chuyển khoản."
        try:
            order = await self.payments.ensure_payment(order)
            return order, "Quét QR hoặc mở trang payOS. Giữ nguyên số tiền và nội dung do payOS cung cấp."
        except Exception:
            log.exception("Payment creation failed for order %s; keeping reservation", order["code"])
            return order, ("Đã lưu đơn nhưng chưa lấy được QR. Dùng /store payment với mã đơn này để thử lại; "
                           "không cần đặt đơn mới. Kho vẫn được giữ để tránh bán trùng.")

    @commands.hybrid_group(name="store", description="Cửa hàng và quản lý đơn", invoke_without_command=True)
    @commands.guild_only()
    async def store(self, ctx):
        await self.private(ctx, "Dùng /store products để xem hàng, /store buy để mua, /store help để xem các lệnh.")

    @store.command(name="help", description="Hướng dẫn cửa hàng")
    async def help_command(self, ctx):
        await self.private(ctx, "**Khách:** products [page], buy <sku> [quantity], orders [page], order <code>, "
            "payment <code>, cancel <code>.\n**Nhân viên:** add <sku> <price> <stock> <name> [description], "
            "edit <sku> [price] [stock_delta] [active], allorders [page], paid <code>, "
            "process <code>, complete <code>, revenue.\n**Admin:** configure <staff_role> <log_channel>.\n"
            "Thêm /store phía trước để dùng slash, hoặc !store với lệnh tin nhắn. "
            "Với !store add, đặt tên/mô tả có khoảng trắng trong dấu nháy kép. Giá tính bằng VND.")

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
        rows = await asyncio.to_thread(self.db.products, ctx.guild.id, max(1, min(page, 10000)))
        embed = discord.Embed(title="Cửa hàng", description="Bấm Mua sản phẩm, nhập mã SKU và số lượng.", color=discord.Color.green())
        for row in rows:
            embed.add_field(name=f"{row['sku']} · {safe(row['name'])}"[:256],
                            value=f"{money(row['price'])} · Còn {row['stock']}", inline=False)
        embed.set_footer(text="Giá/tồn kho trên bảng là lúc đăng. Bot kiểm tra lại khi đặt hàng.")
        await ctx.channel.send(embed=embed, view=Storefront(self))
        await self.private(ctx, "Đã đăng bảng bán hàng công khai.")

    @store.command(name="buy", description="Đặt sản phẩm và nhận thanh toán riêng tư")
    async def buy(self, ctx, sku: str, quantity: int = 1):
        if not ctx.interaction:
            await self.private(ctx, "Đang tạo đơn hàng của bạn…")  # Verify DM delivery before reserving stock.
        order, message = await self.place_order(ctx.guild.id, ctx.author.id, sku, quantity)
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
                         f"{money(r['total'])} · {STATUS[r['status']]}" for r in rows) or "Chưa có đơn."

    @store.command(name="order", description="Xem và đối soát trạng thái đơn")
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def order(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        warning = None
        if order["provider"] == "payos" and order["status"] in ("pending", "review"):
            try:
                order = await self.payments.sync(order)
            except Exception:
                log.exception("Manual order reconciliation failed for %s", order["code"])
                warning = "Chưa kết nối được payOS; dưới đây là trạng thái đang lưu."
        await self.private(ctx, warning, **payment_message(order))

    @store.command(name="payment", description="Lấy lại QR/link của cùng một đơn")
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def payment(self, ctx, code: str):
        order = await self.owned_order(ctx, code)
        if order["provider"] != "payos":
            return await self.private(ctx, "Đơn manual: liên hệ nhân viên để nhận thông tin chuyển khoản.")
        try:
            order = await self.payments.sync(order)
        except Exception:
            # A previous create may not have reached payOS; retry uses the same unique order code.
            pass
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
                        log.warning("Could not reconcile order %s; retaining stock", row["code"])
        except Exception:
            log.exception("Store maintenance failed")

    @maintenance.before_loop
    async def maintenance_ready(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def notify(self):
        try:
            notices = await asyncio.to_thread(self.db.notifications)
            for row in notices:
                # The event keeps its state even if the order has since advanced.
                embed = discord.Embed(title=f"Đơn #{row['code']}: {STATUS[row['notice_status']]}",
                    description=f"{safe(row['name'])} ×{row['quantity']} · {money(row['total'])}")
                ok = True
                try:
                    user = self.bot.get_user(row["buyer_id"]) or await self.bot.fetch_user(row["buyer_id"])
                    await user.send(embed=embed)
                except discord.Forbidden:
                    pass  # User can still inspect persistent state via /store orders.
                except discord.HTTPException:
                    ok = False
                settings = await asyncio.to_thread(self.db.settings, row["guild_id"])
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
                        ok = False
                await asyncio.to_thread(self.db.notification_done, row["notice_id"], ok)
        except Exception:
            log.exception("Store notifications failed")

    @notify.before_loop
    async def notify_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Store(bot))
