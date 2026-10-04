"""Runtime dependency diagnostics and explicit same-interpreter repair."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PIL_MISSING_KO = "이미지 기능에 필요한 Pillow가 현재 실행 환경에 없습니다."


def dependency_doctor(*, project_path: str | Path | None = None, executable: str | None = None,
                      find_spec=importlib.util.find_spec) -> dict:
    project = Path(project_path or Path(__file__).resolve().parents[2]).resolve()
    checks = {name: bool(find_spec(name)) for name in ("PIL", "nicegui")}
    return {"python": str(executable or sys.executable), "project_path": str(project), "checks": checks,
            "ready": all(checks.values()), "pillow_message": None if checks["PIL"] else PIL_MISSING_KO,
            "repair_command": [str(executable or sys.executable), "-m", "pip", "install", "-e", ".[ui]"]}


def repair_runtime_dependencies(*, project_path: str | Path | None = None, python: str | None = None,
                                 runner=subprocess.run, timeout: int = 600) -> dict:
    executable = str(python or sys.executable)
    project = Path(project_path or Path(__file__).resolve().parents[2]).resolve()
    command = [executable, "-m", "pip", "install", "-e", ".[ui]"]
    try:
        result = runner(command, cwd=str(project), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "message": "환경 복구 시간이 초과되었습니다. 네트워크와 Python 환경을 확인하세요."}
    except OSError as exc:
        return {"status": "FAILED", "message": f"환경 복구를 시작하지 못했습니다: {type(exc).__name__}"}
    if result.returncode:
        return {"status": "FAILED", "message": "필수 패키지 설치에 실패했습니다. 자세한 출력은 고급 정보에서 확인하세요.",
                "detail": str(result.stderr or "")[-1000:]}
    return {"status": "RESTART_REQUIRED", "message": "Pillow 설치가 완료되었습니다. ShopSource를 다시 실행하세요."}
