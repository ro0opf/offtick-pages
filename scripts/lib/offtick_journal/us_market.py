"""미국 저장 자료 조회와 웹 방문 시 실행하는 단일 백그라운드 수집 작업."""
from datetime import date, datetime
import os
from threading import RLock, Thread

from offtick_history.common import HistoryError, load_env, output_lock
from offtick_history.kiwoom import Client
from offtick_history.schema import SEOUL
from offtick_history.us_calendar import NEW_YORK, latest_completed
from offtick_history.us_market import collect_us, read_day, read_ref

from .common import JournalError
from .us_valuation import collect_exchange_rate, read_exchange_rate, turnover_value


class UsMarket:
    def __init__(self, root, env_file=None, *, clock=None, runner=None, fx_runner=None, publisher=None):
        self.root, self.env_file = root, env_file
        self.clock = clock or (lambda: datetime.now(SEOUL))
        self.runner = runner or self.collect
        self.fx_runner = fx_runner or (lambda day: collect_exchange_rate(self.root, day))
        self.publisher = publisher
        self.lock = RLock()
        self.job = None
        self.auto_attempts = set()

    def saved(self, day):
        empty = {"trade_date": day.isoformat(), "status": "not_collected", "record_count": 0,
                 "indices": [], "items": [], "turnover_unit_unverified_count": 0,
                 "exchange_rate": read_exchange_rate(self.root, day), "turnover_estimate_count": 0}
        if not self.root:
            return empty
        try:
            snapshot = read_day(self.root, day)
            if "daily" not in snapshot:
                return empty
            daily = read_ref(self.root / "market", snapshot["daily"])
            selection = read_ref(self.root / "market", snapshot["selection"])
            for value in (daily, selection):
                if value.get("market") != "US" or value.get("trade_date") != day.isoformat() or value.get("data_mode") != "live":
                    raise ValueError("scope")
            records = daily["records"]
            identities = {(row["kind"], row.get("exchange", ""), row["symbol"]) for row in records}
            members = {(row["exchange"], row["symbol"]): row for row in selection["items"]}
            expected = {("index", "", "IXIC"), ("index", "", "DJI")} | {("stock", exchange, symbol) for exchange, symbol in members}
            if len(members) != 50 or len(identities) != len(records) or not identities <= expected or any(row["trade_date"] != day.isoformat() for row in records):
                raise ValueError("records")
            complete = (identities == expected and daily.get("collection_status") == "complete"
                        and snapshot.get("collection_status") == "complete"
                        and not any(row.get("latest_attempt_failed") for row in records))
            fields = ("kind", "symbol", "exchange", "name", "trade_date", "currency", "open", "high", "low", "close",
                      "previous_close", "change_pct", "volume", "ma20", "ma60", "turnover_usd", "turnover_unit_status", "latest_attempt_failed", "price_provider", "price_basis")
            public = [{**{key: row[key] for key in fields if key in row},
                       **({"rank": members[(row["exchange"], row["symbol"])]["rank"],
                           **turnover_value(row, empty["exchange_rate"])} if row["kind"] == "stock" else {})} for row in records]
            return {**empty, "status": "complete" if complete else "partial", "record_count": len(records),
                    "indices": [row for row in public if row["kind"] == "index"],
                    "items": sorted((row for row in public if row["kind"] == "stock"), key=lambda row: row["rank"]),
                    "turnover_unit_unverified_count": daily.get("turnover_unit_unverified_count", 0),
                    "turnover_estimate_count": sum(bool(row.get("turnover_is_estimate")) for row in public),
                    "observed_at": daily.get("observed_at"), "rank_source": selection.get("rank_source"),
                    "rank_date_explicit_in_response": selection.get("rank_date_explicit_in_response", False),
                    "regular_session_scope_verified": False}
        except (HistoryError, OSError, ValueError, TypeError, KeyError):
            return {**empty, "status": "invalid_data"}

    def days(self):
        items, unreadable = [], 0
        if self.root:
            for path in sorted((self.root / "market/days").glob("????-??-??.json"), reverse=True):
                try:
                    value = self.saved(date.fromisoformat(path.stem))
                    if value["status"] not in {"complete", "partial"}:
                        raise ValueError("invalid day")
                    items.append({key: value[key] for key in ("trade_date", "status", "record_count")})
                except (ValueError, OSError):
                    unreadable += 1
        return {"items": items, "unreadable_days": unreadable}

    def status(self):
        with self.lock:
            try:
                target = latest_completed(self.clock())
            except HistoryError:
                return {"enabled": bool(self.root and self.env_file), "status": "calendar_error", "job": self.job,
                        "message": "미국 거래소 달력을 갱신해야 합니다."}
            saved = self.saved(date.fromisoformat(target["trade_date"]))
            return {**saved, "enabled": bool(self.root and self.env_file), "job": self.job, "public_publish_enabled": bool(self.publisher),
                    "session_close": target["close"].isoformat(), "delay_minutes": 20}

    def start(self, trigger):
        with self.lock:
            state = self.status()
            if not state["enabled"]:
                if trigger == "manual":
                    raise JournalError("이 서버에는 미국 수집 경로가 연결되지 않았습니다.", 409)
                return state
            ready = state["status"] == "complete" and state.get("exchange_rate")
            retry_publication = self.publisher and self.job and self.job.get("phase") == "publish" and self.job["state"] == "failed"
            publish_requested = trigger == "manual" and self.publisher
            if (state["status"] == "calendar_error" or ready and not (retry_publication or publish_requested)
                    or self.job and self.job["state"] == "running"):
                return state
            day = state["trade_date"]
            # 같은 웹 서버에서 자동 실패를 반복하지 않는다. 수동 재시도는 항상 허용한다.
            if trigger == "auto" and day in self.auto_attempts:
                return state
            self.auto_attempts.add(day)
            self.job = {"state": "running", "trade_date": day, "processed": state["record_count"] if state["status"] == "complete" else 0,
                        "started_at": self.clock().isoformat(), "message": "원화 환산용 환율 조회 중" if state["status"] == "complete" else "미국 일별 자료 수집 중"}
            Thread(target=self.work, args=(date.fromisoformat(day),), daemon=True).start()
            return {**state, "job": dict(self.job)}

    def work(self, day):
        phase = "collect"
        try:
            if self.saved(day)["status"] != "complete":
                self.runner(day, self.progress)
            if not read_exchange_rate(self.root, day):
                with self.lock:
                    self.job = {**self.job, "message": "원화 환산용 환율 조회 중"}
                try:
                    self.fx_runner(day)
                except Exception:
                    # 환율 실패는 저장된 시세의 완료 상태와 별개다.
                    pass
            state = self.saved(day)
            success = state["status"] == "complete" and bool(state.get("exchange_rate"))
            message = ("수집 완료 · 원화 환산 준비 완료" if success else
                       "가격 자료는 보관했습니다. 환율을 조회하지 못해 원화 환산을 보류했습니다. 환율 다시 조회를 눌러주세요."
                       if state["status"] == "complete" else "일부 자료를 확보하지 못했습니다. 수집 버튼으로 다시 시도하세요.")
            if success and self.publisher:
                phase = "publish"
                with self.lock:
                    self.job = {**self.job, "phase": phase, "message": "Pages 자료 갱신 중"}
                self.publisher()
                message = "수집 완료 · Git 저장 및 Pages 갱신 요청 완료"
        except Exception:
            # 공급자 오류 문자열에 인증값·로컬 경로가 포함될 수 있으므로 공개하지 않는다.
            success = False
            message = ("자료는 보관했습니다. Git 저장·Pages 갱신에 실패했습니다. 다시 시도하세요." if phase == "publish"
                       else "수집에 실패했습니다. 키·등록 IP·인터넷 연결을 확인하고 다시 시도하세요.")
        with self.lock:
            self.job = {**self.job, "state": "succeeded" if success else "failed", "phase": phase, "message": message,
                        "finished_at": self.clock().isoformat()}

    def progress(self, component, value):
        with self.lock:
            self.job = {**self.job, "processed": self.job["processed"] + 1}

    def collect(self, day, progress):
        with output_lock(self.root):
            # 다른 웹 프로세스가 먼저 끝냈을 수도 있다.
            if self.saved(day)["status"] == "complete":
                return
            now = self.clock()
            source = "current" if now.astimezone(NEW_YORK).date() == day else "previous"
            load_env(self.env_file)
            client = Client(os.environ.get("KIWOOM_APP_KEY", "").strip(), os.environ.get("KIWOOM_APP_SECRET", "").strip(), self.root)
            collect_us(client, day, self.root, rank_source=source, now=now, progress=progress)
