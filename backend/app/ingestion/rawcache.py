import json
import os
from datetime import datetime
from pathlib import Path

from app.adapters.molit.parser import rows_from_fields
from app.adapters.molit.types import RawTrade, SourceKind


class RawCache:
    """원천 응답을 (종류, 시군구, 계약월) 단위 JSON 파일로 저장한다. 서버는 이 파일만 읽고 네트워크를 쓰지 않는다."""

    def __init__(self, data_dir: Path):
        self._dir = data_dir / "molit"

    def _path(self, kind: SourceKind, sgg_cd: str, ym: str) -> Path:
        return self._dir / f"{kind.value}_{sgg_cd}_{ym}.json"

    def save(self, kind: SourceKind, sgg_cd: str, ym: str, rows: list[RawTrade], fetched_at: datetime) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(kind, sgg_cd, ym)
        tmp = path.with_suffix(".tmp")
        payload = {"fetched_at": fetched_at.isoformat(), "rows": [r.raw for r in rows]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def fetched_at(self, kind: SourceKind, sgg_cd: str, ym: str) -> datetime | None:
        path = self._path(kind, sgg_cd, ym)
        if not path.exists():
            return None
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["fetched_at"])

    def load(self, kind: SourceKind, sgg_cd: str, ym: str) -> list[RawTrade]:
        data = json.loads(self._path(kind, sgg_cd, ym).read_text(encoding="utf-8"))
        return rows_from_fields(kind, data["rows"])

    def windows(self) -> list[tuple[SourceKind, str, str]]:
        found = []
        for path in sorted(self._dir.glob("*.json")) if self._dir.exists() else []:
            kind, sgg_cd, ym = path.stem.rsplit("_", 2)
            found.append((SourceKind(kind), sgg_cd, ym))
        return found

    def latest_fetch(self) -> datetime | None:
        times = [self.fetched_at(*w) for w in self.windows()]
        return max((t for t in times if t), default=None)
