//! 컬럼값 바이트 → 파이썬 객체.
//!
//! 설계 원칙 하나: **Rust가 확신하는 모양만 빠른 길로 가고, 나머지는 pymysql의
//! 원래 converter를 그대로 부른다.** 그래서 어긋날 수 있는 값은 정의상 없다.
//! 빠른 길은 MySQL이 실제로 보내는 정상 형태만 덮는다.

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDate, PyDateTime, PyDelta, PyString};

use crate::schema::{ColumnSpec, Decoder, Encoding};

/// 필드 하나를 파이썬 객체로.
pub fn decode<'py>(
    py: Python<'py>,
    data: &[u8],
    spec: &ColumnSpec,
) -> PyResult<Bound<'py, PyAny>> {
    match spec.decoder {
        Decoder::Raw => materialize(py, data, &spec.encoding),
        Decoder::Int => match parse_int(data) {
            Some(v) => v.into_bound_py_any(py),
            None => fallback(py, data, spec),
        },
        Decoder::Float => match parse_float(data) {
            Some(v) => v.into_bound_py_any(py),
            None => fallback(py, data, spec),
        },
        Decoder::Date => match parse_date(data) {
            Some((y, m, d)) => match PyDate::new(py, y, m, d) {
                Ok(o) => Ok(o.into_any()),
                // '0000-00-00'처럼 형식은 맞지만 없는 날짜.
                // pymysql은 ValueError를 잡아 문자열을 그대로 돌려준다.
                // '0000-00-00'처럼 형식은 맞지만 없는 날짜.
                //
                // pymysql `convert_date`는 ValueError를 잡아 **문자열**을 돌려준다.
                // encoding이 None이라 converter가 bytes를 받았더라도, 함수 첫 줄에서
                // 스스로 ascii 디코드를 하므로 결과는 언제나 str이다. 여기서 원본
                // bytes를 돌려주면 예외 없이 타입이 어긋난다.
                Err(_) => ascii_str(py, data),
            },
            None => fallback(py, data, spec),
        },
        Decoder::DateTime => match parse_datetime(data) {
            Some((y, mo, d, h, mi, s, us)) => {
                match PyDateTime::new(py, y, mo, d, h, mi, s, us, None) {
                    Ok(o) => Ok(o.into_any()),
                    // pymysql은 여기서 convert_date로 넘어간다. 그 경로는
                    // 원래 함수에 맡긴다.
                    Err(_) => fallback(py, data, spec),
                }
            }
            None => fallback(py, data, spec),
        },
        Decoder::Time => match parse_timedelta(data) {
            Some(micros) => {
                let (days, rem) = (micros.div_euclid(86_400_000_000), micros.rem_euclid(86_400_000_000));
                let secs = rem / 1_000_000;
                let us = rem % 1_000_000;
                Ok(PyDelta::new(py, days as i32, secs as i32, us as i32, true)?.into_any())
            }
            None => fallback(py, data, spec),
        },
        Decoder::Custom => fallback(py, data, spec),
    }
}

/// pymysql이 converter에 넘겼을 값을 그대로 만든다. `str` 또는 `bytes`.
fn materialize<'py>(
    py: Python<'py>,
    data: &[u8],
    encoding: &Encoding,
) -> PyResult<Bound<'py, PyAny>> {
    match encoding {
        Encoding::Bytes => Ok(PyBytes::new(py, data).into_any()),
        Encoding::Ascii => {
            if data.is_ascii() {
                // SAFETY 불필요: is_ascii()면 유효한 UTF-8이다.
                let s = std::str::from_utf8(data).expect("ascii는 utf-8이다");
                Ok(PyString::new(py, s).into_any())
            } else {
                // 파이썬에 맡겨 UnicodeDecodeError를 똑같이 낸다.
                decode_via_python(py, data, "ascii")
            }
        }
        Encoding::Utf8 => match std::str::from_utf8(data) {
            Ok(s) => Ok(PyString::new(py, s).into_any()),
            Err(_) => decode_via_python(py, data, "utf-8"),
        },
        Encoding::Other(name) => decode_via_python(py, data, name),
    }
}

/// 이미 ASCII임이 확인된 바이트를 `str`로. 스캐너를 통과한 값에만 쓴다.
fn ascii_str<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    match std::str::from_utf8(data) {
        Ok(s) => Ok(PyString::new(py, s).into_any()),
        // 스캐너가 통과시켰다면 여기 올 수 없다. 그래도 파이썬에 맡겨 같은 예외를 낸다.
        Err(_) => decode_via_python(py, data, "ascii"),
    }
}

fn decode_via_python<'py>(
    py: Python<'py>,
    data: &[u8],
    encoding: &str,
) -> PyResult<Bound<'py, PyAny>> {
    PyBytes::new(py, data).call_method1("decode", (encoding,))
}

/// pymysql의 원래 converter를 부른다. 입력도 pymysql이 넘겼을 것과 같게 만든다.
fn fallback<'py>(
    py: Python<'py>,
    data: &[u8],
    spec: &ColumnSpec,
) -> PyResult<Bound<'py, PyAny>> {
    let value = materialize(py, data, &spec.encoding)?;
    match &spec.fallback {
        Some(f) => f.bind(py).call1((value,)),
        None => Ok(value),
    }
}

// ---- 바이트 스캐너들 ----
//
// 전부 "정상 형태만 받고 그 외에는 None"이다. None이면 호출자가 파이썬으로 넘긴다.

/// `[+-]?\d+`만 받는다. 공백·밑줄은 파이썬 `int()`가 허용하지만 여기선 넘긴다.
fn parse_int(b: &[u8]) -> Option<i64> {
    let (neg, digits) = match b.first()? {
        b'-' => (true, &b[1..]),
        b'+' => (false, &b[1..]),
        _ => (false, b),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let mut acc: i64 = 0;
    for &d in digits {
        // 오버플로면 파이썬 큰 정수에 맡긴다.
        acc = acc.checked_mul(10)?.checked_add((d - b'0') as i64)?;
    }
    Some(if neg { -acc } else { acc })
}

/// 숫자·부호·소수점·지수만 있는 형태. `inf`/`nan` 철자는 파이썬에 맡긴다.
fn parse_float(b: &[u8]) -> Option<f64> {
    if b.is_empty() || !b.iter().any(u8::is_ascii_digit) {
        return None;
    }
    if !b
        .iter()
        .all(|c| c.is_ascii_digit() || matches!(c, b'+' | b'-' | b'.' | b'e' | b'E'))
    {
        return None;
    }
    std::str::from_utf8(b).ok()?.parse().ok()
}

/// `숫자-숫자-숫자` 세 토막. pymysql `convert_date`의 `split("-", 2)`에 대응.
fn parse_date(b: &[u8]) -> Option<(i32, u8, u8)> {
    let mut it = b.splitn(3, |&c| c == b'-');
    let y = parse_int(it.next()?)?;
    let m = parse_int(it.next()?)?;
    let d = parse_int(it.next()?)?;
    if it.next().is_some() {
        return None;
    }
    Some((
        i32::try_from(y).ok()?,
        u8::try_from(m).ok()?,
        u8::try_from(d).ok()?,
    ))
}

/// pymysql `DATETIME_RE`와 같은 판정.
///
/// `(\d{1,4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2}):(\d{1,2})(?:.(\d{1,6}))?`
///
/// `re.match`라 **뒤에 쓰레기가 붙어도 통과한다**. 소수부 구분자가 `.`이 아니라
/// 아무 글자여도 통과한다(정규식의 `.`이 이스케이프돼 있지 않다). 둘 다 재현한다.
fn parse_datetime(b: &[u8]) -> Option<(i32, u8, u8, u8, u8, u8, u32)> {
    let mut i = 0;
    let y = take_digits(b, &mut i, 4)?;
    expect(b, &mut i, |c| c == b'-')?;
    let mo = take_digits(b, &mut i, 2)?;
    expect(b, &mut i, |c| c == b'-')?;
    let d = take_digits(b, &mut i, 2)?;
    expect(b, &mut i, |c| c == b'T' || c == b' ')?;
    let h = take_digits(b, &mut i, 2)?;
    expect(b, &mut i, |c| c == b':')?;
    let mi = take_digits(b, &mut i, 2)?;
    expect(b, &mut i, |c| c == b':')?;
    let s = take_digits(b, &mut i, 2)?;
    let us = take_fraction(b, i);
    Some((
        i32::try_from(y).ok()?,
        u8::try_from(mo).ok()?,
        u8::try_from(d).ok()?,
        u8::try_from(h).ok()?,
        u8::try_from(mi).ok()?,
        u8::try_from(s).ok()?,
        us,
    ))
}

/// pymysql `TIMEDELTA_RE`. 전체 마이크로초를 돌려준다(음수 가능).
///
/// `(-)?(\d{1,3}):(\d{1,2}):(\d{1,2})(?:.(\d{1,6}))?`
fn parse_timedelta(b: &[u8]) -> Option<i64> {
    let mut i = 0;
    let neg = b.first() == Some(&b'-');
    if neg {
        i += 1;
    }
    let h = take_digits(b, &mut i, 3)?;
    expect(b, &mut i, |c| c == b':')?;
    let mi = take_digits(b, &mut i, 2)?;
    expect(b, &mut i, |c| c == b':')?;
    let s = take_digits(b, &mut i, 2)?;
    let us = take_fraction(b, i);

    let total = ((h as i64 * 60 + mi as i64) * 60 + s as i64) * 1_000_000 + us as i64;
    Some(if neg { -total } else { total })
}

/// 최대 `max`자리 숫자를 욕심껏 먹는다. 한 자리도 없으면 `None`.
///
/// 정규식 `\d{1,N}`은 역추적을 하지만, 짧게 물러서면 그 자리에 또 숫자가 오므로
/// 뒤따르는 구분자 검사가 어차피 실패한다. 그래서 욕심껏 먹는 것으로 충분하다.
fn take_digits(b: &[u8], i: &mut usize, max: usize) -> Option<u32> {
    let start = *i;
    let mut v: u32 = 0;
    while *i < b.len() && *i - start < max && b[*i].is_ascii_digit() {
        v = v * 10 + (b[*i] - b'0') as u32;
        *i += 1;
    }
    if *i == start { None } else { Some(v) }
}

fn expect(b: &[u8], i: &mut usize, ok: impl Fn(u8) -> bool) -> Option<()> {
    if *i < b.len() && ok(b[*i]) {
        *i += 1;
        Some(())
    } else {
        None
    }
}

/// `(?:.(\d{1,6}))?` — 아무 글자 하나 + 숫자 1~6개. 없으면 0.
///
/// `_convert_second_fraction`이 `ljust(6, "0")`으로 채우므로 자릿수만큼 자리올림한다.
fn take_fraction(b: &[u8], mut i: usize) -> u32 {
    if i >= b.len() {
        return 0;
    }
    i += 1; // 구분자 한 글자
    let start = i;
    let mut v: u32 = 0;
    while i < b.len() && i - start < 6 && b[i].is_ascii_digit() {
        v = v * 10 + (b[i] - b'0') as u32;
        i += 1;
    }
    let n = i - start;
    if n == 0 {
        return 0;
    }
    v * 10u32.pow((6 - n) as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn int_정상형태만() {
        assert_eq!(parse_int(b"42"), Some(42));
        assert_eq!(parse_int(b"-42"), Some(-42));
        assert_eq!(parse_int(b"+42"), Some(42));
        assert_eq!(parse_int(b"007"), Some(7));
        // 파이썬에 넘길 것들
        assert_eq!(parse_int(b""), None);
        assert_eq!(parse_int(b" 42"), None);
        assert_eq!(parse_int(b"1_0"), None);
        assert_eq!(parse_int(b"4.2"), None);
        assert_eq!(parse_int(b"99999999999999999999"), None);
    }

    #[test]
    fn float_정상형태만() {
        assert_eq!(parse_float(b"1.5"), Some(1.5));
        assert_eq!(parse_float(b"-3.25e-10"), Some(-3.25e-10));
        assert_eq!(parse_float(b"1."), Some(1.0));
        assert_eq!(parse_float(b"inf"), None);
        assert_eq!(parse_float(b"nan"), None);
        assert_eq!(parse_float(b""), None);
    }

    #[test]
    fn date_세_토막() {
        assert_eq!(parse_date(b"2007-02-26"), Some((2007, 2, 26)));
        // 형식은 맞고 날짜가 없는 경우. 파이썬도 여기까진 통과하고 date()에서 걸린다.
        assert_eq!(parse_date(b"0000-00-00"), Some((0, 0, 0)));
        // split("-", 2)라 세 번째 토막에 '-'가 남으면 int()가 실패한다.
        assert_eq!(parse_date(b"2007-02-26-99"), None);
        assert_eq!(parse_date(b"2007-02"), None);
    }

    #[test]
    fn datetime_기본형() {
        assert_eq!(
            parse_datetime(b"2007-02-25 23:06:20"),
            Some((2007, 2, 25, 23, 6, 20, 0))
        );
        assert_eq!(
            parse_datetime(b"2007-02-25T23:06:20"),
            Some((2007, 2, 25, 23, 6, 20, 0))
        );
    }

    #[test]
    fn datetime_소수부는_6자리로_채운다() {
        // pymysql _convert_second_fraction: ljust(6, "0")
        assert_eq!(parse_datetime(b"2007-02-25 23:06:20.5").unwrap().6, 500_000);
        assert_eq!(
            parse_datetime(b"2007-02-25 23:06:20.123456").unwrap().6,
            123_456
        );
    }

    #[test]
    fn datetime_정규식_기벽을_재현한다() {
        // re.match라 뒤에 쓰레기가 붙어도 통과한다.
        assert_eq!(
            parse_datetime(b"2007-02-25 23:06:20xyz"),
            Some((2007, 2, 25, 23, 6, 20, 0))
        );
        // (?:.(\d{1,6}))? 의 . 이 이스케이프돼 있지 않아 아무 글자나 구분자가 된다.
        assert_eq!(parse_datetime(b"2007-02-25 23:06:20x12").unwrap().6, 120_000);
        // 소수부 자리는 6개까지만 먹는다.
        assert_eq!(
            parse_datetime(b"2007-02-25 23:06:20.1234567").unwrap().6,
            123_456
        );
    }

    #[test]
    fn datetime_어긋나면_none() {
        assert_eq!(parse_datetime(b"2007-02-25"), None);
        assert_eq!(parse_datetime(b"12345-01-02 03:04:05"), None);
        assert_eq!(parse_datetime(b"random crap"), None);
    }

    #[test]
    fn timedelta_부호와_시간() {
        assert_eq!(parse_timedelta(b"25:06:17"), Some(90_377_000_000));
        assert_eq!(parse_timedelta(b"-25:06:17"), Some(-90_377_000_000));
        assert_eq!(parse_timedelta(b"00:00:01.5"), Some(1_500_000));
        assert_eq!(parse_timedelta(b"random crap"), None);
    }
}
