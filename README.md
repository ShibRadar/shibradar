# ShibRadar

Automated Threads account (@ShibRadar) for the SHIB ecosystem: burns, whale
movements, news and market data. Runs on GitHub Actions every 30 minutes,
at zero cost.

```
Ethereum RPC ──> burns + whales ─┐
DexScreener  ──> price / volume ─┼─> filters / dedup ─> Threads API ─> @ShibRadar
Google News  ──> news (RSS)    ──┘
```

## Files
- `src/main.py` — the bot (scanners, filters, posts, Threads publishing)
- `config.json` — thresholds and limits (public, no secrets)
- `data/state.json` — what was already seen/posted (committed by the bot)
- `.github/workflows/bot.yml` — runs every 30 min
- `.github/workflows/refresh-token.yml` — renews the Threads token weekly
- `tools/threads_token.py` — one-time token setup (run locally)
- `docs/` — GitHub Pages site (donations, OAuth return page, privacy)

## Posting policy
Priority: burns > whales > news > market. At most 3 posts per run and, per
run, 1 whale, 2 news and 1 market update. Whale = transfer ≥ 50B SHIB.
Market updates only when price moves ≥ 2% or 24h volume ≥ 50%.
Price predictions and speculative headlines are blocked.

## Going live (one-time setup)

### 1. Meta app
1. <https://developers.facebook.com/apps> → **Create app** → use case
   **Access the Threads API**.
2. Use case → **Permissions**: add `threads_basic` and
   `threads_content_publish`.
3. Use case → **Settings**:
   - Redirect callback URL: `https://shibradar.github.io/shibradar/`
   - Uninstall / delete callback URLs: the same URL
   - Note the **Threads App ID** and **Threads App Secret**.
4. App roles → **Roles** → add the `ShibRadar` Threads account as
   **Threads Tester**. In the Threads app (logged in as ShibRadar):
   Settings → Account → Website permissions → Invites → **Accept**.

The app can stay in development mode: a tester can publish to its own
account, which is all ShibRadar needs. No app review required.

### 2. Token
```
pip install requests
python tools/threads_token.py url          # open link as @ShibRadar, accept
python tools/threads_token.py code <CODE>  # code shown on the site
```
It prints a long-lived token (60 days).

### 3. GitHub
Repository → Settings → Secrets and variables → Actions:
- Secret `THREADS_ACCESS_TOKEN` = token from step 2
- Secret `GH_PAT` = fine-grained personal access token, this repository
  only, permission **Secrets: Read and write** (used to save the renewed
  token every week)
- Variable `DRY_RUN` = `false` (when ready to publish automatically)

### 4. Test
Actions → **ShibRadar Bot** → Run workflow → `dry_run = false` → check the
post on Threads. Then Actions → **Refresh Threads token** → Run workflow,
to confirm the renewal works.

## Local test
```
pip install -r requirements.txt
python -m src.main          # DRY_RUN by default: prints, does not publish
```
Note: a local run updates `data/state.json`; don't commit it.

Never put tokens, App Secret, private keys or seed phrases in this repository.
