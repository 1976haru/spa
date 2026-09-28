from __future__ import annotations

import json
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .base import ProductSourceConnector


@contextmanager
def materialize_source(source: Path) -> Iterator[Path]:
    if source.is_dir():
        yield source
        return
    if source.suffix.lower() != ".zip":
        raise ValueError("Spark source must be a storage directory or .zip file")
    with tempfile.TemporaryDirectory(prefix="shopsource_spark_") as td:
        with zipfile.ZipFile(source) as zf:
            zf.extractall(td)
        yield Path(td)


class SparkStorageConnector(ProductSourceConnector):
    """Reads Crawlee-style Spark local storage datasets without modifying Spark files."""

    def iter_products(self, source: Path) -> Iterable[tuple[dict, dict]]:
        with materialize_source(source) as root:
            datasets = root / "datasets"
            if not datasets.exists():
                # tolerate one wrapper folder inside the zip
                matches = list(root.glob("*/datasets"))
                if len(matches) == 1:
                    datasets = matches[0]
                else:
                    raise FileNotFoundError("datasets/ folder not found in Spark storage")

            for job_dir in sorted(p for p in datasets.iterdir() if p.is_dir()):
                job_id = job_dir.name
                for file in sorted(job_dir.glob("*.json")):
                    try:
                        payload = json.loads(file.read_text(encoding="utf-8"))
                    except Exception as exc:
                        yield {"_invalid": str(exc)}, {
                            "job_id": job_id,
                            "source_file": file.name,
                        }
                        continue
                    meta = {
                        "job_id": job_id,
                        "source_file": file.name,
                        "collected_at": payload.get("_collectedAt"),
                        "source_url": payload.get("_sourceUrl"),
                        "list_page": payload.get("_listPage"),
                    }
                    yield payload, meta
