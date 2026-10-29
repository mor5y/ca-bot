# Telegram CA bot (Solana / Jupiter)

Reads token addresses from Telegram groups, runs safety checks, buys, and exits at
take-profit / trailing stop / hard stop. Controlled and monitored from a Telegram bot.

## Quick start
1. Open `.env` and fill in at least: TG_API_ID, TG_API_HASH, TG_GROUPS,
   CONTROL_BOT_TOKEN, ADMIN_IDS. Leave DRY_RUN=1.
2. Windows: double-click `run.bat`  (or Mac/Linux: `./run.sh`)
3. First run asks for your phone number and the login code (Telegram user login).
4. Message your control bot /start, then use /menu.

## Active defaults
- No stop-loss.
- Trailing profit activates above +23%.
- Main take-profit is +50%.
- Max positions: 20.
- Daily buys: unlimited (effectively disabled cap).
- Minimum pool liquidity: $1000.

## Files
- tg_ca_bot.py   signal reader, safety checks, buying, exits
- tg_control.py  Telegram control panel (menu, positions, /set, /buy, alerts)
- .env           all settings (never share it)
- positions.json state, created at runtime (survives restarts)
- settings.json  changes made via /set, created at runtime (overrides .env values)

## Going live
Only after days of clean dry-run results: set DRY_RUN=0 and PRIVATE_KEY (burner wallet,
funded with money you can afford to lose), then restart.

## Controls
- Pause buying: button in /menu, or create an empty file named KILL
- /set take_profit 60%    /set buy_sol 0.1    /buy <mint>
