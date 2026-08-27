"""실제 MySQL 없이 행 패킷을 만든다. CI에서도 돌아야 하므로 합성 fixture만 쓴다."""

from __future__ import annotations

import random

from pymysql.connections import TEXT_TYPES
from pymysql.constants import FIELD_TYPE
from pymysql.converters import decoders, through

# (이름, type_code, charsetnr) — charsetnr 63은 binary다.
UTF8_CHARSET = 45
BINARY_CHARSET = 63


def lenenc(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    if n < 1 << 16:
        return b"\xfc" + n.to_bytes(2, "little")
    if n < 1 << 24:
        return b"\xfd" + n.to_bytes(3, "little")
    return b"\xfe" + n.to_bytes(8, "little")


def encode_row(values: list[bytes | None]) -> bytes:
    """컬럼값 리스트를 행 패킷 페이로드로. `None`은 NULL 표지 0xfb."""
    out = bytearray()
    for v in values:
        if v is None:
            out += b"\xfb"
        else:
            out += lenenc(len(v)) + v
    return bytes(out)


def make_converters(fields, conn_encoding="utf8", use_unicode=True):
    """pymysql `_get_descriptions`(connections.py:1404)의 컬럼별 판정을 그대로 옮긴 것."""
    out = []
    for field_type, charsetnr in fields:
        if use_unicode:
            if field_type == FIELD_TYPE.JSON:
                encoding = conn_encoding
            elif field_type in TEXT_TYPES:
                encoding = None if charsetnr == BINARY_CHARSET else conn_encoding
            else:
                encoding = "ascii"
        else:
            encoding = None
        converter = decoders.get(field_type)
        if converter is through:
            converter = None
        out.append((encoding, converter))
    return out


# 타입별 값 생성기. MySQL이 실제로 보내는 형태(전부 텍스트 프로토콜)로 만든다.
# 경계값을 앞에 두고, 뒤는 무작위다.

_EDGE = {
    FIELD_TYPE.LONG: [b"0", b"-1", b"2147483647", b"-2147483648", b"007", b"+5"],
    FIELD_TYPE.LONGLONG: [
        b"9223372036854775807",
        b"-9223372036854775808",
        b"18446744073709551615",  # UNSIGNED 최대. i64를 넘는다
        b"0",
    ],
    FIELD_TYPE.DOUBLE: [b"0", b"1.5", b"-3.25e-10", b"1e308", b"-0.0", b"1."],
    FIELD_TYPE.VAR_STRING: [
        b"",
        b"cat",
        "한글".encode(),
        "이모지 \U0001f600".encode(),
        b"x" * 250,  # lenenc 1바이트 경계
        b"y" * 251,  # 0xfc로 넘어간다
        b"z" * 70000,  # 0xfd로 넘어간다
    ],
    FIELD_TYPE.BLOB: [b"", b"\x00\x01\xfe\xff", bytes(range(256))],
    FIELD_TYPE.DATE: [
        b"2007-02-26",
        b"0000-00-00",  # 없는 날짜. pymysql은 문자열을 그대로 돌려준다
        b"2007-02-31",
        b"9999-12-31",
        b"1-1-1",
    ],
    FIELD_TYPE.DATETIME: [
        b"2007-02-25 23:06:20",
        b"2007-02-25T23:06:20",
        b"0000-00-00 00:00:00",
        b"2007-02-25 23:06:20.5",
        b"2007-02-25 23:06:20.123456",
        b"2007-02-25 23:06:20.1234567",  # 6자리를 넘는 소수부
        b"2007-02-31 23:06:20",  # 없는 날짜
        b"random crap",
    ],
    FIELD_TYPE.TIME: [
        b"00:00:00",
        b"25:06:17",
        b"-25:06:17",
        b"838:59:59",
        b"00:00:01.5",
        b"random crap",
    ],
    FIELD_TYPE.NEWDECIMAL: [b"0.00", b"-12345.6789", b"1E+10", b"0"],
    FIELD_TYPE.YEAR: [b"2024", b"0"],
    FIELD_TYPE.BIT: [b"\x01", b"\x00"],
    FIELD_TYPE.JSON: [b"{}", b'{"a": 1}', "㋡".encode()],
}


def _random_value(rng: random.Random, field_type: int) -> bytes:
    if field_type in (FIELD_TYPE.LONG, FIELD_TYPE.YEAR):
        return str(rng.randint(-(2**31), 2**31 - 1)).encode()
    if field_type == FIELD_TYPE.LONGLONG:
        return str(rng.randint(-(2**63), 2**64 - 1)).encode()
    if field_type == FIELD_TYPE.DOUBLE:
        return repr(rng.uniform(-1e12, 1e12)).encode()
    if field_type == FIELD_TYPE.NEWDECIMAL:
        return f"{rng.uniform(-1e6, 1e6):.4f}".encode()
    if field_type in (FIELD_TYPE.VAR_STRING, FIELD_TYPE.JSON):
        n = rng.randint(0, 300)
        return "".join(rng.choice("abc가나다 \U0001f600") for _ in range(n)).encode()
    if field_type in (FIELD_TYPE.BLOB, FIELD_TYPE.BIT):
        return bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
    if field_type == FIELD_TYPE.DATE:
        return f"{rng.randint(0, 9999):04d}-{rng.randint(0, 13):02d}-{rng.randint(0, 32):02d}".encode()
    if field_type == FIELD_TYPE.DATETIME:
        d = f"{rng.randint(0, 9999):04d}-{rng.randint(0, 13):02d}-{rng.randint(0, 32):02d}"
        t = f"{rng.randint(0, 25):02d}:{rng.randint(0, 61):02d}:{rng.randint(0, 61):02d}"
        frac = rng.choice(["", ".5", ".123456", ".9999999"])
        return f"{d} {t}{frac}".encode()
    if field_type == FIELD_TYPE.TIME:
        sign = rng.choice(["", "-"])
        return f"{sign}{rng.randint(0, 999)}:{rng.randint(0, 61):02d}:{rng.randint(0, 61):02d}".encode()
    raise AssertionError(f"생성기 없음: {field_type}")


def values_for(rng: random.Random, field_type: int, count: int) -> list[bytes | None]:
    """경계값을 먼저 다 쓰고, 모자라면 무작위로 채운다. NULL도 섞는다."""
    out: list[bytes | None] = list(_EDGE.get(field_type, []))
    while len(out) < count:
        out.append(None if rng.random() < 0.1 else _random_value(rng, field_type))
    return out[:count]
