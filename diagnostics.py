"""Contextual logs with rotating files and final-output secret redaction."""
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, quote_plus, unquote, urlsplit

context = ContextVar("log_context", default="-")
SECRET_NAMES = ("DISCORD_TOKEN", "AI_API_KEY", "DATABASE_URL", "PAYOS_CLIENT_ID", "PAYOS_API_KEY",
                "PAYOS_CHECKSUM_KEY", "BANK_ACCOUNT", "BANK_ACCOUNT_NAME")


class SafeFormatter(logging.Formatter):
    def format(self, record):
        # Format a copy: keep secrets out of BOTH messages and exception tracebacks.
        import copy
        record = copy.copy(record)
        record.trace = context.get()
        record.exc_text = None
        output = super().format(record)
        secrets = {os.getenv(name, "") for name in SECRET_NAMES} - {""}
        try:
            password = urlsplit(os.getenv("DATABASE_URL", "")).password
            if password:
                secrets.add(unquote(password))
        except ValueError:
            pass
        for value in sorted(secrets, key=len, reverse=True):
            for encoded in {value, quote(value, safe=""), quote_plus(value)}:
                output = output.replace(encoded, "[REDACTED]")
        output = re.sub(r"(?i)postgres(?:ql)?://[^\s\"'<>]+", "[REDACTED_DATABASE_URL]", output)
        output = re.sub(r"(?i)([?&](?:key|api_key|token)=)[^\s&\"']+", r"\1[REDACTED]", output)
        output = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", output)
        output = re.sub(r"(?i)(\bpassword\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s;]+)", r"\1[REDACTED]", output)
        return output


def configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    formatter = SafeFormatter("%(asctime)s %(levelname)-7s %(name)s [%(trace)s] %(message)s",
                              datefmt="%Y-%m-%dT%H:%M:%S%z")
    root = logging.getLogger()
    # Called once at process entry, before Discord installs handlers.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)
    root.setLevel(level)
    try:
        path = Path(os.getenv("LOG_FILE", "logs/bot.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file.setFormatter(formatter)
        file.setLevel(level)
        root.addHandler(file)
    except OSError:
        root.exception("log.file_unavailable console_logging_remains_enabled")
    # HTTP debug logs can contain provider payloads/headers; app logs record safe summaries.
    for name in ("discord", "aiohttp", "httpx", "httpcore", "payos", "psycopg"):
        logging.getLogger(name).setLevel(logging.INFO if name == "discord" else logging.WARNING)
    logging.getLogger(__name__).info("logging.ready level=%s rotation_mb=5 backups=5", level)


def begin_command(ctx):
    interaction = getattr(ctx, "interaction", None)
    identity = getattr(interaction, "id", None) or getattr(getattr(ctx, "message", None), "id", None)
    trace = f"req={identity or 'unknown'} guild={getattr(ctx.guild, 'id', '-')} channel={getattr(ctx.channel, 'id', '-')} user={ctx.author.id}"
    context.set(trace)
    ctx.log_started = time.monotonic()
    ctx.log_reference = str(identity or "unknown")
    return trace


def interaction_context(interaction):
    return context.set(f"req={interaction.id} guild={interaction.guild_id} channel={interaction.channel_id} user={interaction.user.id}")


def unwrap_error(error):
    seen = set()
    while id(error) not in seen:
        seen.add(id(error))
        inner = getattr(error, "original", error)
        if inner is error:
            break
        error = inner
    return error


def db_operation(fn):
    """Wrap transactions, never record SQL, parameters, customer text or banking data."""
    signature = inspect.signature(fn)
    logger = logging.getLogger("store.db")
    reads = {"settings", "products", "order", "list_orders", "transfer_order", "ticket", "ticket_category",
             "notifications", "revenue", "maintenance", "notification_done"}

    @wraps(fn)
    def wrapped(*args, **kwargs):
        started = time.monotonic()
        bound = signature.bind(*args, **kwargs).arguments
        ids = " ".join(f"{k}={v}" for k, v in bound.items()
                       if k in ("code", "guild", "buyer", "actor", "channel") and type(v) is int)
        logger.debug("db.start operation=%s %s", fn.__name__, ids)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            from store_db import StoreError
            if isinstance(exc, StoreError):
                logger.warning("db.rejected operation=%s %s reason=%s", fn.__name__, ids, exc)
            else:
                logger.exception("db.failed operation=%s %s elapsed_ms=%.0f", fn.__name__, ids,
                                 (time.monotonic() - started) * 1000)
            raise
        elapsed = (time.monotonic() - started) * 1000
        level = logging.WARNING if elapsed >= 1000 else (logging.DEBUG if fn.__name__ in reads else logging.INFO)
        logger.log(level, "db.ok operation=%s %s elapsed_ms=%.0f", fn.__name__, ids,
                   elapsed)
        return result
    return wrapped
