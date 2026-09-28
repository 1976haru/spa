import json
import sqlite3
from pathlib import Path

from shopsource.core.rules import price_bucket, risk_check


def load_profile(name="001_cabin_tidy.json"):
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "stores" / name).read_text(encoding="utf-8"))


def test_dynamic_price_bands_keep_low_prices_as_reserve():
    p = load_profile()
    assert price_bucket(29.99, p)[0] == "LOW_RESERVE"
    assert price_bucket(32.00, p)[0] == "RESERVE_C"
    assert price_bucket(37.50, p)[0] == "RESERVE_B"
    assert price_bucket(59.99, p)[0] == "PRIMARY"
    assert price_bucket(110.00, p)[0] == "RESERVE_A"
    assert price_bucket(150.00, p)[0] == "HIGH_RESERVE"


def test_risk_rules_do_not_delete_product():
    p = load_profile()
    status, reasons = risk_check("rechargeable dashboard camera lithium battery", p)
    assert status == "REVIEW"
    assert reasons
