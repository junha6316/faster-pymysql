"""몽키패치가 제대로 끼워지고 되돌려지는지."""

from __future__ import annotations

import datetime

import pytest
from pymysql.connections import MySQLResult
from pymysql.constants import FIELD_TYPE
from pymysql.protocol import MysqlPacket

import faster_pymysql
from synthetic import BINARY_CHARSET, UTF8_CHARSET, encode_row, make_converters

FIELDS = [
    (FIELD_TYPE.LONG, BINARY_CHARSET),
    (FIELD_TYPE.VAR_STRING, UTF8_CHARSET),
    (FIELD_TYPE.DATETIME, BINARY_CHARSET),
]
PAYLOAD = encode_row([b"42", "고양이".encode(), b"2026-08-27 09:30:00"])
EXPECTED = (42, "고양이", datetime.datetime(2026, 8, 27, 9, 30, 0))


@pytest.fixture
def installed():
    faster_pymysql.install()
    yield
    faster_pymysql.uninstall()


def _result():
    result = MySQLResult.__new__(MySQLResult)
    result.converters = make_converters(FIELDS)
    # MySQLResult.__del__이 참조한다. __init__을 건너뛰었으므로 직접 채운다.
    result.unbuffered_active = False
    return result


def test_설치와_복원(installed):
    original = _read_row_original()
    assert MySQLResult._read_row_from_packet is not original
    faster_pymysql.uninstall()
    assert MySQLResult._read_row_from_packet is original
    faster_pymysql.install()  # fixture의 uninstall이 짝을 맞춘다


def _read_row_original():
    return faster_pymysql._ORIGINAL["_read_row_from_packet"]


def test_패치된_경로가_값과_위치를_맞춘다(installed):
    packet = MysqlPacket(PAYLOAD, "utf8")
    row = MySQLResult._read_row_from_packet(_result(), packet)
    assert row == EXPECTED
    assert [type(v) for v in row] == [int, str, datetime.datetime]
    assert packet._position == len(PAYLOAD)


def test_중복_install은_원본을_덮지_않는다(installed):
    original = _read_row_original()
    faster_pymysql.install()
    assert _read_row_original() is original


def test_컬럼_수가_바뀌면_스키마를_다시_만든다(installed):
    result = _result()
    MySQLResult._read_row_from_packet(result, MysqlPacket(PAYLOAD, "utf8"))
    first = result._fpm_schema

    result.converters = make_converters(FIELDS[:2])
    row = MySQLResult._read_row_from_packet(
        result, MysqlPacket(encode_row([b"7", b"hi"]), "utf8")
    )
    assert result._fpm_schema is not first
    assert row == (7, "hi")


def test_batch는_rowdata_패킷을_바꾼다():
    original = MySQLResult._read_rowdata_packet
    faster_pymysql.install(batch=True)
    try:
        assert MySQLResult._read_rowdata_packet is not original
    finally:
        faster_pymysql.uninstall()
    assert MySQLResult._read_rowdata_packet is original


def test_batch_없이는_rowdata_패킷을_그대로_둔다():
    original = MySQLResult._read_rowdata_packet
    faster_pymysql.install()
    try:
        assert MySQLResult._read_rowdata_packet is original
    finally:
        faster_pymysql.uninstall()
