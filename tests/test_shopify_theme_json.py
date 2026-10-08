from __future__ import annotations

import json

import pytest

from shopsource.shopify_theme_json import (
    ShopifyJsonDocumentError,
    parse_shopify_json_document,
    render_shopify_json_document,
)


def test_shopify_json_document_parses_leading_block_comment():
    raw = "/* Shopify header comment */\n" + json.dumps({"sections": {}, "order": []})
    document = parse_shopify_json_document(raw)
    assert document.parsed == {"sections": {}, "order": []}
    assert document.had_leading_comment is True


def test_shopify_json_document_preserves_exact_prefix():
    prefix = "  /* first */\r\n\t/* second */ \n"
    document = parse_shopify_json_document(prefix + '{"sections":{}}  \r\n')
    rendered = render_shopify_json_document(document, {"sections": {"a": {"type": "main"}}})
    assert rendered.startswith(prefix)
    assert rendered.endswith("  \r\n")


def test_shopify_json_document_preserves_bom_and_whitespace():
    prefix = "\ufeff \r\n"
    suffix = "\t\r\n"
    document = parse_shopify_json_document(prefix + '{"note":"ok"}' + suffix)
    rendered = render_shopify_json_document(document, document.parsed)
    assert rendered.startswith(prefix) and rendered.endswith(suffix)


def test_shopify_json_document_does_not_strip_comment_tokens_inside_strings():
    raw = '{"url":"https://example.test/a/*literal*/b","text":"// still data"}'
    assert parse_shopify_json_document(raw).parsed["url"].endswith("/*literal*/b")


def test_shopify_json_document_rejects_unclosed_leading_comment():
    with pytest.raises(ShopifyJsonDocumentError) as error:
        parse_shopify_json_document("/* Shopify header\n{\"sections\":{}}")
    assert error.value.code == "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE"
    assert error.value.offset == 0


def test_shopify_json_document_rejects_unsupported_inline_comment_shape():
    with pytest.raises(ShopifyJsonDocumentError) as error:
        parse_shopify_json_document('{"sections":{}} /* not a leading comment */')
    assert error.value.code == "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE"


def test_shopify_json_document_rejects_invalid_json_without_repair():
    with pytest.raises(ShopifyJsonDocumentError) as error:
        parse_shopify_json_document("/* header */\n{broken}")
    assert error.value.code == "INVALID_THEME_JSON"


def test_shopify_json_document_hashes_raw_and_semantic_content_separately():
    parsed = {"sections": {}}
    a = parse_shopify_json_document("/* header */\n" + json.dumps(parsed))
    b = parse_shopify_json_document("/* changed header */\n" + json.dumps(parsed))
    assert a.raw_hash != b.raw_hash
    assert a.semantic_hash == b.semantic_hash
