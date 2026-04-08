"""
AUTO-SUPERVISOR WORKFORCE ECOSYSTEM
Telegram Bot — Supervisor & Access Controller

Adapted for Neon (serverless PostgreSQL) — uses asyncpg instead of supabase-py.

Dependencies:
    pip install python-telegram-bot asyncpg bcrypt python-dotenv aiohttp

Environment variables (.env):
    TELEGRAM_BOT_TOKEN=...
    MANAGER_CHAT_ID=...
    DATABASE_URL=postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require
"""

import os
import re
import bcrypt
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import asyncpg
from telegram import Update
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
MANAGER_CHAT_ID   = int(os.environ["MANAGER_CHAT_ID"])
DATABASE_URL      = os.environ["DATABASE_URL"]   # Neon connection string

MIN_QUALITY_SCORE = 85.0

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("supervisor-bot")

# ── Connection pool (created once at startup) ─────────────────────────────────
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            ssl="require",          # Neon requires SSL
            command_timeout=10,
        )
    return _pool


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


async def get_worker_by_username(username: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM workers WHERE telegram_username = $1",
        username.lstrip("@")
    )
    return dict(row) if row else None


async def get_worker_by_chat_id(chat_id: int) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM workers WHERE telegram_chat_id = $1", chat_id
    )
    return dict(row) if row else None


async def get_active_shift(worker_id: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT s.*, p.hwid AS pc_hwid, p.label AS pc_label, p.status AS pc_status
        FROM shifts s
        JOIN pcs p ON p.id = s.pc_id
        WHERE s.worker_id = $1 AND s.status = 'active'
        """,
        worker_id
    )
    if not row:
        return None
    d = dict(row)
    # Nest pc info to match original code's shift["pcs"] pattern
    d["pcs"] = {"hwid": d.pop("pc_hwid"), "label": d.pop("pc_label"), "status": d.pop("pc_status")}
    return d


async def get_last_shift_score(worker_id: str) -> float | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT final_percentage FROM performance_logs
        WHERE worker_id = $1
        ORDER BY created_at DESC LIMIT 1
        """,
        worker_id
    )
    if row and row["final_percentage"] is not None:
        return float(row["final_percentage"])
    return None


async def get_pc_by_hwid(hwid: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM pcs WHERE hwid = $1", hwid)
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════════
# /start  — register worker
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = await get_worker_by_chat_id(user.id)
    if existing:
        await update.message.reply_text(
            f"✅ You're already registered as *{existing['telegram_username']}*.\n"
            "Use /help to see available commands.",
            parse_mode="Markdown"
        )
        return

    username = user.username or f"user_{user.id}"
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO workers (telegram_username, telegram_chat_id)
        VALUES ($1, $2)
        ON CONFLICT (telegram_username) DO UPDATE SET telegram_chat_id = EXCLUDED.telegram_chat_id
        """,
        username, user.id
    )

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
# /stats [Sent] [Received]
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

    worker = await get_worker_by_chat_id(user.id)
    if not worker:
        await update.message.reply_text("❌ You're not registered. Send /start first.")
        return

    shift = await get_active_shift(worker["id"])
    if not shift:
        await update.message.reply_text("❌ You have no active shift to report stats for.")
        return

    pool = await get_pool()
    existing_log = await pool.fetchrow(
        "SELECT * FROM performance_logs WHERE shift_id = $1", shift["id"]
    )

    if not existing_log:
        await pool.execute(
            """
            INSERT INTO performance_logs (worker_id, shift_id, start_sent, start_received)
            VALUES ($1, $2, $3, $4)
            """,
            worker["id"], shift["id"], sent, received
        )
        await update.message.reply_text(
            f"📊 *Start stats recorded!*\n"
            f"Sent: `{sent}` | Received: `{received}`\n\n"
            "Use `/stats` again at shift end to calculate your score.",
            parse_mode="Markdown"
        )
    else:
        start_sent     = existing_log["start_sent"]
        start_received = existing_log["start_received"]
        score          = calc_score(start_sent, start_received, sent, received)

        if score is not None:
            await pool.execute(
                """
                UPDATE performance_logs
                SET end_sent = $1, end_received = $2, final_percentage = $3
                WHERE id = $4
                """,
                sent, received, score, existing_log["id"]
            )
        else:
            await pool.execute(
                "UPDATE performance_logs SET end_sent = $1, end_received = $2 WHERE id = $3",
                sent, received, existing_log["id"]
            )
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
        await ctx.bot.send_message(
            MANAGER_CHAT_ID,
            f"📊 Stats update — *@{worker['telegram_username']}*\nScore: {emoji} *{score:.1f}%*",
            parse_mode="Markdown"
        )


# ══════════════════════════════════════════════════════════════════════════════
# /endshift
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_endshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    worker = await get_worker_by_chat_id(user.id)
    if not worker:
        await update.message.reply_text("❌ Not registered.")
        return

    shift = await get_active_shift(worker["id"])
    if not shift:
        await update.message.reply_text("❌ No active shift found.")
        return

    pool = await get_pool()
    now  = datetime.now(timezone.utc)

    await pool.execute(
        "UPDATE shifts SET status = 'completed', end_time = $1 WHERE id = $2",
        now, shift["id"]
    )
    await pool.execute(
        "UPDATE pcs SET status = 'vacant' WHERE id = $1", shift["pc_id"]
    )
    await pool.execute(
        """
        INSERT INTO unlock_signals (pc_hwid, worker_id, shift_id, action, reason)
        VALUES ($1, $2, $3, 'lock', 'Shift ended')
        """,
        shift["pcs"]["hwid"], worker["id"], shift["id"]
    )

    log_entry = await pool.fetchrow(
        "SELECT final_percentage FROM performance_logs WHERE shift_id = $1", shift["id"]
    )
    score_text = (
        f"{float(log_entry['final_percentage']):.1f}%"
        if log_entry and log_entry["final_percentage"] is not None
        else "Not calculated"
    )

    await update.message.reply_text(
        f"✅ *Shift ended.*\n\n"
        f"PC: `{shift['pcs']['label']}`\n"
        f"Final score: *{score_text}*\n\n"
        "The PC has been locked. Good work! 👋",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Core access control (called by HTTP endpoint and /grant)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_unlock_request(
    bot, worker_username: str, pc_hwid: str, pin: str
) -> tuple[bool, str]:
    worker = await get_worker_by_username(worker_username)
    if not worker:
        return False, "Worker not registered in the system."

    pc = await get_pc_by_hwid(pc_hwid)
    if not pc:
        return False, f"PC with HWID `{pc_hwid}` is not registered."

    if pc["status"] != "vacant":
        return False, f"PC *{pc['label']}* is currently occupied by another worker."

    existing_shift = await get_active_shift(worker["id"])
    if existing_shift:
        return False, "You already have an active shift on another PC."

    last_score = await get_last_shift_score(worker["id"])
    if last_score is not None and last_score < MIN_QUALITY_SCORE:
        return False, (
            f"Your last shift score was *{last_score:.1f}%*, "
            f"which is below the required *{MIN_QUALITY_SCORE}%*. "
            "Please speak with your supervisor."
        )

    hashed_pin = hash_pin(pin)
    pool = await get_pool()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE pcs SET status = 'occupied' WHERE id = $1", pc["id"]
                )
                shift_row = await conn.fetchrow(
                    """
                    INSERT INTO shifts (worker_id, pc_id, password_pin, status)
                    VALUES ($1, $2, $3, 'active')
                    RETURNING id
                    """,
                    worker["id"], pc["id"], hashed_pin
                )
                shift_id = shift_row["id"]

                await conn.execute(
                    """
                    INSERT INTO performance_logs (worker_id, shift_id, start_sent, start_received)
                    VALUES ($1, $2, 0, 0)
                    """,
                    worker["id"], shift_id
                )
                await conn.execute(
                    """
                    INSERT INTO unlock_signals (pc_hwid, worker_id, shift_id, action)
                    VALUES ($1, $2, $3, 'unlock')
                    """,
                    pc_hwid, worker["id"], shift_id
                )

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
        log.error(f"Shift creation failed: {e}")
        return False, "System error. Please try again."


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/grant @username PC-HWID`", parse_mode="Markdown")
        return

    granted, reason = await handle_unlock_request(ctx.bot, args[0], args[1], "000000")
    await update.message.reply_text(
        f"{'✅' if granted else '❌'} {reason}", parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /addpc
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_addpc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/addpc HWID PC-Label`\nExample: `/addpc ABC123XYZ PC-01`",
            parse_mode="Markdown"
        )
        return

    hwid  = args[0]
    label = " ".join(args[1:])
    pool  = await get_pool()

    try:
        await pool.execute(
            "INSERT INTO pcs (hwid, label) VALUES ($1, $2)", hwid, label
        )
        await update.message.reply_text(
            f"✅ PC *{label}* (HWID: `{hwid}`) registered.", parse_mode="Markdown"
        )
    except asyncpg.UniqueViolationError:
        await update.message.reply_text(f"❌ A PC with HWID `{hwid}` is already registered.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /status
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        worker = await get_worker_by_chat_id(update.effective_user.id)
        if not worker:
            await update.message.reply_text("Not registered.")
            return
        shift = await get_active_shift(worker["id"])
        if shift:
            await update.message.reply_text(
                f"🟢 Active shift on *{shift['pcs']['label']}*\n"
                f"Started: {str(shift['start_time'])[:16].replace('T',' ')} UTC",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("🔘 No active shift.")
        return

    pool = await get_pool()
    pcs    = await pool.fetch("SELECT * FROM pcs ORDER BY label")
    shifts = await pool.fetch(
        """
        SELECT s.*, w.telegram_username, p.label AS pc_label
        FROM shifts s
        JOIN workers w ON w.id = s.worker_id
        JOIN pcs p ON p.id = s.pc_id
        WHERE s.status = 'active'
        """
    )

    occupied_labels = {s["pc_label"] for s in shifts}
    lines = ["📋 *System Status*\n", f"*PCs:* {len(pcs)} registered"]

    for pc in pcs:
        icon = "🟢" if pc["label"] in occupied_labels else "⚪"
        lines.append(f"  {icon} {pc['label']} — {pc['status']}")

    if shifts:
        lines.append(f"\n*Active Shifts:* {len(shifts)}")
        for s in shifts:
            lines.append(f"  • @{s['telegram_username']} → {s['pc_label']}")
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
# HTTP endpoint for Desktop Client auth requests (aiohttp on port 8765)
# ══════════════════════════════════════════════════════════════════════════════

async def http_server(bot):
    import json
    from aiohttp import web

    async def handle_auth(request):
        try:
            body    = await request.json()
            username = body.get("username", "")
            hwid     = body.get("hwid", "")
            pin      = body.get("pin", "")

            if not all([username, hwid, pin]):
                return web.json_response({"granted": False, "reason": "Missing fields."}, status=400)

            granted, reason = await handle_unlock_request(bot, username, hwid, pin)

            worker = await get_worker_by_username(username)
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

    # ── NEW: polling endpoint for Desktop Client ──────────────────────────────
    # GET /signals?hwid=<hwid>
    # Returns unconsumed unlock_signals for this PC and marks them consumed.
    async def handle_signals(request):
        hwid = request.query.get("hwid", "")
        if not hwid:
            return web.json_response([], status=400)
        try:
            pool = await get_pool()
            rows = await pool.fetch(
                """
                SELECT id, action, reason FROM unlock_signals
                WHERE pc_hwid = $1 AND consumed = FALSE
                ORDER BY created_at DESC LIMIT 5
                """,
                hwid
            )
            if rows:
                ids = [str(r["id"]) for r in rows]
                await pool.execute(
                    "UPDATE unlock_signals SET consumed = TRUE WHERE id = ANY($1::uuid[])",
                    ids
                )
            return web.json_response([dict(r) for r in rows])
        except Exception as e:
            log.error(f"Signals handler error: {e}")
            return web.json_response([], status=500)

    # ── Portal login endpoint ─────────────────────────────────────────────────
    async def handle_portal_login(request):
        """
        POST /portal-login
        Body: { "username": "@alice", "pin": "123456" }
        Returns: { "granted": true, "worker": {...}, "shifts": [...], "perfs": [...] }
        Workers log into the Vercel portal with their Telegram username + PIN.
        PIN is verified against their most recent active or completed shift.
        """
        try:
            body     = await request.json()
            username = body.get("username", "").lstrip("@")
            pin      = body.get("pin", "")

            if not username or not pin:
                return web.json_response({"granted": False, "reason": "Username and PIN required."}, status=400)

            pool   = await get_pool()
            worker = await pool.fetchrow(
                "SELECT * FROM workers WHERE telegram_username = $1", username
            )
            if not worker:
                return web.json_response({"granted": False, "reason": "Worker not registered. Send /start to the bot first."})

            # Verify PIN against most recent shift
            shift = await pool.fetchrow(
                "SELECT * FROM shifts WHERE worker_id = $1 ORDER BY created_at DESC LIMIT 1",
                worker["id"]
            )
            if not shift:
                return web.json_response({"granted": False, "reason": "No shift found. Ask your supervisor to start a shift first."})

            if not check_pin(pin, shift["password_pin"]):
                return web.json_response({"granted": False, "reason": "Incorrect PIN."})

            # Load shifts with pc label
            shifts = await pool.fetch(
                """
                SELECT s.id, s.start_time, s.end_time, s.status, p.label AS pc_label
                FROM shifts s JOIN pcs p ON p.id = s.pc_id
                WHERE s.worker_id = $1
                ORDER BY s.created_at DESC LIMIT 30
                """,
                worker["id"]
            )
            perfs = await pool.fetch(
                """
                SELECT pl.*, s.start_time AS shift_date
                FROM performance_logs pl
                JOIN shifts s ON s.id = pl.shift_id
                WHERE pl.worker_id = $1
                ORDER BY pl.created_at DESC LIMIT 30
                """,
                worker["id"]
            )

            return web.json_response({
                "granted": True,
                "worker":  dict(worker),
                "shifts":  [dict(r) for r in shifts],
                "perfs":   [dict(r) for r in perfs],
            }, dumps=lambda obj, **kw: __import__("json").dumps(obj, default=str))

        except Exception as e:
            log.error(f"Portal login error: {e}")
            return web.json_response({"granted": False, "reason": "Server error."}, status=500)

    # ── Portal payment update endpoint ────────────────────────────────────────
    async def handle_portal_payment(request):
        """
        POST /portal-payment
        Body: { "username": "@alice", "mpesa_number": "0712345678", "mpesa_name": "Alice Wanjiru" }
        """
        try:
            body         = await request.json()
            username     = body.get("username", "").lstrip("@")
            mpesa_number = body.get("mpesa_number", "").strip()
            mpesa_name   = body.get("mpesa_name", "").strip()

            if not all([username, mpesa_number, mpesa_name]):
                return web.json_response({"ok": False, "error": "All fields required."}, status=400)

            pool = await get_pool()
            result = await pool.execute(
                "UPDATE workers SET mpesa_number = $1, mpesa_name = $2 WHERE telegram_username = $3",
                mpesa_number, mpesa_name, username
            )
            if result == "UPDATE 0":
                return web.json_response({"ok": False, "error": "Worker not found."})

            return web.json_response({"ok": True})

        except Exception as e:
            log.error(f"Portal payment error: {e}")
            return web.json_response({"ok": False, "error": "Server error."}, status=500)

    # CORS middleware so Vercel portal can call Railway endpoints
    ALLOWED_ORIGIN = "https://team-elite-bots-prf5iyu4r-workwanyoike-8537s-projects.vercel.app"

    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            try:
                resp = await handler(request)
            except Exception:
                resp = web.json_response({"error": "server error"}, status=500)
        resp.headers["Access-Control-Allow-Origin"]  = ALLOWED_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("OPTIONS", "/{path_info:.*}", lambda r: web.Response())
    app.router.add_post("/auth",           handle_auth)
    app.router.add_get("/signals",         handle_signals)
    app.router.add_post("/portal-login",   handle_portal_login)
    app.router.add_post("/portal-payment", handle_portal_payment)
    app.router.add_get("/health",          lambda r: web.json_response({"status": "ok"}))

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8765))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Auth HTTP server listening on :{port}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    await get_pool()   # warm up connection pool at startup
    asyncio.get_event_loop().create_task(http_server(app.bot))


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start",    cmd_start))
    application.add_handler(CommandHandler("stats",    cmd_stats))
    application.add_handler(CommandHandler("endshift", cmd_endshift))
    application.add_handler(CommandHandler("status",   cmd_status))
    application.add_handler(CommandHandler("help",     cmd_help))
    application.add_handler(CommandHandler("addpc",    cmd_addpc))
    application.add_handler(CommandHandler("grant",    cmd_grant))

    log.info("Bot starting…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
