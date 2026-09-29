from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from ..db import connect, get_store, init_db, utc_now
from .keyword_models import KeywordRecommendation
from .keyword_scoring import lexical_semantic_fit, score_keyword
from .text_features import extract_ngrams, normalize_keyword, tokenize

GENERIC_TEMPLATES = (
    "{context} {place} {product}",
    "{place} {product}",
    "{context} {use} {product}",
    "{product} for {place}",
)
DEFAULT_PLACES = ("trunk", "backseat", "center console", "seat gap", "under seat", "cargo area")
DEFAULT_USES = ("storage", "organization", "travel", "protection")
DEFAULT_PRODUCTS = ("organizer", "storage", "holder", "tray")


def _similarity(left: str, right: str) -> float:
    try:
        from rapidfuzz.fuzz import token_sort_ratio
        return token_sort_ratio(left, right) / 100.0
    except ImportError:
        a, b = set(left.split()), set(right.split())
        token = len(a & b) / max(1, len(a | b))
        order = difflib.SequenceMatcher(None, left, right).ratio()
        return max(token, order)


class KeywordEngine:
    def __init__(self, db=None, *, enable_models: bool = False, similarity_threshold: float = 0.84,
                 store_dir=None):
        self.db = db
        self.enable_models = enable_models
        self.similarity_threshold = similarity_threshold
        self.store_dir = store_dir

    def recommend(self, store_id: str, limit: int = 40) -> list[dict]:
        init_db(self.db)
        profile = get_store(store_id, self.db)
        texts, brands, existing, primary_texts, keepa_texts = self._store_corpus(store_id)
        seed_candidates = self._expand_seeds(profile)
        ngram_candidates = [word for word, _ in extract_ngrams(texts, brands=brands, max_n=4)[:limit * 3]]
        keybert_candidates = self._keybert_candidates(texts, profile) if self.enable_models else []
        keepa_candidates = self._keepa_discovery_terms(store_id)
        current = {normalize_keyword(value) for value in existing}
        excluded = {normalize_keyword(value) for value in (profile.get("sourcing") or {}).get("excluded_keywords", [])}
        validations = self._latest_validations(store_id, self.db)
        concept_tokens = set(tokenize(
            " ".join([profile.get("store_name", ""), profile.get("category", ""),
                      profile.get("concept", ""), *profile.get("include_keywords", [])])
        ))
        exclusions = [normalize_keyword(value) for value in profile.get("exclude_keywords", [])]
        risk_terms = [normalize_keyword(term) for rule in profile.get("risk_rules", [])
                      for term in rule.get("terms", []) if normalize_keyword(term)]
        raw_candidates: list[tuple[str, str]] = []
        raw_candidates.extend((value, "PROFILE") for value in seed_candidates)
        raw_candidates.extend((value, "NGRAM") for value in ngram_candidates)
        raw_candidates.extend((value, "KEYBERT") for value in keybert_candidates)
        raw_candidates.extend((value, "KEEPA_DISCOVERY") for value in keepa_candidates)
        raw_candidates.extend((value, "KEEPA_DISCOVERY") for value, _ in
                              extract_ngrams(keepa_texts, brands=brands, max_n=4)[:limit * 2])
        raw_candidates.extend((value, "MANUAL") for value in existing)

        sourcing = profile.get("sourcing") or {}
        found_yields, yields_by_term = self._candidate_yields(
            store_id, raw_candidates, self.db,
            float(sourcing.get("price_min", 30)), float(sourcing.get("price_max", 120)),
        )
        matched_counts = dict(extract_ngrams(primary_texts, brands=brands, max_n=4))
        recommendations: list[KeywordRecommendation] = []
        canonical_by_norm: dict[str, str] = {}
        for value, source in raw_candidates:
            keyword = normalize_keyword(value)
            if not keyword or keyword in excluded or len(keyword.split()) > 4 or any(term and term in keyword for term in exclusions):
                continue
            duplicate_of = None
            for other_norm, other_text in canonical_by_norm.items():
                if _similarity(keyword, other_norm) >= self.similarity_threshold:
                    duplicate_of = other_text
                    break
            if duplicate_of:
                continue
            canonical_by_norm[keyword] = value.strip()
            yield_count = yields_by_term.get(keyword, 0)
            semantic = lexical_semantic_fit(keyword, concept_tokens)
            if self.enable_models:
                model_fit = self._embedding_fit(keyword, profile)
                if model_fit is not None:
                    semantic = model_fit
            source_count = matched_counts.get(keyword, 0)
            price_fit = min(1.0, source_count / max(1, len(primary_texts))) if primary_texts else 0.65
            quality_fit = 0.55
            risk_rate = float(any(term in keyword for term in risk_terms))
            if yield_count:
                metrics = found_yields.get(keyword, {})
                risk_rate = metrics.get("risk", 0) / yield_count
                quality_fit = metrics.get("quality", 0) / yield_count
                price_fit = metrics.get("price", 0) / yield_count
            validation = validations.get(keyword)
            if validation:
                yield_count = validation["candidate_yield"]
                price_fit = validation["price_fit"]
                quality_fit = validation["quality_fit"]
                risk_rate = validation["risk_rate"]
            novelty = 0.0 if keyword in current else 1.0
            reasons = [f"{source} 기반 후보", f"의미 유사도 {semantic:.2f}"]
            if yield_count:
                reasons.append(f"Keepa 후보 {yield_count}개, 위험 비율 {risk_rate:.0%}")
            if source_count:
                reasons.append(f"현재 Store 상품 제목 {source_count}개에서 확인")
            if risk_rate:
                reasons.append("Store 위험 규칙과 직접 일치하는 용어 포함")
            if keyword in current:
                reasons.append("기존 recipe와 중복")
            recommendations.append(KeywordRecommendation(
                keyword=value.strip(), source=source, semantic_fit=round(semantic, 3),
                candidate_yield=yield_count, price_fit=round(price_fit, 3),
                quality_fit=round(quality_fit, 3), risk_rate=round(risk_rate, 3),
                novelty=round(novelty, 3),
                score=score_keyword(semantic_fit=semantic, candidate_yield=yield_count,
                                    price_fit=price_fit, quality_fit=quality_fit,
                                    novelty=novelty, risk_rate=risk_rate),
                reason=reasons, duplicate_of=duplicate_of,
                status="EXISTS" if keyword in current else "SUGGESTED",
            ))
        recommendations.sort(key=lambda item: (-item.score, item.keyword.lower()))
        return [item.to_dict() for item in recommendations[:limit]]

    def add_recipes(self, store_id: str, keywords: list[str]) -> dict:
        profile = get_store(store_id, self.db)
        sourcing = profile.setdefault("sourcing", {})
        recipes = sourcing.setdefault("recipes", [])
        existing = {normalize_keyword(item.get("keyword", "") if isinstance(item, dict) else str(item))
                    for item in recipes}
        added = []
        for keyword in keywords:
            normalized = normalize_keyword(keyword)
            if normalized and normalized not in existing:
                recipes.append({"keyword": keyword.strip()})
                existing.add(normalized)
                added.append(keyword.strip())
        self._save_profile(profile)
        return {"store_id": store_id, "added": added, "recipe_count": len(recipes)}

    def remove_recipe(self, store_id: str, keyword: str) -> dict:
        profile = get_store(store_id, self.db)
        recipes = (profile.get("sourcing") or {}).get("recipes", [])
        before = len(recipes)
        target = normalize_keyword(keyword)
        recipes[:] = [item for item in recipes if normalize_keyword(
            item.get("keyword", "") if isinstance(item, dict) else str(item)
        ) != target]
        self._save_profile(profile)
        return {"store_id": store_id, "removed": before - len(recipes)}

    def exclude_recommendations(self, store_id: str, keywords: list[str]) -> dict:
        profile = get_store(store_id, self.db)
        sourcing = profile.setdefault("sourcing", {})
        excluded = sourcing.setdefault("excluded_keywords", [])
        current = {normalize_keyword(value) for value in excluded}
        added = []
        for keyword in keywords:
            normalized = normalize_keyword(keyword)
            if normalized and normalized not in current:
                excluded.append(keyword.strip())
                current.add(normalized)
                added.append(keyword.strip())
        self._save_profile(profile)
        return {"store_id": store_id, "excluded": added}

    def validate(self, store_id: str, keywords: list[str], provider, *, max_keywords: int = 10) -> list[dict]:
        if len(keywords) > max_keywords:
            raise ValueError(f"Validate at most {max_keywords} keywords at a time")
        profile = get_store(store_id, self.db)
        from ..sourcing.models import Recipe
        from ..sourcing.recipes import query_hash
        settings = profile.get("sourcing") or {}
        results = []
        for index, keyword in enumerate(keywords):
            recipe = Recipe(
                recipe_id=f"{store_id}-validation-{index}", keyword=keyword,
                price_min=float(settings.get("price_min", 30)),
                price_max=float(settings.get("price_max", 120)),
                min_rating=float(settings.get("min_rating", 4)),
                min_reviews=int(settings.get("min_reviews", 30)),
                min_images=int(settings.get("min_images", 2)),
            )
            page = provider.discover(recipe, 0)
            asins = list(dict.fromkeys(page.asins))[:100]
            batch = provider.hydrate(asins) if asins else None
            products = batch.products if batch else []
            known = set()
            with connect(self.db) as con:
                if asins:
                    marks = ",".join("?" for _ in asins)
                    known = {row[0] for row in con.execute(f"SELECT asin FROM products WHERE asin IN ({marks})", asins)}
            prices, quality_ok, risky = [], 0, 0
            risk_terms = [term.lower() for rule in profile.get("risk_rules", []) for term in rule.get("terms", [])]
            for product in products:
                canonical = self._canonical(product)
                if canonical.get("price") is not None:
                    prices.append(canonical["price"])
                if (canonical.get("rating") or 0) >= recipe.min_rating and (canonical.get("reviewCount") or 0) >= recipe.min_reviews:
                    quality_ok += 1
                text = " ".join(str(canonical.get(key) or "") for key in ("title", "brand", "category", "tags")).lower()
                if product.get("isAdultProduct") or product.get("isHazMat") or any(term in text for term in risk_terms):
                    risky += 1
            denominator = max(1, len(products))
            results.append({
                "keyword": keyword, "candidate_yield": len(set(page.asins)),
                "price_fit": sum(recipe.price_min <= value <= recipe.price_max for value in prices) / max(1, len(prices)),
                "quality_fit": quality_ok / denominator,
                "risk_rate": risky / denominator,
                "master_duplicate_rate": len(known) / max(1, len(asins)),
                "tokens_consumed": page.telemetry.tokens_consumed + (batch.telemetry.tokens_consumed if batch else 0),
                "query_hash": query_hash({"keyword": keyword, "min": recipe.price_min, "max": recipe.price_max}),
                "checked_at": utc_now(),
            })
            with connect(self.db) as con:
                con.execute("""INSERT INTO keyword_validation_results(store_id,keyword,checked_at,
                    candidate_yield,price_fit,quality_fit,risk_rate,master_duplicate_rate,tokens_consumed)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (store_id, keyword, results[-1]["checked_at"], results[-1]["candidate_yield"],
                     results[-1]["price_fit"], results[-1]["quality_fit"], results[-1]["risk_rate"],
                     results[-1]["master_duplicate_rate"], results[-1]["tokens_consumed"]))
        return results

    def store_wizard_profile(self, *, store_id: str, store_name: str, category: str,
                             concept: str, price_min: float, price_max: float,
                             include_keywords: list[str], exclude_keywords: list[str],
                             risk_rules: list[dict] | None = None) -> dict:
        if price_min < 0 or price_max <= price_min:
            raise ValueError("가격 범위가 올바르지 않습니다.")
        if not store_id.strip() or not store_name.strip() or not category.strip():
            raise ValueError("Store ID, 이름, 카테고리는 필수입니다.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", store_id.strip()):
            raise ValueError("Store ID에는 영문, 숫자, 하이픈, 밑줄만 사용할 수 있습니다.")
        return {
            "store_id": store_id.strip(), "store_name": store_name.strip(),
            "category": category.strip(), "concept": concept.strip(),
            "minimum_fit_score": 34,
            "price_bands": [{"name": "primary", "min": price_min, "max": price_max, "status": "PRIMARY"},
                            {"name": "below", "min": 0, "max": price_min, "status": "RESERVE_B"},
                            {"name": "above", "min": price_max, "max": None, "status": "RESERVE_A"}],
            "include_keywords": include_keywords,
            "exclude_keywords": exclude_keywords,
            "risk_rules": risk_rules or [],
            "sourcing": {"provider": "keepa", "marketplace": "US", "target_candidates": 5,
                         "price_min": price_min, "price_max": price_max,
                         "min_rating": 4.0, "min_reviews": 30, "min_images": 2,
                         "recipes": []},
        }

    def save_wizard_profile(self, profile: dict) -> dict:
        self._save_profile(profile)
        return profile

    def _store_corpus(self, store_id: str):
        with connect(self.db) as con:
            profile_row = con.execute("SELECT profile_json FROM stores WHERE store_id=?", (store_id,)).fetchone()
            profile = json.loads(profile_row[0]) if profile_row else {}
            settings = profile.get("intelligence") or {}
            corpus_limit = min(50000, max(1000, int(settings.get("max_corpus_rows", 10000))))
            products = con.execute(f"""SELECT p.title,p.brand,p.category,p.source_kind,d.final_status
                FROM products p LEFT JOIN store_product_decisions d
                ON d.product_id=p.id AND d.store_id=? WHERE p.archived=0 ORDER BY p.id DESC LIMIT {corpus_limit}""", (store_id,)).fetchall()
        existing = [r.get("keyword", "") if isinstance(r, dict) else str(r)
                    for r in (profile.get("sourcing") or {}).get("recipes", [])]
        texts = [" ".join([p["title"] or "", p["category"] or ""]) for p in products]
        brands = {p["brand"] for p in products if p["brand"]}
        primary_texts = [" ".join([p["title"] or "", p["category"] or ""])
                         for p in products if p["final_status"] in {"PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C"}]
        keepa_texts = [" ".join([p["title"] or "", p["category"] or ""])
                       for p in products if p["source_kind"] == "KEEPA"]
        return texts, brands, existing, primary_texts, keepa_texts

    @staticmethod
    def _expand_seeds(profile: dict) -> list[str]:
        config = profile.get("keyword_templates") or profile.get("sourcing") or {}
        templates = config.get("seed_templates") or GENERIC_TEMPLATES
        places = config.get("places") or profile.get("places") or DEFAULT_PLACES
        uses = config.get("uses") or profile.get("use_cases") or DEFAULT_USES
        products = config.get("product_terms") or profile.get("product_terms") or DEFAULT_PRODUCTS
        contexts = config.get("contexts") or profile.get("keyword_contexts") or [profile.get("category", "")]
        result = list(profile.get("include_keywords") or [])
        for template in templates:
            for context in contexts:
                for place in places:
                    for product in products:
                        result.append(template.format(context=context, place=place, product=product,
                                                      use=uses[0] if uses else "storage"))
        return list(dict.fromkeys(value.strip() for value in result if normalize_keyword(value)))

    @staticmethod
    def _candidate_yields(store_id: str, candidates: list[tuple[str, str]], db=None,
                          price_min: float = 30, price_max: float = 120):
        metrics, terms = {}, {normalize_keyword(value) for value, _ in candidates}
        raw_terms = sorted({value.strip() for value, _ in candidates if value.strip()})
        rows = []
        with connect(db) as con:
            for start in range(0, len(raw_terms), 400):
                batch = raw_terms[start:start + 400]
                marks = ",".join("?" for _ in batch)
                rows.extend(con.execute(f"""SELECT c.keyword,c.asin,c.decision,p.price,p.rating,p.review_count
                    FROM sourcing_run_candidates c JOIN sourcing_runs r ON r.run_id=c.run_id
                    LEFT JOIN products p ON p.asin=c.asin WHERE r.store_id=? AND c.keyword IN ({marks})
                    AND c.id=(SELECT MAX(c2.id) FROM sourcing_run_candidates c2
                        JOIN sourcing_runs r2 ON r2.run_id=c2.run_id
                        WHERE r2.store_id=r.store_id AND c2.keyword=c.keyword AND c2.asin=c.asin)""",
                    [store_id, *batch]).fetchall())
        for keyword, _asin, decision, price, rating, reviews in rows:
            normalized = normalize_keyword(keyword)
            if normalized not in terms:
                continue
            metric = metrics.setdefault(normalized, {"count": 0, "risk": 0, "quality": 0, "price": 0})
            metric["count"] += 1
            metric["risk"] += int(decision == "REJECTED_RISK")
            metric["quality"] += int((rating or 0) >= 4 and (reviews or 0) >= 30)
            metric["price"] += int(price is not None and price_min <= price <= price_max)
        return metrics, {key: value["count"] for key, value in metrics.items()}

    def _keepa_discovery_terms(self, store_id: str) -> list[str]:
        with connect(self.db) as con:
            rows = con.execute("""SELECT DISTINCT c.keyword FROM sourcing_run_candidates c
                JOIN sourcing_runs r ON r.run_id=c.run_id WHERE r.store_id=? AND r.provider='keepa'""",
                (store_id,)).fetchall()
        return [row[0] for row in rows if row[0]]

    @staticmethod
    def _latest_validations(store_id: str, db=None) -> dict:
        with connect(db) as con:
            rows = con.execute("""SELECT v.* FROM keyword_validation_results v
                JOIN (SELECT keyword,MAX(id) id FROM keyword_validation_results WHERE store_id=? GROUP BY keyword) latest
                ON latest.id=v.id WHERE v.store_id=?""", (store_id, store_id)).fetchall()
        return {normalize_keyword(row["keyword"]): dict(row) for row in rows}

    @staticmethod
    def _keybert_candidates(texts: list[str], profile: dict):
        if not texts:
            return [], False
        try:
            from keybert import KeyBERT
            from sentence_transformers import SentenceTransformer
            model_name = (profile.get("intelligence") or {}).get("keybert_model", "all-MiniLM-L6-v2")
            local_model = SentenceTransformer(model_name, local_files_only=True)
            model = KeyBERT(model=local_model)
            extracted = model.extract_keywords(". ".join(texts[:1000]), keyphrase_ngram_range=(1, 4),
                                               stop_words="english", top_n=40)
            return [keyword for keyword, _score in extracted], True
        except Exception:
            return [], False

    def _embedding_fit(self, keyword: str, profile: dict):
        try:
            from sentence_transformers import SentenceTransformer
            from sklearn.metrics.pairwise import cosine_similarity
            model_name = (profile.get("intelligence") or {}).get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2")
            if getattr(self, "_embedding_model_name", None) != model_name:
                self._embedding_model = SentenceTransformer(model_name, local_files_only=True)
                self._embedding_model_name = model_name
            model = self._embedding_model
            concept = " ".join([profile.get("store_name", ""), profile.get("category", ""),
                                *profile.get("include_keywords", [])])
            vectors = model.encode([concept, keyword])
            return max(0.0, float(cosine_similarity([vectors[0]], [vectors[1]])[0][0]))
        except Exception:
            return None

    def _save_profile(self, profile: dict):
        from ..db import upsert_store
        from ..paths import STORE_DIR
        import re
        upsert_store(profile, self.db)
        store_dir = Path(self.store_dir) if self.store_dir is not None else STORE_DIR
        store_dir.mkdir(parents=True, exist_ok=True)
        target = None
        for path in store_dir.glob("*.json"):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if current.get("store_id") == profile.get("store_id"):
                target = path
                break
        if target is None:
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", profile.get("store_name", "store")).strip("_")
            target = store_dir / f"{profile['store_id']}_{slug or 'store'}.json"
        target.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @staticmethod
    def _canonical(raw: dict) -> dict:
        from ..sourcing.mapping import keepa_to_canonical
        return keepa_to_canonical(raw)
