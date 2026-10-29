import pytest

from app.patches import Patch, contrast_ratio, fix_contrast, sanitize


def patch(**kw):
    base = {"selector": "#x", "rule_id": "label", "action": "set_attributes"}
    return Patch.model_validate({**base, **kw})


def test_allows_aria_label():
    clean, why = sanitize(patch(attributes={"aria-label": "Email address"}))
    assert clean and why is None


@pytest.mark.parametrize("attrs", [{"onclick": "alert(1)"}, {"ONMOUSEOVER": "x"}, {"title": "javascript:alert(1)"},
                                   {"aria-label": "java\tscript:alert(1)"}, {"href": "/x"}, {"tabindex": "5"}])
def test_rejects_unsafe_attributes(attrs):
    clean, why = sanitize(patch(attributes=attrs))
    assert clean is None and why


@pytest.mark.parametrize("html", ['<div><script>alert(1)</script></div>', '<button onclick="x()">Go</button>',
                                  '<a href="javascript:alert(1)">x</a>', '<span>a</span><span>b</span>', 'just text'])
def test_rejects_unsafe_replacements(html):
    clean, why = sanitize(patch(action="replace_element", html=html))
    assert clean is None and why


def test_allows_single_root_replacement():
    clean, why = sanitize(patch(action="replace_element", html='<label>Email <input type="email" name="email"></label>'))
    assert clean and why is None


def test_style_allowlist_and_important_stripped():
    clean, _ = sanitize(patch(action="set_style", style={"color": "#595959 !important"}))
    assert clean.style == {"color": "#595959"}
    assert sanitize(patch(action="set_style", style={"position": "absolute"}))[0] is None
    assert sanitize(patch(action="set_style", style={"color": "url(javascript:x)"}))[0] is None


def test_values_are_coerced_to_strings():
    p = patch(attributes={"tabindex": 0, "aria-hidden": True})
    assert p.attributes == {"tabindex": "0", "aria-hidden": "true"}


@pytest.mark.parametrize("fg,bg", [("#aaaaaa", "#ffffff"), ("#f5c518", "#ffffff"), ("#777777", "#000000"), ("#aaaaaa", "#fffaf0")])
def test_contrast_tool_reaches_target(fg, bg):
    fixed = fix_contrast(fg, bg, 4.5)
    assert contrast_ratio(fixed, bg) >= 4.5


def test_contrast_tool_keeps_passing_colors():
    assert fix_contrast("#000000", "#ffffff", 4.5) == "#000000"
