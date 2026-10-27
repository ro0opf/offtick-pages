"""미국 일별 시장 자료. 키움 50종목과 Yahoo 나스닥 종합·다우를 독립 보관한다."""
import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib import error, parse, request

from .common import HistoryError, atomic_write, date_arg, json_text, load_env, number, output_lock, product, write_json
from .kiwoom import Client, NoRedirect, checked_body
from .market import MarketRun
from .market_storage import read_market_bytes, write_market_bytes
from .schema import SEOUL
from .us_calendar import CALENDAR_SOURCE, CALENDAR_VERIFIED_ON, NEW_YORK, latest_completed, previous_session, session

TICKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,19}")
INDICES = {"IXIC": ("^IXIC", "나스닥 종합"), "DJI": ("^DJI", "다우 산업")}


def read_ref(root, reference):
    if not isinstance(reference, dict):
        raise HistoryError("미국 snapshot 참조는 객체여야 합니다.")
    relative = reference.get("file", "")
    if not re.fullmatch(r"runs/[0-9TZ]+-[a-f0-9]{8}/(?:selection|daily)\.json", relative):
        raise HistoryError("미국 snapshot 참조가 올바르지 않습니다.")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise HistoryError("허용되지 않은 미국 snapshot 경로입니다.")
    content = read_market_bytes(path)
    if hashlib.sha256(content).hexdigest() != reference.get("sha256"):
        raise HistoryError("미국 snapshot 해시가 일치하지 않습니다.")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise HistoryError("미국 snapshot 형식을 확인하세요.")
    return value


def read_day(root, day):
    path = root / "market/days" / (day.isoformat() + ".json")
    if not path.exists():
        return {"market": "US", "trade_date": day.isoformat()}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("market") != "US" or value.get("trade_date") != day.isoformat():
        raise HistoryError("미국 거래일 snapshot 범위를 확인하세요.")
    return value


def select_stocks(run, rank_source, now, count=50):
    current = now.astimezone(NEW_YORK)
    if rank_source == "current":
        target_session = session(run.day)
        if current.date() != run.day or not target_session or current < target_session["close"]:
            raise HistoryError("당일 순위는 해당 미국 세션 종료 후에만 보관합니다.")
        api = "usa20540"
        query = {"stex_tp": "0", "inds_cd": "000", "stk_tp": "1", "trde_qty_tp": "0", "stk_cnd": "0", "pric_cnd": "0", "trde_prica_cnd": "0"}
    else:
        if previous_session(current.date())["trade_date"] != run.day.isoformat():
            raise HistoryError("전일 순위를 요청한 과거 날짜에 연결할 수 없습니다.")
        api = "usa20960"
        query = {"stex_tp": "0", "inds_cd": "000", "stk_tp": "1", "qry_tp": "1"}
    selected, seen, ranks, previous_rank = [], set(), set(), 0
    for _, rows, source in run.pages(api, query, "us-ranking", "result_list"):
        for i, row in enumerate(rows, 1):
            ticker, exchange = str(row.get("stk_cd", "")).strip(), row.get("stex_tp")
            if not TICKER.fullmatch(ticker) or ".." in ticker or exchange not in {"ND", "NY", "NA"}:
                raise HistoryError("미국 순위의 종목·거래소 코드를 확인하세요.")
            rank = Decimal(number(row.get("rank"), positive=True))
            if rank != rank.to_integral_value() or int(rank) <= previous_rank or (exchange, ticker) in seen or int(rank) in ranks:
                raise HistoryError("미국 순위가 중복되거나 올바른 정수가 아닙니다.")
            previous_rank = int(rank)
            seen.add((exchange, ticker)); ranks.add(int(rank))
            detail = run.client.post("usa10100", {"stex_tp": exchange, "stk_cd": ticker})
            raw = run.client.scrub(detail.content)
            path = run.path / "raw/us-types" / (exchange + "-" + ticker + ".json")
            write_market_bytes(path, raw)
            body = checked_body(detail)
            if body.get("stex_tp") != exchange or body.get("stk_cd") != ticker or body.get("isEtf") not in {"N", "Y"}:
                raise HistoryError("미국 종목 유형 응답 범위를 확인하세요.")
            if body["isEtf"] == "Y":
                continue
            selected.append({"symbol": ticker, "exchange": exchange, "name": row.get("stk_nm") or ticker,
                "english_name": row.get("stk_enm"), "provider_rank": int(rank), "rank": len(selected) + 1,
                "sector": body.get("upgb"), "source_run_id": run.id,
                "source": {**source, "row": i}, "type_source": {"file": str(path.relative_to(run.path)), "sha256": hashlib.sha256(raw).hexdigest()}})
            if len(selected) == count:
                return {"trade_date": run.day.isoformat(), "market": "US", "data_mode": run.client.data_mode, "items": selected,
                    "observed_at": now.isoformat(), "rank_source": rank_source,
                    "policy": "provider_stock_filter_non_etf_top50_v1",
                    "rank_date_basis": "provider_current_or_previous_session_and_exchange_calendar",
                    "rank_date_explicit_in_response": False, "regular_session_scope_verified": False,
                    "scope_note": "제공자 누적 순위입니다. 정규장만의 순위·미국 전체 시장 완전성·ADR/우선주 등 세부 유형은 미검증입니다."}
    raise HistoryError("유형 확인 후 미국 50종목을 확보하지 못했습니다.")


def stock_daily(run, item):
    query = {"stex_tp": item["exchange"], "stk_cd": item["symbol"], "base_dt": run.day.strftime("%Y%m%d")}
    component = "us-daily-" + item["exchange"] + "-" + item["symbol"]
    for _, rows, source in run.pages("usa20590", query, component, "result_list"):
        matches = [row for row in rows if row.get("dt") == query["base_dt"]]
        if not matches:
            dates = [str(row.get("dt", "")) for row in rows]
            if dates and all(re.fullmatch(r"\d{8}", value) for value in dates) and max(dates) < query["base_dt"]:
                break
            continue
        if len(matches) != 1:
            raise HistoryError("미국 일별 주가의 목표 날짜가 중복됩니다.")
        raw = matches[0]
        prices = {name: number(Decimal(number(raw.get(field))).copy_abs()) for name, field in (
            ("open", "open_pric"), ("high", "high_pric"), ("low", "low_pric"), ("close", "cur_prc"))}
        if not (0 < Decimal(prices["low"]) <= min(Decimal(prices["open"]), Decimal(prices["close"]))
                <= max(Decimal(prices["open"]), Decimal(prices["close"])) <= Decimal(prices["high"])):
            raise HistoryError("미국 일별 OHLC 가격 범위가 올바르지 않습니다.")
        volume, amount = number(raw.get("acc_trde_qty")), number(raw.get("trde_prica"))
        if Decimal(volume) < 0 or Decimal(amount) < 0:
            raise HistoryError("미국 거래량·거래대금은 음수일 수 없습니다.")
        lower, upper = Decimal(product(prices["low"], volume)), Decimal(product(prices["high"], volume))
        documented = Decimal(product(amount, "1000"))
        consistent = lower <= documented <= upper
        raw_consistent = lower <= Decimal(amount) <= upper
        return {"kind": "stock", "symbol": item["symbol"], "exchange": item["exchange"], "name": item["name"],
            "trade_date": run.day.isoformat(), "currency": "USD", **prices, "volume": volume,
            "previous_close": number(Decimal(number(raw.get("base_pric"))).copy_abs()),
            "change_pct": number(raw.get("flu_rt")), "turnover_raw": amount,
            "turnover_reported_unit": "thousand_USD", "turnover_usd": number(documented) if consistent else None,
            "turnover_unit_status": "documented_unit_consistent_with_price_volume" if consistent else "documentation_response_inconsistent",
            "raw_amount_fits_usd_price_volume_bounds": raw_consistent,
            "adjustment": "provider_reported_unverified", "regular_session_scope_verified": False,
            "source_run_id": run.id, "source": {**source, "row": rows.index(raw) + 1},
            "api_id": "usa20590", "collected_at": datetime.now(SEOUL).isoformat()}
    raise HistoryError("해당 미국 거래일의 일별 주가가 없습니다.")


def yahoo_fetch(ticker, start, end):
    query = parse.urlencode({"interval": "1d", "period1": int(start.timestamp()), "period2": int(end.timestamp())})
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + parse.quote(ticker, safe="") + "?" + query
    req = request.Request(url, headers={"User-Agent": "offtick/1.0 personal-market-journal", "Accept": "application/json"})
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            return response.read(), url
    except (error.URLError, OSError, TimeoutError):
        raise HistoryError("공개 지수 일별 자료를 조회하지 못했습니다.") from None


def index_daily(run, code, fetch=yahoo_fetch):
    ticker, name = INDICES[code]
    start = datetime.combine(run.day - timedelta(days=130), time(), NEW_YORK)
    end = datetime.combine(run.day + timedelta(days=1), time(), NEW_YORK)
    content, url = fetch(ticker, start, end)
    path = run.path / "raw/indices" / (code + ".json")
    write_market_bytes(path, content)
    source = {"file": str(path.relative_to(run.path)), "sha256": hashlib.sha256(content).hexdigest()}
    write_json(path.with_suffix(".meta.json"), {"url": url, "provider": "Yahoo Finance public chart", "requested_trade_date": run.day.isoformat(), "sha256": source["sha256"]})
    result = json.loads(content, parse_float=Decimal)["chart"]["result"][0]
    if result["meta"]["symbol"] != ticker or result["meta"]["exchangeTimezoneName"] != "America/New_York":
        raise HistoryError("공개 지수 응답의 심볼·거래소 시간대가 다릅니다.")
    quotes, history = result["indicators"]["quote"][0], []
    for i, stamp in enumerate(result["timestamp"]):
        day = datetime.fromtimestamp(stamp, NEW_YORK).date()
        if day > run.day or any(quotes[field][i] is None for field in ("open", "high", "low", "close")):
            continue
        if not session(day):
            raise HistoryError("지수 일봉에 거래소 휴장일이 포함됐습니다.")
        prices = {field: format(Decimal(number(quotes[field][i])).quantize(Decimal("0.01")), "f") for field in ("open", "high", "low", "close")}
        if not (0 < Decimal(prices["low"]) <= min(Decimal(prices["open"]), Decimal(prices["close"]))
                <= max(Decimal(prices["open"]), Decimal(prices["close"])) <= Decimal(prices["high"])):
            raise HistoryError("공개 지수의 일별 가격 범위가 올바르지 않습니다.")
        history.append({"trade_date": day.isoformat(), **prices})
    history.sort(key=lambda row: row["trade_date"])
    if len({row["trade_date"] for row in history}) != len(history) or not history or history[-1]["trade_date"] != run.day.isoformat():
        raise HistoryError("공개 지수의 요청 거래일 자료가 아직 없거나 날짜가 중복됩니다.")
    target = history[-1]
    prior = history[-2] if len(history) >= 2 else None
    if prior and prior["trade_date"] != previous_session(run.day)["trade_date"]:
        prior = None
    with localcontext() as context:
        context.prec = 64
        change = format(((Decimal(target["close"]) / Decimal(prior["close"]) - 1) * 100).quantize(Decimal("0.0001")), "f") if prior else None
        averages = {"ma" + str(count): format((sum(Decimal(row["close"]) for row in history[-count:]) / count).quantize(Decimal("0.01")), "f") if len(history) >= count else None for count in (20, 60)}
    return {"kind": "index", "symbol": code, "provider_symbol": ticker, "name": name, **target,
            "previous_close": prior["close"] if prior else None, "change_pct": change, **averages,
            "currency": "index_point", "volume": None, "turnover_usd": None, "history": history,
            "provider": "Yahoo Finance public chart", "provider_api_status": "unofficial_best_effort",
            "source_run_id": run.id, "source": source, "collected_at": datetime.now(SEOUL).isoformat()}


def collect_us(client, day, root, *, rank_source="current", now=None, fetch=yahoo_fetch, progress=None):
    now = now or datetime.now(SEOUL)
    market_session = session(day)
    if not market_session or market_session["close"] > now:
        raise HistoryError("완료된 미국 거래 세션을 지정하세요.")
    expected = {"market": "US", "schema_version": 1, "data_mode": client.data_mode}
    profile = root / "profile.json"
    if profile.exists() and json.loads(profile.read_text()) != expected:
        raise HistoryError("국내·미국 또는 실전·데모 저장 경로를 섞을 수 없습니다.")
    write_json(profile, expected)
    snapshot = read_day(root, day)
    run = MarketRun(client, root, day, 20)
    run.manifest.update({"market": "US", "frequency": "1d", "calendar_source": CALENDAR_SOURCE,
                         "calendar_verified_on": CALENDAR_VERIFIED_ON})
    market_root = root / "market"
    try:
        if "selection" in snapshot:
            selection = read_ref(market_root, snapshot["selection"])
            if (selection.get("market") != "US" or selection.get("trade_date") != day.isoformat()
                    or selection.get("data_mode") != client.data_mode):
                raise HistoryError("미국 선정 자료의 범위가 다릅니다.")
        else:
            selection = select_stocks(run, rank_source, now)
            snapshot["selection"] = run.publish("selection", selection)
            write_json(market_root / "days" / (day.isoformat() + ".json"), snapshot)
        members = selection.get("items", [])
        if (len(members) != 50 or len({(item["exchange"], item["symbol"]) for item in members}) != 50
                or any(item["exchange"] not in {"ND", "NY", "NA"} or not TICKER.fullmatch(item["symbol"]) or ".." in item["symbol"] for item in members)):
            raise HistoryError("보존된 미국 50종목의 범위·코드를 확인하세요.")
        previous = read_ref(market_root, snapshot["daily"]) if "daily" in snapshot else {}
        if previous and (previous.get("market") != "US" or previous.get("trade_date") != day.isoformat() or previous.get("data_mode") != client.data_mode):
            raise HistoryError("미국 일별 자료의 날짜·실전 범위를 확인하세요.")
        records = {(row["kind"], row.get("exchange", ""), row["symbol"]): row for row in previous.get("records", [])}
        actions = [("index-" + code, lambda code=code: index_daily(run, code, fetch), ("index", "", code)) for code in INDICES]
        actions += [("stock-" + item["exchange"] + "-" + item["symbol"], lambda item=item: stock_daily(run, item), ("stock", item["exchange"], item["symbol"])) for item in selection["items"]]
        for component, action, identity in actions:
            try:
                records[identity] = {**action(), "latest_attempt_failed": False}
                run.manifest["components"][component] = {"status": "complete"}
            except (HistoryError, OSError, ValueError, KeyError, TypeError, ArithmeticError):
                run.manifest["components"][component] = {"status": "failed", "reason": "조회·날짜·단위·응답 형식을 확인하세요. 원본과 이전 정상 자료는 보존했습니다."}
                if identity in records:
                    records[identity] = {**records[identity], "latest_attempt_failed": True}
            run.save()
            if progress:
                progress(component, run.manifest["components"][component])
        failed = sum(value["status"] == "failed" for value in run.manifest["components"].values())
        unverified = sum(row.get("kind") == "stock" and row.get("turnover_usd") is None for row in records.values())
        status = "complete" if not failed and len(records) == 52 else "partial"
        daily = {"market": "US", "trade_date": day.isoformat(), "data_mode": client.data_mode,
                 "frequency": "1d", "records": list(records.values()), "collection_status": status,
                 "quality_status": "turnover_unit_confirmation_needed" if unverified else "reported_units_checked",
                 "turnover_unit_unverified_count": unverified, "session_open": market_session["open"].isoformat(),
                 "session_close": market_session["close"].isoformat(), "observed_at": now.isoformat()}
        snapshot.update({"data_mode": client.data_mode, "daily": run.publish("daily", daily),
                         "last_attempt": run.id, "collection_status": status,
                         "quality_status": daily["quality_status"], "record_count": len(records)})
        write_json(market_root / "days" / (day.isoformat() + ".json"), snapshot)
        run.manifest.update({"status": status, "completed_at": datetime.now(SEOUL).isoformat()})
        summary = {"trade_date": day.isoformat(), "collection_status": status, "record_count": len(records),
                "failed_count": failed, "turnover_unit_unverified_count": unverified,
                "quality_status": daily["quality_status"], "selection_source": selection["rank_source"],
                "directory": str(run.path), "daily": snapshot["daily"]}
        write_json(root / "reports" / (day.isoformat() + "-summary.json"), summary)
        return summary
    except BaseException:
        run.manifest["status"] = "interrupted_or_failed"
        raise
    finally:
        run.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date_arg)
    parser.add_argument("--due", action="store_true", help="미국 세션 종료 20분 후에만 수집. 완료 거래일은 건너뜀")
    parser.add_argument("--rank-source", choices=("current", "previous"), default="current")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        now = datetime.now(SEOUL)
        if bool(args.date) == bool(args.due):
            raise HistoryError("--date 또는 --due 중 하나만 지정하세요.")
        target = latest_completed(now) if args.due else session(args.date)
        if not target:
            print(json_text({"status": "market_closed", "trade_date": args.date.isoformat()}))
            return 0
        day = date.fromisoformat(target["trade_date"])
        with output_lock(args.output):
            existing = read_day(args.output, day)
            if args.due and existing.get("collection_status") == "complete":
                if "daily" not in existing:
                    raise HistoryError("완료된 미국 거래일의 snapshot 참조가 없습니다.")
                daily = read_ref(args.output / "market", existing["daily"])
                if daily.get("market") != "US" or daily.get("trade_date") != day.isoformat() or len(daily.get("records", [])) != 52:
                    raise HistoryError("완료된 미국 거래일의 snapshot 범위를 확인하세요.")
                print(json_text({"status": "already_collected", "trade_date": day.isoformat()}))
                return 0
            if args.due and "selection" not in existing and (now - target["close"]) > timedelta(hours=2):
                print(json_text({"status": "outside_capture_window", "trade_date": day.isoformat()}))
                return 0
            load_env(args.env_file)
            client = Client(os.environ.get("KIWOOM_APP_KEY", "").strip(), os.environ.get("KIWOOM_APP_SECRET", "").strip(), args.output)
            result = collect_us(client, day, args.output, rank_source=args.rank_source, now=now,
                                progress=lambda component, value: print(component + ": " + value["status"], flush=True))
        print(json_text(result))
        return 0 if result["collection_status"] == "complete" else 1
    except HistoryError as error:
        print("미국 일별 수집 실패: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("미국 일별 수집 실패. 키·허용 IP·미국 거래일·보존된 원본을 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
