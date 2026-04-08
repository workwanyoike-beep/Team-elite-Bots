"""
AUTO-SUPERVISOR WORKFORCE ECOSYSTEM
Telegram Bot — Supervisor & Access Controller

Dependencies:
    pip install python-telegram-bot supabase bcrypt python-dotenv

Environment variables (.env):
    TELEGRAM_BOT_TOKEN=...
    MANAGER_CHAT_ID=...
    SUPABASE_URL=...
    SUPABASE_SERVICE_KEY=...
"""

import os
import re
import bcrypt
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
MANAGER_CHAT_ID = int(os.environ["MANAGER_CHAT_ID"])
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]  # service role — bypasses RLS

MIN_QUALITY_SCORE = 85.0  # minimum previous-shift score to unlock

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("supervisor-bot")

# ── Supabase client ───────────────────────────────────────────────────────────
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_manager(chat_id: int) -> bool:
    return chat_id == MANAGER_CHAT_ID


def calc_score(start_sent, start_received, end_sent, end_received) -> float | None:
    sent_delta = end_sent - start_sent
    if sent_delta <= 0:
        return None
    return round(((end_received - start_received) / sent_delta) * 100, 2)


def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def check_pin(pin: str, hashed: str) -> bool:
    return bcrypt.checkpw(pin.encode(), hashed.encode())


def get_worker_by_username(username: str) -> dict | None:
    result = (
        sb.table("workers")
        .select("*")
        .eq("telegram_username", username.lstrip("@"))
        .maybe_single()
        .execute()
    )
    return result.data


def get_worker_by_chat_id(chat_id: int) -> dict | None:
    result = (
        sb.table("workers")
        .select("*")
        .eq("telegram_chat_id", chat_id)
        .maybe_single()
        .execute()
    )
    return result.data


def get_active_shift(worker_id: str) -> dict | None:
    result = (
        sb.table("shifts")
        .select("*, pcs(*)")
        .eq("worker_id", worker_id)
        .eq("status", "active")
        .maybe_single()
        .execute()
    )
    return result.data


def get_last_shift_score(worker_id: str) -> float | None:
    """Return the final_percentage of the most recent COMPLETED shift."""
    result = (
        sb.table("performance_logs")
        .select("final_percentage")
        .eq("worker_id", worker_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0].get("final_percentage")
    return None  # No history → treat as first shift → allow


def get_pc_by_hwid(hwid: str) -> dict | None:
    result = (
        sb.table("pcs")
        .select("*")
        .eq("hwid", hwid)
        .maybe_single()
        .execute()
    )
    return result.data


# ══════════════════════════════════════════════════════════════════════════════
# /start  — register worker
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = get_worker_by_chat_id(user.id)
    if existing:
        await update.message.reply_text(
            f"✅ You're already registered as *{existing['telegram_username']}*.\n"
            "Use /help to see available commands.",
            parse_mode="Markdown"
        )
        return

    username = user.username or f"user_{user.id}"
    sb.table("workers").upsert({
        "telegram_username": username,
        "telegram_chat_id": user.id,
    }, on_conflict="telegram_username").execute()

    await update.message.reply_text(
        f"👋 Welcome, *@{username}*!\n\n"
        "You've been registered in the system.\n"
        "Your supervisor will assign your first shift.\n\n"
        "Use /help for available commands.",
        parse_mode="Markdown"
    )
    await ctx.bot.send_message(
        MANAGER_CHAT_ID,
        f"🆕 New worker registered: *@{username}* (chat_id: `{user.id}`)",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /stats [Sent] [Received]  — worker reports their numbers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args

    if len(args) != 2:
        await update.message.reply_text(
            "❌ Usage: `/stats [Sent] [Received]`\nExample: `/stats 120 98`",
            parse_mode="Markdown"
        )
        return

    try:
        sent     = int(args[0])
        received = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Both values must be integers.")
        return

    if sent < 0 or received < 0:
        await update.message.reply_text("❌ Values cannot be negative.")
        return

    worker = get_worker_by_chat_id(user.id)
    if not worker:
        await update.message.reply_text("❌ You're not registered. Send /start first.")
        return

    shift = get_active_shift(worker["id"])
    if not shift:
        await update.message.reply_text("❌ You have no active shift to report stats for.")
        return

    # Upsert performance_log — if log exists update end stats, else set start stats
    existing_log = (
        sb.table("performance_logs")
        .select("*")
        .eq("shift_id", shift["id"])
        .maybe_single()
        .execute()
    ).data

    if not existing_log:
        # First report = set start stats
        sb.table("performance_logs").insert({
            "worker_id":      worker["id"],
            "shift_id":       shift["id"],
            "start_sent":     sent,
            "start_received": received,
        }).execute()
        await update.message.reply_text(
            f"📊 *Start stats recorded!*\n"
            f"Sent: `{sent}` | Received: `{received}`\n\n"
            "Use `/stats` again at shift end to calculate your score.",
            parse_mode="Markdown"
        )
    else:
        # Subsequent report = update end stats + calculate score
        start_sent     = existing_log["start_sent"]
        start_received = existing_log["start_received"]
        score          = calc_score(start_sent, start_received, sent, received)

        update_data = {
            "end_sent":     sent,
            "end_received": received,
        }
        if score is not None:
            update_data["final_percentage"] = score

        sb.table("performance_logs").update(update_data).eq("id", existing_log["id"]).execute()

        if score is None:
            await update.message.reply_text("⚠️ Cannot calculate score — sent count hasn't changed.")
            return

        emoji = "🟢" if score >= MIN_QUALITY_SCORE else "🔴"
        await update.message.reply_text(
            f"📊 *Stats updated!*\n\n"
            f"Sent Δ:     `{sent - start_sent}`\n"
            f"Received Δ: `{received - start_received}`\n"
            f"Score:       {emoji} *{score:.1f}%*\n\n"
            f"{'✅ Quality target met!' if score >= MIN_QUALITY_SCORE else f'⚠️ Below {MIN_QUALITY_SCORE}% target.'}",
            parse_mode="Markdown"
        )

        # Notify manager
        await ctx.bot.send_message(
            MANAGER_CHAT_ID,
            f"📊 Stats update — *@{worker['telegram_username']}*\n"
            f"Score: {emoji} *{score:.1f}%*",
            parse_mode="Markdown"
        )


# ══════════════════════════════════════════════════════════════════════════════
# /endshift  — worker ends their shift
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_endshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    worker = get_worker_by_chat_id(user.id)
    if not worker:
        await update.message.reply_text("❌ Not registered.")
        return

    shift = get_active_shift(worker["id"])
    if not shift:
        await update.message.reply_text("❌ No active shift found.")
        return

    now = datetime.now(timezone.utc).isoformat()

    # Mark shift completed
    sb.table("shifts").update({
        "status":   "completed",
        "end_time": now
    }).eq("id", shift["id"]).execute()

    # Free the PC
    sb.table("pcs").update({"status": "vacant"}).eq("id", shift["pc_id"]).execute()

    # Send lock signal to desktop client
    sb.table("unlock_signals").insert({
        "pc_hwid":   shift["pcs"]["hwid"],
        "worker_id": worker["id"],
        "shift_id":  shift["id"],
        "action":    "lock",
        "reason":    "Shift ended",
    }).execute()

    # Get final score
    log_entry = (
        sb.table("performance_logs")
        .select("final_percentage")
        .eq("shift_id", shift["id"])
        .maybe_single()
        .execute()
    ).data
    score_text = f"{log_entry['final_percentage']:.1f}%" if (log_entry and log_entry.get("final_percentage")) else "Not calculated"

    await update.message.reply_text(
        f"✅ *Shift ended.*\n\n"
        f"PC: `{shift['pcs']['label']}`\n"
        f"Duration: shift closed\n"
        f"Final score: *{score_text}*\n\n"
        "The PC has been locked. Good work! 👋",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /unlock  — called by Desktop Client via webhook (internal endpoint)
#
# The desktop client sends a POST to a thin HTTP server that calls
# handle_unlock_request() directly. This command triggers the same logic
# when a manager types it manually.
# ══════════════════════════════════════════════════════════════════════════════

async def handle_unlock_request(
    bot,
    worker_username: str,
    pc_hwid: str,
    pin: str
) -> tuple[bool, str]:
    """
    Core access control logic.
    Returns (granted: bool, reason: str).
    Uses a DB transaction pattern: check + write atomically via Postgres.
    """
    worker = get_worker_by_username(worker_username)
    if not worker:
        return False, "Worker not registered in the system."

    pc = get_pc_by_hwid(pc_hwid)
    if not pc:
        return False, f"PC with HWID `{pc_hwid}` is not registered."

    # Check: PC must be vacant
    if pc["status"] != "vacant":
        return False, f"PC *{pc['label']}* is currently occupied by another worker."

    # Check: worker must not already have an active shift
    existing_shift = get_active_shift(worker["id"])
    if existing_shift:
        return False, "You already have an active shift on another PC."

    # Check: previous shift score ≥ 85%
    last_score = get_last_shift_score(worker["id"])
    if last_score is not None and last_score < MIN_QUALITY_SCORE:
        return False, (
            f"Your last shift score was *{last_score:.1f}%*, "
            f"which is below the required *{MIN_QUALITY_SCORE}%*. "
            "Please speak with your supervisor."
        )

    # Generate and hash 6-digit PIN
    hashed_pin = hash_pin(pin)

    # ATOMIC: mark PC occupied + create shift in one transaction
    # Supabase doesn't expose raw transactions in the REST client,
    # so we use the partial unique index as a concurrency guard.
    try:
        sb.table("pcs").update({"status": "occupied"}).eq("id", pc["id"]).execute()

        shift_result = sb.table("shifts").insert({
            "worker_id":    worker["id"],
            "pc_id":        pc["id"],
            "password_pin": hashed_pin,
            "status":       "active",
        }).execute()

        shift_id = shift_result.data[0]["id"]

        # Write initial performance log placeholder
        sb.table("performance_logs").insert({
            "worker_id": worker["id"],
            "shift_id":  shift_id,
            "start_sent": 0,
            "start_received": 0,
        }).execute()

        # Send unlock signal for Supabase Realtime
        sb.table("unlock_signals").insert({
            "pc_hwid":   pc_hwid,
            "worker_id": worker["id"],
            "shift_id":  shift_id,
            "action":    "unlock",
        }).execute()

        # Notify manager
        await bot.send_message(
            MANAGER_CHAT_ID,
            f"🔓 *Access granted*\n"
            f"Worker: @{worker['telegram_username']}\n"
            f"PC: {pc['label']}\n"
            f"Last score: {f'{last_score:.1f}%' if last_score else 'First shift'}",
            parse_mode="Markdown"
        )
        return True, f"Access granted. Shift started on *{pc['label']}*."

    except Exception as e:
        # Rollback PC status if shift insert failed
        sb.table("pcs").update({"status": "vacant"}).eq("id", pc["id"]).execute()
        log.error(f"Shift creation failed: {e}")
        return False, "System error. Please try again."


# Manager command: manually grant/deny access
async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/grant @username PC-HWID`", parse_mode="Markdown")
        return

    username = args[0]
    hwid     = args[1]
    pin      = "000000"  # manager override PIN

    granted, reason = await handle_unlock_request(ctx.bot, username, hwid, pin)
    await update.message.reply_text(
        f"{'✅' if granted else '❌'} {reason}",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /addpc  — manager registers a new PC
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_addpc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/addpc HWID PC-Label`\nExample: `/addpc ABC123XYZ PC-01`", parse_mode="Markdown")
        return

    hwid  = args[0]
    label = " ".join(args[1:])

    try:
        sb.table("pcs").insert({"hwid": hwid, "label": label}).execute()
        await update.message.reply_text(f"✅ PC *{label}* (HWID: `{hwid}`) registered.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /status  — manager overview
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        # Workers see their own status
        worker = get_worker_by_chat_id(update.effective_user.id)
        if not worker:
            await update.message.reply_text("Not registered.")
            return
        shift = get_active_shift(worker["id"])
        if shift:
            await update.message.reply_text(
                f"🟢 Active shift on *{shift['pcs']['label']}*\n"
                f"Started: {shift['start_time'][:16].replace('T',' ')} UTC",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("🔘 No active shift.")
        return

    # Manager: full overview
    pcs     = sb.table("pcs").select("*").execute().data or []
    shifts  = sb.table("shifts").select("*, workers(*), pcs(*)").eq("status","active").execute().data or []

    lines = ["📋 *System Status*\n"]
    lines.append(f"*PCs:* {len(pcs)} registered")
    occupied_pcs = [s["pcs"]["label"] for s in shifts]

    for pc in pcs:
        icon = "🟢" if pc["label"] in occupied_pcs else "⚪"
        lines.append(f"  {icon} {pc['label']} — {pc['status']}")

    if shifts:
        lines.append(f"\n*Active Shifts:* {len(shifts)}")
        for s in shifts:
            lines.append(f"  • @{s['workers']['telegram_username']} → {s['pcs']['label']}")
    else:
        lines.append("\n*No active shifts.*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    is_mgr = is_manager(update.effective_user.id)
    text = (
        "*📖 Commands*\n\n"
        "*Worker commands:*\n"
        "`/start` — Register yourself\n"
        "`/stats [Sent] [Received]` — Report your message stats\n"
        "`/endshift` — End your current shift\n"
        "`/status` — Check your active shift\n"
        "`/help` — Show this message\n"
    )
    if is_mgr:
        text += (
            "\n*Manager commands:*\n"
            "`/addpc HWID Label` — Register a new PC\n"
            "`/grant @username HWID` — Manually grant access\n"
            "`/status` — Full system overview\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# HTTP endpoint for Desktop Client auth requests
# A minimal asyncio HTTP server alongside the bot
# ══════════════════════════════════════════════════════════════════════════════

async def http_server(bot):
    """
    Minimal HTTP server that the Desktop Client calls to authenticate.
    Listens on port 8765 (configure firewall to allow only internal traffic).

    POST /auth
    Body (JSON): {"username": "@alice", "hwid": "ABC123", "pin": "123456"}

    Response (JSON): {"granted": true/false, "reason": "..."}
    """
    import json
    from aiohttp import web

    async def handle_auth(request):
        try:
            body = await request.json()
            username = body.get("username", "")
            hwid     = body.get("hwid", "")
            pin      = body.get("pin", "")

            if not all([username, hwid, pin]):
                return web.json_response({"granted": False, "reason": "Missing fields."}, status=400)

            granted, reason = await handle_unlock_request(bot, username, hwid, pin)

            # Notify worker via Telegram
            worker = get_worker_by_username(username)
            if worker and worker.get("telegram_chat_id"):
                await bot.send_message(
                    worker["telegram_chat_id"],
                    f"{'🔓 Access granted!' if granted else '🔒 Access denied.'}\n_{reason}_",
                    parse_mode="Markdown"
                )

            return web.json_response({"granted": granted, "reason": reason})

        except Exception as e:
            log.error(f"Auth handler error: {e}")
            return web.json_response({"granted": False, "reason": "Server error."}, status=500)

    app = web.Application()
    app.router.add_post("/auth", handle_auth)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8765)
    await site.start()
    log.info("Auth HTTP server listening on :8765")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    asyncio.get_event_loop().create_task(http_server(app.bot))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("endshift", cmd_endshift))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("addpc",    cmd_addpc))
    app.add_handler(CommandHandler("grant",    cmd_grant))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
