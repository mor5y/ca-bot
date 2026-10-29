"""
Telegram control panel for tg_ca_bot.py. Runs inside the same process, so it
reads and changes the live bot state directly.

pip install "python-telegram-bot>=21"

Env vars:
  CONTROL_BOT_TOKEN   token from @BotFather (create a NEW bot just for this)
  ADMIN_IDS           comma-separated numeric Telegram user ids allowed to use it
                      (get yours from @userinfobot)
  MAX_BUY_SOL         ceiling for /set buy_sol, default 1

Message your control bot /start once, otherwise it can't push alerts to you.
Only users in ADMIN_IDS, and only in private chat, can do anything.
"""
import asyncio, functools, html, json, logging, os, time
from pathlib import Path

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = logging.getLogger("control")
SETTINGS_FILE = Path("settings.json")
ADMINS: set[int] = set()
MAX_BUY_SOL = float(os.getenv("MAX_BUY_SOL", "1"))

# Fields editable from chat. dry_run, keys, RPC and groups are deliberately NOT here.
PCT = {"take_profit", "trail_activate", "trail_pct", "trail_floor", "initial_stop", "max_roundtrip_loss"}
EDITABLE = {
    "buy_sol": float, "max_positions": int, "max_buys_per_day": int,
    "take_profit": float, "trail_activate": float, "trail_pct": float, "trail_floor": float,
    "initial_stop": float, "min_liquidity_usd": float, "max_roundtrip_loss": float,
    "buy_slip_bps": int, "sell_slip_bps": int, "poll_secs": float,
}


# ---------------- helpers ----------------
def esc(s): return html.escape(str(s))
def short(m): return f"{m[:4]}…{m[-4:]}"
def fmt_val(k, v): return f"{v * 100:g}%" if k in PCT else f"{v:g}"

def parse_value(key, raw):
    if key in PCT:
        v = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    else:
        v = EDITABLE[key](raw)
    if key == "trail_floor": ok = -1 <= v <= 10
    elif key in ("initial_stop", "trail_pct", "max_roundtrip_loss"): ok = 0 <= v < 1
    elif key == "buy_sol": ok = 0 < v <= MAX_BUY_SOL
    else: ok = v > 0
    if not ok: raise ValueError("out of allowed range")
    return v

def load_overrides(core):
    if SETTINGS_FILE.exists():
        for k, v in json.loads(SETTINGS_FILE.read_text()).items():
            if k in EDITABLE: setattr(core.CFG, k, EDITABLE[k](v))

def save_overrides(core):
    SETTINGS_FILE.write_text(json.dumps({k: getattr(core.CFG, k) for k in EDITABLE}, indent=1))

def menu_markup(core):
    paused = core.KILL_FILE.exists()
    return Markup([
        [Btn("📊 Status", callback_data="m:status"), Btn("📈 Positions", callback_data="m:pos")],
        [Btn("▶️ Resume buying" if paused else "⏸ Pause buying", callback_data="m:resume" if paused else "m:pause"),
         Btn("⚙️ Settings", callback_data="m:set")],
        [Btn("🛑 Sell ALL", callback_data="m:sellall")],
    ])

async def portfolio_snapshot(core):
    items = [p for p in core.POS.values() if p["status"] in ("open", "stuck")]
    if not items:
        return {"entry_sol": 0.0, "value_sol": 0.0, "pnl_sol": 0.0, "pnl_pct": 0.0, "count": 0}

    entry_total = 0.0
    value_total = 0.0
    for p in items:
        entry_total += p["entry_lamports"] / 1e9
        try:
            q = await core.quote(p["mint"], core.SOL, p["amount"], core.CFG.sell_slip_bps)
            value_total += int(q["outAmount"]) / 1e9
        except Exception:
            value_total += p["entry_lamports"] / 1e9

    pnl_sol = value_total - entry_total
    pnl_pct = (pnl_sol / entry_total * 100.0) if entry_total else 0.0
    return {"entry_sol": entry_total, "value_sol": value_total, "pnl_sol": pnl_sol, "pnl_pct": pnl_pct, "count": len(items)}


async def pnl_card_text(core):
    pnl = await portfolio_snapshot(core)
    return (
        "<b>📈 PnL Card</b>\n"
        "━━━━━━━━━━━━\n"
        f"<b>Open:</b> {pnl['count']} position(s)\n"
        f"<b>Entry:</b> {pnl['entry_sol']:.4f} SOL\n"
        f"<b>Value:</b> {pnl['value_sol']:.4f} SOL\n"
        f"<b>Live PnL:</b> {pnl['pnl_sol']:+.4f} SOL ({pnl['pnl_pct']:+.1f}%)"
    )


async def status_text(core):
    c = core.CFG
    ps = list(core.POS.values())
    opn = [p for p in ps if p["status"] == "open"]
    stuck = [p for p in ps if p["status"] == "stuck"]
    closed = [p for p in ps if p["status"] == "closed" and "exit_sol" in p]
    realized = sum(p["exit_sol"] - p["entry_lamports"] / 1e9 for p in closed)
    wins = sum(1 for p in closed if p["exit_sol"] * 1e9 > p["entry_lamports"])
    buys24 = sum(1 for t in core.BUY_TIMES if t > time.time() - 86400)
    return (f"<b>Mode:</b> {'DRY RUN (paper)' if c.dry_run else '🔴 LIVE'}\n"
            f"<b>New buys:</b> {'⏸ paused' if core.KILL_FILE.exists() else '▶️ active'}\n"
            f"<b>Open:</b> {len(opn)}/{c.max_positions}    <b>Stuck:</b> {len(stuck)}\n"
            f"<b>Buys (24h):</b> {buys24}/{c.max_buys_per_day}\n"
            f"<b>Closed:</b> {len(closed)} ({wins} wins)\n"
            f"<b>Realized PnL:</b> {realized:+.4f} SOL\n\n"
            f"{await pnl_card_text(core)}")

def settings_text(core):
    lines = [f"<code>{k}</code> = <b>{fmt_val(k, getattr(core.CFG, k))}</b>" for k in EDITABLE]
    return ("<b>Settings</b> (applied live, saved to settings.json)\n" + "\n".join(lines) +
            "\n\nChange: <code>/set key value</code>\n"
            "e.g. <code>/set take_profit 60%</code> or <code>/set buy_sol 0.1</code>")

async def positions_view(core):
    items = [p for p in core.POS.values() if p["status"] in ("open", "stuck")]
    if not items:
        return "No open positions.", menu_markup(core)

    async def cur(p):
        try:
            q = await core.quote(p["mint"], core.SOL, p["amount"], core.CFG.sell_slip_bps)
            return int(q["outAmount"]) / p["entry_lamports"] - 1
        except Exception:
            return None

    pnl = await portfolio_snapshot(core)
    pnl_header = (
        "<b>📊 PnL Card</b>\n"
        f"<b>Entry:</b> {pnl['entry_sol']:.4f} SOL   <b>Value:</b> {pnl['value_sol']:.4f} SOL\n"
        f"<b>Live PnL:</b> {pnl['pnl_sol']:+.4f} SOL ({pnl['pnl_pct']:+.1f}%)"
    )

    pnls = await asyncio.gather(*(cur(p) for p in items))
    lines, rows = [pnl_header], []
    for p, pnl_val in zip(items, pnls):
        age = int((time.time() - p["opened"]) / 60)
        pn = "no quote" if pnl_val is None else f"{pnl_val * 100:+.1f}%"
        lines.append(f"<code>{esc(p['mint'])}</code>\n  {p['status']} | PnL {pn} | peak {p['peak'] * 100:+.0f}% "
                     f"| {age}m | {p['entry_lamports'] / 1e9:.3f} SOL in")
        rows.append([Btn(f"Sell {short(p['mint'])}", callback_data=f"s:{p['mint']}")])
    rows.append([Btn("⬅️ Menu", callback_data="m:menu")])
    return "\n\n".join(lines), Markup(rows)

async def reply(update: Update, text, markup=None):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except BadRequest:
            pass  # "message is not modified"
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)

def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u, ch = update.effective_user, update.effective_chat
        if not u or u.id not in ADMINS or not ch or ch.type != "private":
            if update.callback_query:
                await update.callback_query.answer("Not authorized", show_alert=True)
            return
        return await fn(update, ctx)
    return wrapper


# ---------------- handlers ----------------
@admin_only
async def cmd_menu(update, ctx):
    core = ctx.application.bot_data["core"]
    await reply(update, await status_text(core), menu_markup(core))

@admin_only
async def cmd_set(update, ctx):
    core = ctx.application.bot_data["core"]
    if len(ctx.args) != 2:
        await reply(update, settings_text(core), menu_markup(core)); return
    key, raw = ctx.args[0].lower(), ctx.args[1]
    if key not in EDITABLE:
        await reply(update, f"Unknown or locked setting <code>{esc(key)}</code>.\n\n" + settings_text(core)); return
    try:
        val = parse_value(key, raw)
    except ValueError as e:
        await reply(update, f"Bad value for <code>{key}</code>: {esc(e)}"); return
    old = getattr(core.CFG, key)
    setattr(core.CFG, key, val)
    save_overrides(core)
    await reply(update, f"<code>{key}</code>: {fmt_val(key, old)} → <b>{fmt_val(key, val)}</b>")

@admin_only
async def cmd_buy(update, ctx):
    core = ctx.application.bot_data["core"]
    if not ctx.args or not core.valid_mint(ctx.args[0]):
        await reply(update, "Usage: <code>/buy &lt;token mint address&gt;</code>"); return
    mint = ctx.args[0]
    core.SEEN.add(mint)
    await reply(update, f"Running safety checks on <code>{esc(mint)}</code>…")
    asyncio.create_task(core.handle_signal(mint))   # same checks and caps as a group signal

@admin_only
async def on_button(update, ctx):
    core = ctx.application.bot_data["core"]
    d = update.callback_query.data

    if d in ("m:menu", "m:status"):
        await reply(update, await status_text(core), menu_markup(core))
    elif d == "m:pos":
        text, mk = await positions_view(core)
        await reply(update, text, mk)
    elif d == "m:pause":
        core.KILL_FILE.touch()
        await reply(update, "⏸ New buys paused. Open positions are still managed.\n\n" + await status_text(core), menu_markup(core))
    elif d == "m:resume":
        core.KILL_FILE.unlink(missing_ok=True)
        await reply(update, "▶️ Buying resumed.\n\n" + await status_text(core), menu_markup(core))
    elif d == "m:set":
        await reply(update, settings_text(core), Markup([[Btn("⬅️ Menu", callback_data="m:menu")]]))
    elif d == "m:sellall":
        await reply(update, "Sell <b>all</b> open positions at market?",
                    Markup([[Btn("✅ Yes, sell all", callback_data="csa"), Btn("Cancel", callback_data="m:menu")]]))
    elif d == "csa":
        n = 0
        for mint, p in list(core.POS.items()):
            if p["status"] in ("open", "stuck"):
                asyncio.create_task(core.close_position(mint, "manual_sell_all", 0.0)); n += 1
        await reply(update, f"Selling {n} position(s). You'll get a message for each fill.", menu_markup(core))
    elif d.startswith("s:"):
        mint = d[2:]
        await reply(update, f"Sell <code>{esc(mint)}</code> now?",
                    Markup([[Btn("✅ Confirm sell", callback_data=f"cs:{mint}"), Btn("Cancel", callback_data="m:pos")]]))
    elif d.startswith("cs:"):
        mint = d[3:]
        if mint in core.POS and core.POS[mint]["status"] in ("open", "stuck"):
            asyncio.create_task(core.close_position(mint, "manual", 0.0))
            await reply(update, f"Selling <code>{esc(mint)}</code>… you'll get a message when it fills.", menu_markup(core))
        else:
            await reply(update, "That position is already closed.", menu_markup(core))

async def on_error(update, ctx):
    log.warning("control bot error: %s", ctx.error)


# ---------------- entry point ----------------
async def start_control(core):
    """Start the control bot in the running event loop and hook up push alerts."""
    token = os.environ["CONTROL_BOT_TOKEN"]
    ADMINS.update(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
    if not ADMINS:
        raise RuntimeError("Set ADMIN_IDS, otherwise nobody could use (or be protected on) the control bot")

    load_overrides(core)
    app = Application.builder().token(token).build()
    app.bot_data["core"] = core
    app.add_handler(CommandHandler(["start", "menu", "status"], cmd_menu))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    async def notifier(text):
        for uid in ADMINS:
            try:
                await app.bot.send_message(uid, text, parse_mode="HTML")
            except Exception as e:
                log.warning("alert to %s failed (did they /start the bot?): %s", uid, e)

    core.NOTIFIER = notifier
    await notifier("🤖 Bot started. /menu for the control panel.")
    return app
