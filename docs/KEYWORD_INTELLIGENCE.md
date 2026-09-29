# Keyword Intelligence

`KeywordEngine` creates profile seed combinations and mines 1–4 word n-grams from MASTER and Keepa
product title/category text. It drops common stopwords, ASIN patterns, numeric sizes, and brand terms.
Store-specific seed terms/templates are optional fields in the Store Profile; the generic templates do
not contain Cabin Tidy product phrases. For bounded memory, mining uses the latest 10,000 MASTER rows by
default (`intelligence.max_corpus_rows`, configurable up to 50,000) rather than materializing the full
catalog; Store seeds continue to work when the catalog is empty.

RapidFuzz token-sort similarity groups reordered near-duplicates when installed; difflib/Jaccard provides
the fallback. The explainable score weights semantic fit 35%, candidate yield 20%, price fit 20%, quality
fit 10%, novelty 10%, less a 25 point risk-rate penalty. Weights live in `keyword_scoring.py`.

Keepa validation is explicit and capped at 10 keywords per call. It obtains candidate ASINs, hydrates up
to 100 unique ASINs per keyword, computes yield/price/quality/risk/MASTER duplicate measures, stores the
validation result, and uses the latest validation in the recommendation score. It incurs Keepa token cost.

KeyBERT and sentence-transformers are optional and disabled by default. If enabled, embeddings load only
from a local cache (`local_files_only=True`); a missing/offline model falls back to lexical matching.
The model is configurable in Store Profile and is never silently downloaded.
