"""실제 MySQL 서버에 붙여 pymysql 원본과 결과가 같은지 확인한다.

합성 패킷으로는 못 보는 것들을 본다.

- 서버가 실제로 붙이는 charsetnr, 그에 따른 컬럼별 encoding 판정
- 서버가 실제로 보내는 값 형태 (DECIMAL 자릿수, TIME 부호, BIT, JSON, ENUM/SET)
- unbuffered 커서(SSCursor)와 DictCursor
- 16MB를 넘어 여러 패킷으로 쪼개져 오는 행
- 커넥션 charset이 latin1일 때 (Rust가 직접 디코드하지 않고 파이썬에 넘기는 경로)

서버가 없으면 통째로 skip한다. 접속 정보는 환경변수로 바꾼다.

    FPM_MYSQL_HOST=127.0.0.1 FPM_MYSQL_PORT=3307 FPM_MYSQL_USER=root \\
    FPM_MYSQL_PASSWORD= FPM_MYSQL_DB=fpm pytest tests/test_e2e.py
"""

from __future__ import annotations

import datetime
import decimal
import os

import pytest

pymysql = pytest.importorskip("pymysql")

import faster_pymysql
from compare import diff, same

DSN = {
    "host": os.environ.get("FPM_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("FPM_MYSQL_PORT", "3307")),
    "user": os.environ.get("FPM_MYSQL_USER", "root"),
    "password": os.environ.get("FPM_MYSQL_PASSWORD", ""),
    "database": os.environ.get("FPM_MYSQL_DB", "fpm"),
}

TABLE = "fpm_e2e"

DDL = f"""
CREATE TABLE {TABLE} (
    id          BIGINT PRIMARY KEY,
    c_tiny      TINYINT,
    c_small     SMALLINT,
    c_medium    MEDIUMINT,
    c_int       INT,
    c_ubig      BIGINT UNSIGNED,
    c_float     FLOAT,
    c_double    DOUBLE,
    c_decimal   DECIMAL(20, 6),
    c_varchar   VARCHAR(255),
    c_text      TEXT,
    c_blob      BLOB,
    c_varbin    VARBINARY(64),
    c_datetime  DATETIME(6),
    c_timestamp TIMESTAMP(3) NULL DEFAULT NULL,
    c_date      DATE,
    c_time      TIME(6),
    c_year      YEAR,
    c_bit       BIT(8),
    c_json      JSON,
    c_enum      ENUM('a', 'b'),
    c_set       SET('x', 'y')
) CHARACTER SET utf8mb4
"""

ROWS = [
    (
        1, 127, 32767, 8388607, 2147483647, 18446744073709551615,
        1.5, -3.25e-10, decimal.Decimal("-12345678901234.567890"),
        "고양이 \U0001f600", "긴 텍스트 " * 40, b"\x00\x01\xfe\xff", bytes(range(64)),
        datetime.datetime(2026, 8, 27, 9, 30, 1, 123456),
        datetime.datetime(2026, 8, 27, 9, 30, 1, 123000),
        datetime.date(2026, 8, 27),
        "838:59:59",  # timedelta로 넣으면 pymysql 이스케이프가 범위를 넘긴다
        2026, b"\xa5", '{"a": [1, 2], "b": "가"}', "a", "x,y",
    ),
    (
        2, -128, -32768, -8388608, -2147483648, 0,
        0.0, 0.0, decimal.Decimal("0.000000"),
        "", "", b"", b"",
        datetime.datetime(1000, 1, 1, 0, 0, 0),
        datetime.datetime(1970, 1, 2, 0, 0, 0),
        datetime.date(1000, 1, 1),
        "-838:59:59",
        1901, b"\x00", "null", "b", "",
    ),
    # 전 컬럼 NULL
    (3,) + (None,) * 21,
]


def connect(**kw):
    return pymysql.connect(**DSN, **kw)


@pytest.fixture(scope="module")
def server():
    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001 - 서버가 없으면 skip이 목적이다
        pytest.skip(f"MySQL에 못 붙었다: {type(exc).__name__} {exc}")
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(DDL)
        cur.executemany(
            f"INSERT INTO {TABLE} VALUES ({', '.join(['%s'] * 22)})", ROWS
        )
    conn.commit()
    conn.close()
    yield
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def clean_install():
    """테스트마다 확실히 원본 상태에서 시작한다."""
    faster_pymysql.uninstall()
    yield
    faster_pymysql.uninstall()


def fetch(sql, *, mode=None, cursor_class=None, **conn_kw):
    """`mode`가 None이면 순수 pymysql, "row"/"batch"면 Rust 파서.

    Rust 모드에서는 **실제로 Rust가 불렸는지 센다.** install()이 조용히 실패하면
    양쪽 다 pymysql이 되어 이 파일의 비교가 전부 무의미하게 통과한다.
    """
    faster_pymysql.uninstall()
    calls = 0

    if mode is None:
        conn_ctx = connect(**conn_kw)
    else:
        real = faster_pymysql.parse_rows if mode == "batch" else faster_pymysql.parse_row

        def counted(*args):
            nonlocal calls
            calls += 1
            return real(*args)

        name = "parse_rows" if mode == "batch" else "parse_row"
        setattr(faster_pymysql, name, counted)
        faster_pymysql.install(batch=(mode == "batch"))
        conn_ctx = connect(**conn_kw)

    try:
        with conn_ctx.cursor(cursor_class) if cursor_class else conn_ctx.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            description = cur.description
        return list(rows), description
    finally:
        conn_ctx.close()
        faster_pymysql.uninstall()
        if mode is not None:
            setattr(faster_pymysql, name, real)
            assert calls > 0, f"mode={mode}인데 Rust 파서가 한 번도 안 불렸다"


def assert_matches_pymysql(sql, *, modes=("row", "batch"), cursor_class=None, **conn_kw):
    expected, expected_desc = fetch(sql, cursor_class=cursor_class, **conn_kw)
    assert expected, "행이 하나도 없으면 비교가 의미 없다"
    for mode in modes:
        actual, actual_desc = fetch(
            sql, mode=mode, cursor_class=cursor_class, **conn_kw
        )
        assert same(expected, actual), f"mode={mode}: {diff(expected, actual)}"
        assert expected_desc == actual_desc, f"mode={mode}: description이 다르다"
    return expected


def test_모든_타입(server):
    rows = assert_matches_pymysql(f"SELECT * FROM {TABLE} ORDER BY id")
    assert len(rows) == 3
    # 비교가 헛돌지 않았는지: 타입이 실제로 다양하게 나왔는지 확인한다.
    types = {type(v) for row in rows for v in row}
    assert {int, float, str, bytes, decimal.Decimal} <= types
    assert {datetime.datetime, datetime.date, datetime.timedelta} <= types


def test_전_컬럼_null(server):
    rows = assert_matches_pymysql(f"SELECT * FROM {TABLE} WHERE id = 3")
    assert rows[0][1:] == (None,) * 21


def test_dict_커서(server):
    assert_matches_pymysql(
        f"SELECT * FROM {TABLE} ORDER BY id", cursor_class=pymysql.cursors.DictCursor
    )


def test_unbuffered_커서(server):
    """SSCursor는 _read_rowdata_packet_unbuffered를 타 행마다 파싱한다."""
    assert_matches_pymysql(
        f"SELECT * FROM {TABLE} ORDER BY id",
        modes=("row",),  # batch는 버퍼드 경로만 바꾼다
        cursor_class=pymysql.cursors.SSCursor,
    )


def test_latin1_커넥션(server):
    """conn.encoding이 utf8도 ascii도 아니면 Rust가 파이썬 디코더에 넘긴다."""
    assert_matches_pymysql(
        f"SELECT id, c_decimal, c_datetime FROM {TABLE} ORDER BY id", charset="latin1"
    )


def test_use_unicode_꺼진_커넥션(server):
    """encoding이 전부 None이라 값이 bytes로 나온다."""
    rows = assert_matches_pymysql(
        f"SELECT id, c_varchar, c_date FROM {TABLE} WHERE id = 1", use_unicode=False
    )
    assert isinstance(rows[0][1], bytes)


def test_행이_많은_결과셋(server):
    """행 하나짜리 표를 조인해 수천 행을 만든다. 배치 경로가 실제로 도는지 본다."""
    sql = f"""
        SELECT t.* FROM {TABLE} t
        JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) a
        JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) b
        JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) c
        JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) d
        JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) e
        ORDER BY t.id
    """
    rows = assert_matches_pymysql(sql)
    assert len(rows) == 3 * 4**5


def test_16mb를_넘는_행(server):
    """MAX_PACKET_LEN을 넘으면 서버가 패킷을 쪼개 보낸다.

    _read_packet이 이어붙인 뒤에 파서가 보므로 결과는 같아야 한다.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT @@max_allowed_packet")
            limit = cur.fetchone()[0]
    finally:
        conn.close()
    size = 20 * 1024 * 1024
    if limit < size + 1024:
        pytest.skip(f"max_allowed_packet={limit}이라 {size}바이트 행을 못 만든다")
    assert_matches_pymysql(f"SELECT REPEAT('가', {size // 3}) AS big")
