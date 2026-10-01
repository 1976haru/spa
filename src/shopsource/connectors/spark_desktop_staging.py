"""Safe, additive staging into Spark Desktop's observed datasets directory."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


class DatasetAlreadyExists(FileExistsError):
    """Raised rather than changing any existing Spark dataset."""


def spark_desktop_datasets_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; Spark Desktop storage cannot be located")
    return Path(appdata).expanduser() / "spark" / "storage" / "datasets"


def _safe_dataset_id(dataset_id: str) -> str:
    if not dataset_id or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
        raise ValueError("dataset_id may contain only letters, numbers, hyphen, and underscore")
    return dataset_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SparkDesktopStageResult:
    dataset_id: str
    source_path: Path
    destination_path: Path
    product_count: int
    staged_at: str
    source_hashes: dict[str, str]
    destination_hashes: dict[str, str]
    all_hashes_match: bool

    def to_dict(self) -> dict:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["destination_path"] = str(self.destination_path)
        return result


def validate_product_json_folder(source: str | Path) -> list[Path]:
    folder = Path(source).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("Package source must be a directory")
    children = list(folder.iterdir())
    if not children or any(not p.is_file() or p.is_symlink() or p.suffix.lower() != ".json" for p in children):
        raise ValueError("Package folder must contain product JSON files only")
    files = sorted(children, key=lambda p: p.name)
    expected = [f"{index:09d}.json" for index in range(1, len(files) + 1)]
    if [p.name for p in files] != expected:
        raise ValueError("Package JSON filenames must be contiguous 9-digit names starting at 000000001.json")
    seen_asins: set[str] = set()
    for path in files:
        try:
            item = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid product JSON: {path.name}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Product JSON root must be an object: {path.name}")
        asin = item.get("asin")
        if not isinstance(asin, str) or not asin.strip():
            raise ValueError(f"Product JSON has no ASIN: {path.name}")
        if asin in seen_asins:
            raise ValueError(f"Duplicate ASIN in package: {asin}")
        seen_asins.add(asin)
    return files


def stage_dataset(
    source: str | Path,
    dataset_id: str,
    *,
    datasets_root: str | Path | None = None,
) -> SparkDesktopStageResult:
    """Atomically add a new dataset containing byte-identical product JSON only."""
    dataset_id = _safe_dataset_id(dataset_id)
    source_path = Path(source).resolve(strict=True)
    files = validate_product_json_folder(source_path)
    root = Path(datasets_root) if datasets_root is not None else spark_desktop_datasets_root()
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Spark Desktop datasets root must already exist")
    destination = root / dataset_id
    if destination.exists():
        raise DatasetAlreadyExists(f"Spark dataset already exists: {destination}")

    temp_path = Path(tempfile.mkdtemp(prefix=f".shopsource_stage_{dataset_id}_", dir=root))
    try:
        source_hashes: dict[str, str] = {}
        destination_hashes: dict[str, str] = {}
        for src in files:
            dst = temp_path / src.name
            with src.open("rb") as reader, dst.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            source_hashes[src.name] = _sha256(src)
            destination_hashes[src.name] = _sha256(dst)
            if source_hashes[src.name] != destination_hashes[src.name]:
                raise IOError(f"SHA-256 mismatch while staging {src.name}")
            # Validate the copied bytes as well, without reserializing them.
            if not isinstance(json.loads(dst.read_text(encoding="utf-8-sig")), dict):
                raise ValueError(f"Staged product JSON root must be an object: {src.name}")
        if destination.exists():
            raise DatasetAlreadyExists(f"Spark dataset already exists: {destination}")
        # Same-volume rename exposes either no dataset or the complete dataset.
        os.rename(temp_path, destination)
        temp_path = None
        staged_at = datetime.now(timezone.utc).isoformat()
        return SparkDesktopStageResult(
            dataset_id=dataset_id,
            source_path=source_path,
            destination_path=destination,
            product_count=len(files),
            staged_at=staged_at,
            source_hashes=source_hashes,
            destination_hashes=destination_hashes,
            all_hashes_match=source_hashes == destination_hashes,
        )
    finally:
        if temp_path is not None and temp_path.exists():
            shutil.rmtree(temp_path)
