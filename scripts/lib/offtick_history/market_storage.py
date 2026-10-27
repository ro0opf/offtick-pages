"""시장 원문·불변 snapshot의 무손실 gzip 저장. 논리 경로와 원본 SHA-256은 유지한다."""
import gzip
import io
from pathlib import Path
import re
import zlib

from .common import HistoryError, atomic_write


def gzip_bytes(content):
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as stream:
        stream.write(content)
    return output.getvalue()


def read_market_bytes(path):
    path = Path(path)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        compressed = Path(str(path) + ".gz")
        if compressed.is_symlink():
            raise HistoryError("압축 시장 자료의 심볼릭 링크는 허용하지 않습니다.")
        content = compressed.read_bytes()
    if content.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(content)
        except (OSError, EOFError, zlib.error) as error:
            raise HistoryError("압축된 시장 자료가 손상됐습니다.") from error
    return content


def write_market_bytes(path, content):
    path = Path(path)
    compressed = Path(str(path) + ".gz")
    if path.is_file() and path.read_bytes() != content:
        raise HistoryError("기존 시장 원문과 다른 내용으로 덮어쓸 수 없습니다.")
    if compressed.is_file():
        if gzip.decompress(compressed.read_bytes()) != content:
            raise HistoryError("기존 압축 시장 원문과 내용이 다릅니다.")
    else:
        packed = gzip_bytes(content)
        if gzip.decompress(packed) != content:
            raise HistoryError("시장 자료의 무손실 압축 검증에 실패했습니다.")
        atomic_write(compressed, packed)
    if path.is_file():
        path.unlink()
    return compressed.stat().st_size


def compress_market(root, *, apply=False):
    """계좌·키·manifest·날짜별 포인터는 건드리지 않고 시장 원문과 snapshot만 변환한다."""
    files = []
    for path in sorted((root / "runs").glob("*/**/*.json")):
        relative = path.relative_to(root / "runs")
        raw = len(relative.parts) >= 3 and relative.parts[1] == "raw" and not path.name.endswith(".meta.json")
        snapshot = len(relative.parts) == 2 and re.fullmatch(r"(?:selection|daily|turnover|stock-[0-9A-Z]{6}|index-\d{3})\.json", path.name)
        if raw or snapshot:
            files.append(path)
    before = after = 0
    for path in files:
        content = path.read_bytes()
        before += len(content)
        after += write_market_bytes(path, content) if apply else len(gzip_bytes(content))
    return {"converted_files": len(files), "before_bytes": before, "after_bytes": after, "applied": apply}
