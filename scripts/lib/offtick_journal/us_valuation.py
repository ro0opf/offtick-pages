"""미국 거래대금의 표시용 추정과 거래일 기준 원화 환산. 원본 snapshot은 변경하지 않는다."""
from datetime import date, datetime
from decimal import Decimal
import json
from urllib import error, parse, request

from offtick_history.common import HistoryError, number, product, write_json
from offtick_history.kiwoom import NoRedirect
from offtick_history.schema import SEOUL


def rate_url(day):
    return "https://api.frankfurter.dev/v2/rate/USD/KRW?" + parse.urlencode({"date": day.isoformat(), "providers": "ecb"})


def validate_rate(value, day):
    rate_day = date.fromisoformat(value["date"])
    if (value["base"] != "USD" or value["quote"] != "KRW"
            or not 0 <= (day - rate_day).days <= 7):
        raise HistoryError("환율의 통화·기준일을 확인하세요.")
    return {"base": "USD", "quote": "KRW", "rate": number(value["rate"], positive=True),
            "date": rate_day.isoformat(), "requested_date": day.isoformat(),
            "source": "ECB · Frankfurter", "source_url": rate_url(day)}


def read_exchange_rate(root, day):
    if not root:
        return None
    try:
        value = json.loads((root / "fx" / (day.isoformat() + "-USD-KRW.json")).read_text())
        if value["requested_date"] != day.isoformat():
            return None
        return validate_rate(value, day)
    except (HistoryError, OSError, ValueError, KeyError, TypeError):
        return None


def fetch_exchange_rate(day):
    req = request.Request(rate_url(day), headers={"User-Agent": "offtick/1.0", "Accept": "application/json"})
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            content = response.read(16385)
        if len(content) > 16384:
            raise HistoryError("환율 응답이 너무 큽니다.")
        return json.loads(content, parse_float=Decimal)
    except (error.URLError, OSError, TimeoutError, ValueError):
        raise HistoryError("원화 환산용 환율을 조회하지 못했습니다.") from None


def collect_exchange_rate(root, day, fetch=None):
    cached = read_exchange_rate(root, day)
    if cached:
        return cached
    value = validate_rate((fetch or fetch_exchange_rate)(day), day)
    write_json(root / "fx" / (day.isoformat() + "-USD-KRW.json"),
               {**value, "fetched_at": datetime.now(SEOUL).isoformat()})
    return value


def turnover_value(row, exchange_rate):
    """확인된 USD 우선. 단위 추정은 가격×거래량 범위 검산, 나머지는 종가 기준 추정으로 명시."""
    empty = {"turnover_display_usd": None, "turnover_krw": None, "turnover_basis": "unavailable",
             "turnover_is_estimate": False}
    try:
        if row.get("currency") != "USD":
            return empty
        if row.get("turnover_usd") is not None:
            amount, basis = number(row["turnover_usd"]), "provider_reported"
        else:
            close, low, high = (number(row[key], positive=True) for key in ("close", "low", "high"))
            volume = number(row["volume"])
            if Decimal(volume) < 0 or not Decimal(low) <= Decimal(close) <= Decimal(high):
                return empty
            # 보관된 부울 플래그를 믿지 않고 수치를 다시 검산한다.
            try:
                raw = number(row.get("turnover_raw"))
            except HistoryError:
                raw = None
            if (raw is not None and Decimal(product(low, volume)) <= Decimal(raw) <= Decimal(product(high, volume))
                    and row.get("turnover_reported_unit") == "thousand_USD"):
                amount, basis = raw, "raw_usd_inferred"
            else:
                amount, basis = product(close, volume), "close_times_volume"
        if Decimal(amount) < 0:
            return empty
        return {"turnover_display_usd": amount,
                "turnover_krw": product(amount, exchange_rate["rate"]) if exchange_rate else None,
                "turnover_basis": basis, "turnover_is_estimate": basis != "provider_reported"}
    except (HistoryError, KeyError, TypeError, ArithmeticError):
        return empty
