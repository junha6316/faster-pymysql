"""행 파싱 비용을 순수 파이썬 pymysql과 Rust로 나란히 잰다.

DB 없이 합성 패킷으로 파싱 경로만 돌린다. 소켓은 어느 쪽에도 들어 있지 않다.

    ./.venv312/bin/python bench/bench_row.py [rows] [repeat]

두 가지를 잰다.

  행 파싱만   pymysql `_read_row_from_packet` ↔ `parse_row`
  결과셋 전체 위 + `MysqlPacket` 생성(프레이밍) ↔ `parse_rows` 배치 한 방
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tests"))

from pymysql import converters
from pymysql.connections import MySQLResult
from pymysql.constants import FIELD_TYPE
from pymysql.protocol import MysqlPacket

import faster_pymysql
from faster_pymysql import parse_row, parse_rows
from synthetic import BINARY_CHARSET, UTF8_CHARSET, encode_row, make_converters

N_ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
REPEAT = int(sys.argv[2]) if len(sys.argv) > 2 else 5

ORIGINAL_READ_ROW = MySQLResult._read_row_from_packet
ENCODING = "utf8"

# 전형적인 유저 테이블 한 행. 실제 워크로드로 재려면 여기를 그 쿼리의 컬럼 분포로 바꾼다.
SCHEMA = [
    ("id", FIELD_TYPE.LONGLONG, BINARY_CHARSET, lambda i: str(1_000_000 + i)),
    ("name", FIELD_TYPE.VAR_STRING, UTF8_CHARSET, lambda i: f"사용자{i}"),
    ("email", FIELD_TYPE.VAR_STRING, UTF8_CHARSET, lambda i: f"user{i}@example.com"),
    ("age", FIELD_TYPE.LONG, BINARY_CHARSET, lambda i: str(20 + i % 50)),
    ("created_at", FIELD_TYPE.DATETIME, BINARY_CHARSET, lambda i: f"2026-08-26 14:23:{i % 60:02d}"),
    ("birth", FIELD_TYPE.DATE, BINARY_CHARSET, lambda i: f"1995-{i % 12 + 1:02d}-{i % 28 + 1:02d}"),
    ("score", FIELD_TYPE.NEWDECIMAL, BINARY_CHARSET, lambda i: f"{i % 100}.{i % 100:02d}"),
    ("flag", FIELD_TYPE.TINY, BINARY_CHARSET, lambda i: str(i % 2)),
    ("bio", FIELD_TYPE.BLOB, BINARY_CHARSET, lambda i: "자기소개 텍스트 " * 3),
    ("ratio", FIELD_TYPE.FLOAT, BINARY_CHARSET, lambda i: f"0.{i % 1000:03d}"),
    ("country", FIELD_TYPE.VAR_STRING, UTF8_CHARSET, lambda i: "KR"),
    ("updated_at", FIELD_TYPE.DATETIME, BINARY_CHARSET, lambda i: f"2026-08-26 15:{i % 60:02d}:00"),
]


class FakeResult:
    def __init__(self, converters):
        self.converters = converters


def build_payloads() -> list[bytes]:
    return [
        encode_row([gen(i).encode(ENCODING) for _n, _t, _c, gen in SCHEMA])
        for i in range(N_ROWS)
    ]


def best(label, thunk):
    """thunk를 돌려주는 함수를 받는다. setup을 타이머 밖에 두면 0ms가 나온다."""
    times = []
    for _ in range(REPEAT):
        run = thunk()
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    seconds = min(times)
    print(
        f"  {label:<28} {seconds * 1000:8.1f} ms"
        f"  {seconds / N_ROWS * 1e6:7.2f} µs/row"
        f"  {N_ROWS / seconds / 1000:8.1f}k rows/s"
    )
    return seconds


def main():
    payloads = build_payloads()
    conv = make_converters([(t, c) for _n, t, c, _g in SCHEMA], conn_encoding=ENCODING)
    schema = faster_pymysql.build_schema(conv)
    result = FakeResult(conv)

    # 재기 전에 두 파서가 같은 결과를 내는지부터 확인한다.
    sample = MysqlPacket(payloads[0], ENCODING)
    py_row = ORIGINAL_READ_ROW(result, sample)
    rust_row, _ = parse_row(payloads[0], 0, schema)
    assert py_row == rust_row, f"\n{py_row!r}\n{rust_row!r}"
    assert all(type(a) is type(b) for a, b in zip(py_row, rust_row)), "타입이 다르다"

    total_bytes = sum(len(p) for p in payloads)
    print(f"{N_ROWS}행 × {len(SCHEMA)}컬럼, {total_bytes / 1e6:.1f}MB, 최선 {REPEAT}회")

    print("\n행 파싱만")

    def py_rows():
        packets = [MysqlPacket(p, ENCODING) for p in payloads]

        def run():
            for packet in packets:
                packet._position = 0
                ORIGINAL_READ_ROW(result, packet)

        return run

    def rust_rows():
        def run():
            for payload in payloads:
                parse_row(payload, 0, schema)

        return run

    a = best("pymysql", py_rows)
    b = best("rust parse_row", rust_rows)
    print(f"  {'':<28} {a / b:8.1f}x")

    print("\n결과셋 전체 (프레이밍 포함, 소켓 제외)")

    def py_full():
        # _read_rowdata_packet과 같게 행을 모아 tuple로 만든다. 배치 쪽도
        # 결과셋 전체를 만들어 들고 있으므로 여기서 버리면 비교가 기운다.
        def run():
            rows = []
            for payload in payloads:
                rows.append(ORIGINAL_READ_ROW(result, MysqlPacket(payload, ENCODING)))
            return tuple(rows)

        return run

    def rust_batch():
        def run():
            parse_rows(payloads, schema)

        return run

    c = best("pymysql", py_full)
    d = best("rust parse_rows", rust_batch)
    print(f"  {'':<28} {c / d:8.1f}x")


if __name__ == "__main__":
    main()
