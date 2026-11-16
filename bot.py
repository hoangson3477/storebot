import asyncio
import io
import json
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from plan import Plan, SYSTEM_PROMPT

load_dotenv()
log = logging.getLogger(__name__)
locks: dict[int, asyncio.Lock] = {}


async def propose(description: str) -> Plan:
    key, model = os.getenv("AI_API_KEY"), os.getenv("AI_MODEL")
    if not key or not model:
        raise ValueError("Chưa cấu hình AI_API_KEY và AI_MODEL trong .env.")
    base = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("AI_BASE_URL phải dùng HTTPS.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": description},
            ], "response_format": {"type": "json_object"}, "max_tokens": 4000},
        ) as response:
            if response.status != 200:
                raise ValueError(f"AI trả lỗi HTTP {response.status}. Kiểm tra API key, model và hạn mức.")
            body = await response.json()
    try:
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Truncated")
        return Plan.model_validate_json(choice["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("AI trả cấu trúc không hợp lệ. Hãy chạy setup lại với mô tả rõ hơn.") from exc


def preview(plan: Plan) -> str:
    lines = [plan.summary, "", f"Role nhân viên mới: {plan.staff_role}",
             "Role mới không có quyền quản trị. Chủ server tự gán role cho nhân viên."]
    for category in plan.categories:
        lines.append(f"\n[{category.name}]")
        for ch in category.channels:
            lines.append(f"  {ch.kind}: {ch.name} ({ch.access}) — {ch.topic}")
    return "\n".join(lines)


class Confirm(discord.ui.View):
    def __init__(self, ctx, plan):
        super().__init__(timeout=180)
        self.ctx, self.plan, self.used = ctx, plan, False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Chỉ người yêu cầu được xác nhận.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Xác nhận tạo", style=discord.ButtonStyle.green)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            return await interaction.response.send_message("Bản này đã được xử lý.", ephemeral=True)
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        guild = interaction.guild
        member = await guild.fetch_member(interaction.user.id)
        if not member.guild_permissions.administrator:
            return await interaction.followup.send("Bạn cần quyền Administrator.", ephemeral=True)
        lock = locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            return await interaction.followup.send("Server đang có setup khác chạy.", ephemeral=True)
        async with lock:
            created = []
            try:
                me = await guild.fetch_member(self.ctx.bot.user.id)
                if not (me.guild_permissions.manage_channels and me.guild_permissions.manage_roles):
                    raise ValueError("Bot cần Manage Channels và Manage Roles.")
                channels = await guild.fetch_channels()
                roles = await guild.fetch_roles()
                total = len(self.plan.categories) + sum(len(c.channels) for c in self.plan.categories)
                if len(channels) + total > 500 or len(roles) >= 250:
                    raise ValueError("Server không còn đủ chỗ cho cấu trúc mới.")
                # Avoid silent reuse of unrelated channels or staff roles.
                if any(r.name.casefold() == self.plan.staff_role.casefold() for r in roles):
                    raise ValueError("Role cùng tên đã tồn tại. Hãy yêu cầu AI dùng tên khác.")
                existing = {c.name.casefold() for c in channels if isinstance(c, discord.CategoryChannel)}
                if any(c.name.casefold() in existing for c in self.plan.categories):
                    raise ValueError("Category cùng tên đã tồn tại. Hãy yêu cầu cấu trúc khác để tránh trùng.")
                reason = f"AI setup confirmed by {member.id}"
                staff = await guild.create_role(name=self.plan.staff_role, permissions=discord.Permissions.none(), reason=reason)
                created.append(staff)
                for cat in self.plan.categories:
                    category = await guild.create_category(cat.name, overwrites={
                        me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
                    }, reason=reason)
                    created.append(category)
                    for ch in cat.channels:
                        private = ch.access == "staff"
                        readonly = ch.access == "readonly"
                        overwrites = {
                            guild.default_role: discord.PermissionOverwrite(
                                view_channel=not private,
                                send_messages=False if private or readonly else None,
                                add_reactions=False if private or readonly else None,
                                create_public_threads=False if private or readonly else None,
                                create_private_threads=False if private or readonly else None,
                                send_messages_in_threads=False if private or readonly else None,
                                connect=False if private or readonly else None,
                            ),
                            staff: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                read_message_history=True, connect=True, speak=True),
                            me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                read_message_history=True, manage_channels=True, connect=True),
                        }
                        if ch.kind == "text":
                            new = await guild.create_text_channel(ch.name, category=category,
                                topic=ch.topic, overwrites=overwrites, reason=reason)
                        else:
                            new = await guild.create_voice_channel(ch.name, category=category,
                                overwrites=overwrites, reason=reason)
                        created.append(new)
                await interaction.followup.send(f"Đã tạo {len(created)} tài nguyên. Bạn có thể gán role {staff.mention} cho nhân viên.",
                                                allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                log.exception("Setup failed; rolling back newly created resources")
                remaining = []
                for resource in reversed(created):
                    try:
                        await resource.delete(reason="Rollback incomplete AI setup")
                    except discord.HTTPException:
                        remaining.append(str(resource.id))
                text = "Setup không thành công. Kiểm tra quyền bot, tên trùng và giới hạn server."
                text += (" Chưa dọn được các ID: " + ", ".join(remaining)) if remaining else " Đã dọn tài nguyên vừa tạo."
                await interaction.followup.send(text, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Đã hủy setup.", view=None)


class Bot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("store")
        test = os.getenv("TEST_GUILD_ID")
        if test:
            guild = discord.Object(id=int(test))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        if "store" in self.extensions:
            await self.unload_extension("store")
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = Bot(command_prefix="!", intents=intents, allowed_mentions=discord.AllowedMentions.none())


@bot.hybrid_command(name="setup", description="AI đề xuất cấu trúc server theo yêu cầu")
@app_commands.describe(describe="Mô tả server bạn muốn xây dựng")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
@commands.bot_has_guild_permissions(manage_channels=True, manage_roles=True)
@commands.cooldown(1, 60, commands.BucketType.guild)
@commands.max_concurrency(1, per=commands.BucketType.guild, wait=False)
async def setup_command(ctx: commands.Context, *, describe: str):
    if not 10 <= len(describe) <= 2000:
        return await ctx.send("Mô tả cần từ 10 đến 2000 ký tự.", ephemeral=True)
    await ctx.defer(ephemeral=True)
    try:
        plan = await propose(describe)
    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        text = str(exc) if isinstance(exc, ValueError) else "Không kết nối được AI. Thử lại sau."
        return await ctx.send(text, ephemeral=True)
    data = preview(plan).encode("utf-8")
    await ctx.send("Bản đề xuất đính kèm: public = công khai; readonly = chỉ nhân viên gửi; staff = nội bộ. "
                   "Tạo mới theo bản này, không xóa kênh cũ. Xác nhận trong 3 phút. "
                   "Role khác có quyền ghi đè hoặc Administrator vẫn có thể truy cập theo quyền Discord.",
                   file=discord.File(io.BytesIO(data), filename="setup-preview.txt"),
                   view=Confirm(ctx, plan), ephemeral=True)


@bot.event
async def on_command_error(ctx, error):
    if ctx.command and ctx.command.has_error_handler():
        return
    if ctx.cog and ctx.cog.has_error_handler():
        return
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.UserInputError):
        text = "Dùng /setup describe: mô tả hoặc !setup mô tả server."
    elif isinstance(error, (commands.CheckFailure, commands.CommandOnCooldown, commands.MaxConcurrencyReached)):
        text = "Cần quyền Administrator, bot cần Manage Channels/Manage Roles; mỗi server chờ 60 giây giữa các yêu cầu."
    else:
        log.error("Command failed", exc_info=(type(error), error, error.__traceback__))
        text = "Có lỗi xử lý. Xem log bot để biết chi tiết."
    await ctx.send(text, ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Thiếu DISCORD_TOKEN trong .env")
    bot.run(token)
