"""Rust 파서 결과가 순수 파이썬 pymysql과 완전히 같은지 확인한다.

파싱이 틀리면 예외 없이 데이터가 조용히 망가진다. 값이 같은지만이 아니라
**타입이 같은지**까지 본다 (`1 == 1.0 == True`이므로 `==`만으로는 못 잡는다).
"""

from __future__ import annotations

import math
import random

import pytest
from pymysql.connections import MySQLResult
from pymysql.constants import FIELD_TYPE
from pymysql.protocol import MysqlPacket

import faster_pymysql
from faster_pymysql import parse_row, parse_rows
from synthetic import (
    BINARY_CHARSET,
    UTF8_CHARSET,
    encode_row,
    make_converters,
    values_for,
)

# install() 전의 원본. 이 파일은 몽키패치를 걸지 않고 원본을 직접 부른다.
ORIGINAL_READ_ROW = MySQLResult._read_row_from_packet

FIELDS = [
    (FIELD_TYPE.LONG, BINARY_CHARSET),
    (FIELD_TYPE.LONGLONG, BINARY_CHARSET),
    (FIELD_TYPE.DOUBLE, BINARY_CHARSET),
    (FIELD_TYPE.VAR_STRING, UTF8_CHARSET),
    (FIELD_TYPE.BLOB, BINARY_CHARSET),
    (FIELD_TYPE.DATE, BINARY_CHARSET),
    (FIELD_TYPE.DATETIME, BINARY_CHARSET),
    (FIELD_TYPE.TIME, BINARY_CHARSET),
    (FIELD_TYPE.NEWDECIMAL, BINARY_CHARSET),
    (FIELD_TYPE.YEAR, BINARY_CHARSET),
    (FIELD_TYPE.BIT, BINARY_CHARSET),
    (FIELD_TYPE.JSON, UTF8_CHARSET),
]


class FakeResult:
    """`_read_row_from_packet`이 실제로 쓰는 건 `converters`뿐이다."""

    def __init__(self, converters):
        self.converters = converters


def same(a, b) -> bool:
    """값과 타입이 모두 같은가. NaN은 자기 자신과 같은 것으로 본다."""
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def outcome(call):
    """결과 아니면 예외를 같은 모양으로 담는다.

    converter가 예외를 던지는 경우도 동작의 일부다. pymysql은 use_unicode=False에서
    DECIMAL 컬럼에 `Decimal(bytes)`를 불러 TypeError를 낸다. Rust도 같은 함수를
    부르므로 똑같이 나야 한다.
    """
    try:
        return ("ok", *call())
    except Exception as exc:  # noqa: BLE001 - 동작 비교가 목적이다
        return ("raise", type(exc), str(exc))


def run_both(payload: bytes, converters):
    packet = MysqlPacket(payload, "utf8")
    schema = faster_pymysql.build_schema(converters)

    def py():
        row = ORIGINAL_READ_ROW(FakeResult(converters), packet)
        return row, packet._position

    def rust():
        row, position = parse_row(payload, 0, schema)
        return row, position

    return outcome(py), outcome(rust)


def assert_identical(payload: bytes, converters, label=""):
    py_out, rust_out = run_both(payload, converters)
    assert py_out[0] == rust_out[0], f"{label}\n파이썬: {py_out}\nRust  : {rust_out}"
    if py_out[0] == "raise":
        assert py_out[1:] == rust_out[1:], (
            f"{label}\n파이썬 예외: {py_out[1:]}\nRust 예외  : {rust_out[1:]}"
        )
        return
    assert same(py_out[1], rust_out[1]), (
        f"{label}\n파이썬: {py_out[1]!r}\nRust  : {rust_out[1]!r}"
    )
    assert py_out[2] == rust_out[2], f"{label} 위치 불일치: {py_out[2]} != {rust_out[2]}"


@pytest.mark.parametrize("field", FIELDS, ids=lambda f: str(f[0]))
def test_타입별_경계값(field):
    """타입 하나짜리 결과셋에 경계값을 몰아넣는다."""
    rng = random.Random(0xC0FFEE)
    converters = make_converters([field])
    for value in values_for(rng, field[0], 40):
        payload = encode_row([value])
        assert_identical(payload, converters, label=f"{field[0]} value={value!r}")


def test_여러_컬럼_무작위_행():
    """실제 결과셋 모양대로 12컬럼 행을 많이 만들어 비교한다."""
    rng = random.Random(1234)
    converters = make_converters(FIELDS)
    pools = [values_for(rng, ft, 60) for ft, _ in FIELDS]
    for i in range(300):
        row = [rng.choice(pool) for pool in pools]
        assert_identical(encode_row(row), converters, label=f"row {i}")


def test_use_unicode_꺼짐():
    """encoding이 전부 None이면 값이 bytes로 나온다."""
    rng = random.Random(7)
    converters = make_converters(FIELDS, use_unicode=False)
    pools = [values_for(rng, ft, 30) for ft, _ in FIELDS]
    for i in range(100):
        row = [rng.choice(pool) for pool in pools]
        assert_identical(encode_row(row), converters, label=f"row {i}")


def test_컬럼이_모자란_행():
    """컬럼 수보다 값이 적으면 pymysql은 IndexError를 잡아 짧은 tuple을 돌려준다."""
    converters = make_converters(FIELDS)
    payload = encode_row([b"1", b"2", b"3"])
    py_out, rust_out = run_both(payload, converters)
    assert py_out[0] == "ok" and len(py_out[1]) == 3
    assert_identical(payload, converters)


def test_0xff는_null이_된다():
    """read_length_encoded_integer의 if/elif가 0xff를 안 덮어 None이 나온다."""
    converters = make_converters([(FIELD_TYPE.LONG, BINARY_CHARSET)])
    assert_identical(b"\xff", converters)


def test_잘린_패킷은_양쪽_다_예외():
    """예외 종류는 다를 수 있다. panic이 아니라 예외로 나오는 게 조건이다."""
    converters = make_converters([(FIELD_TYPE.VAR_STRING, UTF8_CHARSET)])
    schema = faster_pymysql.build_schema(converters)
    for payload in (b"\x05ab", b"\xfc\x10\x00ab", b"\xfe\x01"):
        with pytest.raises(Exception):
            ORIGINAL_READ_ROW(FakeResult(converters), MysqlPacket(payload, "utf8"))
        with pytest.raises(Exception):
            parse_row(payload, 0, schema)


def test_커스텀_converter는_파이썬이_처리한다():
    """Rust가 모르는 converter는 원래 함수가 그대로 불려야 한다."""
    calls = []

    def custom(value):
        calls.append(value)
        return f"<{value}>"

    converters = [("utf8", custom)]
    schema = faster_pymysql.build_schema(converters)
    row, _ = parse_row(encode_row([b"cat"]), 0, schema)
    assert row == ("<cat>",)
    assert calls == ["cat"]


def test_batch가_행마다_부른_것과_같다():
    rng = random.Random(99)
    converters = make_converters(FIELDS)
    schema = faster_pymysql.build_schema(converters)
    pools = [values_for(rng, ft, 30) for ft, _ in FIELDS]
    payloads = [encode_row([rng.choice(p) for p in pools]) for _ in range(50)]

    batched = parse_rows(payloads, schema)
    one_by_one = tuple(parse_row(p, 0, schema)[0] for p in payloads)
    assert same(batched, one_by_one)
