from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from .engine import BidLevel, _dec, _int

IST = ZoneInfo("Asia/Kolkata")
NSE_BASE_URL = "https://www.nseindia.com"
BSE_NON_RETAIL_URL = "https://api.bseindia.com/BseIndiaAPI/api/bsebidofs_details/w"


@dataclass(frozen=True)
class OFSIssue:
    symbol: str
    company_name: str
    series: str
    offer_date: str | None
    tick_size: Decimal
    floor_price: Decimal | None
    allocation_methodology: str | None
    stock_exchanges: str | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class OFSSummary:
    symbol: str
    company_name: str
    series: str
    offer_date: str
    status: str
    base_offer_quantity: int
    total_offer_quantity: int | None
    total_demand: int
    margin_100_quantity: int | None
    margin_0_quantity: int | None
    floor_price: Decimal
    indicative_price: Decimal | None
    ltp: Decimal | None
    as_of: datetime | None
    source_url: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class LadderSnapshot:
    exchange: str
    symbol: str
    category: str
    bids: tuple[BidLevel, ...]
    as_of: datetime | None
    fetched_at: datetime
    source_url: str
    confirmed_quantity: int | None
    unconfirmed_quantity: int | None
    raw_payload: Any

    @property
    def total_quantity(self) -> int:
        return sum(level.quantity for level in self.bids)


def _browser_headers(*, referer: str) -> dict[str, str]:
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


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    timeout: float = 20.0,
    session: Any | None = None,
) -> Any:
    client = session or requests
    response = client.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    if response.text.lstrip().startswith("<"):
        raise ValueError(f"Expected JSON but received HTML from {response.url}")
    return response.json()


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "Table",
        "table",
        "data",
        "Data",
        "records",
        "Record",
        "result",
        "Result",
    ):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _pick(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _optional_int(value: Any) -> int | None:
    if value in (None, "", "-"):
        return None
    return _int(value)


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "-"):
        return None
    return _dec(value)


def parse_exchange_timestamp(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    text = re.sub(r"\s+IST$", "", text, flags=re.IGNORECASE)
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


def parse_nse_issue(
    payload: dict[str, Any],
    *,
    symbol: str,
    series: str = "IS",
) -> OFSIssue:
    issue_info = payload.get("issueInfo") or {}
    data_list = issue_info.get("dataList") or []
    fields = {
        str(row.get("title", "")).strip(): str(row.get("value", "")).strip().strip('"')
        for row in data_list
        if isinstance(row, dict)
    }

    offer_date = fields.get(f"Bidding session Date (For {series} series)")
    if not offer_date:
        offer_date = next(
            (
                value
                for title, value in fields.items()
                if "bidding session date" in title.lower()
                and series.lower() in title.lower()
            ),
            None,
        )

    tick_text = fields.get("Tick Size", "")
    tick_match = re.search(r"\d+(?:\.\d+)?", tick_text.replace(",", ""))
    tick_size = Decimal(tick_match.group(0)) if tick_match else Decimal("0.05")

    floor_text = fields.get("Floor Price", "")
    floor_match = re.search(r"\d+(?:\.\d+)?", floor_text.replace(",", ""))
    floor_price = Decimal(floor_match.group(0)) if floor_match else None

    exchange_field = next(
        (
            value
            for title, value in fields.items()
            if title.lower().startswith("stock exchanges where")
        ),
        None,
    )

    return OFSIssue(
        symbol=symbol.upper(),
        company_name=str(
            payload.get("companyName") or issue_info.get("heading") or symbol
        ),
        series=series.upper(),
        offer_date=offer_date,
        tick_size=tick_size,
        floor_price=floor_price,
        allocation_methodology=fields.get("Allocation Methodology"),
        stock_exchanges=exchange_field,
        raw_payload=payload,
    )


def parse_nse_summary(
    payload: dict[str, Any],
    *,
    symbol: str,
    series: str = "IS",
    offer_date: str,
    source_url: str = "",
) -> OFSSummary:
    rows = _rows(payload)
    if len(rows) != 1:
        raise ValueError(f"Expected one NSE OFS summary row, received {len(rows)}")
    row = rows[0]
    actual_symbol = str(_pick(row, ("symbol", "Symbol")) or "").upper()
    actual_series = str(_pick(row, ("series", "Series")) or "").upper()
    if actual_symbol and actual_symbol != symbol.upper():
        raise ValueError(
            f"NSE summary symbol mismatch: {actual_symbol} != {symbol.upper()}"
        )
    if actual_series and actual_series != series.upper():
        raise ValueError(
            f"NSE summary series mismatch: {actual_series} != {series.upper()}"
        )

    total_demand = _int(_pick(row, ("totQty", "TotQty", "TOTAL_QTY")))
    margin_100 = _optional_int(_pick(row, ("cumu_100pcQty", "Cumu_100pcQty")))
    margin_0 = _optional_int(_pick(row, ("cumu_0pcQty", "Cumu_0pcQty")))
    if margin_100 is not None and margin_0 is not None:
        margin_total = margin_100 + margin_0
        if margin_total != total_demand:
            raise ValueError(
                "NSE summary failed margin-bucket reconciliation: "
                f"total={total_demand}, margin_buckets={margin_total}"
            )

    return OFSSummary(
        symbol=symbol.upper(),
        company_name=str(_pick(row, ("company", "Company")) or symbol),
        series=series.upper(),
        offer_date=str(_pick(row, ("offerDate", "OfferDate")) or offer_date),
        status=str(_pick(row, ("status", "Status")) or "UNKNOWN"),
        base_offer_quantity=_int(
            _pick(row, ("totissueSize", "TotissueSize", "baseIssueSize"))
        ),
        total_offer_quantity=_optional_int(
            _pick(row, ("gsIssueSize", "GsIssueSize", "totalIssueSize"))
        ),
        total_demand=total_demand,
        margin_100_quantity=margin_100,
        margin_0_quantity=margin_0,
        floor_price=_dec(_pick(row, ("floorPrice", "FloorPrice"))),
        indicative_price=_optional_decimal(
            _pick(row, ("indicative_Price", "Indicative_Price", "indicativePrice"))
        ),
        ltp=_optional_decimal(_pick(row, ("ltp", "LTP"))),
        as_of=parse_exchange_timestamp(payload.get("timestamp")),
        source_url=source_url,
        raw_payload=payload,
    )


def _validate_cumulative_rows(
    rows: list[dict[str, Any]],
    *,
    price_names: tuple[str, ...],
    quantity_names: tuple[str, ...],
    cumulative_names: tuple[str, ...],
    exchange: str,
) -> None:
    by_price: dict[Decimal, int] = {}
    cumulative_by_price: dict[Decimal, int] = {}
    for row in rows:
        price_value = _pick(row, price_names)
        quantity_value = _pick(row, quantity_names)
        if price_value in (None, "") or quantity_value in (None, ""):
            continue
        price = _dec(price_value)
        quantity = _int(quantity_value)
        if price <= 0 or quantity <= 0:
            continue
        by_price[price] = by_price.get(price, 0) + quantity
        cumulative = _optional_int(_pick(row, cumulative_names))
        if cumulative is not None:
            cumulative_by_price[price] = cumulative

    running = 0
    for price in sorted(by_price, reverse=True):
        running += by_price[price]
        displayed = cumulative_by_price.get(price)
        if displayed is not None and displayed != running:
            raise ValueError(
                f"{exchange} ladder failed cumulative reconciliation at {price}: "
                f"calculated={running}, displayed={displayed}"
            )


def parse_nse_market_by_price(
    payload: Any,
    *,
    symbol: str,
    category: str = "NON_RETAIL",
) -> list[BidLevel]:
    rows = _rows(payload)
    _validate_cumulative_rows(
        rows,
        price_names=("pri", "price", "Price"),
        quantity_names=("totQty", "quantity", "Quantity"),
        cumulative_names=("cumTQty", "cumulativeQuantity", "CumulativeQuantity"),
        exchange="NSE",
    )
    levels: list[BidLevel] = []
    for row in rows:
        row_symbol = str(_pick(row, ("sym", "symbol", "Symbol")) or "").upper()
        if row_symbol and row_symbol != symbol.upper():
            raise ValueError(
                f"NSE ladder symbol mismatch: {row_symbol} != {symbol.upper()}"
            )
        price = _pick(row, ("pri", "price", "Price"))
        quantity = _pick(row, ("totQty", "quantity", "Quantity"))
        if price in (None, "") or quantity in (None, ""):
            continue
        price_dec, qty_int = _dec(price), _int(quantity)
        confirmed = _optional_int(_pick(row, ("conQty", "confirmedQty")))
        unconfirmed = _optional_int(_pick(row, ("uCQty", "unconfirmedQty")))
        if (
            confirmed is not None
            and unconfirmed is not None
            and confirmed + unconfirmed != qty_int
        ):
            raise ValueError(
                "NSE ladder failed confirmed/unconfirmed reconciliation at "
                f"{price_dec}: total={qty_int}, buckets={confirmed + unconfirmed}"
            )
        if price_dec <= 0 or qty_int <= 0:
            continue
        levels.append(
            BidLevel(
                price=price_dec,
                quantity=qty_int,
                exchange="NSE",
                category=category,
            )
        )
    return levels


def parse_nse_ladder(
    payload: Any,
    *,
    symbol: str,
    category: str = "NON_RETAIL",
    source_url: str = "",
    fetched_at: datetime | None = None,
) -> LadderSnapshot:
    rows = _rows(payload)
    levels = parse_nse_market_by_price(payload, symbol=symbol, category=category)
    timestamps = {
        parsed
        for row in rows
        if (
            parsed := parse_exchange_timestamp(_pick(row, ("dat", "date", "timestamp")))
        )
        is not None
    }
    if len(timestamps) > 1:
        raise ValueError("NSE ladder contains multiple exchange timestamps")
    confirmed = sum(
        _optional_int(_pick(row, ("conQty", "confirmedQty"))) or 0 for row in rows
    )
    unconfirmed = sum(
        _optional_int(_pick(row, ("uCQty", "unconfirmedQty"))) or 0 for row in rows
    )
    return LadderSnapshot(
        exchange="NSE",
        symbol=symbol.upper(),
        category=category,
        bids=tuple(levels),
        as_of=next(iter(timestamps), None),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source_url=source_url,
        confirmed_quantity=confirmed if rows else None,
        unconfirmed_quantity=unconfirmed if rows else None,
        raw_payload=payload,
    )


def parse_generic_bse_levels(
    payload: Any,
    *,
    category: str = "NON_RETAIL",
) -> list[BidLevel]:
    rows = _rows(payload)
    levels: list[BidLevel] = []
    for row in rows:
        price = _pick(
            row,
            (
                "OE_PRICE",
                "price",
                "Price",
                "BIDPRICE",
                "BID_PRICE",
                "BidPrice",
                "bidPrice",
            ),
        )
        quantity = _pick(
            row,
            (
                "TOTAL_QTY",
                "quantity",
                "Quantity",
                "BIDQTY",
                "BID_QTY",
                "BidQty",
                "bidQty",
            ),
        )
        if price in (None, "") or quantity in (None, ""):
            continue
        try:
            price_dec, qty_int = _dec(price), _int(quantity)
        except ValueError:
            # Retail payloads can contain a literal "Cut-off" row. It is not
            # a price-priority level and must not enter non-retail calculations.
            continue
        confirmed = _optional_int(_pick(row, ("CONFIRMEDQTY", "CONFIRMED_QTY")))
        unconfirmed = _optional_int(_pick(row, ("UNC_QTY", "UNCONFIRMED_QTY")))
        if (
            confirmed is not None
            and unconfirmed is not None
            and confirmed + unconfirmed != qty_int
        ):
            raise ValueError(
                "BSE ladder failed confirmed/unconfirmed reconciliation at "
                f"{price_dec}: total={qty_int}, buckets={confirmed + unconfirmed}"
            )
        if price_dec <= 0 or qty_int <= 0:
            continue
        levels.append(
            BidLevel(
                price=price_dec,
                quantity=qty_int,
                exchange="BSE",
                category=category,
            )
        )
    return levels


def parse_bse_ladder(
    payload: Any,
    *,
    symbol: str,
    category: str = "NON_RETAIL",
    source_url: str = "",
    fetched_at: datetime | None = None,
) -> LadderSnapshot:
    rows = _rows(payload)
    levels = parse_generic_bse_levels(payload, category=category)
    timestamps = {
        parsed
        for row in rows
        if (
            parsed := parse_exchange_timestamp(
                _pick(row, ("DAT", "DATE_TIME", "TIMESTAMP", "Date", "date"))
            )
        )
        is not None
    }
    confirmed_values = [
        value
        for row in rows
        if (value := _optional_int(_pick(row, ("CONFIRMEDQTY", "CONFIRMED_QTY"))))
        is not None
    ]
    unconfirmed_values = [
        value
        for row in rows
        if (value := _optional_int(_pick(row, ("UNC_QTY", "UNCONFIRMED_QTY"))))
        is not None
    ]
    return LadderSnapshot(
        exchange="BSE",
        symbol=symbol.upper(),
        category=category,
        bids=tuple(levels),
        as_of=max(timestamps) if timestamps else None,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source_url=source_url,
        confirmed_quantity=sum(confirmed_values) if confirmed_values else None,
        unconfirmed_quantity=sum(unconfirmed_values) if unconfirmed_values else None,
        raw_payload=payload,
    )


def fetch_nse_issue(symbol: str, *, series: str = "IS") -> OFSIssue:
    params = {"symbol": symbol.upper(), "series": series.upper()}
    url = f"{NSE_BASE_URL}/api/ofs-detail"
    payload = _request_json(
        url,
        params=params,
        headers=_browser_headers(referer=f"{NSE_BASE_URL}/"),
    )
    return parse_nse_issue(payload, symbol=symbol, series=series)


def fetch_nse_summary(
    symbol: str,
    *,
    offer_date: str,
    series: str = "IS",
) -> OFSSummary:
    params = {
        "index": series.upper(),
        "symbol": symbol.upper(),
        "offer_date": offer_date,
    }
    url = f"{NSE_BASE_URL}/api/live-ofs-active-issues-ss"
    page_url = f"{NSE_BASE_URL}/market-data/ofs-information?" + urlencode(
        {
            "symbol": symbol.upper(),
            "series": series.upper(),
            "type": "Active",
            "offerDate": offer_date,
        }
    )
    payload = _request_json(
        url,
        params=params,
        headers=_browser_headers(referer=page_url),
    )
    source_url = f"{url}?{urlencode(params)}"
    return parse_nse_summary(
        payload,
        symbol=symbol,
        series=series,
        offer_date=offer_date,
        source_url=source_url,
    )


def fetch_nse_market_by_price(
    symbol: str,
    *,
    offer_date: str | None = None,
    endpoint_url: str | None = None,
    category: str = "NON_RETAIL",
) -> LadderSnapshot:
    if endpoint_url:
        url = endpoint_url
        params: dict[str, str] | None = None
    else:
        if not offer_date:
            raise ValueError("offer_date is required for the public NSE OFS ladder")
        url = f"{NSE_BASE_URL}/api/ofs-activeissues-dd"
        params = {"symbol": symbol.upper(), "offerdate": offer_date}
    page_url = f"{NSE_BASE_URL}/market-data/ofs-information?" + urlencode(
        {
            "symbol": symbol.upper(),
            "series": "IS",
            "type": "Active",
            "offerDate": offer_date or "",
        }
    )
    payload = _request_json(
        url,
        params=params,
        headers=_browser_headers(referer=page_url),
    )
    source_url = f"{url}?{urlencode(params)}" if params else url
    return parse_nse_ladder(
        payload,
        symbol=symbol,
        category=category,
        source_url=source_url,
    )


def _curl_cffi_session() -> Any | None:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests.Session(impersonate="chrome")


def fetch_bse_levels(
    scrip_code_or_url: str,
    *,
    symbol: str = "UNKNOWN",
    category: str = "NON_RETAIL",
) -> LadderSnapshot:
    if str(scrip_code_or_url).lower().startswith(("http://", "https://")):
        url = str(scrip_code_or_url)
        params = None
    else:
        url = BSE_NON_RETAIL_URL
        params = {"scripcode": str(scrip_code_or_url), "strflag": "NR"}
    headers = _browser_headers(
        referer="https://www.bseindia.com/markets/PublicIssues/OFSIssuse_new?expandable=0"
    )
    headers["Origin"] = "https://www.bseindia.com"
    session = _curl_cffi_session()
    try:
        payload = _request_json(
            url,
            params=params,
            headers=headers,
            timeout=25.0,
            session=session,
        )
    finally:
        if session is not None:
            session.close()
    source_url = f"{url}?{urlencode(params)}" if params else url
    return parse_bse_ladder(
        payload,
        symbol=symbol,
        category=category,
        source_url=source_url,
    )


def retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    delay: float = 0.8,
) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay * (2**attempt))
    raise RuntimeError(
        f"Exchange fetch failed after {attempts} attempts: {last}"
    ) from last
