from pathlib import Path

import pytest

from shopsource.db import connect, init_db, upsert_store
from shopsource.ui.v2 import (
    PRODUCT_SOURCE_OPTIONS,
    product_grid_options,
    product_row_from_event_args,
    selected_product_asins,
    update_product_grid,
)
from shopsource.ui.v2_service import product_page


ROOT = Path(__file__).parents[1]
UI_SOURCE = (ROOT / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")


@pytest.fixture
def browser_capture_db(tmp_path):
    db = tmp_path / "browser_capture_products.sqlite3"
    init_db(db)
    upsert_store({
        "store_id": "001",
        "store_name": "Cabin Tidy",
        "category": "Automotive",
        "price_bands": [],
    }, db)
    now = "2026-10-01T00:00:00Z"
    with connect(db) as con:
        for index in range(5):
            asin = f"BCAP00000{index}"
            cursor = con.execute(
                """INSERT INTO products
                (asin,title,price,source_kind,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?)""",
                (asin, f"Browser product {index}", 40 + index, "BROWSER_CAPTURE", "{}", now, now),
            )
            con.execute(
                """INSERT INTO store_product_decisions
                (store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("001", cursor.lastrowid, "RESERVE_B", "SAFE", "RESERVE_B", "RESERVE_B", now),
            )
    return db


def test_product_page_lists_browser_capture_products(browser_capture_db):
    result = product_page(store_id="001", source="ALL", status="ALL", db=browser_capture_db)
    assert result["total"] == 5
    assert len(result["rows"]) == 5


def test_product_page_browser_capture_source_filter(browser_capture_db):
    result = product_page(store_id="001", source="BROWSER_CAPTURE", status="ALL", db=browser_capture_db)
    assert result["total"] == 5
    assert product_page(store_id="001", source="KEEPA", status="ALL", db=browser_capture_db)["total"] == 0


def test_product_page_all_includes_browser_capture(browser_capture_db):
    result = product_page(store_id="001", source="ALL", status="ALL", db=browser_capture_db)
    assert {row["source_kind"] for row in result["rows"]} == {"BROWSER_CAPTURE"}


def test_product_page_includes_product_without_store_decision(browser_capture_db):
    now = "2026-10-01T00:00:00Z"
    with connect(browser_capture_db) as con:
        con.execute(
            """INSERT INTO products
            (asin,title,source_kind,raw_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?)""",
            ("NODEC00001", "No decision", "BROWSER_CAPTURE", "{}", now, now),
        )
    result = product_page(store_id="001", source="ALL", status="ALL", db=browser_capture_db)
    assert result["total"] == 6
    assert any(row["asin"] == "NODEC00001" and row["final_status"] is None for row in result["rows"])


def test_products_ui_source_filter_has_browser_capture():
    assert PRODUCT_SOURCE_OPTIONS == ["ALL", "BROWSER_CAPTURE", "SPARK_STORAGE", "AMAZON_SOURCE_FOLDER", "KEEPA"]


def test_products_ui_row_click_payload_safe():
    assert product_row_from_event_args({"data": {"id": 7, "asin": "BCAP000007"}})["id"] == 7
    assert product_row_from_event_args({"id": 8})["id"] == 8
    assert product_row_from_event_args(None) == {}
    assert '"rowSelection": "multiple"' in UI_SOURCE


def test_products_ui_empty_state_visible():
    assert "현재 필터에 해당하는 상품이 없습니다." in UI_SOURCE


def test_products_ui_error_state_visible():
    assert "상품 목록을 불러오지 못했습니다:" in UI_SOURCE
    assert "products_status.set_text" in UI_SOURCE


def test_five_browser_capture_products_select_for_package(browser_capture_db):
    rows = product_page(store_id="001", source="BROWSER_CAPTURE", status="ALL", db=browser_capture_db)["rows"]
    selected = selected_product_asins(rows)
    assert len(selected) == 5
    assert selected == [row["asin"] for row in rows]
    assert "상품 페이지에서 선택한 {len(self.package_selected_asins)}개 ASIN 사용 예정" in UI_SOURCE


def test_products_grid_uses_colon_get_row_id():
    options = product_grid_options()
    assert options[":getRowId"] == "params => params.data.asin"
    assert "getRowId" not in options


def test_products_grid_js_renderer_uses_colon_expression():
    options = product_grid_options()
    thumbnail = next(column for column in options["columnDefs"] if column["field"] == "thumbnail")
    assert ":cellRenderer" in thumbnail
    assert "cellRenderer" not in thumbnail
    assert thumbnail[":cellRenderer"].startswith("params =>")


def test_products_grid_initial_row_data_update():
    class Grid:
        def __init__(self):
            self.options = product_grid_options()
            self.updates = 0

        def update(self):
            self.updates += 1

    grid = Grid()
    rows = [{"id": index, "asin": f"BCAP{index}"} for index in range(5)]
    assert grid.options["rowData"] == []
    update_product_grid(grid, rows)
    assert grid.options["rowData"] == rows
    assert grid.updates == 1


def test_products_grid_does_not_recreate_on_every_load():
    products_method = UI_SOURCE.split("    def _products(self):", 1)[1].split("    def _show_product_detail", 1)[0]
    assert products_method.count("ui.aggrid(") == 1
    assert "grid_container.clear()" not in products_method
    assert "update_product_grid(grid_ref[\"grid\"], rows)" in products_method


def test_products_grid_five_rows_contract(browser_capture_db):
    result = product_page(store_id="001", page=0, page_size=100, status="ALL", source="ALL", db=browser_capture_db)
    options = product_grid_options()
    update_rows = [
        {**row, "thumbnail": (row["images"] or [""])[0]}
        for row in result["rows"]
    ]
    assert result["total"] == 5
    assert len(update_rows) == 5
    assert options["rowSelection"] == "multiple"
    assert all(row.get("id") and row.get("asin") for row in update_rows)


def test_products_grid_selection_five_products(browser_capture_db):
    rows = product_page(store_id="001", page=0, page_size=100, status="ALL", source="ALL", db=browser_capture_db)["rows"]
    selected = selected_product_asins(rows)
    assert len(selected) == 5


def test_existing_five_browser_capture_products_visible(browser_capture_db):
    rows = product_page(store_id="001", page=0, page_size=100, status="ALL", source="ALL", db=browser_capture_db)["rows"]
    visible = [{key: row.get(key) for key in ("asin", "title", "price", "source_kind", "final_status")} for row in rows]
    assert len(visible) == 5
    assert all(row["asin"] and row["title"] and row["price"] is not None for row in visible)
    assert all(row["source_kind"] == "BROWSER_CAPTURE" and row["final_status"] == "RESERVE_B" for row in visible)
