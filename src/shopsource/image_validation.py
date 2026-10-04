"""Local image checks that explain recommendations without guessing content."""
from __future__ import annotations

from pathlib import Path


def inspect_image(path: str | Path, *, asset_type: str = "HERO_BANNER") -> dict:
    target = Path(path)
    if not target.is_file():
        return {"valid": False, "status": "INVALID", "reason": "missing", "message_ko": "파일을 찾을 수 없습니다."}
    if target.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return {"valid": False, "status": "INVALID", "reason": "extension", "message_ko": "PNG, JPEG, WebP 중 하나를 선택하세요."}
    size = target.stat().st_size
    if size <= 0 or size > 20_000_000:
        return {"valid": False, "status": "INVALID", "reason": "file_size", "message_ko": "파일이 비어 있거나 20MB를 넘습니다."}
    try:
        from PIL import Image
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height, image_format, mode = image.width, image.height, image.format, image.mode
    except ModuleNotFoundError:
        return {"valid": False, "status": "DEPENDENCY_MISSING", "reason": "pillow_missing",
                "message_ko": "이미지 기능에 필요한 Pillow가 현재 실행 환경에 없습니다."}
    except Exception:
        return {"valid": False, "status": "INVALID", "reason": "invalid_image", "message_ko": "이미지 파일을 열 수 없습니다."}
    warnings = []
    if asset_type == "HERO_BANNER":
        minimum, expected = (1200, 600), 16 / 9
    elif asset_type in {"COLLECTION_IMAGE", "CATEGORY_SHORTCUT"}:
        minimum, expected = (800, 800), 1.0
    else:
        minimum, expected = (800, 600), 4 / 3
    ratio = width / height if height else 0
    if width < minimum[0] or height < minimum[1]: warnings.append("권장보다 작은 이미지입니다.")
    if abs(ratio - expected) > (0.25 if expected == 1 else 0.12): warnings.append("권장 비율과 다릅니다. 미리보기에서 자르기를 확인하세요.")
    transparent = mode in {"RGBA", "LA", "PA"}
    if transparent: warnings.append("투명 배경이 포함되어 있습니다. 테마 배경에서 확인하세요.")
    if asset_type == "HERO_BANNER": warnings.append("이미지 안의 글자 포함 여부는 자동 판정하지 않았습니다. mobile crop과 접근성을 직접 확인하세요.")
    return {"valid": image_format in {"PNG", "JPEG", "WEBP"}, "status": "WARNINGS" if warnings else "FIT",
            "reason": None if not warnings else "review_recommendations", "width": width, "height": height,
            "format": image_format, "size_bytes": size, "aspect_ratio": round(ratio, 4),
            "transparent": transparent, "text_in_image_estimate": "NOT_CHECKED",
            "warnings": warnings, "message_ko": "적합" if not warnings else " · ".join(warnings)}
