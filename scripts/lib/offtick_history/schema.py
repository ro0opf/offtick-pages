"""kt00009의 개별 체결. 주문 시각·평균가로 체결을 대체하지 않습니다."""
from __future__ import annotations

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

from .common import HistoryError, digest, number, product

SCHEMA_VERSION = 1
API_ID = "kt00009"
API_PATH = "/api/dostk/acnt"
SEOUL = ZoneInfo("Asia/Seoul")


class InvalidExecution(HistoryError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def identifier(value, name: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,20}", text) or int(text) == 0:
        raise InvalidExecution("missing_or_invalid_" + name)
    return str(int(text))


def normalize_execution(row: dict, day: date, alias: str, source: dict, mode: str = "live") -> dict:
    if mode not in {"live", "demo"} or not re.fullmatch(r"kiwoom-account-\d{3,}", alias):
        raise InvalidExecution("invalid_data_scope")
    clock = str(row.get("cntr_tm") or "").strip()
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock) or clock == "00:00:00":
        raise InvalidExecution("missing_or_invalid_execution_time")
    try:
        parsed = datetime.strptime(clock, "%H:%M:%S").time()
    except ValueError:
        raise InvalidExecution("missing_or_invalid_execution_time") from None
    if row.get("ord_dt") and str(row["ord_dt"]).strip() != day.strftime("%Y%m%d"):
        raise InvalidExecution("response_date_differs_from_requested_date")
    order_id = identifier(row.get("ord_no"), "order_id")
    execution_number = identifier(row.get("cntr_no"), "execution_id")
    raw_symbol = str(row.get("stk_cd") or "").strip()
    prefix = raw_symbol[:1] if re.fullmatch(r"[AJQ][A-Z0-9]{6}", raw_symbol) else None
    symbol = raw_symbol[1:] if prefix else raw_symbol
    if not re.fullmatch(r"[A-Z0-9]{6}", symbol):
        raise InvalidExecution("missing_or_invalid_symbol")
    description = str(row.get("io_tp_nm") or "").strip()
    buy, sell = "매수" in description, "매도" in description
    if buy == sell:
        raise InvalidExecution("missing_or_ambiguous_side")
    market = str(row.get("dmst_stex_tp") or "").strip().upper()
    if market not in {"KRX", "NXT", "SOR"}:
        raise InvalidExecution("missing_or_invalid_market")
    try:
        quantity = number(row.get("cntr_qty"), positive=True)
        price = number(row.get("cntr_uv"), positive=True)
        order_quantity = number(row.get("ord_qty")) if row.get("ord_qty") not in (None, "") else None
    except HistoryError:
        raise InvalidExecution("missing_or_invalid_execution_quantity_or_price") from None
    # 체결번호는 전역 ID라고 가정하지 않고 계좌·일자·시장·주문번호로 범위를 한정합니다.
    identity = ["kiwoom", mode, alias, day.isoformat(), market, order_id, execution_number]
    return {
        "schema_version": SCHEMA_VERSION, "record_type": "execution", "broker": "kiwoom", "data_mode": mode,
        "execution_id": digest(identity), "broker_execution_id": execution_number, "order_id": order_id,
        "account_alias": alias, "trade_date": day.isoformat(),
        "executed_at": datetime.combine(day, parsed, tzinfo=SEOUL).isoformat(),
        "execution_time": clock, "execution_time_basis": "reported_cntr_tm",
        "execution_date_basis": "requested_order_date", "time_precision": "second", "timezone": "Asia/Seoul",
        "symbol": symbol, "broker_symbol": raw_symbol, "instrument_prefix": prefix,
        "name": str(row.get("stk_nm") or "").strip() or None,
        "side": "buy" if buy else "sell", "side_description": description,
        "quantity": quantity, "price": price, "amount": product(quantity, price),
        "currency": "KRW", "currency_basis": "domestic_stock_api", "fee": None, "tax": None,
        "broker_market_code": market,
        "execution_venue": market if market in {"KRX", "NXT"} else None,
        "venue_basis": "reported_dmst_stex_tp" if market in {"KRX", "NXT"} else "unverified_sor_venue",
        "order_quantity": order_quantity,
        "order_channel": str(row.get("comm_ord_tp") or "").strip() or None,
        "credit_description": str(row.get("crd_deal_tp") or "").strip() or None,
        "modification_type": str(row.get("mdfy_cncl_tp") or "").strip() or None,
        "timing_eligible": True, "sources": [source],
    }


def economic_record(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "sources"}


def merge_execution(records: dict[str, dict], record: dict) -> bool:
    identity = record["execution_id"]
    if identity not in records:
        records[identity] = record
        return False
    existing = records[identity]
    if economic_record(existing) != economic_record(record):
        raise InvalidExecution("conflicting_execution_identity")
    for source in record["sources"]:
        if source not in existing["sources"]:
            existing["sources"].append(source)
    return True
