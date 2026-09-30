from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .spark_handoff import (
    HandoffValidationError,
    SparkHandoffConnector,
    validate_output_id,
)
from ..db import connect, get_store, init_db, utc_now
from ..paths import EXPORT_DIR

TARGET = "SPARK_CENTER_MANUAL"
PACKAGE_STATUSES = {"CREATED", "UPLOADED", "FAILED", "ARCHIVED"}


def safe_store_folder(store_id: str, store_name: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", store_id).strip("_-") or "store"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", store_name).strip("_-") or "Store"
    safe_name = re.sub(r"_+", "_", safe_name)
    return f"{safe_id}_{safe_name}"


def _new_package_id(store_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", store_id).strip("_-") or "store"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"SC_{safe_id}_{stamp}_{secrets.token_hex(2)}"


@dataclass(frozen=True)
class SparkCenterPackageResult:
    package_id: str
    store_id: str
    store_name: str
    statuses: tuple[str, ...]
    requested_limit: int
    product_count: int
    folder: Path
    manifest_path: Path
    validation_report_path: Path
    validation_status: str
    package_status: str
    created_at: str
    portal_package_verified: bool = False
    spark_center_manual_upload_allowed: bool = True
    browser_capture_mapping_verified: bool = False
    observed_spark_schema_compatible: bool | None = None
    spark_desktop_roundtrip_verified: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("folder", "manifest_path", "validation_report_path"):
            result[key] = str(result[key])
        result["statuses"] = list(result["statuses"])
        return result


class SparkCenterPackageService:
    """Build and track local folders for user-driven Spark Center portal upload."""

    def create(
        self,
        *,
        store_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
        asins: list[str] | None = None,
        out_root: str | Path | None = None,
        package_id: str | None = None,
        db: str | Path | None = None,
        allow_restricted: bool = False,
    ) -> SparkCenterPackageResult:
        if limit < 1:
            raise ValueError("limit must be greater than zero")
        init_db(db)
        profile = get_store(store_id, db)
        root = (Path(out_root) if out_root else EXPORT_DIR / "spark_center").resolve()
        store_root = root / safe_store_folder(store_id, profile["store_name"])
        ready_root = store_root / "ready"
        package_id_value = validate_output_id(
            package_id or _new_package_id(store_id), "package_id"
        )
        created_at = utc_now()

        connector = SparkHandoffConnector()
        try:
            handoff = connector.export(
                store_id=store_id,
                statuses=statuses,
                out_root=ready_root,
                limit=limit,
                asins=asins,
                job_id=package_id_value,
                db=db,
                allow_restricted=allow_restricted,
                history_target=TARGET,
                history_store_name=profile["store_name"],
                history_package_status="CREATED",
            )
        except HandoffValidationError:
            raise

        if handoff.folder.parent.resolve() != ready_root.resolve():
            self._mark_failed(package_id_value, "Package escaped store ready directory", db)
            raise RuntimeError("Spark Center package folder is outside the store ready directory")

        manifest = json.loads(handoff.manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "target": TARGET,
            "package_id": package_id_value,
            "requested_limit": limit,
            "validation_status": handoff.validation_status,
            "package_status": "CREATED",
            "spark_center_manual_upload_allowed": True,
            "portal_package_verified": False,
            "browser_capture_mapping_verified": manifest.get("browser_capture_mapping_verified", False),
            "observed_spark_schema_compatible": manifest.get("observed_spark_schema_compatible"),
            "spark_desktop_roundtrip_verified": False,
            "shopify_upload_verified": False,
        })
        handoff.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        report = json.loads(handoff.validation_report_path.read_text(encoding="utf-8"))
        report["browser_capture_mapping_verified"] = manifest.get("browser_capture_mapping_verified")
        report["observed_spark_schema_compatible"] = manifest.get("observed_spark_schema_compatible")
        report["spark_desktop_roundtrip_verified"] = False
        report["shopify_upload_verified"] = False
        report["portal_package_verified"] = False
        children = list(handoff.folder.iterdir())
        json_only = all(path.is_file() and path.suffix.lower() == ".json" for path in children)
        report["checks"].update({
            "package_under_store_ready": True,
            "upload_folder_json_only": json_only,
            "manifest_outside_upload_folder": handoff.manifest_path.parent != handoff.folder,
            "report_outside_upload_folder": handoff.validation_report_path.parent != handoff.folder,
        })
        if not json_only:
            report["status"] = "FAIL"
            report["errors"].append("Upload folder contains non-product files")
            handoff.validation_report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._mark_failed(package_id_value, "Upload folder contains non-product files", db)
            raise RuntimeError("Spark Center package validation failed")
        handoff.validation_report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return SparkCenterPackageResult(
            package_id=package_id_value,
            store_id=store_id,
            store_name=profile["store_name"],
            statuses=handoff.statuses,
            requested_limit=limit,
            product_count=handoff.product_count,
            folder=handoff.folder,
            manifest_path=handoff.manifest_path,
            validation_report_path=handoff.validation_report_path,
            validation_status=handoff.validation_status,
            package_status="CREATED",
            created_at=created_at,
            browser_capture_mapping_verified=bool(manifest.get("browser_capture_mapping_verified")),
            observed_spark_schema_compatible=manifest.get("observed_spark_schema_compatible"),
            spark_desktop_roundtrip_verified=False,
        )

    @staticmethod
    def _mark_failed(package_id: str, note: str, db=None) -> None:
        with connect(db) as con:
            con.execute(
                "UPDATE export_runs SET package_status='FAILED',note=? WHERE package_id=?",
                (note, package_id),
            )


def list_packages(store_id: str | None = None, limit: int = 20, db=None) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be greater than zero")
    init_db(db)
    clauses = ["target=?"]
    params: list[object] = [TARGET]
    if store_id:
        clauses.append("store_id=?")
        params.append(store_id)
    params.append(limit)
    with connect(db) as con:
        rows = con.execute(
            f"""
            SELECT package_id,target,store_id,store_name,statuses_json,output_path,
                   requested_limit,product_count,asin_hash,created_at,validation_status,
                   package_status,uploaded_at,note,portal_package_verified
            FROM export_runs WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            **dict(row),
            "statuses": json.loads(row["statuses_json"] or "[]"),
            "portal_package_verified": bool(row["portal_package_verified"]),
        }
        for row in rows
    ]


def mark_package(package_id: str, status: str, note: str = "", db=None) -> dict:
    init_db(db)
    status_value = status.upper()
    if status_value not in PACKAGE_STATUSES:
        raise ValueError(f"Unsupported package status: {status_value}")
    validate_output_id(package_id, "package_id")
    with connect(db) as con:
        row = con.execute(
            "SELECT id,uploaded_at FROM export_runs WHERE package_id=? AND target=?",
            (package_id, TARGET),
        ).fetchone()
        if not row:
            raise KeyError(f"Spark Center package not found: {package_id}")
        uploaded_at = row["uploaded_at"]
        if status_value == "UPLOADED" and not uploaded_at:
            uploaded_at = utc_now()
        con.execute(
            "UPDATE export_runs SET package_status=?,uploaded_at=?,note=? WHERE id=?",
            (status_value, uploaded_at, note, row["id"]),
        )
    return {
        "package_id": package_id,
        "package_status": status_value,
        "uploaded_at": uploaded_at,
        "note": note,
        "portal_package_verified": False,
    }
