"""Subprocess adapter for the optional YouTubeSum headless image bridge."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .security import redact_text
from .paths import EXPORT_DIR


@dataclass(frozen=True)
class LocalImageStudioConfig:
    repo_path: Path = Path(r"D:\03_youtubesum")
    python: str = sys.executable
    bridge_script: str = "shopsource_bridge.py"
    output_root: Path | None = None
    timeout_seconds: int = 900
    candidate_count: int = 3

    @classmethod
    def from_env(cls) -> "LocalImageStudioConfig":
        repo = Path(os.environ.get("SHOP_SOURCE_YOUTUBESUM_PATH", r"D:\03_youtubesum"))
        preferred = repo / ".venv" / "Scripts" / "python.exe"
        py = os.environ.get("SHOP_SOURCE_YOUTUBESUM_PYTHON", str(preferred if preferred.is_file() else sys.executable))
        script = os.environ.get("SHOP_SOURCE_YOUTUBESUM_BRIDGE", "shopsource_bridge.py")
        out = os.environ.get("SHOP_SOURCE_IMAGE_OUTPUT")
        return cls(repo.resolve(), py, script, Path(out).resolve() if out else None,
                   max(5, int(os.environ.get("SHOP_SOURCE_IMAGE_TIMEOUT", "900"))),
                   max(1, min(8, int(os.environ.get("SHOP_SOURCE_IMAGE_CANDIDATES", "3")))))


class LocalImageStudioProvider:
    provider_name = "LOCAL_IMAGE_STUDIO"

    def __init__(self, config: LocalImageStudioConfig | None = None, *, runner=subprocess.run,
                 export_root: str | Path | None = None):
        self.config = config or LocalImageStudioConfig.from_env()
        self.runner = runner
        self.export_root = Path(export_root) if export_root else EXPORT_DIR / "image_studio_jobs"

    @property
    def bridge_path(self) -> Path:
        value = Path(self.config.bridge_script)
        return (self.config.repo_path / value).resolve() if not value.is_absolute() else value.resolve()

    def _run(self, *args: str, timeout: int = 20) -> dict:
        script = self.bridge_path
        if not script.is_file() or not self.config.repo_path.is_dir():
            return {"status": "PROMPT_ONLY_FALLBACK", "available": False,
                    "message": f"로컬 이미지 브리지 파일을 찾을 수 없습니다: {script}"}
        try:
            result = self.runner([self.config.python, str(script), *args], cwd=str(self.config.repo_path),
                                 capture_output=True, text=True, timeout=timeout, check=False, shell=False)
        except subprocess.TimeoutExpired:
            return {"status": "PROMPT_ONLY_FALLBACK", "available": False, "message": "로컬 이미지 브리지 응답 시간이 초과되었습니다.",
                    "error": {"code": "TIMEOUT", "message_ko": "로컬 이미지 생성 시간이 초과되어 안전하게 중단했습니다."}}
        except OSError as exc:
            return {"status": "PROMPT_ONLY_FALLBACK", "available": False,
                    "message": f"로컬 이미지 프로그램을 시작할 수 없습니다 ({type(exc).__name__})."}
        try:
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if result.returncode:
            if payload.get("status") in {"FAILED", "WAITING_FOR_CONFIGURATION"}:
                return payload
            return {"status": "PROMPT_ONLY_FALLBACK", "available": False,
                    "message": payload.get("message_ko") or "로컬 이미지 브리지가 실행되지 않았습니다.",
                    "detail": redact_text(result.stderr or result.stdout, limit=1000)}
        return payload

    def health(self) -> dict:
        return self._run("--health")

    def capabilities(self) -> dict:
        return self._run("--capabilities")

    def doctor(self) -> dict:
        return self._run("--doctor", timeout=60)

    def generate(self, job: dict, *, timeout: int | None = None) -> dict:
        root = self.config.output_root or self.export_root
        root = root.resolve(); root.mkdir(parents=True, exist_ok=True)
        safe_store = re.sub(r"[^A-Za-z0-9_-]+", "_", str(job["store_id"])).strip("._") or "store"
        safe_job = re.sub(r"[^A-Za-z0-9_-]+", "_", str(job["job_id"])).strip("._") or "job"
        job_dir = (root / safe_store / safe_job).resolve()
        job_dir.relative_to(root)
        job_dir.mkdir(parents=True, exist_ok=True)
        output_dir = (job_dir / "candidates").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {k: job[k] for k in ("job_id", "store_id", "store_name", "asset_type", "prompt", "negative_prompt",
                                        "text_policy", "target", "safe_zone", "brand", "collection", "reference_images",
                                        "output_count", "request_context") if k in job}
        manifest.update({"schema_version": "1.0", "output_dir": str(output_dir)})
        job_path, result_path = job_dir / "job.json", job_dir / "result.json"
        temp = job_path.with_suffix(".json.tmp")
        def scrub(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key in {"sha256", "prompt_hash"} and isinstance(item, str) and re.fullmatch(r"[a-fA-F0-9]{64}", item):
                        result[key] = item
                    else:
                        result[key] = scrub(item)
                return result
            if isinstance(value, list): return [scrub(v) for v in value]
            if isinstance(value, str): return redact_text(value)
            return value
        manifest = scrub(manifest)
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, job_path)
        result = self._run("--job", str(job_path), "--result", str(result_path),
                           timeout=max(1, timeout or self.config.timeout_seconds))
        if result.get("status") == "PROMPT_ONLY_FALLBACK":
            return result
        # The bridge result is the stable protocol, not arbitrary subprocess output.
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "FAILED", "error": {"code": "RESULT_INVALID", "message_ko": "이미지 결과 파일을 읽지 못했습니다."}}
        def scrub(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key in {"sha256", "prompt_hash"} and isinstance(item, str) and re.fullmatch(r"[a-fA-F0-9]{64}", item):
                        result[key] = item
                    else:
                        result[key] = scrub(item)
                return result
            if isinstance(value, list): return [scrub(v) for v in value]
            if isinstance(value, str): return redact_text(value)
            return value
        payload = scrub(payload)
        if payload.get("schema_version") != "1.0" or payload.get("job_id") != manifest["job_id"]:
            return {"status": "FAILED", "error": {"code": "RESULT_INVALID", "message_ko": "이미지 결과 정보가 요청과 일치하지 않습니다."}}
        safe_root = output_dir.resolve()
        accepted = []
        for candidate in payload.get("candidates", []):
            try:
                path = Path(candidate["path"]).resolve(strict=True)
                path.relative_to(safe_root)
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    actual_width, actual_height = image.width, image.height
                    actual_format = image.format
                if candidate.get("width") != actual_width or candidate.get("height") != actual_height:
                    continue
                ext = path.suffix.casefold()
                if (ext == ".png" and actual_format != "PNG") or (ext in {".jpg", ".jpeg"} and actual_format != "JPEG") or (ext == ".webp" and actual_format != "WEBP"):
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != candidate.get("sha256"):
                    continue
                candidate["path"], candidate["sha256"] = str(path), digest
                accepted.append(candidate)
            except (KeyError, OSError, ValueError):
                continue
        payload["candidates"] = accepted
        if payload.get("status") in {"SUCCEEDED", "PARTIAL"} and not accepted:
            payload["status"] = "FAILED"
            payload["error"] = {"code": "NO_VALID_CANDIDATES", "message_ko": "검사를 통과한 이미지 후보가 없습니다."}
        payload["job_path"] = str(job_path)
        payload["result_path"] = str(result_path)
        return payload

    def create_job(self, *, store_id: str, store_name: str, asset: dict, context: dict,
                   output_count: int | None = None, reference_images: list[str] | None = None) -> dict:
        kind = asset["asset_type"]
        target = {"HERO_BANNER": {"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
                  "COLLECTION_IMAGE": {"width": 1200, "height": 1200, "aspect_ratio": "1:1"},
                  "CATEGORY_SHORTCUT": {"width": 1200, "height": 1200, "aspect_ratio": "1:1"}}.get(kind, {"width": 1200, "height": 900, "aspect_ratio": "4:3"})
        return {"job_id": "IMG_" + hashlib.sha256(f"{store_id}:{asset.get('title')}:{time.time_ns()}".encode()).hexdigest()[:16],
                "store_id": store_id, "store_name": store_name, "asset_type": kind,
                "prompt": asset["prompt_main"], "negative_prompt": asset["negative_prompt"],
                "text_policy": "NO_EMBEDDED_TEXT", "target": target,
                "safe_zone": {"preferred_text_side": "right", "text_safe_percent": 35, "mobile_center_safe": True},
                "brand": {k: context.get(k) for k in ("store_name", "brand_style", "market", "audience", "main_category", "colors", "visual_direction", "avoid")},
                "collection": {"collection_key": asset.get("collection_key"), "collection_title": asset.get("collection_title")},
                "reference_images": list(reference_images or []), "output_count": max(1, min(8, output_count or self.config.candidate_count)),
                "request_context": {"recommended_use": asset.get("recommended_use"),
                                    "asset": {k: asset.get(k) for k in ("title", "asset_type", "collection_key", "alt_text_suggestion", "plan_id")}}}


    @staticmethod
    def import_candidate(candidate: dict, destination: str | Path) -> dict:
        """Copy a validated candidate to a managed location and recheck its digest."""
        source = Path(candidate["path"]).resolve(strict=True)
        expected = str(candidate.get("sha256") or "")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if not expected or digest != expected:
            raise ValueError("Image candidate hash changed after validation")
        target_root = Path(destination).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        target = (target_root / (digest[:16] + source.suffix.lower())).resolve()
        target.relative_to(target_root)
        if not target.exists():
            shutil.copyfile(source, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            target.unlink(missing_ok=True)
            raise ValueError("Managed image copy failed integrity verification")
        return {**candidate, "path": str(target), "sha256": digest, "approval_status": "NEEDS_REVIEW"}


def image_generation_workflow(job: dict, asset: dict) -> list[dict[str, Any]]:
    """Persistent queue tasks: generation and validation run, approval waits for a person."""
    return [
        {"task_key": "LOCAL_IMAGE_GENERATE", "title": "로컬 이미지 후보 생성", "stage": "이미지 준비",
         "max_attempts": 3, "checkpoint": {"job": job, "asset": asset}},
        {"task_key": "LOCAL_IMAGE_VALIDATE", "title": "이미지 파일 검사", "stage": "파일 검사",
         "checkpoint": {"job_id": job["job_id"], "asset_type": asset.get("asset_type", "HERO_BANNER")}},
        {"task_key": "LOCAL_IMAGE_APPROVAL", "title": "추천 이미지 승인", "stage": "사람 확인",
         "requires_confirmation": True,
         "confirmation_prompt": "추천 이미지를 미리 확인하고 승인하면 ShopSource 자산으로 등록합니다.",
         "checkpoint": {"job": job, "asset": asset}},
    ]

    @staticmethod
    def import_candidate(candidate: dict, destination: str | Path) -> dict:
        """Copy a validated candidate to a managed location and recheck its digest."""
        source = Path(candidate["path"]).resolve(strict=True)
        expected = str(candidate.get("sha256") or "")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if not expected or digest != expected:
            raise ValueError("Image candidate hash changed after validation")
        target_root = Path(destination).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        target = (target_root / (digest[:16] + source.suffix.lower())).resolve()
        target.relative_to(target_root)
        if not target.exists():
            shutil.copyfile(source, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            target.unlink(missing_ok=True)
            raise ValueError("Managed image copy failed integrity verification")
        return {**candidate, "path": str(target), "sha256": digest, "approval_status": "NEEDS_REVIEW"}
