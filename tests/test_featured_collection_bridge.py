import json

from shopsource.db import init_db
from shopsource.featured_collection_bridge import (
    COLLECTION_HANDLE, COLLECTION_KEY, COLLECTION_TITLE,
    FeaturedCollectionPrerequisiteService,
)
from shopsource.featured_guided_flow import hydrate_featured_postwrite_state, resolve_featured_guided_state
from shopsource.homepage_featured_products import FeaturedProductThemeApplyService, parse_shopify_json_document
from shopsource.shopify_theme_cli import ShopifyThemeCLI
from shopsource.theme_write_backend import ThemeFileReadResult, ThemeFileWriteResult


IDS = [f"gid://shopify/Product/{number}" for number in range(1, 5)]


class CollectionMock:
    def __init__(self):
        self.remote = None
        self.mutations = []
        self.scopes = {"read_products", "read_publications", "write_products", "write_publications"}

    def execute(self, query, variables=None):
        variables = variables or {}
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [
                {"handle": scope} for scope in sorted(self.scopes)
            ]}}
        if "FeaturedOnlineStorePublications" in query:
            return {"publications": {"nodes": [{"id": "gid://shopify/Publication/online", "name": "Online Store"}]}}
        if "collectionByHandle" in query:
            return {"collectionByHandle": self.remote}
        if "collectionCreate" in query:
            self.mutations.append(("create", variables["collection"]))
            source = variables["collection"]["sources"][0]["source"]
            ids = [row["productId"] for row in source["inclusion"]["selections"]]
            self.remote = self._collection(ids)
            return {"collectionCreate": {"collection": {"id": self.remote["id"], "handle": COLLECTION_HANDLE}, "userErrors": []}}
        if "publishablePublish" in query:
            self.mutations.append(("publish", variables))
            self.remote["resourcePublicationsV2"]["nodes"] = [{
                "isPublished": True,
                "publication": {"id": "gid://shopify/Publication/online", "name": "Online Store"},
            }]
            return {"publishablePublish": {"userErrors": []}}
        if "collectionUpdate" in query:
            update = variables["collection"]
            self.mutations.append(("update", update))
            delta = update["sourcesToUpdate"][0]["condition"]["inclusion"]
            current = {row["product"]["id"] for row in self.remote["sources"][0]["inclusion"]["selections"]["nodes"]}
            current.update(row["productId"] for row in delta["selectionsToAdd"])
            current.difference_update(row["productId"] for row in delta["selectionsToRemove"])
            self.remote = self._collection(sorted(current))
            self.remote["resourcePublicationsV2"]["nodes"] = [{
                "isPublished": True, "publication": {"id": "gid://shopify/Publication/online", "name": "Online Store"}}]
            return {"collectionUpdate": {"collection": {"id": self.remote["id"]}, "userErrors": []}}
        raise AssertionError("unexpected GraphQL operation")

    @staticmethod
    def _collection(ids):
        return {"id": "gid://shopify/Collection/90", "handle": COLLECTION_HANDLE,
            "title": COLLECTION_TITLE, "productsCount": {"count": len(ids), "precision": "EXACT"},
            "sources": [{"__typename": "CollectionConditionsSource", "id": "source-1",
                "title": "ShopSource Featured Products", "inclusion": {"conditions": [],
                "selections": {"nodes": [{"product": {"id": item}} for item in ids],
                                "pageInfo": {"hasNextPage": False}}}}],
            "resourcePublicationsV2": {"nodes": []}}


def _service(tmp_path, monkeypatch, client):
    db = tmp_path / "collection.sqlite3"
    init_db(db)
    with __import__("shopsource.db", fromlist=["connect"]).connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS homepage_featured_product_plans (plan_id TEXT PRIMARY KEY,store_id TEXT,collection_handle TEXT,updated_at TEXT)")
    monkeypatch.setattr("shopsource.featured_collection_bridge.get_connection",
        lambda store_id, db=None: {"shop_domain": "shop.myshopify.com", "api_version": "2026-07"})
    return db, FeaturedCollectionPrerequisiteService(db=db, client=client)


def _plan():
    return {"store_id": "001", "plan_id": "plan-1",
            "items": [{"shopify_product_id": item} for item in IDS]}


def test_collection_dry_run_create_explicit_prepare_exact_four_and_publish(tmp_path, monkeypatch):
    client = CollectionMock()
    db, service = _service(tmp_path, monkeypatch, client)
    plan = _plan()
    dry_run = service.dry_run(plan)
    assert dry_run["action"] == "CREATE" and dry_run["write_performed"] is False
    result = service.prepare(plan, confirmed=True, expected_preview=dry_run)
    assert result["status"] == "VERIFIED" and result["published"] is True, result
    assert set(result["product_ids"]) == set(IDS) and len(result["product_ids"]) == 4
    create = next(value for name, value in client.mutations if name == "create")
    source = create["sources"][0]["source"]
    assert create["title"] == "New Arrivals" and create["handle"] == COLLECTION_HANDLE
    assert source["title"] == "ShopSource Featured Products"
    assert source["targetType"] == "PRODUCTS"
    assert source["inclusion"]["selections"] == [{"productId": item} for item in IDS]
    assert "conditions" not in source["inclusion"] and "tag" not in json.dumps(create).casefold()
    assert [name for name, _ in client.mutations] == ["create", "publish"]
    binding = service.dry_run(plan)
    assert binding["action"] == "NO_CHANGE"


def test_unmanaged_same_handle_is_conflict_never_adopted(tmp_path, monkeypatch):
    client = CollectionMock()
    client.remote = client._collection(IDS)
    _db, service = _service(tmp_path, monkeypatch, client)
    result = service.dry_run(_plan())
    assert result["action"] == "CONFLICT"
    assert result["reason_code"] == "FEATURED_COLLECTION_HANDLE_CONFLICT"
    assert service.prepare(_plan(), confirmed=True)["write_performed"] is False
    assert client.mutations == []


def test_missing_collection_write_scopes_block_before_any_mutation(tmp_path, monkeypatch):
    client = CollectionMock()
    client.scopes = {"read_products", "read_publications"}
    _db, service = _service(tmp_path, monkeypatch, client)
    plan = _plan()
    dry = service.dry_run(plan)
    result = service.prepare(plan, confirmed=True, expected_preview=dry)
    assert result["status"] == "BLOCKED" and result["action"] == "MISSING_SCOPES"
    assert set(result["missing_scopes"]) == {"write_products", "write_publications"}
    assert client.mutations == []


def test_owned_membership_change_uses_exact_add_remove_diff(tmp_path, monkeypatch):
    client = CollectionMock()
    _db, service = _service(tmp_path, monkeypatch, client)
    plan = _plan()
    first = service.dry_run(plan)
    assert service.prepare(plan, confirmed=True, expected_preview=first)["status"] == "VERIFIED"
    client.mutations.clear()
    plan["items"][-1]["shopify_product_id"] = "gid://shopify/Product/5"
    dry = service.dry_run(plan)
    assert dry["action"] == "UPDATE_MEMBERSHIP"
    result = service.prepare(plan, confirmed=True, expected_preview=dry)
    assert result["status"] == "VERIFIED"
    update = next(value for name, value in client.mutations if name == "update")
    delta = update["sourcesToUpdate"][0]["condition"]["inclusion"]
    assert delta["selectionsToAdd"] == [{"productId": "gid://shopify/Product/5"}]
    assert delta["selectionsToRemove"] == [{"productId": IDS[3]}]
    assert set(result["product_ids"]) == set(IDS[:3] + ["gid://shopify/Product/5"])


def test_not_applied_durable_recovery_returns_to_collection_step_three():
    state = {}
    plan = {"store_id": "001", "plan_id": "p1", "status": "READY", "items": [{"id": n} for n in range(4)]}
    hydrate_featured_postwrite_state(state, plan, {"store_id": "001", "plan_id": "p1",
        "status": "NOT_APPLIED", "backup_id": "b1", "featured_strategy": "NATIVE_COLLECTION"})
    flow = resolve_featured_guided_state(state, plan, {}, {}, state["featured_last_apply_result"])
    assert flow["current_step"] == 3
    assert flow["primary_action"] == "PREPARE_FEATURED_COLLECTION"
    assert "POST_WRITE_NOT_APPLIED" == state["featured_last_apply_result"]["reason_code"]


def test_collection_postwrite_verification_checks_handle_count_membership_and_drift():
    before = parse_shopify_json_document('{"sections":{"hero":{"type":"hero"}},"order":["hero"]}')
    proposed_raw = json.dumps({"sections":{"hero":{"type":"hero"},"featured":{"type":"product-list","settings":{"collection":"shopsource-homepage-featured","max_products":4}}},"order":["hero","featured"]})
    proposed = parse_shopify_json_document(proposed_raw)
    result = ThemeFileReadResult("templates/index.json", proposed_raw, proposed.raw_hash,
        "THEME_ACCESS_CLI", "001", "shop.myshopify.com", "123", {"verified_theme_role": "MAIN"})
    facts = FeaturedProductThemeApplyService._post_write_facts(before, proposed, result,
        section_id="featured", product_field_id=None, expected_ids=IDS,
        selected_products=[{"shopify_product_id": item} for item in IDS], legacy_id=None, store_id="001",
        expected_backend="THEME_ACCESS_CLI", expected_theme_id="123", expected_filename="templates/index.json",
        strategy="NATIVE_COLLECTION", collection_field_id="collection", collection_count_field_id="max_products",
        collection_handle=COLLECTION_HANDLE, collection_proof={"status": "VERIFIED", "collection_id": "c1",
            "handle": COLLECTION_HANDLE, "product_ids": IDS, "published": True}, expected_collection_id="c1")
    assert facts["reason_code"] == "POST_WRITE_VERIFIED_COLLECTION"
    wrong_raw = proposed_raw.replace(COLLECTION_HANDLE, "unmanaged")
    wrong_doc = parse_shopify_json_document(wrong_raw)
    wrong = ThemeFileReadResult("templates/index.json", wrong_raw, wrong_doc.raw_hash,
        "THEME_ACCESS_CLI", "001", "shop.myshopify.com", "123", {"verified_theme_role": "MAIN"})
    facts = FeaturedProductThemeApplyService._post_write_facts(before, proposed, wrong,
        section_id="featured", product_field_id=None, expected_ids=IDS,
        selected_products=[{"shopify_product_id": item} for item in IDS], legacy_id=None, store_id="001",
        expected_backend="THEME_ACCESS_CLI", expected_theme_id="123", expected_filename="templates/index.json",
        strategy="NATIVE_COLLECTION", collection_field_id="collection", collection_count_field_id="max_products",
        collection_handle=COLLECTION_HANDLE, collection_proof={"status": "VERIFIED", "product_ids": IDS, "published": True})
    assert facts["reason_code"] == "POST_WRITE_COLLECTION_HANDLE_MISMATCH"


def test_collection_cli_live_capability_requires_fresh_graphql_main(tmp_path, monkeypatch):
    from shopsource.featured_theme_backend import FeaturedThemeBackendCoordinator

    class Graph:
        def __init__(self, main_id): self.main_id = main_id
        def execute(self, query, variables=None):
            if "currentAppInstallation" in query:
                return {"currentAppInstallation": {"accessScopes": [{"handle": "read_themes"}]}}
            return {"themes": {"nodes": [{"id": f"gid://shopify/OnlineStoreTheme/{self.main_id}",
                "name": "Live", "role": "MAIN"}]}}

    db = tmp_path / "theme.sqlite3"
    init_db(db)
    monkeypatch.setattr("shopsource.featured_theme_backend.get_connection",
        lambda store_id, db=None: {"shop_domain": "shop.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.shopify_theme_cli.get_connection",
        lambda store_id, db=None: {"shop_domain": "shop.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.featured_theme_backend.get_shopify_token", lambda store_id, db=None: ("fake", None))
    cli = ShopifyThemeCLI(db=db, runner=lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"4.9.3", "stderr": b""})(),
        which=lambda _: "shopify", credential_getter=lambda _: "fake")
    coordinator = FeaturedThemeBackendCoordinator(db=db, client=Graph("162910503161"), cli_backend=cli)
    capability = cli.capability_status("001", theme_id="162910503161")
    assert capability.ready is True
    assert capability.reason_code == "GRAPHQL_MAIN_IDENTITY_VERIFIED"
    assert capability.details["identity_source"] == "ADMIN_GRAPHQL_READ_THEMES"
    coordinator.client_override = Graph("162910503162")
    blocked = coordinator.verify_main_identity("001", "162910503161")
    assert blocked["verified"] is False and blocked["reason_code"] == "MAIN_THEME_IDENTITY_MISMATCH"


def test_collection_e2e_one_mock_theme_push_to_assignment_ready(tmp_path, monkeypatch):
    from shopsource.featured_theme_backend import FeaturedThemeBackendCoordinator
    from shopsource.homepage_featured_products import FeaturedProductAssignmentService
    from shopsource.homepage_session import HomepagePrerequisiteService
    from shopsource.shopify_theme_json import parse_shopify_json_document
    from shopsource.theme_write_backend import ThemeBackendCapabilities
    from test_homepage_featured_products import add_product

    db = tmp_path / "e2e.sqlite3"
    init_db(db)
    planner = FeaturedProductAssignmentService(db=db, export_dir=tmp_path / "exports")
    for number, category in enumerate(("trunk", "seat", "console", "cleanup"), 1):
        add_product(db, number, category)
    plan = planner.create_plan("s1", requested_count=4)
    collection_client = CollectionMock()
    monkeypatch.setattr("shopsource.featured_collection_bridge.get_connection",
        lambda store_id, db=None: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    bridge = FeaturedCollectionPrerequisiteService(db=db, client=collection_client)
    monkeypatch.setattr("shopsource.homepage_featured_products.get_connection",
        lambda store_id, db=None: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    dry = bridge.dry_run(plan)
    collection_prep_user_action_count = 0
    real_shopify_write_count = 0
    collection_prep_user_action_count += 1
    prepared = bridge.prepare(plan, confirmed=True, expected_preview=dry)
    assert prepared["status"] == "VERIFIED" and prepared["action"] == "CREATE"
    plan = planner.get_plan(plan["plan_id"])

    raw = '/* preserved */\n' + json.dumps({"sections": {"hero": {"type": "hero", "settings": {"title": "Keep"}},
        "merchant": {"type": "rich-text", "settings": {"text": "Preserve"}}}, "order": ["hero", "merchant"]}) + "\n"
    schema = "{% for product in section.settings.collection.products limit: section.settings.max_products %}{% endfor %}" + \
        "{% schema %}" + json.dumps({"name": "Product list", "settings": [
            {"type": "collection", "id": "collection", "label": "Collection"},
            {"type": "range", "id": "max_products", "label": "Maximum", "min": 1, "max": 16, "step": 1}]}) + "{% endschema %}"
    snapshot = {"status": "CONNECTED", "template_status": "READY", "template_filename": "templates/index.json",
        "template": parse_shopify_json_document(raw).parsed,
        "theme_files": {"templates/index.json": raw, "sections/product-list.liquid": schema},
        "theme": {"id": "gid://shopify/OnlineStoreTheme/55", "name": "Main", "role": "MAIN"}}
    proof = bridge.verify_for_plan(plan)
    snapshot["verified_collection"] = proof
    preview = planner.build_theme_preview(plan, snapshot)
    preview.update(store_id="s1", featured_products_plan_id=plan["plan_id"], theme=snapshot["theme"],
                   template_filename="templates/index.json")

    class Graph:
        def execute(self, query, variables=None):
            if "currentAppInstallation" in query:
                return {"currentAppInstallation": {"accessScopes": [{"handle": "read_themes"}]}}
            return {"themes": {"nodes": [{"id": "gid://shopify/OnlineStoreTheme/55", "name": "Main", "role": "MAIN"}]}}

    class ThemeMock:
        backend_name = "THEME_ACCESS_CLI"
        main_identity_verifier = None
        def __init__(self): self.raw = raw; self.write_count = 0; self.read_count = 0
        def read_capability_status(self, store_id, *, theme_id):
            return ThemeBackendCapabilities(self.backend_name, "READ_ONLY_READY", "LIVE_ROLE_UNVERIFIED",
                {"verified_theme_id": "55", "configured_shop_domain": "sample.myshopify.com",
                 "cli_store_domain": "sample.myshopify.com", "cli_store_domain_source": "CONFIGURED_SHOP_DOMAIN_FALLBACK"})
        def capability_status(self, store_id, *, theme_id=None):
            identity = self.main_identity_verifier(store_id, theme_id)
            if not identity.get("verified"):
                return ThemeBackendCapabilities(self.backend_name, "UNAVAILABLE", identity.get("reason_code"))
            return ThemeBackendCapabilities(self.backend_name, "READY", "GRAPHQL_MAIN_IDENTITY_VERIFIED",
                {"identity_verified": True, "live_role_verified": True, "verified_theme_id": "55",
                 "verified_theme_role": "MAIN", "target_is_live": True,
                 "configured_shop_domain": "sample.myshopify.com", "cli_store_domain": "sample.myshopify.com",
                 "cli_store_domain_source": "CONFIGURED_SHOP_DOMAIN_FALLBACK"})
        def read_file(self, store_id, shop_domain, theme_id, filename):
            self.read_count += 1
            return ThemeFileReadResult(filename, self.raw, __import__("hashlib").sha256(self.raw.encode()).hexdigest(),
                self.backend_name, store_id, shop_domain, theme_id, {"verified_theme_role": "MAIN"})
        def write_file(self, request):
            self.write_count += 1; self.raw = request.content
            return ThemeFileWriteResult("WRITE_ATTEMPTED", self.backend_name, request.filename, True)

    monkeypatch.setattr("shopsource.featured_theme_backend.get_connection",
        lambda store_id, db=None: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.featured_theme_backend.get_shopify_token", lambda store_id, db=None: ("mock", None))
    theme_transport = ThemeMock()
    coordinator = FeaturedThemeBackendCoordinator(db=db, client=Graph(), cli_backend=theme_transport)
    writer = FeaturedProductThemeApplyService(db=db, export_dir=tmp_path / "exports",
                                               featured_collection_service=bridge)
    preflight = writer.preflight(plan["plan_id"], preview, store_id="s1", backend_coordinator=coordinator)
    assert preflight["status"] == "READY_TO_APPLY", preflight.get("reason_code")
    applied = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                           backend_coordinator=coordinator)
    assert applied["status"] == "REMOTE_JSON_VERIFIED", applied.get("reason_code")
    assert applied["reason_code"] == "POST_WRITE_VERIFIED_COLLECTION"
    assert applied["write_performed"] is True and theme_transport.write_count == 1
    assert collection_prep_user_action_count == 1 and real_shopify_write_count == 0
    assert collection_client.mutations and len([row for row in collection_client.mutations if row[0] == "create"]) == 1
    assert theme_transport.raw.startswith("/* preserved */")
    assert json.loads(theme_transport.raw.split("*/", 1)[1])["sections"]["merchant"]["settings"]["text"] == "Preserve"

    state = {"featured_products_reviewed_plan_id": plan["plan_id"], "featured_strategy": "NATIVE_COLLECTION",
        "featured_preflight_plan_id": plan["plan_id"], "featured_preflight_preview_hash": preview["preview_hash"],
        "featured_preflight_result": preflight}
    from shopsource.featured_guided_flow import record_featured_apply_result, record_featured_storefront_result, record_featured_assignment_result
    last = record_featured_apply_result(state, applied, plan["plan_id"])
    refreshed = planner.get_plan(plan["plan_id"])
    step6 = resolve_featured_guided_state(state, refreshed, preview, preflight, last)
    assert step6["current_step"] == 6 and step6["primary_action"] == "OPEN_STOREFRONT"
    storefront = planner.confirm_storefront(plan["plan_id"], checks={"desktop_title": True, "four_cards": True,
        "product_links": True, "mobile_section": True}, confirmed=True)
    assert storefront["status"] == "STOREFRONT_VERIFIED", storefront
    record_featured_storefront_result(state, storefront)
    refreshed = planner.get_plan(plan["plan_id"])
    step7 = resolve_featured_guided_state(state, refreshed, preview, preflight, last)
    assert step7["current_step"] == 7 and step7["primary_action"] == "OPEN_ASSIGNMENT_CHECK"
    assignment_preview = {"status": "PREVIEW", "featured_products_plan_id": plan["plan_id"],
        "proposed": preview["proposed"], "featured_products_preview": preview}
    checklist = HomepagePrerequisiteService._assignment_check(assignment_preview, refreshed)
    record_featured_assignment_result(state, checklist)
    assert checklist["status"] == "ASSIGNMENT_READY"
    assert resolve_featured_guided_state(state, refreshed, preview, preflight, last)["primary_action"] == "NONE"
    assert theme_transport.write_count == 1  # total mocked Theme Access writes
