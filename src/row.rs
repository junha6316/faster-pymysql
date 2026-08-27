//! 행 하나(또는 여럿)를 파이썬 tuple로.
//!
//! pymysql `connections.py:1385` `_read_row_from_packet`을 대체한다.
//! **소켓 읽기는 파이썬에 남는다.** 이 함수들은 이미 읽어둔 바이트만 받는다.

use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::decode::decode;
use crate::lenenc::{Field, ParseError, read_field};
use crate::schema::Schema;

/// 행 하나를 분해해 `(tuple, 다음 위치)`를 돌려준다.
///
/// `_read_row_from_packet`의 1:1 대체물이다. 호출자가 `packet._position`을
/// 돌려받은 값으로 갱신해야 한다.
#[pyfunction]
pub fn parse_row<'py>(
    py: Python<'py>,
    data: &[u8],
    pos: usize,
    schema: &Schema,
) -> PyResult<(Bound<'py, PyTuple>, usize)> {
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(schema.cols.len());
    let end = fill_row(py, data, pos, schema, &mut values)?;
    Ok((PyTuple::new(py, values)?, end))
}

/// 행 패킷 여러 개를 한 번에. FFI 왕복이 결과셋당 한 번이 된다.
///
/// 각 패킷은 위치 0에서 시작한다. `_read_packet`이 만든 `MysqlPacket`이 그렇다.
#[pyfunction]
pub fn parse_rows<'py>(
    py: Python<'py>,
    packets: Vec<Vec<u8>>,
    schema: &Schema,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut rows: Vec<Bound<'py, PyAny>> = Vec::with_capacity(packets.len());
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(schema.cols.len());
    for packet in &packets {
        values.clear();
        fill_row(py, packet, 0, schema, &mut values)?;
        rows.push(PyTuple::new(py, values.drain(..))?.into_any());
    }
    PyTuple::new(py, rows)
}

/// 컬럼 스펙을 훑으며 값을 채운다. 채운 뒤의 위치를 돌려준다.
fn fill_row<'py>(
    py: Python<'py>,
    data: &[u8],
    mut pos: usize,
    schema: &Schema,
    out: &mut Vec<Bound<'py, PyAny>>,
) -> PyResult<usize> {
    for spec in &schema.cols {
        let (field, next) = match read_field(data, pos) {
            Ok(v) => v,
            // 행에 컬럼이 모자라다. pymysql은 IndexError를 잡아 break하고
            // 짧은 tuple을 돌려준다. 에러가 아니다.
            Err(ParseError::Exhausted) => break,
            Err(ParseError::Truncated) => {
                return Err(PyAssertionError::new_err(format!(
                    "행 패킷이 잘렸다. position={pos}, len={}",
                    data.len()
                )));
            }
        };
        pos = next;
        out.push(match field {
            Field::Null => py.None().into_bound(py),
            Field::Data(bytes) => decode(py, bytes, spec)?,
        });
    }
    Ok(pos)
}

/// 진단용. 파이썬 쪽 테스트가 쓴다.
#[pyfunction]
pub fn field_count(data: &[u8]) -> PyResult<usize> {
    let mut pos = 0;
    let mut n = 0;
    loop {
        match read_field(data, pos) {
            Ok((_, next)) => {
                pos = next;
                n += 1;
            }
            Err(ParseError::Exhausted) => return Ok(n),
            Err(ParseError::Truncated) => {
                return Err(PyAssertionError::new_err("행 패킷이 잘렸다"));
            }
        }
    }
}

/// 파이썬 어댑터가 쓰는 decoder id 상수. 여기와 파이썬이 어긋나면 조용히 망가지므로
/// 숫자를 파이썬에 하드코딩하지 않고 모듈에서 가져다 쓴다.
pub fn register_decoder_ids(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("DECODER_RAW", 0u8)?;
    m.add("DECODER_INT", 1u8)?;
    m.add("DECODER_FLOAT", 2u8)?;
    m.add("DECODER_DATE", 3u8)?;
    m.add("DECODER_DATETIME", 4u8)?;
    m.add("DECODER_TIME", 5u8)?;
    m.add("DECODER_CUSTOM", 6u8)?;
    Ok(())
}
