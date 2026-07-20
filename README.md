# Model Standings — standalone

A self-contained, dependency-free version of the Model Standings page. No
Claude account, no API keys, no npm/pip packages — just Python 3's standard
library and three public, unauthenticated data sources:

- **arena.ai** leaderboards (chat + coding) — read directly as server-rendered
  HTML. Arena's own "Price $/M" column is used as each model's list price.
- **Azure Retail Prices API** — for Microsoft Foundry pricing on GPT models
  (public, no auth: `prices.azure.com/api/retail/prices`).
- **OpenRouter's model list API** — best-effort matched by normalized model
  name; shown as `n/a` rather than guessed when no confident match exists.

Claude models on Microsoft Foundry are shown at the same price as Direct:
Microsoft's own docs confirm Foundry's "Claude Consumption Unit" billing
converts tokens using Anthropic's own published rate, with no markup by
default.

## Run it once, locally

```bash
python3 refresh.py
```

Writes `docs/index.html`. Open that file directly in a browser — it's fully
self-contained (inline CSS/JS, no external requests once loaded).

## Run it on a schedule (GitHub Actions + GitHub Pages)

This repo already includes `.github/workflows/refresh.yml`, which runs the
script weekly and commits the result. To turn that into a live URL:

1. Push this repo to GitHub.
2. **Settings → Pages** → set source to **Deploy from a branch**, branch
   `main`, folder `/docs`. GitHub will give you a URL like
   `https://<you>.github.io/<repo>/`.
3. That's it — no secrets to configure. The workflow uses the default
   `GITHUB_TOKEN`, which already has permission to push to the repo.
4. To change the schedule, edit the `cron:` line in the workflow (GitHub
   Actions' minimum interval is hourly; the default here is weekly, Monday
   07:00 UTC).
5. To run it immediately instead of waiting for the schedule: **Actions** tab
   → **Refresh Model Standings** → **Run workflow**.

## Known limitations (read before trusting a number)

- **OpenRouter matching is best-effort.** Model names aren't standardized
  across sources, so the script normalizes and compares strings rather than
  using a hand-maintained mapping. A model can show `n/a` in the OpenRouter
  column even if OpenRouter does list it, if the naming doesn't line up
  cleanly. This is a deliberate fail-safe — the script never guesses a price,
  it either finds a confident match or leaves the cell blank.
- **Foundry pricing for non-Anthropic, non-GPT models is "not hosted"** unless
  you extend `price_cell_data()` in `refresh.py` — the Azure Retail Prices API
  only covers models Microsoft sells directly (currently: OpenAI/GPT, and
  Anthropic via the CCU-parity rule above).
- **If Arena changes its page markup**, the regex-based parser in
  `parse_leaderboard()` will return too few rows, and the script exits
  *without* overwriting `docs/index.html` — so a broken scrape never
  publishes a broken page. Check the Actions run logs if the site stops
  updating; the fix is almost always a one-line regex update once you know
  what changed (search the current HTML for the model's ID string, the same
  way this script's selectors were originally derived).
- **Rankings and prices are Arena's and Azure's, not independently audited.**
  Treat this as a convenience view, not a source of truth for a contract or
  invoice.
