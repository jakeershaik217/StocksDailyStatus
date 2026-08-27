from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

IST = ZoneInfo("Asia/Kolkata")
NSE_ISSUE_URL = "https://www.nseindia.com/api/ofs-detail"
NSE_SUMMARY_URL = "https://www.nseindia.com/api/live-ofs-active-issues-ss"
NSE_RETAIL_URL = "https://www.nseindia.com/api/ofs-activeissues-dr"
BSE_RETAIL_URL = "https://api.bseindia.com/BseIndiaAPI/api/bsebidofs_details/w"


@dataclass(frozen=True)
class BidLevel:
    price: Decimal
    quantity: int
    exchange: str


@dataclass
class Snapshot:
    fetched_at: str
    runtime_seconds: float
    nse_as_of: str | None
    bse_as_of: str | None
    predicted_cutoff: str
    working_bid: str
    allocation_at_cutoff_pct: str
    total_eligible_demand: int
    fresh: bool
    warning: str | None


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid decimal: {value!r}") from exc


def integer(value: Any) -> int:
    return int(dec(value))


def rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("Table", "table", "data", "Data", "records", "result", "Result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def parse_ts(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    text = re.sub(r"\s+IST$", "", str(value).strip(), flags=re.IGNORECASE)
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    except ValueError:
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": referer,
    }


def get_json(client: Any, url: str, params: dict[str, str], request_headers: dict[str, str], timeout: tuple[float, float]) -> Any:
    response = client.get(url, params=params, headers=request_headers, timeout=timeout)
    response.raise_for_status()
    if response.text.lstrip().startswith("<"):
        raise ValueError(f"Expected JSON from {response.url}")
    return response.json()


def startup_fetch(fn: Any) -> Any:
    last: Exception | None = None
    for delay in (0.0, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Startup metadata fetch failed: {last}") from last


def parse_retail(payload: Any, exchange: str) -> tuple[list[BidLevel], int, datetime | None]:
    levels: list[BidLevel] = []
    cutoff_qty = 0
    timestamps: list[datetime] = []
    for row in rows(payload):
        price_value = pick(row, "pri", "price", "Price", "OE_PRICE", "BIDPRICE", "BID_PRICE", "BidPrice", "bidPrice")
        qty_value = pick(row, "totQty", "TOTAL_QTY", "quantity", "Quantity", "BIDQTY", "BID_QTY", "BidQty", "bidQty")
        ts = parse_ts(pick(row, "dat", "DTTM", "DAT", "DATE_TIME", "TIMESTAMP", "Date", "date"))
        if ts:
            timestamps.append(ts)
        if price_value in (None, "") or qty_value in (None, ""):
            continue
        qty = integer(qty_value)
        if qty <= 0:
            continue
        raw_price = str(price_value).strip()
        normalized = re.sub(r"[^a-z]", "", raw_price.lower())
        cutoff_flag = str(pick(row, "isCutOff", "isCutoff", "cutOff", "cutoff", "CUT_OFF_IND") or "").lower()
        if normalized == "cutoff" or cutoff_flag in {"1", "true", "yes", "y", "cutoff"}:
            cutoff_qty += qty
            continue
        price = dec(raw_price)
        if price == Decimal("-1"):
            cutoff_qty += qty
            continue
        if price > 0:
            levels.append(BidLevel(price, qty, exchange))
    return levels, cutoff_qty, max(timestamps) if timestamps else None


def issue_value(payload: dict[str, Any], needle: str) -> str | None:
    for row in ((payload.get("issueInfo") or {}).get("dataList") or []):
        if needle.lower() in str(row.get("title", "")).lower():
            return str(row.get("value", "")).strip().strip('"') or None
    return None


def summary_values(payload: Any) -> tuple[Decimal | None, int | None]:
    data = rows(payload)
    if not data:
        return None, None
    row = data[0]
    cutoff = pick(row, "cutOffPrice", "cutoffPrice", "CutOffPrice")
    total_offer = pick(row, "gsIssueSize", "GsIssueSize", "totalIssueSize")
    return (dec(cutoff) if cutoff not in (None, "", "-") else None, integer(total_offer) if total_offer not in (None, "", "-") else None)


def assess(levels: list[BidLevel], cutoff_qty: int, reference_cutoff: Decimal, reserved_quantity: int, tick_size: Decimal) -> dict[str, Any]:
    by_price: dict[Decimal, int] = {}
    for level in levels:
        if level.price >= reference_cutoff:
            by_price[level.price] = by_price.get(level.price, 0) + level.quantity
    if cutoff_qty:
        by_price[reference_cutoff] = by_price.get(reference_cutoff, 0) + cutoff_qty
    eligible = sum(by_price.values())
    cumulative = 0
    predicted = reference_cutoff
    demand_at = by_price.get(reference_cutoff, 0)
    shares_at = min(demand_at, reserved_quantity)
    if eligible >= reserved_quantity:
        for price in sorted(by_price, reverse=True):
            qty = by_price[price]
            if cumulative + qty >= reserved_quantity:
                predicted = price
                demand_at = qty
                shares_at = max(reserved_quantity - cumulative, 0)
                break
            cumulative += qty
    allocation = Decimal(shares_at) / Decimal(demand_at) if demand_at else Decimal(0)
    return {"eligible": eligible, "predicted": predicted, "working": predicted + tick_size, "allocation": allocation}


def validate_freshness(nse_ts: datetime | None, bse_ts: datetime | None) -> tuple[bool, str | None]:
    max_age = float(env("OFS_MAX_SOURCE_AGE_SECONDS", "20"))
    max_skew = float(env("OFS_MAX_SOURCE_SKEW_SECONDS", "10"))
    now = datetime.now(IST)
    warnings: list[str] = []
    for name, ts in (("NSE", nse_ts), ("BSE", bse_ts)):
        if ts is None:
            warnings.append(f"{name} timestamp unavailable")
        else:
            age = abs((now - ts.astimezone(IST)).total_seconds())
            if age > max_age:
                warnings.append(f"{name} stale by {age:.1f}s")
    if nse_ts and bse_ts:
        skew = abs((nse_ts.astimezone(IST) - bse_ts.astimezone(IST)).total_seconds())
        if skew > max_skew:
            warnings.append(f"source skew {skew:.1f}s")
    return not warnings, "; ".join(warnings) if warnings else None


def parse_clock(text: str) -> dt_time:
    return datetime.strptime(text, "%H:%M:%S").time()


def cadence(now: datetime) -> float:
    t = now.time()
    if t < dt_time(15, 25):
        return float(env("OFS_INTERVAL_BEFORE_1525", "15"))
    if t < dt_time(15, 29):
        return float(env("OFS_INTERVAL_1525_1529", "5"))
    if t < dt_time(15, 29, 30):
        return float(env("OFS_INTERVAL_1529_152930", "4"))
    return float(env("OFS_INTERVAL_FINAL_30S", "2"))


def main() -> int:
    symbol = env("OFS_SYMBOL").upper()
    bse_scrip = env("BSE_SCRIP_CODE")
    start_clock = parse_clock(env("OFS_START_TIME", "15:20:00"))
    end_clock = parse_clock(env("OFS_END_TIME", "15:30:00"))
    if not symbol or not bse_scrip:
        raise RuntimeError("OFS_SYMBOL and BSE_SCRIP_CODE are required")

    nse = requests.Session()
    bse = curl_requests.Session(impersonate="chrome") if curl_requests else requests.Session()
    history: list[dict[str, Any]] = []
    last_good: Snapshot | None = None
    try:
        is_issue = startup_fetch(lambda: get_json(nse, NSE_ISSUE_URL, {"symbol": symbol, "series": "IS"}, headers("https://www.nseindia.com/"), (3.0, 12.0)))
        rs_issue = startup_fetch(lambda: get_json(nse, NSE_ISSUE_URL, {"symbol": symbol, "series": "RS"}, headers("https://www.nseindia.com/"), (3.0, 12.0)))
        offer_date = env("OFS_OFFER_DATE") or issue_value(is_issue, "Bidding session Date (For IS series)")
        retail_date = env("OFS_RETAIL_DATE") or issue_value(rs_issue, "Bidding session Date (For RS series)")
        if not offer_date or not retail_date:
            raise RuntimeError("Unable to resolve OFS dates")
        tick_text = issue_value(rs_issue, "Tick Size") or "0.05"
        match = re.search(r"\d+(?:\.\d+)?", tick_text.replace(",", ""))
        tick_size = Decimal(match.group(0)) if match else Decimal("0.05")
        page = "https://www.nseindia.com/market-data/ofs-information?" + urlencode({"symbol": symbol, "series": "IS", "type": "Active", "offerDate": offer_date})
        summary = startup_fetch(lambda: get_json(nse, NSE_SUMMARY_URL, {"index": "IS", "symbol": symbol, "offer_date": offer_date}, headers(page), (3.0, 12.0)))
        summary_cutoff, non_retail_qty = summary_values(summary)
        reference_cutoff = dec(env("OFS_REFERENCE_CUTOFF")) if env("OFS_REFERENCE_CUTOFF") else summary_cutoff
        if reference_cutoff is None:
            raise RuntimeError("Final non-retail cutoff unavailable")
        if env("OFS_RETAIL_RESERVED_QUANTITY"):
            reserved_quantity = integer(env("OFS_RETAIL_RESERVED_QUANTITY"))
        else:
            gross = integer(env("OFS_GROSS_FINAL_OFFER_QUANTITY"))
            reserved_quantity = gross - non_retail_qty if non_retail_qty and 0 < non_retail_qty < gross else int(Decimal(gross) * (dec(env("OFS_RETAIL_RESERVED_PCT", "10")) / Decimal("100")))

        now = datetime.now(IST)
        start_dt = now.replace(hour=start_clock.hour, minute=start_clock.minute, second=start_clock.second, microsecond=0)
        if start_dt > now:
            time.sleep((start_dt - now).total_seconds())

        retail_page = "https://www.nseindia.com/market-data/ofs-information?" + urlencode({"symbol": symbol, "series": "RS", "type": "Active", "offerDate": retail_date})
        nse_headers = headers(retail_page)
        bse_headers = headers("https://www.bseindia.com/markets/PublicIssues/OFSIssuse_new?expandable=0")
        bse_headers["Origin"] = "https://www.bseindia.com"
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            while datetime.now(IST).time() <= end_clock:
                loop_start = time.perf_counter()
                loop_time = datetime.now(IST)
                nf = executor.submit(get_json, nse, NSE_RETAIL_URL, {"symbol": symbol, "offerdate": retail_date, "series": "RS"}, nse_headers, (1.0, 3.5))
                bf = executor.submit(get_json, bse, BSE_RETAIL_URL, {"scripcode": bse_scrip, "strflag": "R"}, bse_headers, (1.0, 3.5))
                try:
                    nse_payload = nf.result(timeout=5.0)
                    bse_payload = bf.result(timeout=5.0)
                    nse_levels, nse_cutoff, nse_ts = parse_retail(nse_payload, "NSE")
                    bse_levels, bse_cutoff, bse_ts = parse_retail(bse_payload, "BSE")
                    result = assess([*nse_levels, *bse_levels], nse_cutoff + bse_cutoff, reference_cutoff, reserved_quantity, tick_size)
                    fresh, warning = validate_freshness(nse_ts, bse_ts)
                    snap = Snapshot(
                        fetched_at=datetime.now(IST).isoformat(timespec="milliseconds"),
                        runtime_seconds=round(time.perf_counter() - loop_start, 3),
                        nse_as_of=nse_ts.astimezone(IST).isoformat() if nse_ts else None,
                        bse_as_of=bse_ts.astimezone(IST).isoformat() if bse_ts else None,
                        predicted_cutoff=str(result["predicted"]),
                        working_bid=str(result["working"]),
                        allocation_at_cutoff_pct=f"{result['allocation'] * Decimal('100'):.4f}",
                        total_eligible_demand=result["eligible"],
                        fresh=fresh,
                        warning=warning,
                    )
                    last_good = snap
                    history.append(asdict(snap))
                    print(f"LIVE {loop_time.strftime('%H:%M:%S')} | cutoff ₹{snap.predicted_cutoff} | bid ₹{snap.working_bid} | alloc {snap.allocation_at_cutoff_pct}% | runtime {snap.runtime_seconds:.3f}s | {'FRESH' if snap.fresh else 'WARN: ' + str(snap.warning)}", flush=True)
                except Exception as exc:
                    elapsed = time.perf_counter() - loop_start
                    fallback = f"last good ₹{last_good.predicted_cutoff}/₹{last_good.working_bid}" if last_good else "no prior snapshot"
                    print(f"ERROR {loop_time.strftime('%H:%M:%S')} | {elapsed:.3f}s | {fallback} | {exc}", flush=True)
                now_after = datetime.now(IST)
                if now_after.time() >= end_clock:
                    break
                interval = cadence(now_after)
                elapsed = time.perf_counter() - loop_start
                end_dt = now_after.replace(hour=end_clock.hour, minute=end_clock.minute, second=end_clock.second, microsecond=0)
                remaining = (end_dt - now_after).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(max(0.0, interval - elapsed), remaining))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        output = {"symbol": symbol, "reference_cutoff": str(reference_cutoff), "reserved_quantity": reserved_quantity, "last_good": asdict(last_good) if last_good else None, "history": history}
        Path("fast-ofs-monitor.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
        if last_good:
            md = f"# Fast OFS Monitor — {symbol}\n\nFinal observed cutoff: **₹{last_good.predicted_cutoff}**\n\nWorking bid: **₹{last_good.working_bid}**\n\nLast runtime: **{last_good.runtime_seconds:.3f}s**\n"
            Path("fast-ofs-monitor.md").write_text(md, encoding="utf-8")
        return 0
    finally:
        nse.close()
        bse.close()


if __name__ == "__main__":
    raise SystemExit(main())
