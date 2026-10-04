from PIL import Image

from shopsource.image_validation import inspect_image


def test_image_validation_warns_small_and_wrong_aspect(tmp_path):
    path = tmp_path / "small.png"; Image.new("RGB", (300, 200), "white").save(path)
    result = inspect_image(path, asset_type="HERO_BANNER")
    assert result["valid"] and result["status"] == "WARNINGS"
    assert any("작은" in warning for warning in result["warnings"])
    assert any("비율" in warning for warning in result["warnings"])


def test_image_validation_detects_transparency(tmp_path):
    path = tmp_path / "transparent.png"; Image.new("RGBA", (1200, 1200), (255, 255, 255, 0)).save(path)
    result = inspect_image(path, asset_type="COLLECTION_IMAGE")
    assert result["transparent"] and result["status"] == "WARNINGS"


def test_image_validation_friendly_when_pillow_missing(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "PIL": raise ModuleNotFoundError("No module named 'PIL'")
        return original(name, *args, **kwargs)
    path = tmp_path / "valid.png"; Image.new("RGB", (1200, 700), "white").save(path)
    monkeypatch.setattr(builtins, "__import__", blocked)
    result = inspect_image(path)
    assert result["status"] == "DEPENDENCY_MISSING" and "Pillow" in result["message_ko"]
