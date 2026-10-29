"""Nemotron on Nebius Token Factory: tier routing, call log, robust JSON parsing, and MOCK mode."""
import asyncio
import json
import logging
import os
import re
import time
from functools import lru_cache
from urllib.parse import urlsplit

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

log = logging.getLogger("accessfix.llm")

TIERS = ("nano", "super", "ultra")
TIER_LABELS = {"nano": "Nemotron Nano", "super": "Nemotron Super", "ultra": "Nemotron Ultra"}
TEMPERATURE = {"nano": 0.0, "super": 0.2, "ultra": 0.3}
MAX_TOKENS = {"nano": 2000, "super": 4000, "ultra": 2500}
# If a tier isn't configured, use the next tier up (Ultra falls back to Super).
FALLBACK_ORDER = {"nano": ("nano", "super", "ultra"), "super": ("super", "ultra", "nano"), "ultra": ("ultra", "super", "nano")}

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
FENCE = re.compile(r"```(?:json|markdown|md)?", re.I)


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def resolve_models() -> dict[str, dict]:
    configured = {t: os.getenv(f"MODEL_{t.upper()}", "").strip() for t in TIERS}
    resolved = {}
    for tier in TIERS:
        for candidate in FALLBACK_ORDER[tier]:
            if configured[candidate]:
                resolved[tier] = {"model": configured[candidate], "fallback_from": None if candidate == tier else tier}
                break
        else:
            resolved[tier] = {"model": f"mock-{tier}", "fallback_from": None}
    return resolved


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.getenv("NEBIUS_API_KEY", ""),
        base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        timeout=120,
        max_retries=2,
    )


_json_mode_ok: dict[str, bool] = {}


def strip_reasoning(text: str) -> str:
    text = THINK_BLOCK.sub("", text or "")
    if "</think>" in text:  # reasoning that started before the visible output
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def extract_json(text: str):
    """Return the first complete JSON object or array found in model output."""
    cleaned = FENCE.sub("", strip_reasoning(text))
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(cleaned[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in the model output")


class LLMError(RuntimeError):
    pass


class LLM:
    """One per run, so the call log is the run's agent trace."""

    def __init__(self, mock: bool | None = None):
        self.mock = env_flag("MOCK_LLM", "1") if mock is None else mock
        self.models = resolve_models()
        self.calls: list[dict] = []
        self.mock_delay = float(os.getenv("MOCK_DELAY", "0.35"))

    def model_for(self, tier: str) -> str:
        return self.models[tier]["model"]

    def _record(self, tier, purpose, started, usage=None, error=None):
        entry = {
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "model": self.model_for(tier) if not self.mock else f"mock ({self.model_for(tier)})",
            "purpose": purpose,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "mock": self.mock,
        }
        if error:
            entry["error"] = error
        self.calls.append(entry)
        return entry

    async def _complete(self, tier: str, messages: list[dict], json_mode: bool):
        model = self.model_for(tier)
        prefix = os.getenv(f"{tier.upper()}_SYSTEM_PREFIX", "").strip()
        if prefix and messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": f"{prefix}\n{messages[0]['content']}"}] + messages[1:]
        kwargs = dict(model=model, messages=messages, temperature=TEMPERATURE[tier], max_tokens=MAX_TOKENS[tier])
        extra = os.getenv(f"{tier.upper()}_EXTRA_BODY", "").strip()
        if extra:
            kwargs["extra_body"] = json.loads(extra)
        if json_mode and _json_mode_ok.get(model, True):
            try:
                return await _client().chat.completions.create(response_format={"type": "json_object"}, **kwargs)
            except openai.BadRequestError as e:
                log.warning("%s rejected json_object mode (%s); retrying without it", model, e)
                _json_mode_ok[model] = False
        return await _client().chat.completions.create(**kwargs)

    async def call_json(self, tier: str, purpose: str, system: str, user: str, schema: type[BaseModel], mock_fn):
        started = time.perf_counter()
        if self.mock:
            await asyncio.sleep(self.mock_delay)
            result = schema.model_validate(mock_fn())
            self._record(tier, purpose, started)
            return result

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = None
        for attempt in range(2):
            try:
                resp = await self._complete(tier, messages, json_mode=True)
            except openai.OpenAIError as e:
                self._record(tier, purpose, started, error=str(e)[:200])
                raise LLMError(f"{TIER_LABELS[tier]} call failed: {e}") from e
            text = resp.choices[0].message.content or ""
            try:
                result = schema.model_validate(extract_json(text))
                self._record(tier, purpose if attempt == 0 else f"{purpose} (JSON repair)", started, resp.usage)
                return result
            except (ValueError, ValidationError) as e:
                last_error = e
                self._record(tier, f"{purpose} (invalid JSON)", started, resp.usage)
                started = time.perf_counter()
                messages = messages + [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": f"That was not valid JSON for the required schema ({str(e)[:300]}). Return only valid JSON, nothing else."},
                ]
        raise LLMError(f"{TIER_LABELS[tier]} returned invalid JSON twice: {last_error}")

    async def call_text(self, tier: str, purpose: str, system: str, user: str, mock_fn) -> str:
        started = time.perf_counter()
        if self.mock:
            await asyncio.sleep(self.mock_delay)
            self._record(tier, purpose, started)
            return mock_fn()
        try:
            resp = await self._complete(tier, [{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=False)
        except openai.OpenAIError as e:
            self._record(tier, purpose, started, error=str(e)[:200])
            raise LLMError(f"{TIER_LABELS[tier]} call failed: {e}") from e
        self._record(tier, purpose, started, resp.usage)
        text = strip_reasoning(resp.choices[0].message.content or "")
        return re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text).strip()


# ---------- canned responses for MOCK_LLM=1 ----------

MOCK_FIXABLE = {"image-alt", "button-name", "html-has-lang", "label", "select-name", "link-name", "frame-title", "color-contrast"}
_PRIORITY = {"critical": 1, "serious": 2, "moderate": 3, "minor": 4}


def _humanize(token: str) -> str:
    words = re.sub(r"[-_]+", " ", token).strip()
    words = re.sub(r"\b(button|btn|input|field)\b", "", words, flags=re.I).strip()
    return words[:1].upper() + words[1:] if words else ""


def _attr(html: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', html or "")
    return m.group(1) if m else ""


def _label_from_href(href: str) -> str:
    if href.startswith("mailto:"):
        return f"Email {href[7:]}"
    path = urlsplit(href).path.rstrip("/")
    last = path.rsplit("/", 1)[-1]
    if not last or "." in last:
        return "Home"
    return _humanize(last)


def mock_triage(rules: list[dict]) -> dict:
    items = []
    for r in rules:
        auto = r["rule_id"] in MOCK_FIXABLE
        items.append({
            "rule_id": r["rule_id"],
            "fixable": "auto" if auto else "needs_human",
            "priority": _PRIORITY.get(r.get("impact"), 3),
            "reason": "Attribute-level fix" if auto else "No canned fix in mock mode; needs a person",
        })
    return {"items": items}


def mock_fix(nodes: list[dict]) -> dict:
    patches = []
    for n in nodes:
        rule, html, ctx = n["rule"], n.get("html", ""), n.get("context", {})
        base = {"node_id": n["node_id"], "selector": n["selector"], "rule_id": rule, "action": "set_attributes", "wcag": n.get("wcag", "")}
        name = _humanize(_attr(html, "id") or _attr(html, "name"))
        if rule == "image-alt":
            alt = _humanize((ctx.get("src") or "image").rsplit(".", 1)[0])
            patches.append({**base, "attributes": {"alt": alt}, "needs_review": True,
                            "explanation": f'Adds alt="{alt}" so screen reader users hear what the image shows.'})
        elif rule == "button-name":
            label = name or "Open menu"
            patches.append({**base, "attributes": {"aria-label": label},
                            "explanation": f'Names the icon-only button "{label}" so screen readers stop announcing just "button".'})
        elif rule == "html-has-lang":
            patches.append({**base, "attributes": {"lang": "en"},
                            "explanation": "Declares the page language so screen readers use the right pronunciation."})
        elif rule in ("label", "select-name"):
            label = name or "Input"
            patches.append({**base, "attributes": {"aria-label": label},
                            "explanation": f'Gives the field the accessible name "{label}" so screen reader users know what to enter.'})
        elif rule == "link-name":
            label = _label_from_href(ctx.get("href") or _attr(html, "href"))
            if "<img" not in html and n.get("attempt", 1) == 1:
                # Canned first-attempt mistake (alt is not valid on <a>) so the retry loop is visible in mock mode.
                patches.append({**base, "attributes": {"alt": label},
                                "explanation": f'Adds alt="{label}" to the link.'})
            else:
                patches.append({**base, "attributes": {"aria-label": label},
                                "explanation": f'Names the link "{label}" so screen reader users know where it goes.'})
        elif rule == "frame-title":
            title = "Map" if "map" in html.lower() else "Embedded content"
            patches.append({**base, "attributes": {"title": title},
                            "explanation": f'Titles the iframe "{title}" so screen reader users can tell what it contains.'})
    return {"patches": patches}


def mock_report(summary: dict) -> str:
    fixed, unfixed, human = summary["fixed_count"], summary["unfixed"], summary["needs_human"]
    lines = [
        "## Summary",
        f"AccessFix scanned {len(summary['pages'])} page(s) and found {summary['issues_before']} accessibility issues.",
        f"It fixed and re-verified {fixed} of them with axe-core; the score went from {summary['score_before']} to {summary['score_after']}.",
        f"{len(human) + len(unfixed)} item(s) still need a person to look at them.",
        "",
        "## Who this helps",
        "- **Screen reader users** now hear names for images, buttons, links, and form fields.",
        "- **Low-vision users** get text that meets the WCAG 4.5:1 contrast minimum.",
        "- **Keyboard users** can tell what each focusable control does.",
        "",
        "## What changed",
    ]
    lines += [f"- {r['rule_id']}: {r['count']} fix(es)" for r in summary["fixed_by_rule"]] or ["- No changes were needed."]
    lines += ["", "## What still needs a human"]
    rest = [f"- {h['rule_id']}: {h['reason']}" for h in human] + [f"- {u['rule_id']}: {u['reason']}" for u in unfixed]
    lines += rest or ["- Nothing flagged. Still review alt text wording by hand."]
    lines += [
        "",
        "## Top 3 next steps",
        "1. Review the suggested alt text and labels, then copy the patches into your source code.",
        "2. Test the key pages with a screen reader and keyboard only.",
        "3. Add AccessFix or axe-core to CI so new issues are caught before release.",
        "",
        "_Automated checks catch only part of WCAG. These fixes reduce risk but do not guarantee compliance._",
        "",
        "_(Mock report: canned text, no model was called.)_",
    ]
    return "\n".join(lines)
