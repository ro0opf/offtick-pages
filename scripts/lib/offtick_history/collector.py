"""키움 kt00009의 체결번호·체결시각이 있는 개별 체결을 수집합니다."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from .common import HistoryError, atomic_write, date_arg, json_text, load_env, output_lock, write_json, write_jsonl
from .kiwoom import ACCOUNT_API_ID, ROWS_KEY, Client, Reply, checked_body, checked_rows, next_cursor
from .schema import API_ID, API_PATH, SEOUL, InvalidExecution, merge_execution, normalize_execution


def verify_scope(root: Path, mode: str) -> None:
    profile = root / "profile.json"
    if profile.is_file():
        existing = json.loads(profile.read_text())
        if existing != {"broker": "kiwoom", "data_mode": mode, "schema_version": 1}:
            raise HistoryError("증권사·실제·데모 데이터는 서로 다른 데이터 폴더를 사용하세요.")


def account_alias(root: Path, number: str) -> str:
    path = root / ".private/account-aliases.json"
    mapping = json.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) or
        not re.fullmatch(r"\d{10,12}", k) or not re.fullmatch(r"kiwoom-account-\d{3,}", v) for k, v in mapping.items()):
        raise HistoryError("계좌 별칭 매핑 파일이 올바르지 않습니다.")
    if len(set(mapping.values())) != len(mapping):
        raise HistoryError("계좌 별칭 매핑이 중복됩니다.")
    saved_aliases = set()
    for previous_path in (root / "runs").glob("*/manifest.json"):
        previous = json.loads(previous_path.read_text())
        if previous.get("broker") == "kiwoom" and previous.get("status") == "complete":
            saved_aliases.add(previous["account_alias"])
    if not saved_aliases.issubset(set(mapping.values())):
        raise HistoryError("기존 체결의 비공개 계좌 매핑을 복원하세요. 매핑 없이 기존 별칭을 새 계좌에 재사용하지 않습니다.")
    if number not in mapping:
        next_id = max((int(v.rsplit("-", 1)[1]) for v in mapping.values()), default=0) + 1
        mapping[number] = f"kiwoom-account-{next_id:03d}"
        write_json(path, mapping)
    return mapping[number]


def collect(client: Client, start: date, end: date, root: Path, max_pages: int = 10000,
            expected_account: str = "") -> dict:
    if start > end or max_pages < 1:
        raise HistoryError("조회 기간·최대 페이지 수를 확인하세요.")
    verify_scope(root, client.data_mode)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(root / "profile.json", {"broker": "kiwoom", "data_mode": client.data_mode, "schema_version": 1})
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run = root / "runs" / run_id
    run.mkdir(parents=True, mode=0o700)
    manifest = {"schema_version": 1, "broker": "kiwoom", "data_mode": client.data_mode, "run_id": run_id,
                "status": "running", "api_id": API_ID, "endpoint": API_PATH, "market_scope": "%",
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "date_basis": "requested_order_date", "execution_time_required": True,
                "time_precision": "second", "history_limit_verified": False,
                "record_count": 0, "raw_record_count": 0, "duplicate_rows": 0, "rejected_count": 0, "days": []}
    write_json(run / "manifest.json", manifest)
    records, rejected = {}, []
    try:
        account = checked_body(client.post(ACCOUNT_API_ID, {})).get("acctNo")
        if not isinstance(account, str):
            raise HistoryError("앱에 연결된 단일 계좌번호를 확인할 수 없습니다.")
        account = account.strip().replace("-", "")
        if not re.fullmatch(r"\d{10,12}", account):
            raise HistoryError("앱에 연결된 계좌번호 형식을 확인하세요.")
        if expected_account and account != expected_account.replace("-", "").strip():
            raise HistoryError("키움 앱의 조회 계좌가 KIWOOM_ACCOUNT_NO와 다릅니다.")
        alias = account_alias(root, account)
        manifest["account_alias"] = alias
        previously_nonempty = set()
        for previous_path in (root / "runs").glob("*/manifest.json"):
            previous = json.loads(previous_path.read_text())
            if (previous.get("status") == "complete" and previous.get("broker") == "kiwoom"
                    and previous.get("data_mode") == client.data_mode and previous.get("account_alias") == alias):
                previously_nonempty.update(w["date"] for w in previous.get("days", []) if w.get("record_count", 0) > 0)
        day = start
        while day <= end:
            window = {"date": day.isoformat(), "account_alias": alias, "status": "running", "pages": 0,
                      "raw_record_count": 0, "record_count": 0, "duplicate_rows": 0, "rejected_count": 0}
            manifest["days"].append(window)
            write_json(run / "manifest.json", manifest)
            query = {"ord_dt": day.strftime("%Y%m%d"), "stk_bond_tp": "1", "mrkt_tp": "0", "sell_tp": "0",
                     "qry_tp": "1", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "%"}
            raw_dir = run / "raw" / alias / day.isoformat()
            day_records = {}
            flag, cursor, seen = "N", "", set()
            hashes = []
            for page in range(1, max_pages + 1):
                reply = client.post(API_ID, query, flag, cursor)
                content = client.scrub(reply.content)
                path = raw_dir / f"page-{page:05d}.json"
                atomic_write(path, content)
                hashes.append(hashlib.sha256(content).hexdigest())
                write_json(path.with_suffix(".meta.json"), {"http_status": reply.status,
                    "credential_redacted": content != reply.content,
                    "pagination_headers": {k: v for k, v in reply.headers.items() if k in {"cont-yn", "next-key", "api-id"}}})
                if reply.headers.get("api-id", API_ID) != API_ID:
                    raise HistoryError("응답의 API ID가 요청과 다릅니다.")
                rows = checked_rows(reply)
                window["raw_record_count"] += len(rows)
                manifest["raw_record_count"] += len(rows)
                for index, raw in enumerate(rows, 1):
                    source = {"file": str(path.relative_to(run)), "row": index, "endpoint": API_PATH, "api_id": API_ID, "sha256": hashes[-1]}
                    try:
                        record = normalize_execution(raw, day, alias, source, client.data_mode)
                        if merge_execution(day_records, record):
                            window["duplicate_rows"] += 1
                            manifest["duplicate_rows"] += 1
                    except InvalidExecution as exc:
                        rejected.append({"date": day.isoformat(), "account_alias": alias,
                                         "reason": exc.reason, "source": source})
                        window["rejected_count"] += 1
                        manifest["rejected_count"] += 1
                window["pages"] = page
                following = next_cursor(reply, seen)
                if following is None:
                    break
                flag, cursor = following
            else:
                raise HistoryError("최대 페이지 수를 초과했습니다. 조회를 완료로 처리하지 않았습니다.")
            if not window["raw_record_count"] and day.isoformat() in previously_nonempty:
                # 체결 이벤트를 쌓는 저장소에서 0건 재조회로 기존 사실을 지우지 않습니다.
                rejected.append({"date": day.isoformat(), "account_alias": alias,
                    "reason": "empty_result_after_saved_executions", "source": {
                        "file": str(path.relative_to(run)), "row": None, "api_id": API_ID, "endpoint": API_PATH,
                        "sha256": hashes[-1]}})
                window["rejected_count"] += 1
                manifest["rejected_count"] += 1
            window["record_count"] = len(day_records)
            records.update(day_records)
            manifest["record_count"] = len(records)
            window["status"] = "invalid" if window["rejected_count"] else "complete"
            if not window["raw_record_count"] and not window["rejected_count"]:
                # 0건인 날짜의 빈 원본 폴더를 남기지 않고 조회 근거는 manifest에 기록합니다.
                window["empty_response_sha256"] = hashes
                window["raw_removed_for_empty_day"] = True
                shutil.rmtree(raw_dir)
                if not list(raw_dir.parent.iterdir()):
                    raw_dir.parent.rmdir()
                if (run / "raw").is_dir() and not list((run / "raw").iterdir()):
                    (run / "raw").rmdir()
            write_json(run / "manifest.json", manifest)
            print(f"{alias} {day}: 개별 체결 {len(day_records)}건, 검증 실패 {window['rejected_count']}건", flush=True)
            if day == end:
                break
            day += timedelta(days=1)
        ordered = sorted(records.values(), key=lambda r: (r["executed_at"], r["execution_id"]))
        write_jsonl(run / "executions.jsonl.partial", ordered)
        if rejected:
            write_jsonl(run / "rejected_rows.jsonl", rejected)
            raise HistoryError("체결시각·체결번호·조회 결과 검증에 실패했습니다. rejected_rows.jsonl과 원본을 확인하세요.")
        os.replace(run / "executions.jsonl.partial", run / "executions.jsonl")
        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["export_sha256"] = hashlib.sha256((run / "executions.jsonl").read_bytes()).hexdigest()
        write_json(run / "manifest.json", manifest)
        write_json(root / "latest.json", {"run_id": run_id, "run_directory": "runs/" + run_id,
                                         "executions": "runs/" + run_id + "/executions.jsonl"})
        return {"run_id": run_id, "record_count": len(records), "directory": str(run.resolve())}
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed_validation" if rejected else "failed"
        manifest["error"] = str(exc) if isinstance(exc, HistoryError) else "예기치 않은 처리 오류"
        if rejected:
            write_jsonl(run / "rejected_rows.jsonl", rejected)
        write_json(run / "manifest.json", manifest)
        raise


class DemoTransport:
    """가상 분할 체결 2건과 NXT 매도 1건. 같은 초의 체결번호를 구분합니다."""
    is_demo = True

    def __call__(self, path, payload, headers):
        body = json.loads(payload)
        if path.endswith("/token"):
            return Reply(200, {}, json_text({"return_code": 0, "token": "demo-token", "expires_dt": "29991231235959"}).encode())
        if headers["api-id"] == ACCOUNT_API_ID:
            return Reply(200, {}, json_text({"return_code": 0, "acctNo": "0000000001"}).encode())
        row = {"ord_no": "0000050", "cntr_no": "0000001", "stk_cd": "A0015M0", "stk_nm": "가상 종목",
               "io_tp_nm": "현금매수", "cntr_qty": "0000000002", "cntr_uv": "0000001000",
               "cntr_tm": "09:07:47", "dmst_stex_tp": "KRX", "ord_qty": "0000000005", "comm_ord_tp": "영웅문S#"}
        if body["ord_dt"] != "20261023":
            return Reply(200, {"cont-yn": "N"}, json_text({"return_code": 0, ROWS_KEY: []}).encode())
        if not headers["next-key"]:
            return Reply(200, {"cont-yn": "Y", "next-key": "demo-page-2"},
                         json_text({"return_code": 0, ROWS_KEY: [row, dict(row, cntr_no="0000002", cntr_qty="3", cntr_uv="1001")]}).encode())
        return Reply(200, {"cont-yn": "N"}, json_text({"return_code": 0, ROWS_KEY: [dict(row,
            ord_no="0000051", cntr_no="0000003", io_tp_nm="현금매도", cntr_qty="5", cntr_uv="1010",
            cntr_tm="19:57:03", dmst_stex_tp="NXT")]}).encode())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date_arg, help="조회 시작일(YYYY-MM-DD)")
    parser.add_argument("--end", type=date_arg, help="조회 종료일. 기본 한국시간 오늘")
    parser.add_argument("--output", type=Path, help="기본 data, 데모는 demo-data")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--max-pages", type=int, default=10000)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    start = args.start or (date(2026, 10, 23) if args.demo else None)
    end = args.end or (date(2026, 10, 23) if args.demo else datetime.now(SEOUL).date())
    if start is None:
        parser.error("--start YYYY-MM-DD를 지정하세요.")
    if start > end or end > datetime.now(SEOUL).date() or args.max_pages < 1:
        parser.error("조회 기간·최대 페이지 수를 확인하세요. 미래 날짜는 조회할 수 없습니다.")
    root = args.output or Path("demo-data" if args.demo else "data")
    try:
        if not args.demo:
            load_env(args.env_file)
        client = Client("demo-key" if args.demo else os.environ.get("KIWOOM_APP_KEY", "").strip(),
                        "demo-secret" if args.demo else os.environ.get("KIWOOM_APP_SECRET", "").strip(), root,
                        transport=DemoTransport() if args.demo else None, interval=0 if args.demo else 0.35)
        with output_lock(root):
            result = collect(client, start, end, root, args.max_pages,
                             "" if args.demo else os.environ.get("KIWOOM_ACCOUNT_NO", "").strip())
            if not args.no_build:
                from .warehouse import build_warehouse
                try:
                    build_warehouse(root, client.data_mode)
                except Exception:
                    print("키움 원본 수집은 완료됐지만 누적 생성에 실패했습니다. offtick-history-build로 재생성하세요.", file=sys.stderr)
                    return 1
        print(json_text({"status": "complete", **result}))
        return 0
    except HistoryError as exc:
        print("수집 실패: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("환경설정·저장 파일을 확인하세요. 이전 성공 결과는 유지됩니다.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("수집을 중단했습니다. 이전 성공 결과는 유지됩니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
