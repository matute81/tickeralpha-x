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

## Web app

GitHub Pages cannot run the generator (it would expose API keys). The public site at `docs/` is a **read-only** view of the latest packs. Live generate/edit still runs locally:

```text
python web/server.py
```

Then go to [http://127.0.0.1:8787](http://127.0.0.1:8787). Three buttons map to three streams:

- Morning → Stream 1 (US data, economy, politics, news)
- Close → Stream 2 (3 ideas plus a long-form previous-session recap, 10–12 paragraph points)
- Sunday → Stream 3 (what to look for next week)

The page has three columns (Morning / Close / Sunday). Click a session card (or its pen) to edit that stream’s **STREAM-SPECIFIC FOCUS** only (`prompts/focus-morning.md`, and so on). The **Prompt text** card on the right edits the full shell, [prompts/x_ideas.md](prompts/x_ideas.md). Quality context under that card is saved to `prompts/context.json` and injected as few-shot calibration (not facts to copy). Refresh a column or **Refresh all sessions**. Copy pastes the post body. Hover **Reference** for sources. Numbers still come from FMP. Generation uses `CURSOR_API_KEY` when set, otherwise Anthropic or OpenAI.

If the LLM key is missing or the model call fails, the app shows **AI is not available** and does not fall back to Python drafts. There is no 280-character cap.

## GitHub Actions

Daily generation is [`.github/workflows/daily-briefs.yml`](.github/workflows/daily-briefs.yml). It runs every day at **08:00 Hong Kong time** and generates Morning, Close, and Sunday in one job. Add repository secrets (never commit them):

- `FMP_API_KEY` (required)
- `CURSOR_API_KEY` (preferred), or `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`

Generation prefers Cursor (`cursor-sdk`, model `composer-2.5`) when `CURSOR_API_KEY` is set. After a run, drafts land in `drafts/` and the Pages payload in `docs/latest.json`.

## Output

- Morning: `drafts/YYYY-MM-DD-am.md`
- Close: `drafts/YYYY-MM-DD.md`
- Sunday: `drafts/YYYY-MM-DD-week-ahead.md`

Templates live in `drafts/`. Skip any slot the skill marks empty.
