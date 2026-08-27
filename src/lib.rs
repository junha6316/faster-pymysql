//! pymysql 결과셋 행 파싱의 Rust 대체물.
//!
//! 경계: 소켓 읽기는 파이썬에 남는다. gevent가 허브에 양보하는 지점이 거기라,
//! recv를 Rust로 가져오면 GIL을 풀어도 OS 스레드가 블로킹돼 모든 그린렛이 멈춘다.

use pyo3::prelude::*;

pub mod decode;
pub mod lenenc;
pub mod row;
pub mod schema;

#[pymodule]
fn _rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<schema::Schema>()?;
    m.add_function(wrap_pyfunction!(row::parse_row, m)?)?;
    m.add_function(wrap_pyfunction!(row::parse_rows, m)?)?;
    m.add_function(wrap_pyfunction!(row::field_count, m)?)?;
    row::register_decoder_ids(m)?;
    Ok(())
}
