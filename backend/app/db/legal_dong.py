"""부산 법정동 코드 마스터.

출처: 공공데이터포털 「국토교통부_전국 법정동」(https://www.data.go.kr/data/15063424/fileData.do,
2026-07-29 갱신본, 이용허락 제한 없음)에서 부산(26)·현존 코드만 잘라 `seed/legal_dong_busan.csv`로 둔다.
법정동은 거의 바뀌지 않으므로 연 1회 새 파일로 CSV를 갱신하고 `python -m app.db seed-regions`를 다시 실행한다.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

SEED_CSV = Path(__file__).parent / "seed" / "legal_dong_busan.csv"


@dataclass(frozen=True)
class LegalDong:
    code: str  # 시군구 5자리, 읍면동·리 10자리
    level: str  # sigungu | dong
    parent_code: str | None
    name: str  # 실거래가 원천의 법정동명과 같은 표기 (기장군은 '기장읍 교리'처럼 리까지)


def load_legal_dongs(path: Path = SEED_CSV) -> list[LegalDong]:
    """시군구를 먼저, 그 아래 읍면동·리를 뒤에 둔다(부모가 먼저 들어가야 외래키가 맞는다)."""
    sigungu: list[LegalDong] = []
    dongs: list[LegalDong] = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row["code"]
            if code.endswith("00000") and not row["eupmyeondong"]:
                sigungu.append(LegalDong(code[:5], "sigungu", None, row["sigungu"]))
            else:
                name = f"{row['eupmyeondong']} {row['ri']}" if row["ri"] else row["eupmyeondong"]
                dongs.append(LegalDong(code, "dong", code[:5], name))
    return sigungu + dongs
