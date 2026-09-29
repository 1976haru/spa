from __future__ import annotations

import json
import secrets
from pathlib import Path

from ..classifier import classify_store
from ..db import connect, get_store, init_db, utc_now
from ..stats import store_summary
from .mapping import keepa_to_canonical
from .providers.keepa import KeepaProvider
from .recipes import finder_query, query_hash, recipes_for_profile

TERMINAL = {"DONE", "FAILED", "CANCELLED"}


def new_run_id(store_id: str) -> str:
    return f"SRC_{store_id}_{utc_now().replace(':', '').replace('-', '')}_{secrets.token_hex(2)}"


class SourcingEngine:
    def __init__(self, provider=None, progress_callback=None):
        self.provider = provider
        self.progress_callback = progress_callback

    def preview(self, store_id: str, target: int = 5, db=None) -> dict:
        if target < 1:
            raise ValueError("target must be greater than zero")
        init_db(db)
        profile = get_store(store_id, db)
        recipes = recipes_for_profile(profile)
        estimate = (self.provider.estimate(recipes, target) if self.provider else {
            "finder_requests_min": 1,
            "finder_requests_max": len(recipes),
            "product_requests_max": (target + 99) // 100,
            "estimated_token_min": target,
            "estimated_token_max": target + len(recipes) * 10,
        })
        return {
            "dry_run": True,
            "network_requests": 0,
            "store_id": store_id,
            "store_name": profile["store_name"],
            "provider": "keepa",
            "marketplace": "US",
            "target_candidates": target,
            "keywords": [recipe.keyword for recipe in recipes],
            "filters": {
                "price_usd": [recipes[0].price_min, recipes[0].price_max] if recipes else [],
                "min_rating": recipes[0].min_rating if recipes else None,
                "min_reviews": recipes[0].min_reviews if recipes else None,
                "min_images": recipes[0].min_images if recipes else None,
            },
            "estimate": estimate,
        }

    def run(self, store_id: str, target: int = 5, *, db: str | Path | None = None,
            max_tokens_per_run: int = 1000, min_tokens_reserve: int = 100,
            run_id: str | None = None) -> dict:
        if target < 1:
            raise ValueError("target must be greater than zero")
        init_db(db)
        profile = get_store(store_id, db)
        recipes = recipes_for_profile(profile)
        provider = self.provider or KeepaProvider()
        rid = run_id or new_run_id(store_id)
        checkpoint = {"recipe_index": 0, "page": 0, "discovered": [], "hydrated": []}
        with connect(db) as con:
            old = con.execute("SELECT * FROM sourcing_runs WHERE run_id=?", (rid,)).fetchone()
            if old:
                if old["status"] == "CANCELLED":
                    return self.status(rid, db)
                checkpoint = json.loads(old["checkpoint_json"] or "{}")
                con.execute("UPDATE sourcing_runs SET status='RUNNING',error=NULL WHERE run_id=?", (rid,))
            else:
                con.execute(
                    """INSERT INTO sourcing_runs(run_id,provider,store_id,marketplace,
                    recipe_snapshot_json,target_candidates,status,checkpoint_json,started_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (rid, provider.name, store_id, "US", json.dumps([r.to_dict() for r in recipes]),
                     target, "RUNNING", json.dumps(checkpoint), utc_now()),
                )
        try:
            discovered = list(checkpoint.get("discovered") or [])
            seen = set(discovered)
            for recipe_index in range(int(checkpoint.get("recipe_index", 0)), len(recipes)):
                recipe = recipes[recipe_index]
                page = int(checkpoint.get("page", 0)) if recipe_index == checkpoint.get("recipe_index", 0) else 0
                while len(discovered) < target:
                    if self._cancelled(rid, db):
                        return self.status(rid, db)
                    result = provider.discover(recipe, page)
                    if self._budget_pause(rid, result.telemetry, max_tokens_per_run, min_tokens_reserve, db):
                        return self.status(rid, db)
                    query = finder_query(recipe, page)
                    for rank, asin in enumerate(result.asins, 1):
                        asin = asin.strip().upper()
                        if not asin:
                            continue
                        self._record_candidate(rid, asin, recipe, page, rank, query_hash(query), db)
                        if asin not in seen:
                            seen.add(asin)
                            discovered.append(asin)
                            if len(discovered) >= target:
                                break
                    page += 1
                    checkpoint.update(recipe_index=recipe_index, page=page, discovered=discovered)
                    self._checkpoint(rid, checkpoint, finder=1, telemetry=result.telemetry, db=db)
                    self._notify(rid, db)
                    if not result.has_more:
                        break
                checkpoint["page"] = 0
                checkpoint["recipe_index"] = recipe_index + 1
                self._checkpoint(rid, checkpoint, db=db)
                self._notify(rid, db)
                if len(discovered) >= target:
                    break

            hydrated = set(checkpoint.get("hydrated") or [])
            pending = [asin for asin in discovered if asin not in hydrated]
            for start in range(0, len(pending), 100):
                if self._cancelled(rid, db):
                    return self.status(rid, db)
                batch_asins = pending[start:start + 100]
                batch = provider.hydrate(batch_asins)
                if self._budget_pause(rid, batch.telemetry, max_tokens_per_run, min_tokens_reserve, db):
                    return self.status(rid, db)
                stats = self._store_products(rid, batch.products, profile, db)
                hydrated.update(str(raw.get("asin") or "").upper() for raw in batch.products)
                checkpoint["hydrated"] = sorted(value for value in hydrated if value)
                self._checkpoint(rid, checkpoint, product=1, telemetry=batch.telemetry, db=db, **stats)
                self._notify(rid, db)
            classification = classify_store(store_id, db)
            self._apply_source_risks(rid, store_id, db)
            classification = {
                "store_id": store_id,
                "processed": classification["processed"],
                "counts": store_summary(store_id, db)["counts"],
            }
            with connect(db) as con:
                con.execute("UPDATE sourcing_runs SET status='DONE',finished_at=? WHERE run_id=?", (utc_now(), rid))
            return {**self.status(rid, db), "classification": classification}
        except Exception as exc:
            with connect(db) as con:
                con.execute("UPDATE sourcing_runs SET status='FAILED',finished_at=?,error=? WHERE run_id=?",
                            (utc_now(), str(exc)[:1000], rid))
            raise

    def _notify(self, run_id, db):
        if self.progress_callback:
            self.progress_callback(self.status(run_id, db))

    def resume(self, run_id: str, db=None) -> dict:
        row = self.status(run_id, db)
        if row["status"] in TERMINAL:
            raise ValueError(f"Run cannot be resumed from {row['status']}")
        return self.run(row["store_id"], row["target_candidates"], db=db, run_id=run_id)

    @staticmethod
    def cancel(run_id: str, db=None) -> dict:
        init_db(db)
        with connect(db) as con:
            changed = con.execute(
                "UPDATE sourcing_runs SET status='CANCELLED',finished_at=? WHERE run_id=? AND status NOT IN ('DONE','FAILED')",
                (utc_now(), run_id),
            ).rowcount
        if not changed:
            raise KeyError(f"Active sourcing run not found: {run_id}")
        return SourcingEngine.status(run_id, db)

    @staticmethod
    def pause(run_id: str, db=None) -> dict:
        with connect(db) as con:
            changed = con.execute("UPDATE sourcing_runs SET status='PAUSED' WHERE run_id=? AND status='RUNNING'", (run_id,)).rowcount
        if not changed:
            raise KeyError(f"Running sourcing run not found: {run_id}")
        return SourcingEngine.status(run_id, db)

    @staticmethod
    def status(run_id: str, db=None) -> dict:
        init_db(db)
        with connect(db) as con:
            row = con.execute("SELECT * FROM sourcing_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Sourcing run not found: {run_id}")
        result = dict(row)
        result["checkpoint"] = json.loads(result.pop("checkpoint_json") or "{}")
        result.pop("recipe_snapshot_json", None)
        return result

    @staticmethod
    def _cancelled(run_id, db) -> bool:
        with connect(db) as con:
            row = con.execute("SELECT status FROM sourcing_runs WHERE run_id=?", (run_id,)).fetchone()
        return bool(row and row["status"] in {"CANCELLED", "PAUSED"})

    @staticmethod
    def _budget_pause(run_id, telemetry, max_tokens, reserve, db) -> bool:
        with connect(db) as con:
            current = con.execute("SELECT tokens_consumed FROM sourcing_runs WHERE run_id=?", (run_id,)).fetchone()[0]
            pause = current + telemetry.tokens_consumed > max_tokens or (
                telemetry.tokens_left is not None and telemetry.tokens_left < reserve
            )
            if pause:
                con.execute("""UPDATE sourcing_runs SET status='PAUSED_TOKEN_BUDGET',tokens_left=?,
                            tokens_consumed=tokens_consumed+? WHERE run_id=?""",
                            (telemetry.tokens_left, telemetry.tokens_consumed, run_id))
        return pause

    @staticmethod
    def _checkpoint(run_id, checkpoint, finder=0, product=0, telemetry=None, db=None, **stats):
        telemetry = telemetry or type("T", (), {"tokens_consumed": 0, "tokens_left": None})()
        with connect(db) as con:
            con.execute(
                """UPDATE sourcing_runs SET checkpoint_json=?,finder_requests=finder_requests+?,
                product_requests=product_requests+?,tokens_consumed=tokens_consumed+?,
                tokens_left=COALESCE(?,tokens_left),discovered_asins=?,hydrated_products=?,
                inserted=inserted+?,updated=updated+?,duplicates=duplicates+?,invalid=invalid+?
                WHERE run_id=?""",
                (json.dumps(checkpoint), finder, product, telemetry.tokens_consumed,
                 telemetry.tokens_left, len(checkpoint.get("discovered", [])),
                 len(checkpoint.get("hydrated", [])), stats.get("inserted", 0),
                 stats.get("updated", 0), stats.get("duplicates", 0), stats.get("invalid", 0), run_id),
            )

    @staticmethod
    def _record_candidate(run_id, asin, recipe, page, rank, hash_value, db):
        with connect(db) as con:
            con.execute(
                """INSERT OR IGNORE INTO sourcing_run_candidates(
                run_id,asin,recipe_id,keyword,finder_page,discovered_at,discovery_rank,query_hash)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, asin, recipe.recipe_id, recipe.keyword, page, utc_now(), rank, hash_value),
            )

    @staticmethod
    def _store_products(run_id, raws, profile, db):
        stats = {"inserted": 0, "updated": 0, "duplicates": 0, "invalid": 0}
        with connect(db) as con:
            for raw in raws:
                canonical = keepa_to_canonical(raw)
                asin, title = canonical["asin"], canonical["title"]
                if not asin or not title:
                    stats["invalid"] += 1
                    continue
                if raw.get("isAdultProduct") or raw.get("isHazMat"):
                    decision = "REJECTED_RISK"
                    reason = ["adult" if raw.get("isAdultProduct") else "hazmat"]
                    con.execute("UPDATE sourcing_run_candidates SET decision=?,reason_json=? WHERE run_id=? AND asin=?",
                                (decision, json.dumps(reason), run_id, asin))
                existing = con.execute("SELECT id FROM products WHERE asin=?", (asin,)).fetchone()
                now = utc_now()
                encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
                values = (canonical.get("url"), title, canonical.get("brand") or "", canonical.get("price"),
                          canonical.get("category") or "", json.dumps(canonical.get("tags") or []),
                          json.dumps(canonical.get("overview") or []), json.dumps(canonical.get("aboutThis") or []),
                          json.dumps(canonical.get("images") or []), json.dumps(canonical.get("options") or {}),
                          canonical.get("rating"), canonical.get("reviewCount"), canonical.get("_sourceUrl"), encoded, now)
                if existing:
                    product_id = existing["id"]
                    con.execute("""UPDATE products SET url=?,title=?,brand=?,price=?,category=?,tags_json=?,overview_json=?,
                    about_json=?,images_json=?,options_json=?,rating=?,review_count=?,source_url=?,raw_json=?,last_seen_at=?,
                    source='keepa',source_kind='KEEPA' WHERE id=?""", values + (product_id,))
                    stats["updated"] += 1; stats["duplicates"] += 1
                else:
                    cur = con.execute("""INSERT INTO products(asin,source,source_kind,url,title,brand,price,currency,
                    category,tags_json,overview_json,about_json,images_json,options_json,rating,review_count,source_url,
                    raw_json,first_seen_at,last_seen_at) VALUES(?,'keepa','KEEPA',?,?,?,?,'USD',?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asin,) + values[:4] + values[4:14] + (now, now))
                    product_id = cur.lastrowid; stats["inserted"] += 1
                con.execute("""INSERT INTO product_occurrences(product_id,job_id,source_file,collected_at,source_url,raw_json,source_kind)
                VALUES(?,?,?,?,?,?, 'KEEPA') ON CONFLICT(product_id,job_id,source_file) DO NOTHING""",
                (product_id, run_id, f"{asin}.json", now, "keepa:product", json.dumps(raw, ensure_ascii=False)))
                con.execute("""UPDATE sourcing_run_candidates SET decision='HYDRATED'
                            WHERE run_id=? AND asin=? AND decision!='REJECTED_RISK'""", (run_id, asin))
        return stats

    @staticmethod
    def _apply_source_risks(run_id, store_id, db):
        with connect(db) as con:
            rows = con.execute(
                """SELECT DISTINCT p.id,c.reason_json FROM sourcing_run_candidates c
                JOIN products p ON p.asin=c.asin
                WHERE c.run_id=? AND c.decision='REJECTED_RISK'""",
                (run_id,),
            ).fetchall()
            for row in rows:
                reasons = json.loads(row["reason_json"] or "[]")
                status = "RESTRICTED" if "adult" in reasons else "REVIEW"
                con.execute(
                    """UPDATE store_product_decisions SET risk_status=?,auto_status=?,
                    final_status=CASE WHEN manual_override=1 THEN final_status ELSE ? END,
                    reasons_json=?,classified_at=? WHERE store_id=? AND product_id=?""",
                    (status, status, status, json.dumps([f"source_{x}" for x in reasons]),
                     utc_now(), store_id, row["id"]),
                )
