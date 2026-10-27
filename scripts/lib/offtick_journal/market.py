"""저장된 시장 snapshot만 읽는 분석 화면용 조회. 외부 API를 호출하지 않는다."""
from datetime import date
from decimal import Decimal, localcontext

from offtick_history.common import HistoryError, exact_sum, number, product
from offtick_history.market import INDEX_NAMES, read_component, read_day


def saved_days(root):
    items, unreadable = [], 0
    if root:
        for path in sorted((root / "days").glob("????-??-??.json"), reverse=True):
            try:
                day = date.fromisoformat(path.stem)
                snapshot = read_day(root, day)
                mode = snapshot.get("data_mode")
                if mode not in {"live", "demo"}:
                    raise HistoryError("실전·데모 범위를 확인하세요.")
                items.append({"trade_date": day.isoformat(), "data_mode": mode,
                              "series_count": len(snapshot["series"]),
                              "selection_status": snapshot.get("selection_status", "not_collected")})
            except (HistoryError, OSError, ValueError, TypeError, KeyError):
                unreadable += 1
    return {"items": items, "unreadable_days": unreadable}


def overview(root, day, mode):
    snapshot = read_day(root, day) if root else {"series": {}}
    empty = {"trade_date": day.isoformat(), "data_mode": mode, "indices": [], "items": [],
             "selection": {}, "return_basis": "first_stored_open", "turnover": {"status": "not_collected", "available_count": 0}}
    if snapshot.get("data_mode", mode) != mode:
        return {**empty, "status": "different_data_mode"}
    if not snapshot["series"] and "selection" not in snapshot:
        return {**empty, "status": "not_collected"}

    amounts, turnover_status = {}, "not_collected"
    if "turnover" in snapshot:
        try:
            daily = read_component(root, snapshot["turnover"])
            if (daily.get("trade_date") != day.isoformat() or daily.get("data_mode") != mode
                    or daily.get("basis") != "provider_reported_daily" or daily.get("status") not in {"complete", "partial"}):
                raise HistoryError("일별 거래대금의 날짜·실전 범위를 확인하세요.")
            for item in daily["items"]:
                amount, won, volume = (number(item[field]) for field in ("turnover_million_krw", "turnover_krw", "reported_volume"))
                if (item["trade_date"] != day.isoformat() or item["data_mode"] != mode or item["unit"] != "million_KRW"
                        or item["basis"] != "provider_reported_daily" or item["kind"] not in {"stock", "index"}
                        or item["market_scope"] != ("KRX_index" if item["kind"] == "index" else "KRX_NXT_integrated")
                        or Decimal(amount) < 0 or Decimal(volume) < 0 or Decimal(won) != Decimal(product(amount, "1000000"))):
                    raise HistoryError("일별 거래대금의 단위·금액을 확인하세요.")
                component = item["kind"] + "-" + item["symbol"]
                if component in amounts:
                    raise HistoryError("일별 거래대금 종목이 중복됩니다.")
                amounts[component] = {"daily_turnover_status": "available", "daily_turnover_million_krw": amount,
                    "daily_turnover_krw": won, "reported_daily_volume": volume,
                    "daily_turnover_scope": item["market_scope"], "daily_turnover_basis": item["basis"],
                    "daily_turnover_latest_attempt_failed": item.get("latest_attempt_failed", False)}
            turnover_status = daily["status"]
        except (HistoryError, OSError, ValueError, TypeError, KeyError, ArithmeticError):
            amounts, turnover_status = {}, "invalid_data"

    def series(kind, symbol, name):
        key = kind + "-" + symbol
        base = {"kind": kind, "symbol": symbol, "name": name,
                **amounts.get(key, {"daily_turnover_status": "invalid_data" if turnover_status == "invalid_data" else "not_collected"}),
                "latest_attempt_failed": snapshot.get("component_statuses", {}).get(key, {}).get("status") == "failed"}
        reference = snapshot["series"].get(key)
        if not reference:
            return {**base, "status": "not_collected"}
        try:
            value = read_component(root, reference)
            if any(value.get(field) != expected for field, expected in (
                    ("kind", kind), ("symbol", symbol), ("trade_date", day.isoformat()), ("data_mode", mode))):
                raise HistoryError("시장 snapshot 범위가 다릅니다.")
            bars = sorted(value["bars"], key=lambda bar: bar["at"])
            if not bars or any(not bar["at"].startswith(day.isoformat() + "T") for bar in bars):
                raise HistoryError("시장 snapshot의 시각을 확인하세요.")
            opening, closing = Decimal(bars[0]["open"]), Decimal(bars[-1]["close"])
            high = max(bars, key=lambda bar: Decimal(bar["high"]))
            low = min(bars, key=lambda bar: Decimal(bar["low"]))
            if opening <= 0:
                raise HistoryError("시가를 확인하세요.")
            with localcontext() as context:
                context.prec = 128
                change = format(((closing / opening - 1) * 100).quantize(Decimal("0.0001")), "f")
                spread = format(((Decimal(high["high"]) - Decimal(low["low"])) / opening * 100)
                                .quantize(Decimal("0.0001")), "f")
            total_volume = exact_sum([bar["volume"] for bar in bars])
            return {**base, "status": "available", "open": bars[0]["open"], "close": bars[-1]["close"],
                    "high": high["high"], "low": low["low"], "high_at": high["at"], "low_at": low["at"],
                    "change_pct": change, "range_pct": spread, "total_volume": total_volume,
                    "daily_volume_matches_stored": (Decimal(base["reported_daily_volume"]) == Decimal(total_volume)
                                                     if kind == "stock" and "reported_daily_volume" in base else None),
                    "bar_count": len(bars), "first_at": bars[0]["at"], "last_at": bars[-1]["at"],
                    "session_completeness": value.get("session_completeness", "unverified"),
                    "version": reference["run_id"]}
        except (HistoryError, OSError, ValueError, TypeError, KeyError, ArithmeticError):
            return {**base, "status": "invalid_data"}

    selection = read_component(root, snapshot["selection"]) if "selection" in snapshot else {}
    if selection and (selection.get("trade_date") != day.isoformat() or not isinstance(selection.get("items"), list)):
        raise HistoryError("선정 자료의 날짜·목록을 확인하세요.")
    metadata = {key: selection[key] for key in (
        "status", "policy", "observed_at", "market_scope", "provider_session_scope_verified",
        "rank_observation_date", "security_classification_as_of", "historical_security_eligibility_verified",
        "turnover_amount_available", "caveats",
    ) if key in selection}
    metadata["status"] = snapshot.get("selection_status", "not_collected")
    items = []
    for item in selection.get("items", []):
        items.append({**series("stock", item["symbol"], item.get("name") or item["symbol"]),
                      "rank": item["rank"], "provider_rank": item.get("provider_rank"),
                      "market_code": item.get("market_code"),
                      "turnover_million_krw": item.get("turnover_million_krw")})
    return {**empty, "status": "available", "selection": metadata, "items": items,
            "turnover": {"status": turnover_status, "available_count": len(amounts),
                         "stock_count": sum(key.startswith("stock-") for key in amounts),
                         "index_count": sum(key.startswith("index-") for key in amounts),
                         "basis": "provider_reported_daily", "minute_turnover_available": False},
            "indices": [series("index", symbol, name) for symbol, name in INDEX_NAMES.items()]}
