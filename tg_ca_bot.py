"""
Telegram contract-address bot (Solana / Jupiter). DRY_RUN is ON by default.

Flow: read group messages -> extract Solana mint addresses -> safety checks ->
buy with Jupiter -> monitor executable value -> sell at take-profit or stop.

pip install telethon httpx solders

Env vars (put them in your shell or export them):
  TG_API_ID, TG_API_HASH     from https://my.telegram.org  (uses YOUR user account)
  TG_GROUPS                  comma-separated group @usernames or numeric ids
  TG_ALLOWED_SENDERS         optional comma-separated sender ids (strongly advised)
  RPC_URL                    Solana RPC (a paid/private one is much better than public)
  PRIVATE_KEY                base58 secret key of a BURNER wallet (live mode only)
  JUP_BASE                   default https://lite-api.jup.ag ; JUP_API_KEY if using api.jup.ag
  DRY_RUN=0                  to trade for real (default 1 = paper trading on real quotes)

NOT tested against live APIs. Jupiter's API changes often (Swap V2 is live at
api.jup.ag/swap/v2); this uses the /swap/v1 quote+swap flow, isolated in
quote() and execute_swap() so you can swap it out.
"""
import asyncio, base64, json, logging, os, re, sqlite3, sys, time
from pathlib import Path

import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv(override=True)  # .env next to this file wins over stale shell variables

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("cabot")

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STATE_FILE = Path("positions.json")
KILL_FILE = Path("KILL")  # `touch KILL` -> stop opening NEW positions (exits keep running)
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def env(name, default=""): return os.getenv(name, default)
def envf(name, default): return float(os.getenv(name, default))


class CFG:
    dry_run = env("DRY_RUN", "1") != "0"
    api_id = int(env("TG_API_ID", "0"))
    api_hash = env("TG_API_HASH")
    groups = [int(g) if g.lstrip("-").isdigit() else g for g in env("TG_GROUPS").split(",") if g]
    allowed_senders = {int(s) for s in env("TG_ALLOWED_SENDERS").split(",") if s}
    rpc_url = env("RPC_URL", "https://api.mainnet-beta.solana.com")
    jup_base = env("JUP_BASE", "https://lite-api.jup.ag")
    jup_key = env("JUP_API_KEY")
    private_key = env("PRIVATE_KEY")

    buy_sol = envf("BUY_SOL", "0.05")              # SOL per trade
    max_positions = int(envf("MAX_POSITIONS", "20"))
    max_parallel_buys = int(envf("MAX_PARALLEL_BUYS", "3"))
    max_buys_per_day = int(envf("MAX_BUYS_PER_DAY", "999999"))
    buy_slip_bps = int(envf("BUY_SLIP_BPS", "1000"))   # 10%
    sell_slip_bps = int(envf("SELL_SLIP_BPS", "1500"))  # 15%, widened on retries
    buy_retry_attempts = int(envf("BUY_RETRY_ATTEMPTS", "3"))
    buy_retry_delay = envf("BUY_RETRY_DELAY", "0.5")

    take_profit = envf("TAKE_PROFIT", "0.50")      # sell everything at +50%
    trail_activate = envf("TRAIL_ACTIVATE", "0.23")  # trailing profit starts once price is +23%
    trail_pct = envf("TRAIL_PCT", "0.20")          # sells if value falls 20% from its peak
    trail_floor = envf("TRAIL_FLOOR", "0.20")      # keep the trailing sell at minimum +20% profit
    initial_stop = envf("INITIAL_STOP", "0.00")    # stop-loss disabled

    min_liquidity_usd = envf("MIN_LIQ_USD", "1000")
    max_roundtrip_loss = envf("MAX_ROUNDTRIP_LOSS", "0.20")  # reject if buy->sell quote loses >20%
    reject_freeze_authority = env("REJECT_FREEZE_AUTH", "1") == "1"
    reject_mint_authority = env("REJECT_MINT_AUTH", "0") == "1"
    poll_secs = envf("POLL_SECS", "0.02")


http: httpx.AsyncClient
kp: Keypair | None = None
POS: dict[str, dict] = {}
SEEN: set[str] = set()
BUY_TIMES: list[float] = []
IGNORE = {SOL, USDC, USDT}
CLOSING: set[str] = set()
BUY_SEMAPHORE = asyncio.Semaphore(CFG.max_parallel_buys)
NOTIFIER = None  # set by tg_control.start_control to push alerts to your Telegram


async def notify(msg: str):
    if NOTIFIER:
        try: await NOTIFIER(msg)
        except Exception as e: log.warning("notify failed: %s", e)


# ---------------- persistence ----------------
def save_state():
    STATE_FILE.write_text(json.dumps({"positions": POS, "buy_times": BUY_TIMES, "seen": list(SEEN)}))

def load_state():
    if STATE_FILE.exists():
        d = json.loads(STATE_FILE.read_text())
        POS.update(d.get("positions", {})); BUY_TIMES[:] = d.get("buy_times", [])
        SEEN.update(d.get("seen", []))


def clear_telethon_session_files():
    for name in ("tg_session.session", "tg_session.session-journal", "tg_session.session-shm", "tg_session.session-wal"):
        p = Path(name)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                log.warning("Could not remove stale Telethon session file %s: %s", p, e)


def clear_runtime_state_files():
    for name in ("positions.json", "settings.json"):
        p = Path(name)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                log.warning("Could not remove stale runtime state %s: %s", p, e)


# ---------------- chain / API helpers ----------------
async def rpc(method, params):
    r = await http.post(CFG.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "error" in j: raise RuntimeError(j["error"])
    return j["result"]

def jup_headers():
    return {"x-api-key": CFG.jup_key} if CFG.jup_key else {}

async def quote(inp, out, amount, slip_bps):
    r = await http.get(f"{CFG.jup_base}/swap/v1/quote", headers=jup_headers(), timeout=15,
                       params={"inputMint": inp, "outputMint": out, "amount": int(amount),
                               "slippageBps": slip_bps})
    r.raise_for_status()
    return r.json()

async def execute_swap(q):
    """Build, sign, send and confirm a Jupiter swap. Returns signature."""
    last_err = None
    for attempt in range(1, CFG.buy_retry_attempts + 1):
        try:
            r = await http.post(f"{CFG.jup_base}/swap/v1/swap", headers=jup_headers(), timeout=20, json={
                "quoteResponse": q, "userPublicKey": str(kp.pubkey()), "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": 1_000_000, "priorityLevel": "high"}},
            })
            r.raise_for_status()
            tx = VersionedTransaction.from_bytes(base64.b64decode(r.json()["swapTransaction"]))
            signed = VersionedTransaction(tx.message, [kp])
            sig = await rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                {"encoding": "base64", "maxRetries": 3}])
            for _ in range(45):
                await asyncio.sleep(1)
                res = await rpc("getSignatureStatuses", [[sig]])
                st = res["value"][0]
                if st:
                    if st.get("err"): raise RuntimeError(f"tx failed: {st['err']}")
                    if st.get("confirmationStatus") in ("confirmed", "finalized"): return sig
            raise TimeoutError(f"tx not confirmed: {sig}")
        except Exception as e:
            last_err = e
            if attempt >= CFG.buy_retry_attempts:
                raise
            log.warning("swap attempt %d/%d failed for %s: %s; retrying", attempt, CFG.buy_retry_attempts, q.get("outMint"), e)
            await asyncio.sleep(CFG.buy_retry_delay * attempt)
    raise last_err

async def token_balance(mint):
    res = await rpc("getTokenAccountsByOwner", [str(kp.pubkey()), {"mint": mint},
                                                 {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])


# ---------------- safety checks ----------------
async def safety_checks(mint) -> str | None:
    """Return a rejection reason, or None if OK."""
    # 1) liquidity on DexScreener
    r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=15)
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs: return "no solana pair on dexscreener"
    liq = max((p.get("liquidity") or {}).get("usd", 0) or 0 for p in pairs)
    if liq < CFG.min_liquidity_usd: return f"liquidity ${liq:,.0f} < min"
    # 2) mint / freeze authority
    info = await rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    parsed = ((info.get("value") or {}).get("data") or {}).get("parsed", {}).get("info")
    if not parsed: return "not a token mint"
    if CFG.reject_freeze_authority and parsed.get("freezeAuthority"): return "freeze authority set"
    if CFG.reject_mint_authority and parsed.get("mintAuthority"): return "mint authority set"
    return None


# ---------------- trading ----------------
async def open_position(mint):
    async with BUY_SEMAPHORE:
        lamports = int(CFG.buy_sol * 1e9)
        q = await quote(SOL, mint, lamports, CFG.buy_slip_bps)
        est_out = int(q["outAmount"])
        # Round-trip check: can we sell it back, and at what loss? Catches many honeypots / thin pools.
        try:
            back = int((await quote(mint, SOL, est_out, CFG.sell_slip_bps))["outAmount"])
        except Exception as e:
            log.info("REJECT %s: cannot quote a sell (%s)", mint, e)
            await notify(f"🚫 Rejected <code>{mint}</code>: cannot quote a sell")
            return
        if back / lamports < 1 - CFG.max_roundtrip_loss:
            log.info("REJECT %s: round-trip returns only %.0f%%", mint, 100 * back / lamports)
            await notify(f"🚫 Rejected <code>{mint}</code>: round-trip returns only {100 * back / lamports:.0f}%")
            return

        if CFG.dry_run:
            amount, sig = est_out, "dry-run"
        else:
            sig = await execute_swap(q)
            amount = await token_balance(mint)
            if amount == 0: log.error("bought but balance is 0 for %s (%s)", mint, sig); return

    POS[mint] = {"mint": mint, "entry_lamports": lamports, "amount": amount, "peak": 0.0,
                 "opened": time.time(), "status": "open", "buy_sig": sig}
    BUY_TIMES.append(time.time()); save_state()
    log.info("BOUGHT %s | %.3f SOL | sig=%s%s", mint, CFG.buy_sol, sig, " (DRY)" if CFG.dry_run else "")
    await notify(f"✅ BOUGHT <code>{mint}</code>\n{CFG.buy_sol} SOL{' (DRY)' if CFG.dry_run else ''}")
    asyncio.create_task(manage(mint))

async def close_position(mint, reason, pnl):
    """Guarded so the monitor loop and a manual sell can't both fire."""
    if mint in CLOSING or POS[mint]["status"] == "closed": return
    CLOSING.add(mint)
    try: await _close_position(mint, reason, pnl)
    finally: CLOSING.discard(mint)

async def _close_position(mint, reason, pnl):
    p = POS[mint]
    for attempt in range(6):
        try:
            amount = p["amount"] if CFG.dry_run else await token_balance(mint)
            if amount == 0:
                p["status"] = "closed"; save_state(); return
            slip = min(CFG.sell_slip_bps + attempt * 1000, 5000)  # widen slippage on retries
            q = await quote(mint, SOL, amount, slip)
            sig = "dry-run" if CFG.dry_run else await execute_swap(q)
            out_sol = int(q["outAmount"]) / 1e9
            p.update(status="closed", exit_reason=reason, exit_sol=out_sol, exit_sig=sig, closed=time.time())
            save_state()
            pnl = out_sol * 1e9 / p["entry_lamports"] - 1   # realized, from the actual exit quote
            log.info("SOLD %s | %s | pnl %.1f%% | got %.4f SOL | sig=%s", mint, reason, pnl * 100, out_sol, sig)
            await notify(f"💰 SOLD <code>{mint}</code>\n{reason} | PnL {pnl * 100:+.1f}% | got {out_sol:.4f} SOL"
                         f"{' (DRY)' if CFG.dry_run else ''}")
            return
        except Exception as e:
            log.warning("sell attempt %d for %s failed: %s", attempt + 1, mint, e)
            await asyncio.sleep(0.02)
    log.error("!!! COULD NOT SELL %s after retries -- intervene manually", mint)
    await notify(f"🚨 COULD NOT SELL <code>{mint}</code> after retries. Intervene manually.")
    p["status"] = "stuck"; save_state()

async def manage(mint):
    """Poll the *executable* value (quote to sell everything) and apply exit rules."""
    p = POS[mint]
    while p["status"] == "open":
        await asyncio.sleep(max(CFG.poll_secs, 0.02))
        try:
            q = await quote(mint, SOL, p["amount"], CFG.sell_slip_bps)
        except Exception as e:
            log.debug("quote fail %s: %s", mint, e); continue
        pnl = int(q["outAmount"]) / p["entry_lamports"] - 1
        p["peak"] = max(p["peak"], pnl)
        reason = None
        if pnl >= CFG.take_profit:
            reason = "take_profit"
        elif p["peak"] >= CFG.trail_activate:
            stop = max((1 + p["peak"]) * (1 - CFG.trail_pct) - 1, CFG.trail_floor)
            if pnl <= stop: reason = f"trailing_stop(peak {p['peak']*100:.0f}%)"
        if reason:
            await close_position(mint, reason, pnl)

async def handle_signal(mint):
    try:
        if mint in POS and POS[mint]["status"] in ("open", "stuck"): return
        if KILL_FILE.exists():
            log.info("KILL file present, ignoring %s", mint)
            await notify(f"⏸ Paused, ignored <code>{mint}</code>")
            return
        open_n = sum(1 for p in POS.values() if p["status"] in ("open", "stuck"))
        if open_n >= CFG.max_positions: log.info("max positions reached, skip %s", mint); return
        if len([t for t in BUY_TIMES if t > time.time() - 86400]) >= CFG.max_buys_per_day:
            log.info("daily buy cap reached, skip %s", mint); return
        why = await safety_checks(mint)
        if why:
            log.info("REJECT %s: %s", mint, why)
            await notify(f"🚫 Rejected <code>{mint}</code>: {why}")
            return
        await open_position(mint)
    except Exception as e:
        log.exception("handle_signal %s failed: %s", mint, e)


# ---------------- telegram ----------------
def valid_mint(s):
    try: Pubkey.from_string(s); return True
    except Exception: return False

def build_client():
    client = TelegramClient("tg_session", CFG.api_id, CFG.api_hash)

    @client.on(events.NewMessage(chats=CFG.groups))
    async def on_msg(ev):
        if CFG.allowed_senders and ev.sender_id not in CFG.allowed_senders: return
        for addr in set(ADDR_RE.findall(ev.raw_text or "")):
            if addr in SEEN or addr in IGNORE or not valid_mint(addr): continue
            SEEN.add(addr); save_state()
            log.info("signal: %s", addr)
            await notify(f"👀 Signal: <code>{addr}</code>")
            asyncio.create_task(handle_signal(addr))

    return client


async def start_client_with_recovery():
    client = build_client()
    try:
        await client.start()
        return client
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "database is locked" not in msg and "locked" not in msg:
            raise
        log.warning("Telethon session DB is locked; clearing stale session and retrying.")
        clear_telethon_session_files()
        client = build_client()
        await client.start()
        return client


async def main():
    global http, kp
    http = httpx.AsyncClient()
    clear_runtime_state_files()
    if not CFG.api_id or not CFG.api_hash or not CFG.groups:
        raise SystemExit("Fill TG_API_ID, TG_API_HASH and TG_GROUPS in the .env file first.")
    if not CFG.dry_run and not CFG.private_key:
        raise SystemExit("DRY_RUN=0 needs PRIVATE_KEY in .env")
    if not CFG.dry_run:
        kp = Keypair.from_base58_string(CFG.private_key)
        IGNORE.add(str(kp.pubkey()))
    load_state()
    log.info("start | dry_run=%s | TP %.0f%% | trail %.0f%% after +%.0f%% | initial stop %.0f%% | parallel buys=%d | buy retries=%d",
             CFG.dry_run, CFG.take_profit * 100, CFG.trail_pct * 100, CFG.trail_activate * 100, CFG.initial_stop * 100,
             CFG.max_parallel_buys, CFG.buy_retry_attempts)

    client = await start_client_with_recovery()

    if env("CONTROL_BOT_TOKEN"):
        from tg_control import start_control
        await start_control(sys.modules[__name__])
    for mint, p in POS.items():          # resume monitoring after a restart
        if p["status"] == "open": asyncio.create_task(manage(mint))
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
