from link_coroner.scanner.extractors import extract_urls


def test_extracts_plain_urls():
    text = "see https://example.com and http://foo.bar/baz for details"
    assert list(extract_urls(text)) == [
        "https://example.com",
        "http://foo.bar/baz",
    ]


def test_strips_trailing_punctuation():
    text = "visit https://example.com, https://foo.dev. end."
    assert list(extract_urls(text)) == [
        "https://example.com",
        "https://foo.dev",
    ]


def test_balances_parens_in_markdown():
    text = "[link](https://example.com/path) and (https://wrap.dev/x)"
    urls = list(extract_urls(text))
    assert "https://example.com/path" in urls
    assert "https://wrap.dev/x" in urls
    # No stray ')' kept.
    assert all(not u.endswith(")") for u in urls)


def test_ignores_non_http_schemes():
    text = "ftp://nope.example.com mailto:nope@example.com https://yes.dev"
    assert list(extract_urls(text)) == ["https://yes.dev"]


def test_handles_empty_input():
    assert list(extract_urls("")) == []
