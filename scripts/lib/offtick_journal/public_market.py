"""검증된 시장 시세만 정적 웹용 JSON으로 내보낸다. 계좌·인증·원문 경로는 제외한다."""
from datetime import date
from decimal import Decimal
import hashlib
import gzip
import json

from offtick_history.common import HistoryError, atomic_write, json_text, number, output_lock
from offtick_history.market import read_component, read_day
from offtick_history.market_storage import gzip_bytes

from .market import overview, saved_days
from .us_market import UsMarket
from .market_codec import pack_bars

SUMMARY_FIELDS = ("kind", "symbol", "name", "status", "rank", "provider_rank", "market_code", "open", "high", "low", "close",
                  "high_at", "low_at", "change_pct", "range_pct", "total_volume", "bar_count", "first_at", "last_at",
                  "session_completeness", "latest_attempt_failed", "turnover_million_krw", "daily_turnover_status",
                  "daily_turnover_million_krw", "daily_turnover_krw", "reported_daily_volume", "daily_turnover_scope",
                  "daily_turnover_basis", "daily_turnover_latest_attempt_failed", "daily_volume_matches_stored")
SELECTION_FIELDS = ("status", "policy", "observed_at", "market_scope", "provider_session_scope_verified", "rank_observation_date",
                    "security_classification_as_of", "historical_security_eligibility_verified", "turnover_amount_available", "caveats")
US_FIELDS = ("kind", "symbol", "exchange", "name", "rank", "trade_date", "currency", "open", "high", "low", "close",
             "previous_close", "change_pct", "volume", "ma20", "ma60", "turnover_usd", "turnover_unit_status", "latest_attempt_failed",
             "turnover_display_usd", "turnover_krw", "turnover_basis", "turnover_is_estimate")
BAR_FIELDS = ("at", "open", "high", "low", "close", "volume")


def pick(value, fields):
    return {key: value[key] for key in fields if key in value}


def public_bars(root, day, kind, symbol):
    snapshot = read_day(root, day)
    value = read_component(root, snapshot["series"][kind + "-" + symbol])
    if any(value.get(key) != expected for key, expected in (("kind", kind), ("symbol", symbol), ("trade_date", day.isoformat()), ("data_mode", "live"))):
        raise HistoryError("공개할 분봉의 시장·날짜 범위가 다릅니다.")
    bars, previous = [], None
    for raw in value["bars"]:
        bar = pick(raw, BAR_FIELDS)
        if not bar["at"].startswith(day.isoformat() + "T") or previous and bar["at"] <= previous:
            raise HistoryError("공개할 분봉의 시각·정렬을 확인하세요.")
        previous = bar["at"]
        for field in BAR_FIELDS[1:]:
            bar[field] = number(bar[field])
        if (Decimal(bar["volume"]) < 0 or Decimal(bar["low"]) <= 0
                or not Decimal(bar["low"]) <= min(Decimal(bar["open"]), Decimal(bar["close"]))
                <= max(Decimal(bar["open"]), Decimal(bar["close"])) <= Decimal(bar["high"])):
            raise HistoryError("공개할 분봉의 수치를 확인하세요.")
        bars.append(bar)
    if not bars:
        raise HistoryError("공개할 분봉이 없습니다.")
    return {"status": "available", "kind": kind, "symbol": symbol, "trade_date": day.isoformat(), "data_mode": "live",
            "bars": bars, "session_completeness": value.get("session_completeness", "unverified"),
            "minute_turnover_verified": False,
            "latest_attempt_failed": False}


def export_market(kr_root, us_root, destination):
    # CLI와 웹 작업은 비공개 경로의 같은 잠금을 사용한다.
    with output_lock(kr_root.parent / ".private/public-export"):
        return _export_market(kr_root, us_root, destination)


def _export_market(kr_root, us_root, destination):
    """전체 입력을 검증한 후 파일을 교체하고 최종 manifest를 마지막에 쓴다."""
    files, kr_days, us_days = {}, [], []
    listing = saved_days(kr_root)
    if listing["unreadable_days"]:
        raise HistoryError("손상된 국내 거래일이 있어 공개 자료를 갱신하지 않았습니다.")
    for entry in listing["items"]:
        if entry["data_mode"] != "live":
            continue
        day = date.fromisoformat(entry["trade_date"])
        value = overview(kr_root, day, "live")
        rows = value["indices"] + value["items"]
        if (len(value["indices"]) != 2 or len(value["items"]) != 50
                or value["turnover"].get("status") != "complete" or value["turnover"].get("available_count") != 52
                or any(row["status"] != "available" or row.get("latest_attempt_failed")
                       or row.get("daily_turnover_latest_attempt_failed") for row in rows)):
            raise HistoryError("국내 52개 시계열·일별 금액이 모두 정상인 날짜만 공개할 수 있습니다.")
        public = {**pick(value, ("trade_date", "data_mode", "status", "return_basis")),
                  "selection": pick(value["selection"], SELECTION_FIELDS),
                  "turnover": pick(value["turnover"], ("status", "available_count", "stock_count", "index_count", "basis", "minute_turnover_available")),
                  "indices": [pick(row, SUMMARY_FIELDS) for row in value["indices"]],
                  "items": [pick(row, SUMMARY_FIELDS) for row in value["items"]]}
        prefix = "kr/" + day.isoformat() + "/"
        files[prefix + "overview.json"] = public
        for row in rows:
            files[prefix + row["kind"] + "-" + row["symbol"] + ".json"] = public_bars(kr_root, day, row["kind"], row["symbol"])
        kr_days.append(pick(entry, ("trade_date", "data_mode", "series_count", "selection_status")))
    if us_root:
        service = UsMarket(us_root)
        for path in sorted((us_root / "market/days").glob("????-??-??.json"), reverse=True):
            day = date.fromisoformat(path.stem)
            value = service.saved(day)
            if value["status"] != "complete":
                raise HistoryError("미국 52개 일별 자료가 정상인 날짜만 공개할 수 있습니다.")
            files["us/" + day.isoformat() + "/daily.json"] = {
                **pick(value, ("trade_date", "status", "record_count", "turnover_unit_unverified_count", "turnover_estimate_count", "exchange_rate", "observed_at",
                               "rank_source", "rank_date_explicit_in_response", "regular_session_scope_verified")),
                "indices": [pick(row, US_FIELDS) for row in value["indices"]],
                "items": [pick(row, US_FIELDS) for row in value["items"]]}
            us_days.append({"trade_date": day.isoformat(), "record_count": value["record_count"]})
    files["kr/days.json"] = {"items": kr_days, "unreadable_days": 0}
    files["us/days.json"] = {"items": us_days}
    encoded, references, original_bytes = {}, {}, 0
    try:
        previous = json.loads((destination / "index.json").read_text()).get("files", {})
    except (OSError, ValueError, TypeError):
        previous = {}
    for logical, value in files.items():
        original_bytes += len((json_text(value) + "\n").encode())
        packed_value = pack_bars(value) if "bars" in value else value
        content = (json_text(packed_value) + "\n").encode()
        compressed = logical not in {"kr/days.json", "us/days.json"}
        filename = logical + ".gz" if compressed else logical
        stored = None
        # 동일 입력은 기존 gzip 바이트를 재사용해 압축 라이브러리 갱신 시에도 이력을 불필요하게 늘리지 않는다.
        old = previous.get(logical, {})
        if old.get("file") == filename and old.get("content_sha256") == hashlib.sha256(content).hexdigest():
            try:
                existing = (destination / filename).read_bytes()
                if hashlib.sha256(existing).hexdigest() == old["sha256"] and (gzip.decompress(existing) if compressed else existing) == content:
                    stored = existing
            except (OSError, ValueError, EOFError, KeyError):
                pass
        if stored is None:
            stored = gzip_bytes(content) if compressed else content
        encoded[filename] = stored
        references[logical] = {"file": filename, "sha256": hashlib.sha256(stored).hexdigest(), "bytes": len(stored),
                               "content_sha256": hashlib.sha256(content).hexdigest(), "uncompressed_bytes": len(content),
                               "encoding": "gzip" if compressed else "json", "format": "minute_rows_v1" if "bars" in value else "json"}
    manifest = {"schema_version": 2, "data_mode": "live", "purpose": "public_market_prices_only",
                "kr_days": [entry["trade_date"] for entry in kr_days], "us_days": [entry["trade_date"] for entry in us_days],
                "files": references}
    for path, content in encoded.items():
        target = destination / path
        if not target.is_file() or target.read_bytes() != content:
            atomic_write(target, content)
    index = (json_text(manifest) + "\n").encode()
    if not (destination / "index.json").is_file() or (destination / "index.json").read_bytes() != index:
        atomic_write(destination / "index.json", index)
    # 새 manifest에 대응하는 기존 공개 JSON만 정리한다. 원문과 다른 파일은 유지한다.
    for logical, reference in references.items():
        legacy = destination / logical
        if reference["file"] != logical and legacy.is_file():
            legacy.unlink()
    return {"kr_days": manifest["kr_days"], "us_days": manifest["us_days"], "file_count": len(encoded) + 1,
            "bytes": sum(len(content) for content in encoded.values()) + len(index), "source_json_bytes": original_bytes}
