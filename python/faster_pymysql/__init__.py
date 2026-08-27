"""pymysql 결과셋 행 파싱을 Rust로 대체한다.

    import faster_pymysql
    faster_pymysql.install()

pymysql을 포크하지 않고 런타임에 메서드만 갈아끼운다. `uninstall()`로 되돌린다.

소켓 읽기는 건드리지 않는다. gevent가 허브에 양보하는 지점이 거기라, recv를
Rust로 가져오면 GIL을 풀어도 OS 스레드가 블로킹돼 모든 그린렛이 멈춘다.
"""

from __future__ import annotations

import pymysql.converters as _converters
from pymysql.connections import MySQLResult

from . import _rust
from ._rust import Schema, parse_row, parse_rows

__all__ = ["install", "uninstall", "installed", "build_schema", "Schema"]

# converter 함수 → Rust decoder id.
#
# **동일성(is)으로 비교한다.** 사내에서 갈아끼운 converter는 여기에 안 걸리고
# DECODER_CUSTOM으로 떨어져 원래 파이썬 함수가 그대로 불린다.
_DECODERS = {
    id(int): _rust.DECODER_INT,
    id(float): _rust.DECODER_FLOAT,
    id(_converters.convert_date): _rust.DECODER_DATE,
    id(_converters.convert_datetime): _rust.DECODER_DATETIME,
    id(_converters.convert_timedelta): _rust.DECODER_TIME,
}

_ORIGINAL: dict[str, object] = {}


def _decoder_id(converter) -> int:
    if converter is None:
        return _rust.DECODER_RAW
    return _DECODERS.get(id(converter), _rust.DECODER_CUSTOM)


def build_schema(converters) -> Schema:
    """pymysql `MySQLResult.converters`를 Rust 컬럼 스펙으로 바꾼다.

    결과셋마다 한 번만 부른다. `converters`는 `(encoding, converter)` 리스트다.

    Decimal과 BIT은 CUSTOM으로 떨어진다. `Decimal(str)` 생성 비용은 파이썬
    객체 생성 하한선이라 Rust로 옮겨도 줄지 않는다.
    """
    return Schema([(_decoder_id(conv), enc, conv) for enc, conv in converters])


def _schema_for(result) -> Schema:
    schema = getattr(result, "_fpm_schema", None)
    if schema is None or len(schema) != len(result.converters):
        schema = build_schema(result.converters)
        result._fpm_schema = schema
    return schema


def _read_row_from_packet(self, packet):
    row, position = parse_row(packet._data, packet._position, _schema_for(self))
    packet._position = position
    return row


def _get_descriptions(self):
    _ORIGINAL["_get_descriptions"](self)
    self._fpm_schema = build_schema(self.converters)


def _read_rowdata_packet(self):
    """결과셋 전체를 한 번의 FFI 왕복으로 처리한다.

    소켓 읽기 루프는 파이썬에 그대로 남고, 모아둔 바이트만 Rust에 넘긴다.
    행마다 파이썬↔Rust를 오가지 않아 더 빠르지만, 파싱이 끝까지 미뤄지므로
    결과셋 크기만큼 원시 바이트를 들고 있게 된다.
    """
    payloads = []
    while True:
        packet = self.connection._read_packet()
        if self._check_packet_is_eof(packet):
            self.connection = None  # 순환 참조를 끊는다
            break
        payloads.append(packet._data)

    self.rows = parse_rows(payloads, _schema_for(self))
    self.affected_rows = len(self.rows)


_PATCHES = {
    "_get_descriptions": _get_descriptions,
    "_read_row_from_packet": _read_row_from_packet,
}


def install(*, batch: bool = False) -> None:
    """Rust 파서를 끼운다. 이미 끼워져 있으면 아무것도 안 한다.

    `batch=True`면 버퍼드 커서의 결과셋 전체를 한 번에 처리한다.
    """
    if _ORIGINAL:
        return
    patches = dict(_PATCHES)
    if batch:
        patches["_read_rowdata_packet"] = _read_rowdata_packet
    for name, fn in patches.items():
        _ORIGINAL[name] = getattr(MySQLResult, name)
        setattr(MySQLResult, name, fn)


def uninstall() -> None:
    """원래 pymysql 구현으로 되돌린다."""
    for name, fn in _ORIGINAL.items():
        setattr(MySQLResult, name, fn)
    _ORIGINAL.clear()


def installed() -> bool:
    return bool(_ORIGINAL)
