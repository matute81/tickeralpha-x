"""Turn an FMP brief into tweet slots and a drafts/*.md file.

Copy is a desk briefing: rank what matters, write prose, keep every number
traceable to the brief. No calendar dumps.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fetch_fmp_brief import (  # noqa: E402
    FmpClient,
    NY,
    ROOT as REPO_ROOT,
    build_brief,
    default_out,
    load_dotenv,
    ny_today,
)
from llm_ideas import generate_x_ideas, llm_configured, unavailable_message  # noqa: E402

GEO_NEEDLES = (
    "iran", "israel", "gaza", "lebanon", "ukraine", "russia", "taiwan",
    "south china", "strait of hormuz", "hormuz", "opec", "sanction",
    "tariff", "ceasefire", "pentagon", "nato", "houthis", "red sea",
)


def _geo_headlines(brief: dict[str, Any], limit: int = 2) -> list[str]:
    hits: list[str] = []
    seen: set[str] = set()
    for item in brief.get("news") or []:
        title = str(item.get("title") or "").strip()
        blob = f"{title} {item.get('text') or ''}".lower()
        if not title or title.lower() in seen:
            continue
        if "5 things to know" in title.lower() or "what to watch" in title.lower():
            continue
        if any(n in blob for n in GEO_NEEDLES):
            seen.add(title.lower())
            hits.append(title.rstrip("."))
        if len(hits) >= limit:
            break
    return hits


PROXY_LABEL = {
    "USO": "oil (USO proxy)",
    "UUP": "dollar (UUP proxy)",
    "GLD": "gold (GLD proxy)",
    "EWG": "Europe (EWG proxy)",
}

WEEKDAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# family, score, needles in the event name
EVENT_RULES: list[tuple[str, int, tuple[str, ...]]] = (
    ("fomc", 100, ("interest rate decision", "fed funds", "federal funds")),
    ("fomc", 92, ("fomc minutes",)),
    ("fomc", 88, ("powell", "fed chair", "fomc")),
    ("cpi", 96, ("core cpi",)),
    ("cpi", 94, ("cpi", "consumer price")),
    ("pce", 93, ("core pce",)),
    ("pce", 90, ("pce", "personal consumption")),
    ("nfp", 95, ("nonfarm", "non-farm", "payroll")),
    ("nfp", 86, ("unemployment rate", "average hourly")),
    ("gdp", 82, ("gdp",)),
    ("claims", 74, ("initial jobless", "initial claims")),
    ("claims", 40, ("continuing jobless", "4-week")),
    ("retail", 68, ("retail sales",)),
    ("ism", 62, ("ism manufacturing", "ism services", "ism")),
    ("pmi", 38, ("s&p global", "pmi")),
    ("housing", 42, ("housing starts", "building permits", "existing home", "new home", "housing")),
    ("confidence", 36, ("consumer confidence", "consumer sentiment", "michigan")),
)

PRINT_EXPLAINERS = {
    "cpi": "Headline CPI is all items. Core strips food and energy — that is what the rates market usually trades first. Hotter than consensus is a beat the wrong way if you wanted cooler inflation.",
    "pce": "PCE is the Fed’s preferred inflation gauge. Core PCE strips food and energy. Treat it as confirmation or contradiction of the last CPI, not a new Fed path by itself.",
    "nfp": "Nonfarm payrolls is the monthly jobs count. A beat means more jobs than consensus. Check unemployment and average hourly earnings before calling it a labor-market regime shift.",
    "fomc": "Minutes are not a new decision. The market will parse last meeting’s debate — dissent, the cut/hike discussion, how they talked about inflation. The first tick is not the whole story.",
    "claims": "Initial claims are weekly and noisy. A 3k miss is usually nothing. A second hot week next to Fed minutes or a weak payrolls is not nothing.",
    "gdp": "GDP is the quarterly growth print. Composition (consumption vs investment) often matters more than the top-line decimal if it is close to consensus.",
    "ism": "ISM is a survey, not a hard print. 50 is the expansion line. Direction vs last month usually matters more than the level if it sits near 50.",
    "pmi": "S&P Global flash PMIs are a survey snapshot, not CPI. Useful color on Friday. They rarely set the week if minutes or claims already did.",
    "housing": "Starts and permits are housing activity, not inflation. They move rates only if they gap hard. Otherwise they are color.",
    "retail": "Retail sales is the consumer-spending print. Watch the control group if it is in the release; the headline can be noisy with autos and gas.",
}


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any, digits: int = 2) -> str | None:
    n = _num(value)
    if n is None:
        return None
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.{digits}f}%"


def _price(value: Any, digits: int = 2) -> str | None:
    n = _num(value)
    if n is None:
        return None
    if abs(n) >= 1000:
        return f"{n:,.{digits}f}"
    return f"{n:.{digits}f}"


def _pretty_stat(value: Any, unit: str = "") -> str | None:
    n = _num(value)
    if n is None:
        return None
    unit = (unit or "").upper()
    if unit in {"K"}:
        return f"{n:,.0f}k"
    if unit in {"M"}:
        return f"{n:.2f}M"
    if unit in {"%"}:
        sign = "+" if n > 0 else ""
        return f"{sign}{n:g}%"
    if abs(n) >= 100:
        return f"{n:,.1f}".rstrip("0").rstrip(".")
    return f"{n:g}"


def _index_line(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    name = row.get("label") or row.get("name") or row.get("symbol")
    price = _price(row.get("price"))
    pct = _pct(row.get("changesPercentage"))
    if not name or not price:
        return None
    if pct:
        return f"{name} {price} ({pct})"
    return f"{name} {price}"


def _short_index(row: dict[str, Any] | None, short: str) -> str | None:
    if not row:
        return None
    pct = _pct(row.get("changesPercentage"))
    price = _price(row.get("price"))
    if not price:
        return None
    if pct:
        return f"{short} {price} ({pct})"
    return f"{short} {price}"


def _asset_line(label: str, row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    symbol = str(row.get("symbol") or "")
    name = PROXY_LABEL.get(symbol) or label
    pct = _pct(row.get("changesPercentage"))
    px = row.get("price")
    digits = 4 if _num(px) is not None and abs(float(px)) < 10 else 2
    price = _price(px, digits=digits)
    bits = [name]
    if price:
        bits.append(price)
    if pct:
        bits.append(f"({pct})")
    return " ".join(bits)


def _event_name(row: dict[str, Any]) -> str:
    return str(row.get("event") or row.get("title") or row.get("name") or row.get("indicator") or "Release")


def _classify(row: dict[str, Any]) -> tuple[str, int]:
    name = _event_name(row).lower()
    best_fam, best_score = "other", 20
    for fam, score, needles in EVENT_RULES:
        if any(n in name for n in needles) and score > best_score:
            best_fam, best_score = fam, score
    impact = str(row.get("impact") or "").lower()
    if impact == "high":
        best_score += 4
    elif impact == "low":
        best_score -= 8
    if "mom" in name and best_fam in {"housing", "retail"}:
        best_score -= 12
    return best_fam, best_score


def _parse_event_dt(row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("date") or "").strip()
    if not raw:
        return None
    try:
        if len(raw) >= 19 and raw[10] == " ":
            naive = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=timezone.utc).astimezone(NY)
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=NY)
    except ValueError:
        return None


def _event_day(row: dict[str, Any]) -> date | None:
    dt = _parse_event_dt(row)
    return dt.date() if dt else None


def _et_clock(row: dict[str, Any]) -> str | None:
    dt = _parse_event_dt(row)
    if not dt or (dt.hour == 0 and dt.minute == 0 and " " not in str(row.get("date") or "")):
        return None
    hour = dt.hour
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if dt.minute:
        return f"{hour12}:{dt.minute:02d} {ampm} ET"
    return f"{hour12} {ampm} ET"


def _weekday(d: date) -> str:
    return ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[d.weekday()]


def _print_vs(row: dict[str, Any]) -> str:
    unit = str(row.get("unit") or "")
    actual = _pretty_stat(row.get("actual"), unit)
    estimate = _pretty_stat(row.get("estimate") or row.get("forecast"), unit)
    previous = _pretty_stat(row.get("previous") or row.get("prior"), unit)
    bits = []
    if actual:
        bits.append(f"actual {actual}")
    if estimate:
        bits.append(f"consensus {estimate}")
    if previous:
        bits.append(f"prior {previous}")
    return ", ".join(bits)


def _clean_title(row: dict[str, Any]) -> str:
    name = _event_name(row)
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _collapse_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per family (keep the highest-score print)."""
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in events:
        fam, score = _classify(row)
        prev = best.get(fam)
        if prev is None or score > prev[0]:
            best[fam] = (score, row)
    ranked = sorted(best.values(), key=lambda x: -x[0])
    return [row for score, row in ranked]


def _hinge_and_color(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    collapsed = _collapse_events(events)
    hinge, color = [], []
    for row in collapsed:
        _fam, score = _classify(row)
        if score >= 70:
            hinge.append(row)
        elif score >= 36:
            color.append(row)
    return hinge[:3], color[:2]


def _news_for_symbol(brief: dict[str, Any], symbol: str) -> str | None:
    needle = symbol.upper()
    for item in brief.get("news") or []:
        blob = f"{item.get('title') or ''} {item.get('text') or ''}".upper()
        if needle in blob:
            title = str(item.get("title") or "").strip()
            return title or None
    return None


def _sectors_ranked(brief: dict[str, Any]) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    scored: list[tuple[float, str]] = []
    for row in brief.get("sectors") or []:
        chg = _num(row.get("averageChange"))
        name = str(row.get("sector") or "")
        if chg is None or not name:
            continue
        scored.append((chg, name))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:2], list(reversed(scored[-2:])) if len(scored) >= 2 else []


def slot(sid: str, title: str, window: str, status: str, body: str = "", skip: str = "", notes: str = "") -> dict[str, Any]:
    return {
        "id": sid,
        "title": title,
        "window": window,
        "status": status,
        "body": body.strip(),
        "skip_reason": skip,
        "source_notes": notes,
    }


def _vs_line(row: dict[str, Any]) -> str:
    stats = _print_vs(row)
    clock = _et_clock(row)
    title = _clean_title(row)
    fam, _ = _classify(row)
    aliases = {
        "claims": "Initial claims",
        "cpi": "CPI",
        "pce": "PCE",
        "nfp": "Payrolls",
        "fomc": title,
        "housing": "Housing (starts/permits)",
        "pmi": "Flash PMIs",
        "ism": "ISM",
        "gdp": "GDP",
        "retail": "Retail sales",
    }
    label = aliases.get(fam, title)
    if clock and stats:
        return f"{label}, {clock}. {stats[0].upper()}{stats[1:]}."
    if clock:
        return f"{label}, {clock}."
    if stats:
        return f"{label}. {stats[0].upper()}{stats[1:]}."
    return f"{label}."


def _what_changes_tape(hinge: list[dict[str, Any]]) -> str:
    if not hinge:
        return "Quiet calendar. A mega-cap gap or a 10-year that breaks Friday’s close would change the tape. Otherwise this is a stock-pick session."
    fam, _ = _classify(hinge[0])
    if fam == "fomc":
        return "What would change the tape: minutes that re-open the cut/hike debate. A nothing-burger leaves equities trading the rest of the week’s data."
    if fam == "claims":
        return "What would change the tape: claims that re-open the labor debate — not a 3k miss. Quiet claims and a held 10-year is a stock-pick day."
    if fam in {"cpi", "pce"}:
        return "What would change the tape: a core print that contradicts the last inflation read. Yields first, then Nasdaq vs Dow."
    if fam == "nfp":
        return "What would change the tape: payrolls that force a labor-market re-rate. The first tick is not the whole story — unemployment and wages have to agree."
    return f"What would change the tape: a {_clean_title(hinge[0]).lower()} print that actually moves yields. Otherwise the prior close still sets the session."


def draft_close(brief: dict[str, Any], session: date) -> list[dict[str, Any]]:
    idx = brief.get("indexes") or {}
    spx, ixic, dji, rut = idx.get("^GSPC"), idx.get("^IXIC"), idx.get("^DJI"), idx.get("^RUT")
    vix = idx.get("^VIX") or {}
    leaders, laggards = _sectors_ranked(brief)
    events = brief.get("economic_calendar") or []
    with_actual = [e for e in events if e.get("actual") not in (None, "")]
    hinge_prints, _ = _hinge_and_color(with_actual)
    main_print = hinge_prints[0] if hinge_prints else (with_actual[0] if with_actual else None)

    spx_pct = _num((spx or {}).get("changesPercentage"))
    rut_pct = _num((rut or {}).get("changesPercentage"))
    ixic_pct = _num((ixic or {}).get("changesPercentage"))
    dji_pct = _num((dji or {}).get("changesPercentage"))

    recap_parts = [f"US cash close, {_weekday(session)} {session.isoformat()}."]
    closes = " ".join(
        x
        for x in (
            _short_index(spx, "S&P 500"),
            _short_index(ixic, "Nasdaq"),
            _short_index(dji, "Dow"),
            _short_index(rut, "Russell 2000"),
        )
        if x
    )
    recap_parts.append(closes + "." if closes else "Index closes [missing].")

    if spx_pct is not None and ixic_pct is not None and rut_pct is not None:
        if abs(spx_pct) < 0.25 and (abs(ixic_pct) >= 0.35 or abs(rut_pct) >= 0.35):
            recap_parts.append(
                "The S&P barely moved. The work was underneath — Nasdaq and small caps did not print the same day as the headline index."
            )
        elif ixic_pct is not None and dji_pct is not None and abs(ixic_pct - dji_pct) >= 0.4:
            recap_parts.append(
                "This was a factor day: Nasdaq and the Dow disagreed. Duration vs defensives, not a broad risk-on/off tape."
            )

    if main_print:
        recap_parts.append("The print: " + _vs_line(main_print).rstrip(".") + ".")

    if leaders and laggards:
        recap_parts.append(
            f"Sectors: {leaders[0][1]} led ({_pct(leaders[0][0])}), {laggards[0][1]} lagged ({_pct(laggards[0][0])}). "
            "That is a Nasdaq-listed snapshot, not the whole exchange."
        )

    if spx_pct is not None and rut_pct is not None and abs(rut_pct) > abs(spx_pct) + 0.2:
        recap_parts.append("Bottom line: the index looks calmer than the median stock. Internals were softer than the S&P line.")
    else:
        recap_parts.append("Bottom line: a quiet index close is not the same as a quiet tape. Read Nasdaq and Russell next to the S&P before calling it nothing.")

    recap = slot("recap", "Session recap", "after 16:00 ET", "ready", "\n\n".join(recap_parts), notes="indexes, sectors, economic_calendar")

    if spx_pct is not None and rut_pct is not None and abs(spx_pct - rut_pct) >= 0.35:
        story_body = (
            f"Today was a breadth day, not a “stocks were mixed” day.\n\n"
            f"S&P 500 {_pct(spx_pct)}. Russell 2000 {_pct(rut_pct)}. "
            "Cap-weight and the median stock were not the same session. If you only watched the S&P, you missed it."
        )
    elif spx_pct is not None and ixic_pct is not None and abs(spx_pct - ixic_pct) >= 0.35:
        story_body = (
            f"Today was a duration/growth day inside the indexes.\n\n"
            f"S&P 500 {_pct(spx_pct)}. Nasdaq {_pct(ixic_pct)}. "
            "That split is the story. The recap is the log; this is the factor."
        )
    elif main_print:
        story_body = (
            f"The print was the day, not the index tick.\n\n"
            f"{_vs_line(main_print)} The S&P close is the second screen. "
            "Whether yields and Nasdaq agreed with the number is the read — we do not invent an intraday path we do not have."
        )
    elif leaders:
        story_body = (
            f"Leadership was the tell. {leaders[0][1]} {_pct(leaders[0][0])} on the sector snapshot we have. "
            "The S&P line is the log. The rotation is the post."
        )
    else:
        story_body = "Quiet tape. No factor split the indexes enough to force a narrative. The recap is the day — do not invent drama."
    story = slot("story", "Story of the day", "after the recap", "ready", story_body, notes="index % and sectors")

    movers_src = (brief.get("liquid_gainers") or []) + (brief.get("liquid_losers") or [])
    mega = list((brief.get("mega_caps") or {}).values())
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(movers_src + mega, key=lambda r: abs(_num(r.get("changesPercentage")) or 0), reverse=True):
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in seen or sym.startswith("^"):
            continue
        pct = _num(row.get("changesPercentage"))
        if pct is None or abs(pct) < 1.2:
            continue
        seen.add(sym)
        picked.append(row)
        if len(picked) >= 4:
            break
    if picked:
        mover_bits = ["A few names did the work. The index did not."]
        for row in picked:
            sym = str(row.get("symbol"))
            why = _news_for_symbol(brief, sym)
            if why:
                mover_bits.append(f"{sym} {_pct(row.get('changesPercentage'))} — {why.rstrip('.')}.")
            else:
                mover_bits.append(
                    f"{sym} {_pct(row.get('changesPercentage'))} — no verified headline in the feed. It moved. That is all we can say."
                )
        movers_body = "\n\n".join(mover_bits)
        movers_status, movers_skip = "ready", ""
    else:
        movers_body, movers_status, movers_skip = "", "empty", "no liquid names with a real move and a clean reason"
    movers = slot(
        "movers",
        "Movers and why",
        "after the recap / story",
        movers_status,
        movers_body,
        skip=movers_skip,
        notes="liquid_gainers, liquid_losers, mega_caps, news",
    )

    ca = brief.get("cross_asset") or {}
    tr = brief.get("treasury") or {}
    cross_parts = []
    if tr.get("year10") is not None:
        curve = ""
        if tr.get("spread_2s10s") is not None:
            spread = _num(tr["spread_2s10s"])
            if spread is not None:
                curve = f" 2s10s at {spread:.2f}pp."
        dated = f" (Treasury print dated {tr['date']})" if tr.get("date") else ""
        y2 = f" 2-year {tr['year2']}%." if tr.get("year2") is not None else ""
        cross_parts.append(f"10-year {tr['year10']}%{dated}.{y2}{curve}")
    dollar = _asset_line("dollar", ca.get("dxy")) or _asset_line("EUR/USD", ca.get("eurusd"))
    gold = _asset_line("gold", ca.get("gold"))
    crude = _asset_line("crude", ca.get("wti")) or _asset_line("Brent", ca.get("brent"))
    extras = [x for x in (dollar, gold, crude) if x]
    if extras:
        cross_parts.append(" ".join(x + "." for x in extras))
    if vix:
        vp = _pct(vix.get("changesPercentage"))
        cross_parts.append(
            f"VIX {_price(vix.get('price'))}"
            + (f" ({vp})" if vp else "")
            + ". Not a fear spike unless this is a multi-point jump — today it is the vol that matches the equity tape."
        )
    if spx_pct is not None and ixic_pct is not None:
        cross_parts.append(
            "Read-through for equities: map the 10-year and the dollar first. "
            "Softer dollar and quiet vol is not a panic tape. A backup in yields is usually a Nasdaq problem before it is an S&P problem."
        )
    cross = slot(
        "cross",
        "Cross-asset tape",
        "after the recap cluster",
        "ready" if len(cross_parts) >= 2 else "empty",
        "\n\n".join(cross_parts),
        skip="" if len(cross_parts) >= 2 else "no cross-asset quotes",
        notes="treasury, cross_asset, VIX",
    )

    if main_print:
        data_body = (
            f"{_vs_line(main_print)}\n\n"
            "That is the number. Whether the market treated it as a regime shift or noise is in the close: "
            f"{_short_index(spx, 'S&P') or 'S&P [missing]'}, {_short_index(ixic, 'Nasdaq') or 'Nasdaq [missing]'}. "
            "We do not invent the 8:31 tick."
        )
        data = slot("data_print", "Data print", "after the number", "ready", data_body, notes="top economic actual")
    else:
        data = slot("data_print", "Data print", "after the number", "empty", "", skip="no high-impact actual today")

    with_eps = [e for e in _large_us_earnings(brief) if e.get("epsActual") not in (None, "")]
    if with_eps:
        e = with_eps[0]
        sym = str(e.get("symbol"))
        est = e.get("epsEstimated")
        act = e.get("epsActual")
        move = None
        mega_map = brief.get("mega_caps") or {}
        if sym in mega_map:
            move = _pct(mega_map[sym].get("changesPercentage"))
        beat = _num(act) is not None and _num(est) is not None and float(act) > float(est)
        result = "beat" if beat else "miss" if _num(act) is not None and _num(est) is not None else "print"
        earn_body = (
            f"{sym} reported. EPS actual {act}"
            + (f" vs {est} estimate" if est not in (None, "") else "")
            + f" — a {result} on the number we have."
        )
        if move:
            earn_body += f" The stock is {move} on the session."
            if beat and _num((mega_map.get(sym) or {}).get("changesPercentage")) is not None:
                chg = _num(mega_map[sym].get("changesPercentage")) or 0
                if chg < 0:
                    earn_body += " Beat-and-drop: the market did not pay for the beat."
                elif chg > 1:
                    earn_body += " The market paid for it — not a fade."
        why = _news_for_symbol(brief, sym)
        if why:
            earn_body += f" {why.rstrip('.')}."
        earnings_slot = slot("earnings", "Earnings reaction", "after the bell or with the close cluster", "ready", earn_body, notes="earnings_calendar")
    else:
        earnings_slot = slot("earnings", "Earnings reaction", "after the bell or with the close cluster", "empty", "", skip="no US $5B+ print with actuals")

    if spx_pct is not None and rut_pct is not None and abs(spx_pct - rut_pct) >= 0.35:
        if abs(spx_pct) < abs(rut_pct):
            breadth_body = (
                f"The index lied a little. S&P 500 {_pct(spx_pct)}. Russell 2000 {_pct(rut_pct)}.\n\n"
                "The fund most people own (cap-weight) had a better day than the median stock. "
                "That is the post. Not “markets mixed.”"
            )
        else:
            breadth_body = (
                f"Small caps held up better than the headline. S&P 500 {_pct(spx_pct)}. Russell 2000 {_pct(rut_pct)}. "
                "Cap-weight was the laggard for once."
            )
        breadth = slot("breadth", "Breadth / the index lied", "with the close cluster", "ready", breadth_body, notes="^GSPC vs ^RUT")
    else:
        breadth = slot("breadth", "Breadth / the index lied", "with the close cluster", "empty", "", skip="indexes agreed")

    upcoming = [e for e in events if e.get("actual") in (None, "")]
    next_hinge, _ = _hinge_and_color(upcoming + with_actual)
    if len(_collapse_events(upcoming)) >= 2 or (main_print and _classify(main_print)[0] in {"claims", "cpi", "fomc", "nfp"}):
        fam = _classify(main_print)[0] if main_print else _classify(next_hinge[0])[0] if next_hinge else ""
        q = {
            "claims": "One claims print: noise, or the start of a labor streak?",
            "cpi": "Does this CPI confirm the last two prints, or contradict them?",
            "fomc": "Minutes: nothing-burger, or do they re-open the cut debate?",
            "nfp": "Payrolls: strong labor, or a headline the wages line will undo?",
        }.get(fam, "Which print actually sets this tape — the one we just got, or the next one on the calendar?")
        engagement = slot("engagement", "Engagement", "afternoon / evening", "ready", q + "\n\nReply with one word.", notes="calendar fork")
    else:
        engagement = slot("engagement", "Engagement", "afternoon / evening", "empty", "", skip="no clean calendar fork")

    if session.weekday() == 4:
        friday_body = (
            f"Week ending {session.isoformat()}.\n\n"
            f"{_short_index(spx, 'S&P 500')}. {_short_index(ixic, 'Nasdaq')}. {_short_index(rut, 'Russell 2000')}.\n\n"
            "That is what the week changed: the closes, not a forecast. Next week inherits these levels and whatever is on the Sunday calendar."
        )
        friday = slot("friday_wrap", "Friday week wrap", "Friday evening, after the recap cluster", "ready", friday_body, notes="indexes")
    else:
        friday = slot("friday_wrap", "Friday week wrap", "Friday evening, after the recap cluster", "empty", "", skip="not Friday")

    return [recap, story, movers, cross, data, earnings_slot, breadth, engagement, friday]


def draft_morning(brief: dict[str, Any], session: date) -> list[dict[str, Any]]:
    idx = brief.get("indexes") or {}
    target = "Monday" if session.weekday() == 4 else "today"
    events = brief.get("economic_calendar") or []
    upcoming = [e for e in events if e.get("actual") in (None, "")]
    hinge, color = _hinge_and_color(upcoming)
    earnings = _large_us_earnings(brief)

    parts = [
        f"US session {target}. Prior close: "
        + " · ".join(
            x
            for x in (
                _short_index(idx.get("^GSPC"), "S&P"),
                _short_index(idx.get("^IXIC"), "Nasdaq"),
                _short_index(idx.get("^DJI"), "Dow"),
                _short_index(idx.get("^RUT"), "Russell"),
            )
            if x
        )
        + "."
    ]
    if hinge:
        lead = hinge[0]
        fam, _ = _classify(lead)
        parts.append("The print that matters: " + _vs_line(lead))
        if len(hinge) > 1:
            parts.append("Also on the tape: " + _vs_line(hinge[1]))
        if color:
            parts.append(f"{_clean_title(color[0])} is color, not the session, unless it gaps.")
    else:
        parts.append("No high-impact US print in the window we pulled. The prior close still sets the open.")
    if earnings:
        names = ", ".join(str(e.get("symbol")) for e in earnings[:3])
        parts.append(f"Earnings that can share airtime: {names}. Treat after-the-bell names as tonight’s tape, not the cash session.")
    parts.append(_what_changes_tape(hinge))
    preview = slot("next_session", "Next session", "pre-open, before 9:30 ET", "ready", "\n\n".join(parts), notes="indexes, ranked calendar")

    overseas = brief.get("overseas") or {}
    moved = []
    for row in overseas.values():
        pct = _num(row.get("changesPercentage"))
        if pct is not None and abs(pct) >= 0.6:
            moved.append((abs(pct), _index_line(row), pct))
    moved.sort(reverse=True)
    if moved:
        names = "; ".join(x[1] for x in moved if x[1])
        overnight_body = (
            f"Asia/Europe actually moved overnight.\n\n{names}.\n\n"
            "Nothing here rewrites last night’s US close by itself. It is the backdrop into the open, not a new US session."
        )
        overnight = slot("overnight", "Overnight", "pre-open, before next session", "ready", overnight_body, notes="overseas")
    else:
        overnight = slot("overnight", "Overnight", "pre-open, before next session", "empty", "", skip="quiet overnight")

    explainer = ""
    if hinge:
        fam, _ = _classify(hinge[0])
        explainer = PRINT_EXPLAINERS.get(fam, "")
        if explainer:
            explainer = _vs_line(hinge[0]) + "\n\n" + explainer
    how = slot(
        "how_to_read",
        "How to read the print",
        "morning of, or the morning before, a high-impact US release",
        "ready" if explainer else "empty",
        explainer,
        skip="" if explainer else "no high-impact print today or tomorrow",
        notes="ranked calendar",
    )

    if len(hinge) >= 2:
        a = _clean_title(hinge[0])
        b = _clean_title(hinge[1])
        engagement = slot(
            "engagement",
            "Engagement",
            "morning",
            "ready",
            f"Which one sets {target}’s tape — {a}, or {b}?\n\nReply with one word.",
            notes="two hinge events",
        )
    else:
        engagement = slot("engagement", "Engagement", "morning", "empty", "", skip="not an engagement day")

    return [preview, overnight, how, engagement]


def _day_label(row: dict[str, Any]) -> str:
    d = _event_day(row)
    return _weekday(d) if d else "This week"


def _earn_day(row: dict[str, Any]) -> str:
    raw = str(row.get("date") or "")[:10]
    try:
        return _weekday(date.fromisoformat(raw)) if raw else "undated"
    except ValueError:
        return "undated"


def _large_us_earnings(brief: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [e for e in (brief.get("earnings_calendar") or []) if e.get("symbol")]
    rows.sort(key=lambda r: -(_num(r.get("marketCap")) or 0))
    return rows


def _week_earnings_body(earnings: list[dict[str, Any]], hinge_fams: list[str]) -> str:
    n = len(earnings)
    top = earnings[:8]
    grouped: dict[str, list[str]] = {}
    day_counts: dict[str, int] = {}
    for row in earnings:
        day = _earn_day(row)
        day_counts[day] = day_counts.get(day, 0) + 1
    for row in top:
        day = _earn_day(row)
        grouped.setdefault(day, []).append(str(row.get("symbol")))

    chips: list[str] = []
    for day in WEEKDAY_ORDER:
        names = grouped.get(day)
        if names:
            chips.append(f"{', '.join(names)} ({day})")
    for day, names in grouped.items():
        if day not in WEEKDAY_ORDER:
            chips.append(f"{', '.join(names)} ({day})")

    heavy = max(day_counts, key=day_counts.get) if day_counts else ""
    lead = f"{n} US-listed names over $5B report this week, separate from the US data calendar."
    if "fomc" in hinge_fams:
        lead += " Do not bury them under minutes."
    cluster = "The ones that can share airtime: " + "; ".join(chips) + "."
    extra = ""
    if heavy and day_counts.get(heavy, 0) >= 3:
        extra += f" {heavy} is the heavy day."
    if n > 8:
        extra += f" The other {n - 8} clear the $5B screen and do not get a line unless one of them gaps."
    return lead + "\n\n" + cluster + extra


def _week_earnings_slot(earnings: list[dict[str, Any]], hinge_fams: list[str]) -> dict[str, Any]:
    if not earnings:
        return slot(
            "week_earnings",
            "Week ahead — Earnings",
            "Sunday",
            "empty",
            "",
            skip="no US-listed $5B+ earnings this week",
        )
    return slot(
        "week_earnings",
        "Week ahead — Earnings",
        "Sunday",
        "ready",
        _week_earnings_body(earnings, hinge_fams),
        notes="US-listed earnings, market cap >= $5B, ranked",
    )


def draft_week_ahead(brief: dict[str, Any], session: date) -> list[dict[str, Any]]:
    events = brief.get("economic_calendar") or []
    hinge, color = _hinge_and_color(events)
    hinge_fams = [_classify(r)[0] for r in hinge]
    color_fams = {_classify(r)[0] for r in color}
    earnings = _large_us_earnings(brief)

    if not hinge and not color:
        data_body = (
            "Quiet US calendar this week. That is the read.\n\n"
            "No CPI, no payrolls, no FOMC. A stock-pick week unless something unscheduled hits. "
            "Do not invent a macro story because the feed looks empty."
        )
        slots = [
            slot("week_ahead", "Week ahead — US data", "Sunday", "ready", data_body, notes="empty calendar"),
            _week_earnings_slot(earnings, hinge_fams),
        ]
        geo = _geo_headlines(brief)
        if geo:
            geo_body = (
                "Not on the official calendar. In the news feed, and only if it is real:\n\n"
                + "\n\n".join(geo)
                + "\n\nThat is political risk in the headlines — not a forecast, and not a reason to ignore the tape. "
                "If the feed is quiet on this, we skip the slot."
            )
            slots.append(slot("week_geo", "Week ahead — Other", "Sunday", "ready", geo_body, notes="filtered news headlines"))
        else:
            slots.append(
                slot(
                    "week_geo",
                    "Week ahead — Other",
                    "Sunday",
                    "empty",
                    "",
                    skip="no clean geopolitical headline in the feed",
                )
            )
        return slots

    paras: list[str] = []

    if "fomc" in hinge_fams and "housing" in color_fams:
        paras.append("This is not a housing week. It is a Fed-minutes week.")
    elif "fomc" in hinge_fams and "claims" in hinge_fams:
        paras.append("Two US data days matter. The rest of the calendar is noise until it isn't.")
    elif "cpi" in hinge_fams:
        paras.append("US inflation week. Everything else on the econ calendar is a sideshow until the print is in.")
    elif "nfp" in hinge_fams:
        paras.append("Payrolls week. Do not get lost in the Tuesday/Thursday color.")
    elif hinge:
        paras.append(f"If you only watch one US print this week, watch {_day_label(hinge[0])}.")
    else:
        paras.append("No Fed-level US print this week. Do not upgrade housing or PMIs into a hinge.")

    for row in hinge:
        fam, _ = _classify(row)
        clock = _et_clock(row) or ""
        stats = _print_vs(row)
        day = _day_label(row)
        when = f"{day}, {clock}" if clock else day
        if fam == "fomc":
            paras.append(
                f"{when}: FOMC minutes. No rate decision. "
                "The tape will comb the last meeting for how close they were to a cut — or a hike. "
                "That is the US data event that can re-price the whole book."
            )
        elif fam == "claims":
            cons = f" {stats[0].upper()}{stats[1:]}." if stats else ""
            extra = " sitting next to those minutes" if "fomc" in hinge_fams else ""
            paras.append(
                f"{when}: initial claims.{cons} "
                f"Ignore a 3k miss. Do not ignore a second hot week{extra}."
            )
        elif fam in {"cpi", "pce"}:
            cons = f" {stats[0].upper()}{stats[1:]}." if stats else ""
            paras.append(
                f"{when}: {_clean_title(row)}.{cons} "
                "Yields first. Then Nasdaq vs Dow. One decimal does not write the Fed path by itself."
            )
        elif fam == "nfp":
            cons = f" {stats[0].upper()}{stats[1:]}." if stats else ""
            paras.append(
                f"{when}: payrolls.{cons} "
                "The headline is the hook. Unemployment and wages decide if it sticks."
            )
        else:
            paras.append(f"{when}: {_vs_line(row)}")

    dismiss: list[str] = []
    if "housing" in color_fams:
        dismiss.append("Tuesday housing (starts/permits) is noise unless it gaps")
    if "pmi" in color_fams:
        dismiss.append("Friday flash PMIs are leftover")
    if dismiss:
        line = dismiss[0][0].upper() + dismiss[0][1:]
        if len(dismiss) > 1:
            line += "; " + dismiss[1]
        paras.append(line + ".")

    if hinge:
        paras.append(f"US data only. If you watch one session, watch {_day_label(hinge[0])}. Earnings is a separate post.")

    data_body = "\n\n".join(paras)
    slots = [
        slot(
            "week_ahead",
            "Week ahead — US data",
            "Sunday",
            "ready",
            data_body,
            notes="ranked US economic calendar",
        )
    ]

    slots.append(_week_earnings_slot(earnings, hinge_fams))

    geo = _geo_headlines(brief)
    if geo:
        geo_body = (
            "Not on the official calendar. In the news feed, and only if it is real:\n\n"
            + "\n\n".join(geo)
            + "\n\nThat is political risk in the headlines — not a forecast, and not a reason to ignore Wednesday's print. "
            "If the feed is quiet on this, we skip the slot."
        )
        slots.append(slot("week_geo", "Week ahead — Other", "Sunday", "ready", geo_body, notes="filtered news headlines"))
    else:
        slots.append(
            slot(
                "week_geo",
                "Week ahead — Other",
                "Sunday",
                "empty",
                "",
                skip="no clean geopolitical headline in the feed",
            )
        )

    if len(hinge) >= 2:
        slots.append(
            slot(
                "engagement",
                "Engagement",
                "Sunday",
                "ready",
                f"{_day_label(hinge[0])} or {_day_label(hinge[1])}. Which US data day actually sets the week?\n\nReply with the weekday.",
                notes="two hinge days",
            )
        )
    return slots


def render_markdown(mode: str, session: date, brief_path: str, slots: list[dict[str, Any]]) -> str:
    title = {"morning": "Morning pack", "close": "Close pack", "week-ahead": "Week ahead"}[mode]
    lines = [
        f"# {title} — {session.isoformat()}",
        "",
        f"Session date (America/New_York): {session.isoformat()}",
        f"FMP brief: `{brief_path}`",
        "",
    ]
    for s in slots:
        lines += ["---", "", f"## {s['title']}", "", f"**Status:** {s['status']}", f"**Suggested window:** {s['window']}"]
        if s.get("skip_reason"):
            lines.append(f"**Skip reason:** {s['skip_reason']}")
        if s.get("kind") == "x_idea":
            if s.get("summary"):
                lines += ["", f"**Short Summary:** {s['summary']}"]
            lines += ["", "### Post Body", "", s.get("body") or ""]
            if s.get("supporting_data"):
                lines += ["", "### Supporting Data", ""]
                lines.extend(f"- {row}" for row in s["supporting_data"])
            if s.get("source_url"):
                lines += ["", f"**Source URL:** {s['source_url']}"]
            lines.append("")
            continue
        lines += ["", "### Body", "", s.get("body") or "", "", "### Source notes", "", s.get("source_notes") or "-", ""]
    return "\n".join(lines).strip() + "\n"


def draft_path(mode: str, session: date) -> Path:
    if mode == "morning":
        name = f"{session.isoformat()}-am.md"
    elif mode == "week-ahead":
        name = f"{session.isoformat()}-week-ahead.md"
    else:
        name = f"{session.isoformat()}.md"
    return REPO_ROOT / "drafts" / name


def run_job(mode: str, session: date | None = None, polish: bool = True) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env")
    api_key = (os.environ.get("FMP_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("FMP_API_KEY is missing. Put it in .env.")
    if not llm_configured():
        raise RuntimeError(unavailable_message())
    session = session or ny_today()
    client = FmpClient(api_key)
    brief = build_brief(client, mode, session)
    out_json = default_out(mode, session)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(brief, indent=2, default=str) + "\n", encoding="utf-8")

    slots, llm_error = generate_x_ideas(brief, mode, session)
    if not slots:
        raise RuntimeError(unavailable_message(llm_error))

    md_path = draft_path(mode, session)
    rel_json = str(out_json.relative_to(REPO_ROOT)).replace("\\", "/")
    md_path.write_text(render_markdown(mode, session, rel_json, slots), encoding="utf-8")
    result = {
        "mode": mode,
        "session_date": session.isoformat(),
        "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
        "brief_path": rel_json,
        "draft_path": str(md_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "errors": brief.get("errors") or [],
        "generator": "llm",
        "polished": True,
        "llm": True,
        "slots": slots,
    }
    update_latest(result)
    return result


def public_pack(result: dict[str, Any]) -> dict[str, Any]:
    """JSON for GitHub Pages. No FMP dump, no API keys."""
    return {
        "mode": result["mode"],
        "session_date": result["session_date"],
        "generated_at": result["generated_at"],
        "draft_path": result["draft_path"],
        "errors": result.get("errors") or [],
        "slots": [
            {
                "title": s.get("title") or "",
                "status": s.get("status") or "",
                "kind": s.get("kind") or "x_idea",
                "body": s.get("body") or "",
                "skip_reason": s.get("skip_reason") or "",
                "supporting_data": s.get("supporting_data") or [],
                "source_url": s.get("source_url") or "",
                "source_notes": s.get("source_notes") or "",
            }
            for s in result.get("slots") or []
        ],
    }


def update_latest(result: dict[str, Any]) -> Path:
    path = REPO_ROOT / "docs" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    latest: dict[str, Any] = {
        "updated_at": None,
        "morning": None,
        "close": None,
        "week-ahead": None,
    }
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            for key in ("morning", "close", "week-ahead", "updated_at"):
                if key in loaded:
                    latest[key] = loaded[key]
    pack = public_pack(result)
    latest[result["mode"]] = pack
    latest["updated_at"] = pack["generated_at"]
    path.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_mode(mode: str) -> str | None:
    if mode != "auto":
        return mode
    now = datetime.now(NY)
    if now.weekday() == 5:
        return None
    if now.weekday() == 6:
        return "week-ahead"
    if now.hour >= 16:
        return "close"
    return "morning"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate one TickerAlpha session pack.")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=("auto", "morning", "close", "week-ahead"),
        help="auto picks morning/close/week-ahead from America/New_York (skips Saturday).",
    )
    args = parser.parse_args()
    mode = resolve_mode(args.mode)
    if not mode:
        print("Saturday in New York — no session to generate.")
        return
    result = run_job(mode)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "session_date": result["session_date"],
                "draft_path": result["draft_path"],
                "generated_at": result["generated_at"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
