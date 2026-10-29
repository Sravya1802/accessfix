"""Patch schema, sanitizer, the deterministic contrast tool, and the in-page JS that applies patches."""
import colorsys
import re
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, field_validator

ALLOWED_ATTRS = {"alt", "lang", "title", "role", "for", "id", "name", "type", "autocomplete", "label", "tabindex"}
ALLOWED_STYLES = {"color", "background-color", "font-weight", "text-decoration", "outline", "outline-offset"}
BANNED_TAGS = {"script", "iframe", "object", "embed", "style", "link", "meta", "base", "frame", "frameset"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
ATTR_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-:.]*$")
SAFE_STYLE_VALUE = re.compile(r"^[#\w\s.,%()\-]+$")


def _as_str(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


class Patch(BaseModel):
    node_id: str = ""
    selector: str = ""
    rule_id: str = ""
    action: Literal["set_attributes", "set_style", "replace_element"]
    attributes: dict[str, str] = {}
    remove_attributes: list[str] = []
    style: dict[str, str] = {}
    html: str = ""
    explanation: str = ""
    wcag: str = ""
    needs_review: bool = False

    @field_validator("attributes", "style", mode="before")
    @classmethod
    def _stringify_values(cls, v):
        if not v:
            return {}
        if not isinstance(v, dict):
            raise ValueError("must be an object")
        return {str(k): _as_str(val) for k, val in v.items()}

    @field_validator("remove_attributes", mode="before")
    @classmethod
    def _listify(cls, v):
        if not v:
            return []
        return [v] if isinstance(v, str) else [str(x) for x in v]

    @field_validator("html", "explanation", "wcag", "selector", "rule_id", "node_id", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return _as_str(v)


def _has_js_url(value: str) -> bool:
    squashed = re.sub(r"[\s\x00-\x1f]", "", value).lower()
    return "javascript:" in squashed or "vbscript:" in squashed


def _attr_problem(name: str, value: str) -> str | None:
    n = name.strip().lower()
    if not ATTR_NAME.match(n):
        return f"invalid attribute name '{name}'"
    if n.startswith("on"):
        return f"event handler attribute '{name}' is not allowed"
    if not (n in ALLOWED_ATTRS or n.startswith("aria-")):
        return f"attribute '{name}' is not on the allowlist"
    if _has_js_url(value):
        return "javascript: URLs are not allowed"
    if n == "tabindex" and value.strip() not in ("0", "-1"):
        return "tabindex may only be 0 or -1"
    return None


class _FragmentCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.roots = 0
        self.stray_text = False
        self.problems: list[str] = []

    def _check_tag(self, tag, attrs):
        if tag in BANNED_TAGS:
            self.problems.append(f"<{tag}> is not allowed")
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.problems.append(f"event handler attribute '{name}' is not allowed")
            if value and _has_js_url(value):
                self.problems.append("javascript: URLs are not allowed")

    def handle_starttag(self, tag, attrs):
        self._check_tag(tag, attrs)
        if self.depth == 0:
            self.roots += 1
        if tag not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self._check_tag(tag, attrs)
        if self.depth == 0:
            self.roots += 1

    def handle_endtag(self, tag):
        if tag not in VOID_TAGS:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.depth == 0 and data.strip():
            self.stray_text = True


def sanitize(patch: Patch) -> tuple[Patch | None, str | None]:
    """Return (clean_patch, None) or (None, reason). Never lets script-capable content through."""
    if not patch.selector or len(patch.selector) > 500:
        return None, "missing or oversized selector"
    for name, value in patch.attributes.items():
        if problem := _attr_problem(name, value):
            return None, problem
    for name in patch.remove_attributes:
        n = name.strip().lower()
        if not ATTR_NAME.match(n) or not (n in ALLOWED_ATTRS or n.startswith("aria-") or n.startswith("on")):
            return None, f"removing '{name}' is not allowed"

    style = {}
    for prop, value in patch.style.items():
        p = prop.strip().lower()
        v = value.replace("!important", "").strip()
        if p not in ALLOWED_STYLES:
            return None, f"style property '{prop}' is not on the allowlist"
        if not SAFE_STYLE_VALUE.match(v) or "url(" in v.lower() or "expression(" in v.lower():
            return None, f"unsafe style value for '{prop}'"
        style[p] = v

    html = patch.html.strip()
    if patch.action == "replace_element":
        if not html:
            return None, "replace_element needs html"
        checker = _FragmentCheck()
        checker.feed(html)
        checker.close()
        if checker.problems:
            return None, checker.problems[0]
        if checker.roots != 1 or checker.stray_text:
            return None, "replacement html must have exactly one root element"
    elif patch.action == "set_attributes" and not (patch.attributes or patch.remove_attributes):
        return None, "set_attributes patch changes nothing"
    elif patch.action == "set_style" and not style:
        return None, "set_style patch changes nothing"

    return patch.model_copy(update={"style": style, "html": html}), None


# ---------- contrast tool ----------

def parse_color(value: str) -> tuple[int, int, int]:
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.match(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", v)
    if m:
        return tuple(round(float(x)) for x in m.groups())  # type: ignore[return-value]
    raise ValueError(f"unsupported color: {value}")


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg) -> float:
    fg = parse_color(fg) if isinstance(fg, str) else fg
    bg = parse_color(bg) if isinstance(bg, str) else bg
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def parse_ratio(value, default: float = 4.5) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*([\d.]+)", str(value or ""))
    return float(m.group(1)) if m else default


def fix_contrast(fg: str, bg: str, target_ratio: float = 4.5) -> str:
    """Darken (or lighten) the foreground, keeping its hue, until it meets target_ratio on bg."""
    fg_rgb, bg_rgb = parse_color(fg), parse_color(bg)
    goal = target_ratio + 0.1  # headroom so axe's rounding never fails the fix
    if contrast_ratio(fg_rgb, bg_rgb) >= goal:
        return to_hex(fg_rgb)
    darken = contrast_ratio((0, 0, 0), bg_rgb) >= contrast_ratio((255, 255, 255), bg_rgb)
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in fg_rgb))
    for step in range(1, 101):
        nl = max(0.0, l - step * 0.01) if darken else min(1.0, l + step * 0.01)
        candidate = tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, nl, s))
        if contrast_ratio(candidate, bg_rgb) >= goal:
            return to_hex(candidate)
    return "#000000" if darken else "#ffffff"


# ---------- in-page JavaScript ----------

# Tag each node we track so verification survives selector changes (new ids, replaced elements).
MARK_JS = """
(marks) => marks.map(({node_id, selector}) => {
  try {
    const el = document.querySelector(selector);
    if (!el) return false;
    const ids = (el.getAttribute('data-afx') || '').split(' ').filter(Boolean);
    if (!ids.includes(node_id)) ids.push(node_id);
    el.setAttribute('data-afx', ids.join(' '));
    return true;
  } catch (e) { return false; }
})
"""

# Each patch's before/after snippet comes from a pristine clone with only that patch applied,
# so overlapping patches (an <img> inside a fixed <a>) don't leak into each other's diffs.
APPLY_JS = """
(patches) => {
  const clean = (node) => {
    node.removeAttribute('data-afx');
    node.querySelectorAll('[data-afx]').forEach((n) => n.removeAttribute('data-afx'));
    return node;
  };
  const snippet = (el) => {
    let html = clean(el.cloneNode(true)).outerHTML;
    if (html.length > 600) {
      html = clean(el.cloneNode(false)).outerHTML.replace(/<\\/[^>]+>$/, '') + ' …';
    }
    return html;
  };
  const applyTo = (el, p) => {
    if (p.action === 'replace_element') {
      const t = document.createElement('template');
      t.innerHTML = p.html;
      const next = t.content.firstElementChild;
      if (!next) throw new Error('empty replacement');
      const marks = el.getAttribute('data-afx');
      if (marks) next.setAttribute('data-afx', marks);
      if (el.parentNode) el.replaceWith(next);
      return next;
    }
    for (const [k, v] of Object.entries(p.attributes || {})) el.setAttribute(k, v);
    for (const k of p.remove_attributes || []) el.removeAttribute(k);
    for (const [k, v] of Object.entries(p.style || {})) el.style.setProperty(k, v, 'important');
    return el;
  };
  const targets = patches.map((p) => {
    try { return document.querySelector(p.selector); } catch (e) { return null; }
  });
  const pristine = targets.map((el) => (el ? el.cloneNode(true) : null));
  return patches.map((p, i) => {
    const out = { node_id: p.node_id, selector: p.selector, applied: false, error: null, before_html: '', after_html: '' };
    const el = targets[i];
    if (!el) { out.error = 'selector not found'; return out; }
    try {
      out.before_html = snippet(pristine[i]);
      out.after_html = snippet(applyTo(pristine[i].cloneNode(true), p));
      applyTo(el, p);
      out.applied = true;
    } catch (e) {
      out.error = String((e && e.message) || e);
    }
    return out;
  });
}
"""

RUN_AXE_JS = """
async (options) => {
  const results = await axe.run(document, options);
  const ids = (el) => ((el && el.getAttribute && el.getAttribute('data-afx')) || '').split(' ').filter(Boolean);
  const context = (el) => {
    if (!el || !el.tagName) return {};
    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
    const src = img ? (img.getAttribute('src') || '') : '';
    const link = el.closest('a[href]');
    const host = el.parentElement ? el.parentElement.closest('form, li, article, section, header, footer, nav, div') : null;
    const text = ((host && host.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
    return {
      tag: el.tagName.toLowerCase(),
      src: src.split('?')[0].split('/').pop(),
      href: link ? link.getAttribute('href') : '',
      nearby_text: text,
    };
  };
  // Only a close, non-root ancestor can be blamed for a new violation (never <html> or <body>).
  const nearMarked = (el) => {
    let cur = el && el.parentElement;
    for (let depth = 0; cur && depth < 3; depth++, cur = cur.parentElement) {
      if (cur.tagName === 'HTML' || cur.tagName === 'BODY') return null;
      if (cur.hasAttribute('data-afx')) return cur;
    }
    return null;
  };
  return results.violations.map((v) => ({
    id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl, tags: v.tags, description: v.description,
    nodes: v.nodes.map((n) => {
      const el = n.element;
      const anc = nearMarked(el);
      return {
        target: n.target,
        html: (n.html || '').slice(0, 600),
        impact: n.impact,
        failureSummary: n.failureSummary,
        data: (n.any || []).map((c) => c.data).filter((d) => d !== null && d !== undefined),
        afx: ids(el),
        afx_anc: ids(anc),
        context: context(el),
      };
    }),
  }));
}
"""
