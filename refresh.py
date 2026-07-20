#!/usr/bin/env python3
"""
Model Standings — standalone refresh script.

Rebuilds docs/index.html from three public, unauthenticated sources:
  - arena.ai leaderboards (chat + coding) — server-rendered HTML, parsed directly.
    Arena's own "Price $/M" column is used as the vendor list price ("Direct").
  - Azure Retail Prices API — for Microsoft Foundry pricing on OpenAI/GPT models.
  - OpenRouter's public model list API — best-effort matched by normalized name;
    left as "n/a" when no confident match is found (never guessed).

No third-party packages required — Python 3.9+ standard library only.
Anthropic-on-Foundry pricing is set equal to Direct: Microsoft's own docs
confirm Claude billing on Foundry (via "Claude Consumption Units") converts
tokens using Anthropic's own published rate, with no markup by default.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import timezone, datetime

UA = "Mozilla/5.0 (compatible; ModelStandingsBot/1.0; +standalone refresh script)"
TIMEOUT = 20

ARENA_TEXT_URL = "https://arena.ai/leaderboard/text"
ARENA_CODE_URL = "https://arena.ai/leaderboard/code/webdev"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
AZURE_PRICES_URL = "https://prices.azure.com/api/retail/prices"

TOP_N = 25          # how deep to read each leaderboard for the aggregate overlap
TOP_CUT = 10         # how many rows to show per tab


def http_get(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url: str, headers: dict | None = None):
    return json.loads(http_get(url, headers))


ROW_RE = re.compile(r'<tr class="hover:bg-surface-highlight.*?</tr>', re.S)
RANK_RE = re.compile(r'min-w-\[28px\]">(\d+)</span>')
MODEL_RE = re.compile(r'title="([^"]+)"')
ORG_RE = re.compile(r'text-text-secondary truncate text-xs">([^<]+)</span>')
# CI is rendered as "\xb17" on the chat board but "+17/-17" on the coding board —
# capture the raw CI text and pull the first number out of it rather than assuming a format.
SCORE_RE = re.compile(r'<span class="body-sm">(\d+)</span><span class="text-text-tertiary body-xs">([^<]*)</span>')
PRICE_RE = re.compile(r'text-sm">\$([\d.]+)<!-- --> / <!-- -->\$([\d.]+)</span>')


def parse_leaderboard(html: str, limit: int = TOP_N) -> list[dict]:
    rows = []
    for raw in ROW_RE.findall(html):
        rank_m = RANK_RE.search(raw)
        model_m = MODEL_RE.search(raw)
        org_m = ORG_RE.search(raw)
        score_m = SCORE_RE.search(raw)
        price_m = PRICE_RE.search(raw)
        if not (rank_m and model_m and score_m):
            continue
        org_full = org_m.group(1) if org_m else "Unknown"
        org = org_full.split("\xb7")[0].strip() if "\xb7" in org_full else org_full.strip()
        ci_digits = re.search(r"\d+", score_m.group(2))
        rows.append({
            "rank": int(rank_m.group(1)),
            "model": model_m.group(1),
            "org": org,
            "score": score_m.group(1),
            "ci": ci_digits.group(0) if ci_digits else "?",
            "price_in": float(price_m.group(1)) if price_m else None,
            "price_out": float(price_m.group(2)) if price_m else None,
        })
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r["rank"])
    return rows


def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[-_. ]", "", s)
    s = re.sub(r"(thinking|xhigh|high|medium|low|preview|max|opt)$", "", s)
    return s


def build_openrouter_index() -> dict:
    """Best-effort id -> (prompt_per_m, completion_per_m). Never guessed —
    entries only exist where OpenRouter actually returned a matching model."""
    try:
        data = http_get_json(OPENROUTER_MODELS_URL)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[warn] OpenRouter fetch failed: {e}", file=sys.stderr)
        return {}
    index = {}
    for m in data.get("data", []):
        mid = m.get("id", "")
        slug = mid.split("/")[-1] if "/" in mid else mid
        pricing = m.get("pricing", {})
        try:
            prompt = float(pricing.get("prompt", "0")) * 1_000_000
            completion = float(pricing.get("completion", "0")) * 1_000_000
        except (TypeError, ValueError):
            continue
        if prompt == 0 and completion == 0:
            continue
        index[normalize(slug)] = (round(prompt, 4), round(completion, 4))
    return index


def azure_gpt_price(model_id: str) -> tuple[float, float] | None:
    """Best-effort lookup on Azure's public retail pricing API for OpenAI/GPT
    models hosted on Microsoft Foundry. Returns None (never a guess) if no
    clean Global-Standard input/output pair is found."""
    term = re.sub(r"^gpt-?", "", model_id)
    term = re.sub(r"-(xhigh|high|medium|low)$", "", term)
    term = term.replace("-", " ")  # Azure skuNames are space-separated ("5.6 sol"), not hyphenated
    if not term:
        return None
    try:
        body = http_get_json(
            AZURE_PRICES_URL
            + "?$filter="
            + urllib.parse.quote(f"contains(skuName,'{term}') and armRegionName eq 'eastus2'")
        )
    except Exception as e:
        print(f"[warn] Azure pricing lookup failed for {model_id}: {e}", file=sys.stderr)
        return None
    # Prefer the short-context ("ShortCo") Global Standard tier — it's the
    # representative <=200k-token rate. Fall back to long-context only if
    # short-context isn't listed for this SKU.
    candidates = {"short": {"inp": None, "out": None}, "long": {"inp": None, "out": None}}
    for item in body.get("Items", []):
        sku = item.get("skuName", "")
        if "Std Gl" not in sku or item.get("unitOfMeasure") != "1M" or "Cd" in sku:
            continue
        bucket = "short" if "ShortCo" in sku else ("long" if "LongCo" in sku else None)
        if bucket is None:
            continue
        if re.search(r"\bInp\b", sku) and candidates[bucket]["inp"] is None:
            candidates[bucket]["inp"] = item["retailPrice"]
        elif re.search(r"\bOpt\b", sku) and candidates[bucket]["out"] is None:
            candidates[bucket]["out"] = item["retailPrice"]
    for bucket in ("short", "long"):
        inp, out = candidates[bucket]["inp"], candidates[bucket]["out"]
        if inp is not None and out is not None:
            return (inp, out)
    return None


def compute_aggregate(chat_rows: list[dict], code_rows: list[dict]) -> list[dict]:
    chat_rank = {r["model"]: r["rank"] for r in chat_rows}
    code_rank = {r["model"]: r["rank"] for r in code_rows}
    by_model = {r["model"]: r for r in chat_rows}
    by_model.update({r["model"]: r for r in code_rows if r["model"] not in by_model})

    overlap = set(chat_rank) & set(code_rank)
    scored = []
    for model in overlap:
        cr, kr = chat_rank[model], code_rank[model]
        scored.append({
            "model": model,
            "org": by_model[model]["org"],
            "chat": cr,
            "code": kr,
            "avg": (cr + kr) / 2,
            "best": min(cr, kr),
            "price_in": by_model[model]["price_in"],
            "price_out": by_model[model]["price_out"],
        })
    scored.sort(key=lambda x: (x["avg"], x["best"]))
    return scored[:TOP_CUT]


def price_cell_data(row: dict, or_index: dict) -> dict:
    direct = [row["price_in"], row["price_out"]] if row["price_in"] is not None else None

    if row["org"] == "Anthropic":
        foundry = direct  # CCU billing == Anthropic's direct rate, per Microsoft's own docs
    elif row["model"].startswith("gpt-") or row["model"].startswith("gpt5") or "gpt" in row["model"][:4]:
        az = azure_gpt_price(row["model"])
        foundry = list(az) if az else "hosted"
    else:
        foundry = "unhosted"

    key = normalize(row["model"])
    # try a couple of normalization variants against the OpenRouter index
    or_price = or_index.get(key)
    if or_price is None:
        # try stripping a trailing version-ish suffix once more
        or_price = or_index.get(normalize(re.sub(r"[\d.]+$", "", row["model"])))
    orv = list(or_price) if or_price else None

    return {"direct": direct, "foundry": foundry, "or": orv}


def main():
    print("Fetching chat leaderboard...")
    chat_html = http_get(ARENA_TEXT_URL)
    chat_rows = parse_leaderboard(chat_html)
    print(f"  {len(chat_rows)} rows")

    print("Fetching coding leaderboard...")
    code_html = http_get(ARENA_CODE_URL)
    code_rows = parse_leaderboard(code_html)
    print(f"  {len(code_rows)} rows")

    if len(chat_rows) < TOP_CUT or len(code_rows) < TOP_CUT:
        print("[error] Leaderboard parsing returned too few rows — Arena's page "
              "structure may have changed. Aborting without publishing a broken page.",
              file=sys.stderr)
        sys.exit(1)

    print("Fetching OpenRouter pricing index...")
    or_index = build_openrouter_index()
    print(f"  {len(or_index)} priced models indexed")

    aggregate = compute_aggregate(chat_rows, code_rows)

    # Build PRICE map + tagged row lists keyed by a stable per-model key
    price_map = {}

    def key_for(model_id: str) -> str:
        return re.sub(r"[^a-z0-9]", "", model_id.lower())

    def tag_rows(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            k = key_for(r["model"])
            if k not in price_map:
                price_map[k] = price_cell_data(r, or_index)
            out.append({**r, "key": k})
        return out

    chat_top = tag_rows(chat_rows[:TOP_CUT])
    code_top = tag_rows(code_rows[:TOP_CUT])
    agg_top = []
    for a in aggregate:
        k = key_for(a["model"])
        if k not in price_map:
            price_map[k] = price_cell_data(
                {"org": a["org"], "model": a["model"], "price_in": a["price_in"], "price_out": a["price_out"]},
                or_index,
            )
        agg_top.append({**a, "key": k})

    snapshot = datetime.now(timezone.utc).strftime("%d %b %Y")
    render(snapshot, agg_top, chat_top, code_top, price_map)
    print("Wrote docs/index.html")


def render(snapshot, agg, chat, code, price_map):
    from pathlib import Path
    template_path = Path(__file__).parent / "template.html"
    template = template_path.read_text(encoding="utf-8")

    def money(v):
        return None if v is None else round(v, 4)

    def clean_price(p):
        d = p["direct"]
        f = p["foundry"]
        o = p["or"]
        return {
            "direct": [money(d[0]), money(d[1])] if d else None,
            "foundry": ([money(f[0]), money(f[1])] if isinstance(f, list) else f),
            "or": [money(o[0]), money(o[1])] if o else None,
        }

    price_json = json.dumps({k: clean_price(v) for k, v in price_map.items()})

    def row_json(rows, kind):
        out = []
        for i, r in enumerate(rows):
            entry = {
                "name": r["model"],
                "org": r["org"],
                "key": r["key"],
                "gold": i == 0,
            }
            if kind == "agg":
                entry["chat"] = r["chat"]
                entry["code"] = r["code"]
                entry["avg"] = f'{r["avg"]:.1f}'
            else:
                entry["score"] = r["score"]
                entry["pm"] = r["ci"]
            out.append(entry)
        return json.dumps(out)

    html = (template
            .replace("__SNAPSHOT__", snapshot)
            .replace("__PRICE_JSON__", price_json)
            .replace("__AGG_JSON__", row_json(agg, "agg"))
            .replace("__CHAT_JSON__", row_json(chat, "chat"))
            .replace("__CODE_JSON__", row_json(code, "code")))

    from pathlib import Path as P
    out_dir = P(__file__).parent / "docs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
