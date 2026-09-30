from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .base import ExportConnector
from .manifest import build_manifest
from ..classifier import ALLOWED_STATUSES
from ..db import connect, get_store, init_db, utc_now
from ..paths import EXPORT_DIR
from ..sourcing.mapping import (BROWSER_CAPTURE_TO_SPARK_CAPABILITY, KEEPA_TO_SPARK_CAPABILITY,
                                browser_capture_to_spark_payload, observed_spark_schema_issues,
                                to_spark_product_payload)

CAPABILITY_STATUS = "DATASET_LOAD_VERIFIED"
INTERNAL_FIELDS = {
    "fit_score", "final_status", "manual_override", "store_id", "risk_status", "memo"
}
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
RUNTIME_FILENAMES = {"SDK_SESSION_POOL_STATE.json"}
RUNTIME_PREFIXES = ("SDK_CRAWLER_STATISTICS_",)


@dataclass(frozen=True)
class SparkHandoffCapability:
    status: str = CAPABILITY_STATUS
    dataset_folder_load_verified: bool = True
    shopify_upload_verified: bool = False
    message: str = (
        "Spark datasets/<job_id> folder import was verified manually with a five-product "
        "subset. Shopify upload remains unverified."
    )


@dataclass(frozen=True)
class SparkHandoffResult:
    job_id: str
    folder: Path
    product_count: int
    asin_count: int
    statuses: tuple[str, ...]
    manifest_path: Path
    validation_report_path: Path
    validation_status: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("folder", "manifest_path", "validation_report_path"):
            result[key] = str(result[key])
        result["statuses"] = list(result["statuses"])
        result["warnings"] = list(result["warnings"])
        return result


class HandoffValidationError(RuntimeError):
    def __init__(self, message: str, result: SparkHandoffResult | None = None):
        super().__init__(message)
        self.result = result


def validate_output_id(value: str, label: str = "job_id") -> str:
    if (
        not JOB_ID_PATTERN.fullmatch(value)
        or ".." in value
        or value.upper() in WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"{label} must contain only letters, digits, underscore, or hyphen")
    return value


def _safe_job_id(store_id: str, requested: str | None) -> str:
    if requested is not None:
        return validate_output_id(requested)
    safe_store = re.sub(r"[^A-Za-z0-9_-]", "_", store_id).strip("_-") or "store"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"SS_{safe_store}_{stamp}_{secrets.token_hex(2)}"


def _parse_payload(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return (
        "cookie" in normalized
        or "password" in normalized
        or "secret" in normalized
        or ("access" in normalized and "token" in normalized)
        or ("session" in normalized and "token" in normalized)
        or ("waf" in normalized and "token" in normalized)
    )


def _sensitive_paths(value, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_sensitive_key(key):
                found.append(path)
            found.extend(_sensitive_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_sensitive_paths(child, f"{prefix}[{index}]"))
    return found


def _chunks(values: list[int], size: int = 500) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class SparkHandoffConnector(ExportConnector):
    """Generate the manually verified Spark datasets/<job_id> handoff structure."""

    capability = SparkHandoffCapability()

    def export(
        self,
        *,
        store_id: str,
        statuses: list[str] | None = None,
        out_root: str | Path | None = None,
        limit: int | None = None,
        asins: list[str] | None = None,
        job_id: str | None = None,
        db: str | Path | None = None,
        allow_restricted: bool = False,
        history_target: str = "SPARK_DESKTOP",
        history_store_name: str | None = None,
        history_package_status: str = "CREATED",
    ) -> SparkHandoffResult:
        init_db(db)
        profile = get_store(store_id, db)
        selected_statuses = tuple(dict.fromkeys(s.upper() for s in (statuses or ["PRIMARY"])))
        unknown = set(selected_statuses) - ALLOWED_STATUSES
        if unknown:
            raise ValueError(f"Unsupported status: {sorted(unknown)}")
        if "RESTRICTED" in selected_statuses and not allow_restricted:
            raise ValueError("RESTRICTED export requires allow_restricted=True")
        if limit is not None and limit < 1:
            raise ValueError("limit must be greater than zero")

        requested_asins = tuple(dict.fromkeys(a.strip().upper() for a in (asins or []) if a.strip()))
        job_id_value = _safe_job_id(store_id, job_id)
        jobs_root = (Path(out_root) if out_root else EXPORT_DIR / "spark_handoff" / "jobs").resolve()
        metadata_root = jobs_root.parent
        job_folder = jobs_root / job_id_value
        manifest_path = metadata_root / "manifests" / f"{job_id_value}.manifest.json"
        report_path = metadata_root / "reports" / f"{job_id_value}.validation.json"
        for target in (job_folder, manifest_path, report_path):
            if target.exists():
                raise FileExistsError(f"Spark handoff output already exists: {target}")
        with connect(db) as con:
            recorded = con.execute(
                "SELECT 1 FROM export_runs WHERE job_id=? OR package_id=?",
                (job_id_value, job_id_value),
            ).fetchone()
        if recorded:
            raise FileExistsError(f"Spark handoff id already exists in export history: {job_id_value}")

        rows = self._select_products(
            store_id, selected_statuses, requested_asins,
            None if requested_asins else limit, db,
        )
        if requested_asins:
            selected = {row["asin"] for row in rows}
            missing = sorted(set(requested_asins) - selected)
            if missing:
                raise ValueError(
                    "Requested ASINs lack a matching Store Decision/status: " + ", ".join(missing)
                )
            if limit is not None:
                rows = rows[:limit]
        if not rows:
            raise ValueError("No products matched the requested Store Decision filters")

        payloads, source_job_ids, preflight_errors, keepa_mapping, browser_mapping = self._payloads(rows, db, store_id)
        jobs_root.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        job_folder.mkdir()

        if not preflight_errors:
            for index, payload in enumerate(payloads, 1):
                (job_folder / f"{index:09}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
                )

        exported_asins = [str(payload["asin"]) for payload in payloads] if not preflight_errors else []
        manifest = build_manifest(
            job_id=job_id_value,
            store_id=store_id,
            store_name=profile["store_name"],
            asins=exported_asins,
            source_job_ids=sorted(source_job_ids),
            filter_profile={"statuses": list(selected_statuses), "limit": limit, "asins": list(requested_asins)},
            export_status="VALIDATION_PENDING",
            selected_statuses=list(selected_statuses),
            output_folder=str(job_folder),
            capability_status=(BROWSER_CAPTURE_TO_SPARK_CAPABILITY if browser_mapping else
                               KEEPA_TO_SPARK_CAPABILITY if keepa_mapping else CAPABILITY_STATUS),
            shopify_upload_verified=False,
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["source_kinds"] = {kind: sum(1 for row in rows if row["source_kind"] == kind)
                                    for kind in sorted({row["source_kind"] for row in rows})}
        if browser_mapping:
            manifest["browser_capture_mapping_verified"] = False
            manifest["observed_spark_schema_compatible"] = not any(
                error.startswith("Observed Spark schema incompatibility:") for error in preflight_errors
            )
            manifest["spark_desktop_roundtrip_verified"] = False
            manifest["shopify_upload_verified"] = False
            manifest["portal_package_verified"] = False
        report = self._validate(job_folder, manifest, preflight_errors)
        if keepa_mapping:
            report["warnings"].append(
                "Keepa canonical to Spark payload mapping has not completed a portal round-trip test"
            )
        if browser_mapping:
            report["warnings"].append(
                "Browser capture to Spark payload mapping has not completed a portal round-trip test"
            )
            manifest["observed_spark_schema_compatible"] = report["observed_spark_schema_compatible"]
            manifest["spark_desktop_roundtrip_verified"] = False
            manifest["browser_capture_mapping_verified"] = False
        manifest["export_status"] = report["status"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        result = SparkHandoffResult(
            job_id=job_id_value,
            folder=job_folder,
            product_count=manifest["product_count"],
            asin_count=manifest["asin_count"],
            statuses=selected_statuses,
            manifest_path=manifest_path,
            validation_report_path=report_path,
            validation_status=report["status"],
            warnings=tuple(report["warnings"]),
        )
        self._record_export(
            store_id, history_store_name or profile["store_name"], history_target,
            history_package_status, limit, result, manifest["asin_hash"], db,
        )
        if report["status"] != "PASS":
            raise HandoffValidationError("Spark handoff validation failed", result)
        return result

    @staticmethod
    def _select_products(store_id, statuses, asins, limit, db):
        params: list[object] = [store_id, *statuses]
        clauses = ["d.store_id=?", f"d.final_status IN ({','.join('?' for _ in statuses)})"]
        if asins:
            clauses.append(f"p.asin IN ({','.join('?' for _ in asins)})")
            params.extend(asins)
        sql = f"""
            SELECT p.id,p.asin,p.raw_json,p.source_kind
            FROM store_product_decisions d JOIN products p ON p.id=d.product_id
            WHERE {' AND '.join(clauses)} ORDER BY p.asin ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with connect(db) as con:
            return con.execute(sql, params).fetchall()

    @staticmethod
    def _payloads(rows, db, store_id: str = ""):
        product_ids = [row["id"] for row in rows]
        occurrences: dict[int, list] = {product_id: [] for product_id in product_ids}
        with connect(db) as con:
            for chunk in _chunks(product_ids):
                sql = f"""
                    SELECT product_id,job_id,raw_json FROM product_occurrences
                    WHERE product_id IN ({','.join('?' for _ in chunk)}) AND raw_json IS NOT NULL
                    ORDER BY product_id, collected_at DESC, id DESC
                """
                for occurrence in con.execute(sql, chunk):
                    occurrences[occurrence["product_id"]].append(occurrence)
            search_metadata: dict[str, dict] = {}
            asins = [row["asin"] for row in rows if row["source_kind"] == "BROWSER_CAPTURE"]
            for chunk in _chunks(asins):
                if not chunk:
                    continue
                sql = f"""SELECT c.asin,c.search_payload_json FROM browser_capture_candidates c
                    JOIN browser_capture_runs r ON r.run_id=c.run_id
                    WHERE r.store_id=? AND c.asin IN ({','.join('?' for _ in chunk)})
                    ORDER BY c.updated_at DESC,c.id DESC"""
                for candidate in con.execute(sql, [store_id, *chunk]):
                    if candidate["asin"] not in search_metadata:
                        parsed = _parse_payload(candidate["search_payload_json"])
                        if parsed is not None:
                            search_metadata[candidate["asin"]] = parsed

        payloads: list[dict] = []
        source_job_ids: set[str] = set()
        errors: list[str] = []
        keepa_mapping = False
        browser_mapping = False
        for row in rows:
            payload = None
            source_job_id = None
            if row["source_kind"] == "KEEPA":
                canonical = _parse_payload(row["raw_json"])
                if canonical is not None:
                    payload = to_spark_product_payload(canonical)
                    keepa_mapping = True
            elif row["source_kind"] == "BROWSER_CAPTURE":
                canonical = _parse_payload(row["raw_json"])
                if canonical is not None:
                    browser_mapping = True
                    metadata = search_metadata.get(row["asin"], {})
                    if not metadata:
                        for occurrence in occurrences[row["id"]]:
                            candidate = _parse_payload(occurrence["raw_json"])
                            if candidate and isinstance(candidate.get("_listPage"), int):
                                metadata = candidate
                                break
                    try:
                        payload = browser_capture_to_spark_payload(
                            {**canonical, "_searchMetadata": metadata}
                        )
                    except ValueError as exc:
                        errors.append(f"{row['asin']}: {exc}")
                for occurrence in occurrences[row["id"]]:
                    if payload is not None:
                        source_job_id = occurrence["job_id"]
                        break
            else:
                for occurrence in occurrences[row["id"]]:
                    candidate = _parse_payload(occurrence["raw_json"])
                    if candidate is not None:
                        payload = candidate
                        source_job_id = occurrence["job_id"]
                        break
            if payload is None:
                payload = _parse_payload(row["raw_json"])
            if payload is None:
                errors.append(f"{row['asin']}: no valid JSON object payload")
                continue
            missing = [
                field for field in ("asin", "title")
                if not str(payload.get(field) or "").strip()
            ]
            if missing:
                errors.append(f"{row['asin']}: missing required fields {', '.join(missing)}")
            if str(payload.get("asin") or "").strip().upper() != row["asin"]:
                errors.append(f"{row['asin']}: payload ASIN does not match MASTER identity")
            internal = sorted(INTERNAL_FIELDS.intersection(payload))
            if internal:
                errors.append(f"{row['asin']}: ShopSource internal fields present: {', '.join(internal)}")
            sensitive = _sensitive_paths(payload)
            if sensitive:
                errors.append(f"{row['asin']}: sensitive fields present: {', '.join(sensitive[:5])}")
            if row["source_kind"] == "BROWSER_CAPTURE":
                for issue in observed_spark_schema_issues(payload):
                    errors.append(f"Observed Spark schema incompatibility: {row['asin']} {issue}")
            payloads.append(payload)
            if source_job_id:
                source_job_ids.add(source_job_id)
        return payloads, source_job_ids, errors, keepa_mapping, browser_mapping

    @staticmethod
    def _validate(job_folder: Path, manifest: dict, preflight_errors: list[str]) -> dict:
        errors = list(preflight_errors)
        warnings: list[str] = []
        browser_mapping = bool(manifest.get("source_kinds", {}).get("BROWSER_CAPTURE", 0))
        files = sorted(job_folder.glob("*.json"))
        expected_names = [f"{index:09}.json" for index in range(1, len(files) + 1)]
        children = list(job_folder.iterdir())
        names = [path.name for path in children]
        runtime_created = any(
            name in RUNTIME_FILENAMES or name.startswith(RUNTIME_PREFIXES) for name in names
        )
        if [file.name for file in files] != expected_names:
            errors.append("JSON filenames are not a contiguous 9-digit sequence")
        if any(path.is_dir() for path in children):
            errors.append("Spark job folder contains subdirectories")
        if runtime_created:
            errors.append("Spark runtime/session files were created")

        asins: list[str] = []
        for file in files:
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{file.name}: invalid JSON: {exc}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{file.name}: payload is not a JSON object")
                continue
            for field in ("asin", "title"):
                if not str(payload.get(field) or "").strip():
                    errors.append(f"{file.name}: missing {field}")
            internal = sorted(INTERNAL_FIELDS.intersection(payload))
            if internal:
                errors.append(f"{file.name}: internal fields present: {', '.join(internal)}")
            if payload.get("asin"):
                asins.append(str(payload["asin"]).strip().upper())
            if browser_mapping:
                for issue in observed_spark_schema_issues(payload):
                    schema_error = f"Observed Spark schema incompatibility: {file.name} {issue}"
                    if schema_error not in errors:
                        errors.append(schema_error)
                if payload.get("quantity") is None:
                    warnings.append(f"{file.name}: quantity is null in captured source; nullability was not observed in Spark samples")
                for image_index, image in enumerate(payload.get("images", [])):
                    main = image.get("main") if isinstance(image, dict) else None
                    if isinstance(main, dict) and any(not sizes for sizes in main.values()):
                        warnings.append(f"{file.name}: image dimensions were not captured; no dimensions were guessed")
        if len(asins) != len(set(asins)):
            errors.append("Duplicate ASIN detected")
        if manifest["product_count"] != len(files):
            errors.append("Manifest product_count does not match JSON file count")
        if manifest["asin_count"] != len(set(asins)):
            errors.append("Manifest asin_count does not match unique ASIN count")
        schema_compatible = browser_mapping and not any(
            error.startswith("Observed Spark schema incompatibility:") for error in errors
        )
        return {
            "schema_version": "1",
            "job_id": manifest["job_id"],
            "status": "FAIL" if errors else "PASS",
            "checks": {
                "job_folder_exists": job_folder.is_dir(),
                "json_file_count": len(files),
                "sequential_filenames": [file.name for file in files] == expected_names,
                "duplicate_asin": len(asins) != len(set(asins)),
                "manifest_outside_job_folder": True,
                "request_queues_created": (job_folder / "request_queues").exists(),
                "key_value_stores_created": (job_folder / "key_value_stores").exists(),
                "runtime_files_created": runtime_created,
                "observed_spark_schema_compatible": schema_compatible if browser_mapping else None,
            },
            "browser_capture_mapping_verified": False if browser_mapping else None,
            "observed_spark_schema_compatible": schema_compatible if browser_mapping else None,
            "spark_desktop_roundtrip_verified": False,
            "shopify_upload_verified": False,
            "portal_package_verified": False,
            "errors": errors,
            "warnings": warnings,
            "validated_at": utc_now(),
        }

    @staticmethod
    def _record_export(
        store_id: str,
        store_name: str,
        target: str,
        package_status: str,
        requested_limit: int | None,
        result: SparkHandoffResult,
        asin_hash: str,
        db,
    ) -> None:
        recorded_status = package_status if result.validation_status == "PASS" else "FAILED"
        with connect(db) as con:
            con.execute(
                """
                INSERT INTO export_runs(
                  job_id,package_id,target,store_id,store_name,statuses_json,output_path,
                  requested_limit,product_count,asin_hash,created_at,validation_status,
                  package_status,portal_package_verified
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    result.job_id, result.job_id, target, store_id, store_name,
                    json.dumps(result.statuses), str(result.folder), requested_limit,
                    result.product_count, asin_hash, utc_now(), result.validation_status,
                    recorded_status,
                ),
            )
