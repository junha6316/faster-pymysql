"""두 파서 결과를 값과 타입 모두로 비교한다.

`1 == 1.0 == True`, `date(2026,1,1) != datetime(2026,1,1,0,0)`이지만 반대로
`Decimal("1") == 1`이므로, 값만 보면 타입이 어긋난 걸 놓친다.
"""

from __future__ import annotations

import math


def same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def diff(a, b) -> str:
    """어긋난 첫 자리를 짚어준다. 12컬럼 × 수백 행에서 눈으로 찾기 어렵다."""
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        if len(a) != len(b):
            return f"길이 {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if not same(x, y):
                return f"[{i}] {diff(x, y)}"
    return f"{a!r} ({type(a).__name__}) != {b!r} ({type(b).__name__})"
