# TickerAlpha marketing agent

This repo drafts a daily finance-Twitter feed for [@tickeralphai](https://x.com/tickeralphai). It does not post to X. It does not pitch tickeralpha.ai.

## Cadence

See [content/cadence.md](content/cadence.md). Quiet weekday: five always-on posts. Event days: extra slots only when FMP has a real print, earnings cluster, breadth split, or overnight move. Sunday: week-ahead. Friday close file also gets a week wrap.

## Always

- Read [brand/voice.md](brand/voice.md) and [brand/compliance.md](brand/compliance.md) before writing.
- Use the skill `.cursor/skills/write-financial-tweets/SKILL.md` when drafting tweets or `drafts/` files.
- Numbers come from FMP via `scripts/fetch_fmp_brief.py`. Never invent a close, percent, consensus, actual, or prior.
- If a cross-asset row is `USO`, `UUP`, or `GLD`, call it a proxy, not the spot index or futures.
- No product plugs (heatmap, Congress, insiders, tickeralpha.ai) unless the sentence is weaker without them. In v1, leave the product out.
- Long-form posts. No 280-character cap. Do not turn the recap into five shorter copies of itself.

## Commands

```text
python scripts/fetch_fmp_brief.py --mode morning
python scripts/fetch_fmp_brief.py --mode close
python scripts/fetch_fmp_brief.py --mode week-ahead
```

Requires `FMP_API_KEY` in the environment or in `.env` (gitignored). Copy `.env.example` locally.

## Output

- Morning: `drafts/YYYY-MM-DD-am.md`
- Close: `drafts/YYYY-MM-DD.md`
- Sunday: `drafts/YYYY-MM-DD-week-ahead.md`

Templates live in `drafts/`. Skip any slot the skill marks empty.
