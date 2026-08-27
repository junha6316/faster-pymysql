//! 결과셋마다 한 번 만드는 컬럼 디스패치 표.
//!
//! pymysql은 행마다 `self.converters`를 순회하며 파이썬 함수 포인터를 따라간다.
//! 여기서는 결과셋을 열 때 한 번 판정해두고 행 파싱은 그 표만 본다.

use pyo3::prelude::*;

/// 컬럼값을 무엇으로 만들지.
///
/// 파이썬 어댑터가 pymysql의 converter 함수를 **동일성으로** 비교해 정한다.
/// 모르는 converter는 전부 `Custom`이라 안전하다.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Decoder {
    /// converter 없음. encoding에 따라 `bytes` 또는 `str`.
    Raw,
    Int,
    Float,
    Date,
    DateTime,
    /// TIME 컬럼 → `timedelta`.
    Time,
    /// Rust가 재현하지 않는 converter. 원래 파이썬 함수를 그대로 부른다.
    /// Decimal, BIT, 사내 커스텀 conv가 여기로 온다.
    Custom,
}

impl Decoder {
    fn from_id(id: u8) -> PyResult<Self> {
        Ok(match id {
            0 => Decoder::Raw,
            1 => Decoder::Int,
            2 => Decoder::Float,
            3 => Decoder::Date,
            4 => Decoder::DateTime,
            5 => Decoder::Time,
            6 => Decoder::Custom,
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "알 수 없는 decoder id: {id}"
                )));
            }
        })
    }
}

/// 바이트를 파이썬 값으로 올릴 때 쓰는 인코딩.
///
/// pymysql `conn.encoding`은 charset명이 아니라 **파이썬 인코딩명**이다.
/// `utf8mb4`가 아니라 `utf8`이 들어온다.
#[derive(Clone, Debug)]
pub enum Encoding {
    /// encoding이 None. 값이 `bytes`로 나간다.
    Bytes,
    Ascii,
    Utf8,
    /// latin1 등. 파이썬 `bytes.decode()`에 맡긴다.
    Other(String),
}

impl Encoding {
    fn parse(name: Option<&str>) -> Self {
        match name {
            None => Encoding::Bytes,
            Some(n) => match n.to_ascii_lowercase().replace('-', "_").as_str() {
                "ascii" | "us_ascii" => Encoding::Ascii,
                "utf8" | "utf_8" | "u8" => Encoding::Utf8,
                _ => Encoding::Other(n.to_owned()),
            },
        }
    }
}

pub struct ColumnSpec {
    pub decoder: Decoder,
    pub encoding: Encoding,
    /// pymysql이 쓰던 원래 converter. `None`이면 converter가 없는 컬럼이다.
    ///
    /// `Custom` 컬럼은 항상 이걸 부르고, 나머지 디코더도 **값 하나가 예상 밖일 때**
    /// 이걸 부른다. 그래서 Rust가 자신 없는 값은 pymysql과 글자 그대로 같은 결과가 된다.
    pub fallback: Option<Py<PyAny>>,
}

/// 컬럼 스펙 묶음. 파이썬 쪽에서 결과셋마다 한 번 만들어 들고 있는다.
#[pyclass(frozen, module = "faster_pymysql._rust")]
pub struct Schema {
    pub cols: Vec<ColumnSpec>,
}

#[pymethods]
impl Schema {
    /// `specs`는 `(decoder_id, encoding_name, converter)` 튜플의 리스트다.
    #[new]
    fn new(specs: Vec<(u8, Option<String>, Option<Py<PyAny>>)>) -> PyResult<Self> {
        let cols = specs
            .into_iter()
            .map(|(id, enc, conv)| {
                Ok(ColumnSpec {
                    decoder: Decoder::from_id(id)?,
                    encoding: Encoding::parse(enc.as_deref()),
                    fallback: conv,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Schema { cols })
    }

    fn __len__(&self) -> usize {
        self.cols.len()
    }
}
