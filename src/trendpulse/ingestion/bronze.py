"""Bronze layer: immutable raw API responses.

Each successful API response is written *immediately* (its own
transaction) as gzip-compressed JSON with lineage metadata, so a run that
dies half-way still leaves replayable raw data behind. A local lake copy
(``data/lake/bronze/dt=YYYY-MM-DD/run=<id>/<response_id>.json.gz``) is
written too when a lake directory is configured.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from trendpulse.logging_utils import get_logger
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc

log = get_logger("bronze")


@dataclass
class BronzeRecord:
    run_id: str
    endpoint: str
    discovery_method: str          # regional_chart | search | search_videos | search_refresh
    discovery_query: str           # chart:<cat|all> or seed id
    region_code: str | None
    category_id: int | None
    page_index: int
    request_params: dict[str, Any]
    http_status: int
    payload: dict[str, Any]
    fetched_at: datetime
    response_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def item_count(self) -> int:
        return len(self.payload.get("items", []) or [])

    def to_row(self) -> dict[str, Any]:
        raw = json.dumps(self.payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {
            "response_id": self.response_id,
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "discovery_method": self.discovery_method,
            "discovery_query": self.discovery_query,
            "region_code": self.region_code,
            "category_id": self.category_id,
            "page_index": self.page_index,
            "request_params": json.dumps(self.request_params, sort_keys=True),
            "http_status": self.http_status,
            "item_count": self.item_count,
            "payload_gz": gzip.compress(raw, compresslevel=6),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BronzeRecord:
        payload = json.loads(gzip.decompress(row["payload_gz"]).decode("utf-8"))
        return cls(
            run_id=row["run_id"], endpoint=row["endpoint"], discovery_method=row["discovery_method"],
            discovery_query=row["discovery_query"], region_code=row["region_code"],
            category_id=row["category_id"], page_index=row["page_index"],
            request_params=json.loads(row["request_params"]), http_status=row["http_status"],
            payload=payload, fetched_at=as_utc(row["fetched_at"]), response_id=row["response_id"],
        )


class BronzeWriter:
    def __init__(self, wh: Warehouse, lake_dir: Path | None = None):
        self.wh = wh
        self.lake_dir = lake_dir
        self.written: list[BronzeRecord] = []
        self.bytes_compressed = 0

    def write(self, rec: BronzeRecord) -> None:
        row = rec.to_row()
        with self.wh.begin() as conn:
            self.wh.insert_many(conn, s.bronze_api_responses, [row])
        self.bytes_compressed += len(row["payload_gz"])
        if self.lake_dir is not None:
            d = (self.lake_dir / "bronze" / f"dt={rec.fetched_at:%Y-%m-%d}" / f"run={rec.run_id}")
            d.mkdir(parents=True, exist_ok=True)
            meta = {k: v for k, v in row.items() if k not in {"payload_gz"}}
            meta["fetched_at"] = rec.fetched_at.isoformat()
            with gzip.open(d / f"{rec.response_id}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump({"meta": meta, "payload": rec.payload}, fh, ensure_ascii=False)
        self.written.append(rec)


def load_bronze(wh: Warehouse, run_id: str) -> list[BronzeRecord]:
    t = s.bronze_api_responses
    with wh.connect() as conn:
        rows = conn.execute(select(t).where(t.c.run_id == run_id).order_by(t.c.fetched_at, t.c.page_index)
                            ).mappings().all()
    return [BronzeRecord.from_row(dict(r)) for r in rows]
