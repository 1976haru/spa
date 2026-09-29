from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .connectors.spark_storage import materialize_source


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _candidate_files(source: Path) -> tuple[Iterable[tuple[Path, str | None]], object | None]:
    if source.is_file() and source.suffix.lower() == ".json":
        return [(source, None)], None
    context = materialize_source(source)
    root = context.__enter__()
    files = []
    for file in sorted(root.rglob("*.json")):
        parts = file.relative_to(root).parts
        job_id = parts[-2] if "datasets" in parts and len(parts) >= 2 else None
        files.append((file, job_id))
    return files, context


def probe_schema(source: str | Path) -> dict:
    """Read-only structural analysis of a JSON file, Spark folder, or ZIP."""
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    if source.is_file() and source.suffix.lower() not in {".json", ".zip"}:
        raise ValueError("Schema probe input must be a JSON file, directory, or ZIP")

    files, context = _candidate_files(source)
    key_presence: Counter[str] = Counter()
    field_types: dict[str, Counter[str]] = defaultdict(Counter)
    sample_types: dict[str, str] = {}
    asins: Counter[str] = Counter()
    job_ids: set[str] = set()
    schema_versions: set[str] = set()
    malformed: list[dict] = []
    json_count = 0
    product_count = 0
    try:
        file_items = list(files)
        for file, job_id in file_items:
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except Exception as exc:
                malformed.append({"file": file.name, "error": str(exc)[:500]})
                continue
            json_count += 1
            if not isinstance(payload, dict):
                continue
            if job_id:
                job_ids.add(job_id)
            for version_key in ("schemaVersion", "schema_version", "version"):
                if payload.get(version_key) not in (None, ""):
                    schema_versions.add(str(payload[version_key]))
            for key, value in payload.items():
                key_presence[key] += 1
                field_types[key][_json_type(value)] += 1
                sample_types.setdefault(key, _json_type(value))
            asin = str(payload.get("asin") or "").strip()
            if asin:
                product_count += 1
                asins[asin] += 1
        fields = {}
        for key in sorted(key_presence):
            fields[key] = {
                "types": dict(sorted(field_types[key].items())),
                "missing_ratio": round((json_count - key_presence[key]) / json_count, 6) if json_count else 0.0,
                "sample_value_type": sample_types[key],
            }
        schema_version_fields = [
            key for key in ("schemaVersion", "schema_version", "version") if key in fields
        ]
        return {
            "source": str(source.resolve()),
            "file_count": len(file_items),
            "json_count": json_count,
            "malformed_count": len(malformed),
            "product_count": product_count,
            "unique_asin": len(asins),
            "duplicate_asin": sum(count - 1 for count in asins.values()),
            "keys": sorted(key_presence),
            "fields": fields,
            "job_ids": sorted(job_ids),
            "possible_schema_version_fields": schema_version_fields,
            "possible_schema_versions": sorted(schema_versions),
            "errors": malformed,
        }
    finally:
        if context is not None:
            context.__exit__(None, None, None)
