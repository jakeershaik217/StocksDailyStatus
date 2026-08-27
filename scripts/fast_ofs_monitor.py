from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time, timezone
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
    nse_numeric_quantity: int
    nse_cutoff_quantity: int
    bse_numeric_quantity: int
    bse_cutoff_quantity: int
    total_eligible_demand: int
    subscription: str
    predicted_cutoff: str
    working_bid: str
    demand_above_cutoff: int
    demand_at_cutoff: int
    shares_available_at_cutoff: int
    allocation_at_cutoff_pct: str
    fresh: bool
    warning: str | None


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


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
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": referer,
    }


def request_json(client: Any, url: str, *, params: dict[str, str], request_headers: dict[str, str]) -> tuple[Any, str]:
    response = client.get(
        url,
        params=params,
        headers=request_headers,
        timeout=(1.5, 4.0),
    )
    response.raise_for_status()
    if response.text.lstrip().startswith("<"):
        raise ValueError(f"Expected JSON but received HTML from {response.url}")
    return response.json(), str(response.url)


def fetch_with_quick_retry(fn: Any) -> Any:
    last: Exception | None = None
    for attempt in range(2):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0:
                time.sleep(0.20)
    raise RuntimeError(str(last)) from last


def parse_retail(payload: Any, exchange: str, expected_series: str | None = None) -> tuple[list[BidLevel], int, datetime | None]:
    book = rows(payload)
    levels: list[BidLevel] = []
    cutoff_qty = 0
    timestamps: list[datetime] = []

    if expected_series:
        series_values = {
            str(v).strip().upper()
            for row in book
            if (v := pick(row, "ser", "series", "Series")) not in (None, "")
        }
        if series_values and series_values != {expected_series.upper()}:
            raise ValueError(f"{exchange} series mismatch: {series_values}")

    for row in book:
        price_value = pick(
            row,
            "pri",
            "price",
            "Price",
            "OE_PRICE",
            "BIDPRICE",
            "BID_PRICE",
            "BidPrice",
            "bidPrice",
        )
        qty_value = pick(
            row,
            "totQty",
            "TOTAL_QTY",
            "quantity",
            "Quantity",
            "BIDQTY",
            "BID_QTY",
            "BidQty",
            "bidQty",
        )
        ts = parse_ts(pick(row, "dat", "DTTM", "DAT", "DATE_TIME", "TIMESTAMP", "Date", "date"))
        if ts:
            timestamps.append(ts)
        if price_value in (None, "") or qty_value in (None, ""):
            continue
        qty = integer(qty_value)
        if qty <= 0:
            continue

        raw_price = str(price_value).strip()
        cutoff_flag = str(pick(row, "isCutOff", "isCutoff", "cutOff", "cutoff", "CUT_OFF_IND") or "").lower()
        normalized = re.sub(r"[^a-z]", "", raw_price.lower())

        # NSE may publish literal retail Cut-off demand as -1 instead of the word Cut-off.
        is_cutoff = normalized == "cutoff" or cutoff_flag in {"1", "true", "yes", "y", "cutoff"}
        if not is_cutoff:
            try:
                price = dec(raw_price)
            except ValueError:
                raise ValueError(f"Unrecognized {exchange} price: {price_value!r}")
            if price == Decimal("-1"):
                is_cutoff = True
        if is_cutoff:
            cutoff_qty += qty
            continue
        if price <= 0:
            continue
        levels.append(BidLevel(price=price, quantity=qty, exchange=exchange))

    return levels, cutoff_qty, max(timestamps) if timestamps else None


def fetch_issue_once(nse: requests.Session, symbol: str, series: str) -> dict[str, Any]:
    payload, _ = request_json(
        nse,
        NSE_ISSUE_URL,
        params={"symbol": symbol, "series": series},
        request_headers=headers("https://www.nseindia.com/"),
    )
    return payload


def issue_offer_date(payload: dict[str, Any], series: str) -> str | None:
    data = ((payload.get("issueInfo") or {}).get("dataList") or [])
    for row in data:
        title = str(row.get("title", ""))
        value = str(row.get("value", "")).strip().strip('"')
        if "bidding session date" in title.lower() and series.lower() in title.lower():
            return value
    return None


def issue_tick_size(payload: dict[str, Any]) -> Decimal:
    data = ((payload.get("issueInfo") or {}).get("dataList") or [])
    for row in data:
        if str(row.get("title", "")).strip().lower() == "tick size":
            match = re.search(r"\d+(?:\.\d+)?", str(row.get("value", "")).replace(",", ""))
            if match:
                return Decimal(match.group(0))
    return Decimal("0.05")


def fetch_final_summary_once(nse: requests.Session, symbol: str, offer_date: str) -> dict[str, Any]:
    page = "https://www.nseindia.com/market-data/ofs-information?" + urlencode(
        {"symbol": symbol, "series": "IS", "type": "Active", "offerDate": offer_date}
    )
    payload, _ = request_json(
        nse,
        NSE_SUMMARY_URL,
        params={"index": "IS", "symbol": symbol, "offer_date": offer_date},
        request_headers=headers(page),
    )
    return payload


def summary_values(payload: dict[str, Any]) -> tuple[Decimal | None, int | None]:
    data = rows(payload)
    if not data:
        return None, None
    row = data[0]
    cutoff = pick(row, "cutOffPrice", "cutoffPrice", "CutOffPrice")
    total_offer = pick(row, "gsIssueSize", "GsIssueSize", "totalIssueSize")
    return (
        dec(cutoff) if cutoff not in (None, "", "-") else None,
        integer(total_offer) if total_offer not in (None, "", "-") else None,
    )


def assess(
    levels: list[BidLevel],
    cutoff_qty: int,
    reference_cutoff: Decimal,
    reserved_quantity: int,
    tick_size: Decimal,
) -> dict[str, Any]:
    eligible = [x for x in levels if x.price >= reference_cutoff]
    by_price: dict[Decimal, int] = {}
    for level in eligible:
        by_price[level.price] = by_price.get(level.price, 0) + level.quantity
    if cutoff_qty:
        by_price[reference_cutoff] = by_price.get(reference_cutoff, 0) + cutoff_qty

    eligible_demand = sum(by_price.values())
    subscription = Decimal(eligible_demand) / Decimal(reserved_quantity)

    cumulative = 0
    predicted = reference_cutoff
    demand_above = 0
    demand_at = by_price.get(reference_cutoff, 0)
    shares_at = min(demand_at, reserved_quantity)

    if eligible_demand >= reserved_quantity:
        for price in sorted(by_price, reverse=True):
            qty = by_price[price]
            if cumulative + qty >= reserved_quantity:
                predicted = price
                demand_above = cumulative
                demand_at = qty
                shares_at = max(reserved_quantity - cumulative, 0)
                break
            cumulative += qty
    else:
        demand_above = sum(q for p, q in by_price.items() if p > reference_cutoff)

    allocation = Decimal(shares_at) / Decimal(demand_at) if demand_at else Decimal(0)
    working_bid = predicted + tick_size

    return {
        "eligible_demand": eligible_demand,
        "subscription": subscription,
        "predicted": predicted,
        "working_bid": working_bid,
        "demand_above": demand_above,
        "demand_at": demand_at,
        "shares_at": shares_at,
        "allocation": allocation,
    }


def parse_clock(text: str) -> dt_time:
    return datetime.strptime(text, "%H:%M:%S").time()


def seconds_until(target: dt_time) -> float:
    now = datetime.now(IST)
    target_dt = now.replace(hour=target.hour, minute=target.minute, second=target.second, microsecond=0)
    return (target_dt - now).total_seconds()


def cadence(now_ist: datetime) -> float:
    t = now_ist.time()
    if t < dt_time(15, 25):
        return float(env("OFS_INTERVAL_BEFORE_1525", "15"))
    if t < dt_time(15, 29):
        return float(env("OFS_INTERVAL_1525_1529", "5"))
    if t < dt_time(15, 29, 30):
        return float(env("OFS_INTERVAL_1529_152930", "4"))
    return float(env("OFS_INTERVAL_FINAL_30S", "2"))


def validate_freshness(nse_ts: datetime | None, bse_ts: datetime | None) -> tuple[bool, str | None]:
    max_age = float(env("OFS_MAX_SOURCE_AGE_SECONDS", "20"))
    max_skew = float(env("OFS_MAX_SOURCE_SKEW_SECONDS", "10"))
    now = datetime.now(IST)
    warnings: list[str] = []
    if nse_ts:
        age = abs((now - nse_ts.astimezone(IST)).total_seconds())
        if age > max_age:
            warnings.append(f"NSE stale by {age:.1f}s")
    else:
        warnings.append("NSE timestamp unavailable")
    if bse_ts:
        age = abs((now - bse_ts.astimezone(IST)).total_seconds())
        if age > max_age:
            warnings.append(f"BSE stale by {age:.1f}s")
    else:
        warnings.append("BSE timestamp unavailable")
    if nse_ts and bse_ts:
        skew = abs((nse_ts.astimezone(IST) - bse_ts.astimezone(IST)).total_seconds())
        if skew > max_skew:
            warnings.append(f"source skew {skew:.1f}s")
    return not warnings, "; ".join(warnings) if warnings else None


def main() -> int:
    symbol = env("OFS_SYMBOL").upper()
    bse_scrip = env("BSE_SCRIP_CODE")
    if not symbol or not bse_scrip:
        print("ERROR: OFS_SYMBOL and BSE_SCRIP_CODE are required", file=sys.stderr)
        return 2

    start_clock = parse_clock(env("OFS_START_TIME", "15:20:00"))
    end_clock = parse_clock(env("OFS_END_TIME", "15:30:00"))

    nse = requests.Session()
    bse = curl_requests.Session(impersonate="chrome") if curl_requests else requests.Session()

    try:
        is_issue = fetch_with_quick_retry(lambda: fetch_issue_once(nse, symbol, "IS"))
        rs_issue = fetch_with_quick_retry(lambda: fetch_issue_once(nse, symbol, "RS"))

        offer_date = env("OFS_OFFER_DATE") or issue_offer_date(is_issue, "IS")
        retail_date = env("OFS_RETAIL_DATE") or issue_offer_date(rs_issue, "RS")
        if not offer_date or not retail_date:
            raise RuntimeError("Unable to resolve OFS offer/retail dates")

        tick_size = dec(env("OFS_TICK_SIZE") or issue_tick_size(rs_issue))

        summary = fetch_with_quick_retry(lambda: fetch_final_summary_once(nse, symbol, offer_date))
        summary_cutoff, summary_non_retail_qty = summary_values(summary)

        reference_cutoff = dec(env("OFS_REFERENCE_CUTOFF")) if env("OFS_REFERENCE_CUTOFF") else summary_cutoff
        if reference_cutoff is None:
            raise RuntimeError("Final non-retail cutoff unavailable; pass OFS_REFERENCE_CUTOFF")

        explicit_retail = env("OFS_RETAIL_RESERVED_QUANTITY")
        if explicit_retail:
            reserved_quantity = integer(explicit_retail)
        else:
            gross = env("OFS_GROSS_FINAL_OFFER_QUANTITY")
            if not gross:
                raise RuntimeError("Pass OFS_RETAIL_RESERVED_QUANTITY or OFS_GROSS_FINAL_OFFER_QUANTITY")
            gross_qty = integer(gross)
            if summary_non_retail_qty and 0 < summary_non_retail_qty < gross_qty:
                reserved_quantity = gross_qty - summary_non_retail_qty
            else:
                pct = dec(env("OFS_RETAIL_RESERVED_PCT", "10")) / Decimal("100")
                reserved_quantity = int(Decimal(gross_qty) * pct)

        wait_seconds = seconds_until(start_clock)
        if wait_seconds > 0:
            print(f"Prepared. Waiting {wait_seconds:.1f}s for {start_clock} IST.", flush=True)
            time.sleep(wait_seconds)

        page = "https://www.nseindia.com/market-data/ofs-information?" + urlencode(
            {"symbol": symbol, "series": "RS", "type": "Active", "offerDate": retail_date}
        )
        nse_headers = headers(page)
        bse_headers = headers("https://www.bseindia.com/markets/PublicIssues/OFSIssuse_new?expandable=0")
        bse_headers["Origin"] = "https://www.bseindia.com"

        history: list[dict[str, Any]] = []
        last_good: Snapshot | None = None
        last_announced: tuple[str, str] | None = None
        executor = ThreadPoolExecutor(max_workers=2)

        try:
            while True:
                now_ist = datetime.now(IST)
                if now_ist.time() > end_clock:
                    break

                loop_started = time.perf_counter()

                nse_future = executor.submit(
                    fetch_with_quick_retry,
                    lambda: request_json(
                        nse,
                        NSE_RETAIL_URL,
                        params={"symbol": symbol, "offerdate": retail_date, "series": "RS"},
                        request_headers=nse_headers,
                    ),
                )
                bse_future = executor.submit(
                    fetch_with_quick_retry,
                    lambda: request_json(
                        bse,
                        BSE_RETAIL_URL,
                        params={"scripcode": bse_scrip, "strflag": "R"},
                        request_headers=bse_headers,
                    ),
                )

                try:
                    nse_payload, _ = nse_future.result(timeout=6.0)
                    bse_payload, _ = bse_future.result(timeout=6.0)
                    nse_levels, nse_cutoff, nse_ts = parse_retail(nse_payload, "NSE", "RS")
                    bse_levels, bse_cutoff, bse_ts = parse_retail(bse_payload, "BSE")
                    result = assess(
                        [*nse_levels, *bse_levels],
                        nse_cutoff + bse_cutoff,
                        reference_cutoff,
                        reserved_quantity,
                        tick_size,
                    )
                    fresh, warning = validate_freshness(nse_ts, bse_ts)
                    runtime = time.perf_counter() - loop_started
                    snap = Snapshot(
                        fetched_at=datetime.now(IST).isoformat(timespec="milliseconds"),
                        runtime_seconds=round(runtime, 3),
                        nse_as_of=nse_ts.astimezone(IST).isoformat() if nse_ts else None,
                        bse_as_of=bse_ts.astimezone(IST).isoformat() if bse_ts else None,
                        nse_numeric_quantity=sum(x.quantity for x in nse_levels),
                        nse_cutoff_quantity=nse_cutoff,
                        bse_numeric_quantity=sum(x.quantity for x in bse_levels),
                        bse_cutoff_quantity=bse_cutoff,
                        total_eligible_demand=result["eligible_demand"],
                        subscription=f"{result['subscription']:.4f}",
                        predicted_cutoff=str(result["predicted"]),
                        working_bid=str(result["working_bid"]),
                        demand_above_cutoff=result["demand_above"],
                        demand_at_cutoff=result["demand_at"],
                        shares_available_at_cutoff=result["shares_at"],
                        allocation_at_cutoff_pct=f"{result['allocation'] * Decimal('100'):.4f}",
                        fresh=fresh,
                        warning=warning,
                    )
                    last_good = snap
                    history.append(asdict(snap))
                    marker = (snap.predicted_cutoff, snap.working_bid)
                    changed = marker != last_announced
                    prefix = "CHANGE" if changed else "LIVE"
                    print(
                        f"{prefix} {now_ist.strftime('%H:%M:%S')} | cutoff ₹{snap.predicted_cutoff} | "
                        f"bid ₹{snap.working_bid} | alloc {snap.allocation_at_cutoff_pct}% | "
                        f"runtime {snap.runtime_seconds:.3f}s | {'FRESH' if snap.fresh else 'WARN: ' + str(snap.warning)}",
                        flush=True,
                    )
                    last_announced = marker
                except Exception as exc:  # noqa: BLE001
                    runtime = time.perf_counter() - loop_started
                    fallback = (
                        f"last good cutoff ₹{last_good.predicted_cutoff}, bid ₹{last_good.working_bid}"
                        if last_good
                        else "no valid prior snapshot"
                    )
                    print(
                        f"ERROR {now_ist.strftime('%H:%M:%S')} | refresh failed after {runtime:.3f}s | {fallback} | {exc}",
                        flush=True,
                    )

                now_after = datetime.now(IST)
                if now_after.time() >= end_clock:
                    break
                interval = cadence(now_after)
                elapsed = time.perf_counter() - loop_started
                sleep_for = max(0.0, interval - elapsed)
                remaining = seconds_until(end_clock)
                if remaining <= 0:
                    break
                time.sleep(min(sleep_for, remaining))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        output = {
            "symbol": symbol,
            "bse_scrip_code": bse_scrip,
            "offer_date": offer_date,
            "retail_date": retail_date,
            "reference_cutoff": str(reference_cutoff),
            "reserved_quantity": reserved_quantity,
            "tick_size": str(tick_size),
            "start_time": str(start_clock),
            "end_time": str(end_clock),
            "last_good": asdict(last_good) if last_good else None,
            "history": history,
        }
        Path("fast-ofs-monitor.json").write_text(json.dumps(output, indent=2), encoding="utf-8")

        if last_good:
            summary_text = (
                f"# Fast OFS Monitor — {symbol}\n\n"
                f"Final observed cutoff: **₹{last_good.predicted_cutoff}**\n\n"
                f"Working bid: **₹{last_good.working_bid}**\n\n"
                f"Allocation at cutoff: **{last_good.allocation_at_cutoff_pct}%**\n\n"
                f"Last refresh runtime: **{last_good.runtime_seconds:.3f}s**\n\n"
                f"Freshness: **{'FRESH' if last_good.fresh else 'WARNING'}**\n"
            )
            Path("fast-ofs-monitor.md").write_text(summary_text, encoding="utf-8")
            github_summary = env("GITHUB_STEP_SUMMARY")
            if github_summary:
                with Path(github_summary).open("a", encoding="utf-8") as fh:
                    fh.write(summary_text)
        return 0
    finally:
        nse.close()
        bse.close()


if __name__ == "__main__":
    raise SystemExit(main())
