# faster-pymysql

pymysql 결과셋의 행 파싱을 Rust(PyO3)로 대체한다. 배경과 측정치는 `README.md`.

## IMPORTANT: 포팅 경계

**소켓 읽기는 파이썬에 남긴다.** `_read_bytes()`의 `self._rfile.read()`가 gevent가
허브에 양보하는 지점이다. recv를 Rust로 옮기면 GIL을 풀어도 OS 스레드가 블로킹되고
프로세스의 모든 그린렛이 멈춘다. 이 경계를 넘는 구조를 만들기 전에 먼저 묻는다.

## YOU MUST: 정합성

파싱이 틀리면 예외 없이 데이터가 조용히 망가진다.

- Rust는 **정상 형태만** 빠른 길로 처리하고, 벗어나면 pymysql의 원래 converter를
  값 하나 단위로 부른다. 새 디코더를 넣을 때도 이 규칙을 지킨다
- 디코더나 스캐너를 건드렸으면 `tests/test_differential.py`(합성 패킷)와
  `tests/test_e2e.py`(실제 MySQL)를 둘 다 돌린다. 값뿐 아니라 **타입까지**
  비교한다 (`1 == 1.0 == True`)
- e2e는 서버가 없으면 조용히 skip된다. "통과"를 봤다고 돌았다고 믿지 말고
  skip 개수를 본다
- pymysql의 기벽(0xff→NULL, 짧은 tuple, `re.match`의 느슨함)을 "고치지" 않는다.
  재현 대상이다

## 명령

`python3`은 pyenv system(3.14)으로 잡힌다. 배포 타깃이 3.11/3.12(abi3)이라 항상
3.12 venv에서 돌린다. `.venv312/`는 gitignore 대상이라 없으면 먼저 만든다.

```bash
uv venv --python 3.12 .venv312
uv pip install --python ./.venv312/bin/python pymysql maturin pytest

cargo test                                              # Rust 단위 테스트
VIRTUAL_ENV=$PWD/.venv312 ./.venv312/bin/maturin develop --release
./.venv312/bin/python -m pytest tests -q                # 차분 + install 테스트
./.venv312/bin/python bench/bench_row.py [rows] [repeat]  # 기본 20000 5

# e2e용 일회용 서버 (없으면 test_e2e.py가 skip된다)
docker run -d --name fpm-e2e -e MYSQL_ALLOW_EMPTY_PASSWORD=1 \
  -e MYSQL_DATABASE=fpm -p 3307:3306 mysql:8.4
```

Rust를 고쳤으면 `maturin develop`을 다시 돌려야 파이썬 테스트에 반영된다.

## 함정

- `conn.encoding`은 charset명이 아니라 **파이썬 인코딩명**이다. `utf8mb4`가 아니라
  `utf8`. charset명을 `bytes.decode()`에 넘기면 `LookupError`가 난다.
- 벤치 케이스는 thunk를 돌려줘야 한다. setup을 타이머 밖에 두면 측정 대상이
  타이머 밖에서 실행돼 0ms가 나온다.
- `pymysql.__version__`은 DB-API 호환용 문자열(`2.2.8`)이라 실제 패키지 버전(1.2.0)과
  다르다. 버전 확인은 `uv pip list`로 한다.
- decoder id는 Rust `row.rs`가 모듈 상수로 내보내고 파이썬이 그걸 가져다 쓴다.
  숫자를 파이썬에 하드코딩하면 어긋나도 조용히 망가진다.
