# faster-pymysql

pymysql 결과셋의 행 파싱을 Rust로 대체한다. gevent 기반 서버에서 이 구간이
CPU를 가장 많이 쓰기 때문이다.

```python
import faster_pymysql

faster_pymysql.install()
```

pymysql을 포크하지 않는다. 런타임에 `MySQLResult`의 메서드만 갈아끼우고,
`faster_pymysql.uninstall()`로 즉시 되돌린다.

## 지금 측정치

Python 3.12, PyMySQL 1.2.0, 20000행 × 12컬럼 합성 패킷(유저 테이블 모양).

| | pymysql | faster-pymysql | |
|---|---|---|---|
| 행 파싱 | 7.29 µs/row | 0.92 µs/row | 7.9배 |
| 결과셋 전체(소켓 제외) | 7.46 µs/row | 1.02 µs/row | 7.3배 |

실제 워크로드 기준으로 재려면 `bench/bench_row.py`의 `SCHEMA`를 그 쿼리의 컬럼
분포로 바꾼다. datetime과 Decimal이 많은 스키마는 이득이 그만큼 작다 — 파이썬
객체 생성 자체가 하한선이기 때문이다.

```bash
uv venv --python 3.12 .venv312
uv pip install --python ./.venv312/bin/python pymysql maturin pytest
VIRTUAL_ENV=$PWD/.venv312 ./.venv312/bin/maturin develop --release

./.venv312/bin/python -m pytest tests -q
./.venv312/bin/python bench/bench_row.py 20000 5
```

## 소켓은 옮기지 않는다

pymysql `connections.py:806` `_read_bytes()`의 `self._rfile.read()`가 gevent가
허브에 양보하는 지점이다. recv를 Rust 안에 넣으면 GIL을 풀어도 OS 스레드가
블로킹되고, gevent는 단일 스레드 협동 스케줄링이라 프로세스의 모든 그린렛이
함께 멈춘다.

- 파이썬 담당: 소켓 recv, 패킷 프레이밍
- Rust 담당: lenenc 분해, 행 분해, 타입 변환

파싱 중에는 GIL을 쥐고 있다. 결과가 파이썬 객체라 풀 수가 없다. 다만 쥐고 있는
시간이 7~8배 짧아진다.

## 값이 어긋날 수 없는 이유

Rust는 **MySQL이 실제로 보내는 정상 형태만** 빠른 길로 처리하고, 조금이라도
벗어나면 pymysql이 쓰던 converter 함수를 그대로 부른다. 폴백은 컬럼 단위가
아니라 값 하나 단위다.

| 상황 | 처리 |
|---|---|
| `42`, `2026-08-27 09:30:00` 같은 정상값 | Rust |
| `1_0`, ` 42` 처럼 파이썬 `int()`만 받는 형태 | 원래 converter |
| i64를 넘는 정수 | 원래 converter |
| Decimal, BIT | 원래 converter (`Decimal()` 생성 비용은 어차피 하한선) |
| 사내 커스텀 conv가 걸린 컬럼 | 원래 converter |

그래서 "Rust가 틀리게 계산한 값"이라는 게 정의상 나올 수 없다. 나머지는
차분 테스트가 본다 — 무작위 결과셋을 두 파서에 넣고 **값과 타입이 모두** 같은지
비교한다 (`1 == 1.0 == True`라서 `==`만으로는 못 잡는다).

재현해야 했던 pymysql의 기벽들:

- `0xff`가 NULL이 된다. `read_length_encoded_integer`의 if/elif 사슬에 가지가
  없어 암묵적으로 `None`이 반환된다
- 행에 컬럼이 모자라면 예외가 아니라 **짧은 tuple**로 끝난다 (`IndexError`를
  잡아 `break`)
- `DATETIME_RE`가 `re.match`라 뒤에 쓰레기가 붙어도 통과한다
- 그 정규식의 소수부 구분자 `.`이 이스케이프돼 있지 않아 아무 글자나 된다
- `convert_date`는 실패하면 **ascii 디코드한 str**을 돌려준다. `use_unicode=False`라
  converter가 bytes를 받았더라도 그렇다

마지막 항목은 차분 테스트가 잡은 실제 버그다.

## 아직 다른 점

잘린 패킷에서 던지는 예외 종류가 다르다. pymysql은 `struct.error`(길이 표지)
또는 `AssertionError`(내용), 여기서는 둘 다 `AssertionError`다. 커넥션이 이미
깨진 상황이라 그대로 뒀다.

## 다음

- [ ] 실제 MySQL 연결로 end-to-end 확인
- [ ] gevent 환경에서 그린렛 공정성 측정 (배치 크기 정하기)
- [ ] 패킷 프레이밍까지 Rust로 (기준선에서 8% 구간이라 우선순위 낮음)
