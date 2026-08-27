//! MySQL 결과셋 행의 length-encoded 필드 분해.
//!
//! pymysql `protocol.py`의 `read_length_encoded_integer` /
//! `read_length_coded_string`와 **바이트 단위로 같은 판단**을 하도록 맞춰져 있다.
//! 여기서 어긋나면 예외 없이 데이터가 조용히 망가진다.

/// 필드 하나의 원시 형태. 아직 파이썬 객체가 아니다.
#[derive(Debug, PartialEq, Eq)]
pub enum Field<'a> {
    /// SQL NULL. 표지 `0xfb`, 그리고 `0xff`도 여기로 온다(아래 참고).
    Null,
    /// 길이만큼의 바이트. `buf`를 가리키는 창이라 복사가 없다.
    Data(&'a [u8]),
}

/// 필드를 더 읽을 수 없는 이유.
#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    /// 버퍼가 이미 끝났다. 행에 컬럼이 모자란 경우다.
    ///
    /// pymysql은 `read_uint8`에서 `IndexError`가 나고 상위
    /// `_read_row_from_packet`이 그걸 잡아 `break`한다. 즉 **에러가 아니라
    /// 짧은 tuple로 끝난다**. 호출자가 그렇게 처리해야 한다.
    Exhausted,
    /// 길이 표지나 내용이 중간에 잘렸다. 진짜 에러다.
    ///
    /// pymysql에서는 `struct.error`(길이 표지) 또는 `AssertionError`(내용)로
    /// 위로 던져진다.
    Truncated,
}

/// `buf[pos]`부터 필드 하나를 읽어 `(필드, 다음 위치)`를 돌려준다.
///
/// 첫 바이트가 길이 또는 표지다.
///
/// | 첫 바이트 | 의미 |
/// |---|---|
/// | `0x00..=0xfa` | 그 값이 곧 길이 |
/// | `0xfb` | NULL |
/// | `0xfc` | 이어지는 2바이트 LE가 길이 |
/// | `0xfd` | 이어지는 3바이트 LE가 길이 |
/// | `0xfe` | 이어지는 8바이트 LE가 길이 |
/// | `0xff` | NULL |
///
/// `0xff`가 NULL인 게 이상해 보이지만 pymysql이 그렇게 동작한다.
/// `read_length_encoded_integer`의 if/elif 사슬에 `0xff` 가지가 없어서
/// 암묵적으로 `None`이 반환되고, 호출자는 그걸 NULL과 구분하지 못한다.
/// 재현하지 않으면 차분 테스트가 갈린다.
pub fn read_field(buf: &[u8], pos: usize) -> Result<(Field<'_>, usize), ParseError> {
    let first = *buf.get(pos).ok_or(ParseError::Exhausted)?;

    let (len, after_len) = match first {
        0x00..=0xfa => (first as u64, pos + 1),
        0xfb | 0xff => return Ok((Field::Null, pos + 1)),
        0xfc => (read_uint_le(buf, pos + 1, 2)?, pos + 3),
        0xfd => (read_uint_le(buf, pos + 1, 3)?, pos + 4),
        0xfe => (read_uint_le(buf, pos + 1, 8)?, pos + 9),
    };

    let len = usize::try_from(len).map_err(|_| ParseError::Truncated)?;
    let end = after_len.checked_add(len).ok_or(ParseError::Truncated)?;
    let data = buf.get(after_len..end).ok_or(ParseError::Truncated)?;
    Ok((Field::Data(data), end))
}

/// `buf[pos]`부터 리틀엔디언 정수 `n`바이트. `n <= 8`.
fn read_uint_le(buf: &[u8], pos: usize, n: usize) -> Result<u64, ParseError> {
    let end = pos.checked_add(n).ok_or(ParseError::Truncated)?;
    let bytes = buf.get(pos..end).ok_or(ParseError::Truncated)?;
    let mut acc: u64 = 0;
    for (i, &b) in bytes.iter().enumerate() {
        acc |= (b as u64) << (8 * i);
    }
    Ok(acc)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(buf: &[u8], pos: usize) -> (&[u8], usize) {
        match read_field(buf, pos) {
            Ok((Field::Data(d), next)) => (d, next),
            other => panic!("Data를 기대했는데 {other:?}"),
        }
    }

    #[test]
    fn 한_바이트_길이() {
        assert_eq!(data(b"\x03cat", 0), (&b"cat"[..], 4));
        assert_eq!(data(b"\x00", 0), (&b""[..], 1));
    }

    #[test]
    fn 중간_위치에서_읽는다() {
        // 앞 필드를 건너뛴 자리에서 시작해도 같다.
        assert_eq!(data(b"\x03cat\x03dog", 4), (&b"dog"[..], 8));
    }

    #[test]
    fn 뒤에_데이터가_남아도_소비량은_같다() {
        assert_eq!(data(b"\x03cat\xaa\xbb", 0), (&b"cat"[..], 4));
    }

    #[test]
    fn 두_바이트_길이_표지() {
        let mut p = vec![0xfc, 0x04, 0x00];
        p.extend_from_slice(b"test");
        assert_eq!(data(&p, 0), (&b"test"[..], 7));
    }

    #[test]
    fn 세_바이트_길이_표지() {
        let mut p = vec![0xfd, 0x02, 0x00, 0x00];
        p.extend_from_slice(b"hi");
        assert_eq!(data(&p, 0), (&b"hi"[..], 6));
    }

    #[test]
    fn 여덟_바이트_길이_표지() {
        let mut p = vec![0xfe, 0x02, 0, 0, 0, 0, 0, 0, 0];
        p.extend_from_slice(b"hi");
        assert_eq!(data(&p, 0), (&b"hi"[..], 11));
    }

    #[test]
    fn null_표지() {
        assert_eq!(read_field(&[0xfb], 0), Ok((Field::Null, 1)));
    }

    #[test]
    fn ff는_pymysql처럼_null이다() {
        // read_length_encoded_integer의 if/elif가 0xff를 안 덮어 None이 된다.
        assert_eq!(read_field(&[0xff], 0), Ok((Field::Null, 1)));
    }

    #[test]
    fn 버퍼_끝은_exhausted() {
        // 행에 컬럼이 모자란 경우. 에러가 아니라 짧은 tuple로 끝나야 한다.
        assert_eq!(read_field(&[], 0), Err(ParseError::Exhausted));
        assert_eq!(read_field(b"\x03cat", 4), Err(ParseError::Exhausted));
    }

    #[test]
    fn 잘린_길이_표지는_truncated() {
        assert_eq!(read_field(&[0xfc, 0x04], 0), Err(ParseError::Truncated));
        assert_eq!(read_field(&[0xfe, 0x01], 0), Err(ParseError::Truncated));
    }

    #[test]
    fn 잘린_내용은_truncated() {
        assert_eq!(read_field(b"\x03ca", 0), Err(ParseError::Truncated));
        assert_eq!(
            read_field(&[0xfc, 0x04, 0x00, b'a'], 0),
            Err(ParseError::Truncated)
        );
    }

    #[test]
    fn 터무니없는_길이는_panic하지_않는다() {
        let p = [0xfe, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff];
        assert_eq!(read_field(&p, 0), Err(ParseError::Truncated));
    }
}
