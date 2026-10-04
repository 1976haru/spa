import hashlib
import json
import subprocess
from pathlib import Path

from PIL import Image
from shopsource.local_image_studio import LocalImageStudioConfig, LocalImageStudioProvider
from shopsource.runtime_doctor import PIL_MISSING_KO, dependency_doctor, repair_runtime_dependencies
from shopsource.local_image_studio import image_generation_workflow
from shopsource.automation import WorkflowAutomationService


def test_local_studio_missing_bridge_falls_back_to_prompt_only(tmp_path):
    provider = LocalImageStudioProvider(LocalImageStudioConfig(repo_path=tmp_path, python="python"))
    assert provider.health()["status"] == "PROMPT_ONLY_FALLBACK"


def test_local_studio_timeout_falls_back_safely(tmp_path):
    (tmp_path / "shopsource_bridge.py").write_text("", encoding="utf-8")
    def runner(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], 1)
    provider = LocalImageStudioProvider(LocalImageStudioConfig(repo_path=tmp_path), runner=runner)
    assert "시간이 초과" in provider.health()["message"]


def test_bridge_result_rejects_output_path_escape(tmp_path):
    repo = tmp_path / "studio"; repo.mkdir(); (repo / "shopsource_bridge.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.png"; outside.write_bytes(b"not image")
    class Result:
        returncode = 0; stdout = "{}"; stderr = ""
    def runner(command, **kwargs):
        job = json.loads(Path(command[command.index("--job") + 1]).read_text(encoding="utf-8"))
        out = Path(command[command.index("--result") + 1])
        out.write_text(json.dumps({"schema_version":"1.0", "job_id":job["job_id"], "status":"SUCCEEDED", "candidates":[{"candidate_id":"x", "path":str(outside), "sha256":hashlib.sha256(outside.read_bytes()).hexdigest()}]}), encoding="utf-8")
        return Result()
    provider = LocalImageStudioProvider(LocalImageStudioConfig(repo_path=repo), runner=runner, export_root=tmp_path / "exports")
    result = provider.generate({"job_id":"job1", "store_id":"001", "asset_type":"HERO_BANNER", "prompt":"safe", "output_count":1})
    assert result["status"] == "FAILED" and result["candidates"] == []


def test_local_studio_accepts_only_schema_and_matching_hash(tmp_path):
    repo = tmp_path / "studio"; repo.mkdir(); (repo / "shopsource_bridge.py").write_text("", encoding="utf-8")
    class Result:
        returncode = 0; stdout = "{}"; stderr = ""
    def runner(command, **kwargs):
        job = json.loads(Path(command[command.index("--job") + 1]).read_text(encoding="utf-8"))
        result_path = Path(command[command.index("--result") + 1]); output = Path(job["output_dir"]) / "a.png"
        Image.new("RGB", (640, 360), "white").save(output)
        result_path.write_text(json.dumps({"schema_version":"1.0", "job_id":job["job_id"], "status":"SUCCEEDED", "candidates":[{"candidate_id":"a", "path":str(output), "width":640, "height":360, "sha256":hashlib.sha256(output.read_bytes()).hexdigest()}]}), encoding="utf-8")
        return Result()
    provider = LocalImageStudioProvider(LocalImageStudioConfig(repo_path=repo), runner=runner, export_root=tmp_path / "exports")
    result = provider.generate({"job_id":"job1", "store_id":"001", "asset_type":"HERO_BANNER", "prompt":"safe", "output_count":1})
    assert result["status"] == "SUCCEEDED" and len(result["candidates"]) == 1


def test_runtime_doctor_uses_running_interpreter_and_friendly_pillow_message(tmp_path):
    def find(name): return object() if name == "nicegui" else None
    doctor = dependency_doctor(project_path=tmp_path, executable="same-python", find_spec=find)
    assert doctor["python"] == "same-python" and doctor["pillow_message"] == PIL_MISSING_KO
    assert doctor["repair_command"] == ["same-python", "-m", "pip", "install", "-e", ".[ui]"]


def test_runtime_repair_is_explicit_same_interpreter_and_does_not_install_in_test(tmp_path):
    calls = []
    class Result: returncode=0; stderr=""
    result = repair_runtime_dependencies(project_path=tmp_path, python="selected-python", runner=lambda command, **kw: (calls.append((command, kw)) or Result()))
    assert result["status"] == "RESTART_REQUIRED"
    assert calls[0][0][0] == "selected-python" and calls[0][0][-1] == ".[ui]"


def test_shop_source_assets_map_to_documented_bridge_asset_types(tmp_path):
    provider = LocalImageStudioProvider(LocalImageStudioConfig(repo_path=tmp_path))
    for source_type, bridge_type in (("COLLECTION_IMAGE", "COLLECTION_SQUARE"),
                                    ("CATEGORY_SHORTCUT", "COLLECTION_CARD")):
        job = provider.create_job(store_id="001", store_name="Cabin Tidy",
            asset={"asset_type": source_type, "title": source_type, "prompt_main": "safe", "negative_prompt": "text"},
            context={})
        assert job["asset_type"] == bridge_type


def test_local_image_workflow_persists_candidates_until_explicit_approval(tmp_path):
    calls = []
    def generate(task):
        calls.append("generate")
        return {"candidates": [{"candidate_id": "candidate-1", "path": "safe.png"}]}
    def validate(task):
        calls.append("validate")
        return {"candidate_count": 1}
    def approve(task):
        calls.append(task["checkpoint"]["user_input"]["candidate_id"])
        return {"approved": True}
    job = {"job_id": "job-1", "store_id": "001"}
    asset = {"asset_type": "HERO_BANNER", "plan_id": "plan-1"}
    queue = WorkflowAutomationService(tmp_path / "queue.sqlite3", handlers={
        "LOCAL_IMAGE_GENERATE": generate, "LOCAL_IMAGE_VALIDATE": validate, "LOCAL_IMAGE_APPROVAL": approve})
    run_id = queue.create_run("001", "IMAGE", image_generation_workflow(job, asset))["run_id"]
    waiting = queue.run(run_id)
    assert waiting["status"] == "WAITING_FOR_CONFIRMATION"
    assert calls == ["generate", "validate"]
    restarted = WorkflowAutomationService(tmp_path / "queue.sqlite3", handlers={
        "LOCAL_IMAGE_GENERATE": generate, "LOCAL_IMAGE_VALIDATE": validate, "LOCAL_IMAGE_APPROVAL": approve})
    done = restarted.confirm(run_id, "LOCAL_IMAGE_APPROVAL", user_input={"candidate_id": "candidate-1"})
    assert done["status"] == "SUCCEEDED" and calls == ["generate", "validate", "candidate-1"]
