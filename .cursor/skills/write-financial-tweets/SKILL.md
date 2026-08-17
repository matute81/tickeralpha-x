---
name: write-financial-tweets
description: >-
  Drafts TickerAlpha daily finance tweets from FMP data: session recap,
  next-session preview, story of the day, movers, cross-asset tape, Sunday
  week-ahead, Friday wrap, and conditional print/earnings/breadth/overnight
  posts. Use when writing tweets, X posts, market briefs, or files under drafts/.
---

# Write financial tweets

Draft long-form X posts for [@tickeralphai](https://x.com/tickeralphai). Do not post to X. Do not pitch the product.

Read before writing (repo root):

- `AGENTS.md`
- `brand/voice.md`
- `brand/compliance.md`
- `content/cadence.md`
- `content/examples.md` for voice only — never copy sample numbers into a live draft

## 1. Fetch data

From the repo root:

```text
python scripts/fetch_fmp_brief.py --mode morning
python scripts/fetch_fmp_brief.py --mode close
python scripts/fetch_fmp_brief.py --mode week-ahead
```

Use `--date YYYY-MM-DD` when backfilling. If `FMP_API_KEY` is missing, stop and say so. Do not draft from memory.

Open the JSON the script printed. Treat `errors` as missing endpoints, not as license to guess.

Index quotes may omit `changesPercentage`; use the computed field if present, or `(price - previousClose) / previousClose`. Prefer `liquid_gainers` / `liquid_losers` / `actives` / `mega_caps` for the movers post — raw `gainers` is often illiquid names. Skip those.

If a cross-asset row is an ETF proxy (`USO`, `UUP`, `GLD`, `EWG`), say so (“oil proxy USO”, “dollar proxy UUP”). Do not call USO the WTI spot price.

## 2. Fail closed

- Every number in a post must appear in that JSON (or a follow-up FMP call you actually ran).
- If a close, %, consensus, actual, or prior is absent, omit it. Put `[missing]` in **Source notes**, not a fake figure in the body.
- If a mover has no headline in `news` / earnings fields, say it moved and stop. Do not invent a why. Prefer liquid names (`liquid_gainers`, `liquid_losers`, `mega_caps`, `actives`) over penny `gainers`.
- If an overseas index is missing, skip the overnight slot.

## 3. Choose the pack

| Job | Modes | Output |
|-----|--------|--------|
| Weekday pre-market | `morning` | `drafts/YYYY-MM-DD-am.md` from `drafts/morning_template.md` |
| Weekday after close | `close` | `drafts/YYYY-MM-DD.md` from `drafts/weekday_template.md` |
| Sunday | `week-ahead` | `drafts/YYYY-MM-DD-week-ahead.md` from `drafts/sunday_template.md` |

`YYYY-MM-DD` is the **US session date** in America/New_York (the date traders mean by “today”). Friday morning still uses Friday. Friday close next-session = Monday. Sunday week-ahead covers the coming Mon–Fri.

## 4. Fill slots

Copy the template. For each slot:

- Write the ready-to-post body (long-form; no 280 cap).
- Set suggested ET window.
- Fill source notes (which JSON fields).
- Set **Status:** `ready` or `empty`.

**Always write (weekday):** next session (morning file); recap, story, movers, cross-asset (close file).

**Write only if the tape is real; otherwise Status: empty and one line why:**

- Overnight: at least one overseas index moved in a way that is not noise (use judgment; a 0.1% DAX is empty).
- How to read the print: a high-impact US release is on **today or tomorrow** morning’s calendar (CPI, PPI, PCE, NFP, FOMC, GDP, etc.).
- Data print: that high-impact series has an `actual` in today’s calendar.
- Earnings reaction: a mega-cap (or a cluster) in `earnings_calendar` with actuals, or a mega-cap in gainers/losers with an earnings headline in `news`.
- Breadth: S&P vs Russell (or Nasdaq vs Dow) disagree enough to be the point — not 2bp of noise.
- Engagement: 2–3 times per week, tied to a real calendar fork. Skip most days.
- Friday wrap: Fridays only, after the close pack.

Do not repeat the recap inside story / movers / cross-asset. Each slot owns its facts.

## 5. Voice checks before saving

- Desk briefing. No hype, no hashtags, no buy/sell, no price targets.
- No tickeralpha.ai, heatmap, Congress, or insider plugs.
- Disclaimer line only if the copy could be read as a recommendation.

## 6. Save

Write the markdown file. Do not commit unless asked. Tell the user which slots are `ready` vs `empty` and when to post each ready slot.
