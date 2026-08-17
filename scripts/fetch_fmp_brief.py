"""Fetch a dated FMP market brief for tweet drafting.

Numbers in drafts must come from this JSON (or a fresh FMP call). If a field
is missing, omit it from the post — do not invent it.

Usage:
  python scripts/fetch_fmp_brief.py --mode morning
  python scripts/fetch_fmp_brief.py --mode close
  python scripts/fetch_fmp_brief.py --mode week-ahead
  python scripts/fetch_fmp_brief.py --mode close --date 2026-08-14
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
FMP = "https://financialmodelingprep.com/stable"
TIMEOUT_SEC = 30
PAUSE_SEC = 0.12

INDEXES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones",
    "^RUT": "Russell 2000",
    "^VIX": "VIX",
}

OVERSEAS = {
    "^N225": "Nikkei 225",
    "^GDAXI": "DAX",
    "^FTSE": "FTSE 100",
    "^HSI": "Hang Seng",
}

OVERSEAS_FALLBACKS = {
    "^GDAXI": ["EWG"],
}

CROSS_ASSET = {
    "gold": "GCUSD",
    "wti": "CLUSD",
    "brent": "BZUSD",
    "dxy": "DXUSD",
    "eurusd": "EURUSD",
    "btc": "BTCUSD",
}

# Fallback tickers if the first symbol 404s
CROSS_ASSET_FALLBACKS = {
    "wti": ["WTIUSD", "USO"],
    "dxy": ["DXY", "UUP"],
    "brent": ["EBUSD"],
    "gold": ["GLD"],
}

MEGA_CAPS = {
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "TSLA",
    "BRK-B",
    "BRK.B",
    "AVGO",
    "JPM",
    "V",
    "UNH",
    "XOM",
    "LLY",
    "JNJ",
    "WMT",
    "MA",
    "ORCL",
    "HD",
    "PG",
    "COST",
    "NFLX",
    "BAC",
    "ABBV",
    "KO",
    "CRM",
    "PEP",
    "AMD",
    "CVX",
    "TSM",
    "ASML",
    "BABA",
}

HIGH_IMPACT_NEEDLES = (
    "cpi",
    "consumer price",
    "ppi",
    "producer price",
    "pce",
    "personal consumption",
    "nonfarm",
    "non-farm",
    "payroll",
    "unemployment rate",
    "jobless",
    "initial claims",
    "continuing claims",
    "gdp",
    "retail sales",
    "ism",
    "pmi",
    "consumer confidence",
    "consumer sentiment",
    "existing home",
    "new home",
    "housing starts",
    "building permits",
    "fomc",
    "federal funds",
    "interest rate decision",
    "fed chair",
    "powell",
    "jolts",
    "average hourly",
)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def ny_today() -> date:
    return datetime.now(NY).date()


def monday_of_coming_week(today: date) -> date:
    # Sunday (6): coming week starts tomorrow. Mon-Sat: this week's Monday,
    # except Saturday uses next Monday.
    weekday = today.weekday()
    if weekday == 6:
        return today + timedelta(days=1)
    if weekday == 5:
        return today + timedelta(days=2)
    return today - timedelta(days=weekday)


def quote_fields(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row or not isinstance(row, dict):
        return None
    keys = (
        "symbol",
        "name",
        "price",
        "change",
        "changesPercentage",
        "previousClose",
        "dayHigh",
        "dayLow",
        "volume",
        "avgVolume",
        "yearHigh",
        "yearLow",
        "timestamp",
    )
    out = {k: row.get(k) for k in keys if row.get(k) is not None}
    if "changesPercentage" not in out and out.get("price") is not None and out.get("previousClose"):
        prev = float(out["previousClose"])
        if prev:
            out["changesPercentage"] = (float(out["price"]) - prev) / prev * 100.0
    return out or None


def first_row(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.errors: list[str] = []

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        record_error: bool = True,
    ) -> Any:
        query = dict(params or {})
        query["apikey"] = self.api_key
        label_params = {k: v for k, v in query.items() if k != "apikey"}
        label = path if not label_params else f"{path} {label_params}"
        url = f"{FMP}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "TickerAlphaMarketingAgent/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8")
            time.sleep(PAUSE_SEC)
            if not raw.strip():
                return None
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if record_error:
                self.errors.append(f"{label}: HTTP {exc.code}")
            time.sleep(PAUSE_SEC)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            if record_error:
                self.errors.append(f"{label}: {exc}")
            time.sleep(PAUSE_SEC)
            return None

    def quote(self, symbol: str, *, record_error: bool = True) -> dict[str, Any] | None:
        payload = self.get("quote", {"symbol": symbol}, record_error=record_error)
        return quote_fields(first_row(payload))

    def quote_with_fallbacks(self, symbols: list[str]) -> dict[str, Any] | None:
        for i, symbol in enumerate(symbols):
            last = i == len(symbols) - 1
            row = self.quote(symbol, record_error=last)
            if row:
                return row
        return None


def is_us_event(row: dict[str, Any]) -> bool:
    country = str(row.get("country") or row.get("countryCode") or "").upper()
    return country in {"US", "USA", "UNITED STATES", ""}


def is_high_impact(row: dict[str, Any]) -> bool:
    impact = str(row.get("impact") or row.get("importance") or "").lower()
    if impact in {"high", "3", "red"}:
        return True
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("event", "title", "name", "indicator")
    ).lower()
    return any(needle in blob for needle in HIGH_IMPACT_NEEDLES)


def slim_event(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "date",
        "time",
        "country",
        "event",
        "title",
        "name",
        "indicator",
        "actual",
        "estimate",
        "forecast",
        "previous",
        "prior",
        "impact",
        "importance",
        "unit",
        "currency",
    )
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "")}


def slim_earnings(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "date",
        "symbol",
        "epsActual",
        "epsEstimated",
        "revenueActual",
        "revenueEstimated",
        "time",
        "hour",
        "fiscalDateEnding",
        "updatedFromDate",
    )
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "")}


def is_liquid_mover(row: dict[str, Any]) -> bool:
    try:
        price = float(row.get("price") or 0)
        pct = abs(float(row.get("changesPercentage") or 0))
    except (TypeError, ValueError):
        return False
    if price < 8:
        return False
    if pct > 40:
        return False
    return True


def slim_mover(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "name",
        "price",
        "change",
        "changesPercentage",
        "volume",
        "avgVolume",
        "exchange",
    )
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "")}


def slim_news(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("publishedDate", "publisher", "title", "text", "url", "symbol", "site")
    out = {k: row[k] for k in keys if k in row and row[k] not in (None, "")}
    text = str(out.get("text") or "")
    if len(text) > 400:
        out["text"] = text[:400] + "…"
    return out


def filter_earnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in MEGA_CAPS or symbol in seen:
            continue
        seen.add(symbol)
        picked.append(slim_earnings(row))
        if len(picked) >= 25:
            break
    if picked:
        return picked
    # If no mega-caps that day, keep the first handful so the agent can still skip cleanly.
    return [slim_earnings(r) for r in rows[:8] if isinstance(r, dict)]


def list_of_dicts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("historical", "data", "results"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def build_brief(client: FmpClient, mode: str, session: date) -> dict[str, Any]:
    iso = session.isoformat()
    brief: dict[str, Any] = {
        "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
        "mode": mode,
        "session_date": iso,
        "timezone": "America/New_York",
        "indexes": {},
        "overseas": {},
        "cross_asset": {},
        "treasury": None,
        "sectors": [],
        "mega_caps": {},
        "gainers": [],
        "losers": [],
        "actives": [],
        "liquid_gainers": [],
        "liquid_losers": [],
        "economic_calendar": [],
        "earnings_calendar": [],
        "news": [],
        "errors": [],
    }

    for symbol, name in INDEXES.items():
        row = client.quote(symbol)
        if row:
            row["label"] = name
            brief["indexes"][symbol] = row

    if mode in {"morning", "close"}:
        for symbol, name in OVERSEAS.items():
            candidates = [symbol] + OVERSEAS_FALLBACKS.get(symbol, [])
            row = client.quote_with_fallbacks(candidates)
            if row:
                row["label"] = name
                brief["overseas"][symbol] = row

        for label, symbol in CROSS_ASSET.items():
            candidates = [symbol] + CROSS_ASSET_FALLBACKS.get(label, [])
            row = client.quote_with_fallbacks(candidates)
            if row:
                brief["cross_asset"][label] = row

        treasury = list_of_dicts(client.get("treasury-rates"))
        if treasury:
            latest = treasury[0]
            keep = (
                "date",
                "month1",
                "month2",
                "month3",
                "month6",
                "year1",
                "year2",
                "year5",
                "year7",
                "year10",
                "year20",
                "year30",
            )
            rates = {k: latest.get(k) for k in keep if latest.get(k) is not None}
            try:
                y2 = float(rates["year2"])
                y10 = float(rates["year10"])
                rates["spread_2s10s"] = round(y10 - y2, 4)
            except (KeyError, TypeError, ValueError):
                pass
            brief["treasury"] = rates

        news = list_of_dicts(client.get("news/general-latest", {"page": "0", "limit": "20"}))
        brief["news"] = [slim_news(row) for row in news[:15]]

    if mode == "close":
        sectors = client.get("sector-performance-snapshot", {"date": iso})
        brief["sectors"] = list_of_dicts(sectors)[:20]
        gainers = [slim_mover(r) for r in list_of_dicts(client.get("biggest-gainers"))[:25]]
        losers = [slim_mover(r) for r in list_of_dicts(client.get("biggest-losers"))[:25]]
        brief["gainers"] = gainers[:15]
        brief["losers"] = losers[:15]
        brief["liquid_gainers"] = [r for r in gainers if is_liquid_mover(r)][:10]
        brief["liquid_losers"] = [r for r in losers if is_liquid_mover(r)][:10]
        brief["actives"] = [slim_mover(r) for r in list_of_dicts(client.get("most-actives"))[:15]]
        mega_symbols = [s for s in MEGA_CAPS if "." not in s and "-" not in s]
        batch = client.get("batch-quote", {"symbols": ",".join(mega_symbols[:20])})
        for row in list_of_dicts(batch)[:20]:
            slim = quote_fields(row)
            symbol = str((slim or {}).get("symbol") or "")
            if slim and symbol:
                brief["mega_caps"][symbol] = slim
        events = [r for r in list_of_dicts(client.get("economic-calendar", {"from": iso, "to": iso})) if is_us_event(r)]
        brief["economic_calendar"] = [slim_event(r) for r in events if is_high_impact(r)] or [
            slim_event(r) for r in events[:12]
        ]
        earnings = list_of_dicts(client.get("earnings-calendar", {"from": iso, "to": iso}))
        brief["earnings_calendar"] = filter_earnings(earnings)

    if mode == "morning":
        nxt = (session + timedelta(days=1)).isoformat()
        # Weekend: Friday morning still wants Friday; Sunday/Monday handled by caller date.
        events = [
            r
            for r in list_of_dicts(client.get("economic-calendar", {"from": iso, "to": nxt}))
            if is_us_event(r)
        ]
        brief["economic_calendar"] = [slim_event(r) for r in events if is_high_impact(r)] or [
            slim_event(r) for r in events[:20]
        ]
        earnings = list_of_dicts(client.get("earnings-calendar", {"from": iso, "to": nxt}))
        brief["earnings_calendar"] = filter_earnings(earnings)

    if mode == "week-ahead":
        start = monday_of_coming_week(session)
        end = start + timedelta(days=4)
        brief["week_start"] = start.isoformat()
        brief["week_end"] = end.isoformat()
        events = [
            r
            for r in list_of_dicts(
                client.get("economic-calendar", {"from": start.isoformat(), "to": end.isoformat()})
            )
            if is_us_event(r)
        ]
        high = [slim_event(r) for r in events if is_high_impact(r)]
        brief["economic_calendar"] = high or [slim_event(r) for r in events[:40]]
        earnings = list_of_dicts(
            client.get("earnings-calendar", {"from": start.isoformat(), "to": end.isoformat()})
        )
        brief["earnings_calendar"] = filter_earnings(earnings)

    brief["errors"] = client.errors
    return brief


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch an FMP brief for tweet drafts")
    parser.add_argument("--mode", choices=("morning", "close", "week-ahead"), required=True)
    parser.add_argument("--date", help="Session date YYYY-MM-DD in America/New_York. Default: today in NY.")
    parser.add_argument("--out", help="Output JSON path. Default: data/<date>[-am|-week-ahead].json")
    return parser.parse_args()


def default_out(mode: str, session: date) -> Path:
    if mode == "morning":
        name = f"{session.isoformat()}-am.json"
    elif mode == "week-ahead":
        name = f"{session.isoformat()}-week-ahead.json"
    else:
        name = f"{session.isoformat()}.json"
    return ROOT / "data" / name


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = (os.environ.get("FMP_API_KEY") or "").strip()
    if not api_key:
        print("FMP_API_KEY is missing. Copy .env.example to .env or export the variable.", file=sys.stderr)
        return 2

    args = parse_args()
    if args.date:
        session = date.fromisoformat(args.date)
    else:
        session = ny_today()

    client = FmpClient(api_key)
    brief = build_brief(client, args.mode, session)
    out = Path(args.out) if args.out else default_out(args.mode, session)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(brief, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({brief['mode']}, {brief['session_date']}, {len(brief['errors'])} errors)")
    if brief["errors"]:
        for err in brief["errors"]:
            print(f"  warn: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
