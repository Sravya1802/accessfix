"""The agent loop: discover → scan → triage → research → write fixes → verify with axe → retry → report."""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from . import prompts, scoring, security
from .llm import LLM, LLMError, mock_fix, mock_report, mock_triage
from .patches import Patch, contrast_ratio, fix_contrast, parse_ratio, sanitize
from .research import Research, choose_pages
from .scanner import Scanner

log = logging.getLogger("accessfix.pipeline")

BATCH_SIZE = 8
DISCLAIMER = "_Automated checks catch only part of WCAG. These fixes reduce risk but do not guarantee compliance._"


@dataclass
class Settings:
    max_pages: int = 3
    max_nodes: int = 30
    max_retries: int = 2

    @classmethod
    def from_env(cls):
        return cls(int(os.getenv("MAX_PAGES", 3)), int(os.getenv("MAX_NODES", 30)), int(os.getenv("MAX_RETRIES", 2)))


class TriageItem(BaseModel):
    rule_id: str
    fixable: Literal["auto", "needs_human"] = "auto"
    priority: int = 3
    reason: str = ""


class TriageOut(BaseModel):
    items: list[TriageItem]


class FixOut(BaseModel):
    patches: list[dict]  # validated one by one so a single bad patch doesn't sink the batch


class PipelineError(RuntimeError):
    pass


def step(id, label, status, detail="", tier=None) -> dict:
    return {"type": "step", "id": id, "label": label, "status": status, "detail": detail, "tier": tier}


def note(message, **data) -> dict:
    return {"type": "log", "message": message, **({"data": data} if data else {})}


def wcag_criterion(tags: list[str]) -> str:
    for t in tags:
        m = re.fullmatch(r"wcag(\d)(\d)(\d+)", t)
        if m:
            return ".".join(m.groups())
    return ""


def _avg(values) -> int:
    values = list(values)
    return round(sum(values) / len(values)) if values else 100


class AgentRun:
    def __init__(self, url: str, scanner: Scanner, llm: LLM, research: Research, cfg: Settings):
        self.url, self.scanner, self.llm, self.research, self.cfg = url, scanner, llm, research, cfg
        self.pages: list[dict] = []
        self.nodes: dict[str, dict] = {}
        self.sources: dict[str, list[dict]] = {}
        self.tool_calls: list[dict] = []

    # ---------- helpers ----------

    async def _timed_scan(self, page_url, label, **kwargs):
        started = time.perf_counter()
        try:
            return await self.scanner.scan(page_url, **kwargs)
        finally:
            self.tool_calls.append({"tool": "axe-core scan", "detail": f"{label}: {page_url}",
                                    "latency_ms": round((time.perf_counter() - started) * 1000)})

    def _rule_summary(self) -> list[dict]:
        rules = {}
        for n in self.nodes.values():
            r = rules.setdefault(n["rule"], {"rule_id": n["rule"], "impact": n["impact"], "help": n["help"], "wcag": n["wcag"],
                                             "count": 0, "sample_html": n["html"][:200], "sample_failure": n["failureSummary"][:200]})
            r["count"] += 1
        return list(rules.values())

    def _contrast_patch(self, n: dict, attempt: int) -> tuple[Patch | None, str | None]:
        data = n.get("last_failure_data") or n["data"]
        d = next((x for x in data if isinstance(x, dict) and x.get("fgColor") and x.get("bgColor")), None)
        if not d:
            return None, "axe didn't report foreground/background colors for this node"
        target = parse_ratio(d.get("expectedContrastRatio")) + 0.5 * (attempt - 1)
        new = fix_contrast(d["fgColor"], d["bgColor"], target)
        after = contrast_ratio(new, d["bgColor"])
        return Patch(
            node_id=n["node_id"], selector=n["selector"], rule_id="color-contrast", action="set_style", style={"color": new},
            explanation=f"Changes text color {d['fgColor']} → {new} on {d['bgColor']}, raising contrast from "
                        f"{d.get('contrastRatio')}:1 to {after:.2f}:1 so low-vision readers can read it.",
            wcag=n["wcag"] or "1.4.3",
        ), None

    async def write_fixes(self, targets: list[dict], attempt: int) -> tuple[dict[str, Patch], dict[str, str]]:
        """Return ({node_id: patch}, {node_id: failure_reason}) for the given nodes."""
        patches, failures = {}, {}
        contrast = [n for n in targets if n["rule"] == "color-contrast"]
        others = [n for n in targets if n["rule"] != "color-contrast"]

        if contrast:
            started = time.perf_counter()
            for n in contrast:
                p, why = self._contrast_patch(n, attempt)
                if p:
                    patches[n["node_id"]] = p
                else:
                    failures[n["node_id"]] = why
            self.tool_calls.append({"tool": "Contrast tool", "detail": f"{len(contrast)} color-contrast node(s), attempt {attempt}",
                                    "latency_ms": round((time.perf_counter() - started) * 1000)})

        by_rule = defaultdict(list)
        for n in others:
            by_rule[n["rule"]].append(n)
        batches = [(rule, ns[i:i + BATCH_SIZE]) for rule, ns in by_rule.items() for i in range(0, len(ns), BATCH_SIZE)]
        sem = asyncio.Semaphore(3)

        async def run_batch(rule, batch):
            async with sem:
                return rule, batch, await self._fix_batch(rule, batch, attempt)

        for result in await asyncio.gather(*(run_batch(r, b) for r, b in batches), return_exceptions=True):
            if isinstance(result, Exception):
                log.warning("fix batch failed: %s", result)
                continue
            rule, batch, (got, bad) = result
            patches.update(got)
            failures.update(bad)
        for n in targets:
            if n["node_id"] not in patches and n["node_id"] not in failures:
                failures[n["node_id"]] = "The model didn't return a usable patch for this node"
        return patches, failures

    async def _fix_batch(self, rule: str, batch: list[dict], attempt: int):
        first = batch[0]
        brief = []
        for n in batch:
            item = {"node_id": n["node_id"], "selector": n["selector"], "html": n["html"][:600],
                    "failureSummary": n["failureSummary"][:500], "context": n["context"]}
            if attempt > 1:
                item["previous_patch"] = n.get("last_patch")
                item["new_failure"] = n.get("last_failure", "")[:500]
            brief.append(item)
        payload = {"rule": rule, "help": first["help"], "wcag": first["wcag"],
                   "guidance": [{"title": s["title"], "snippet": s["content"]} for s in self.sources.get(rule, [])],
                   "nodes": brief}
        system = prompts.FIX_SYSTEM + ("\n\n" + prompts.RETRY_NOTE if attempt > 1 else "")
        purpose = f"{'Retry' if attempt > 1 else 'Fix'} {len(batch)}× {rule}" + (f" (attempt {attempt})" if attempt > 1 else "")
        out = await self.llm.call_json("super", purpose, system, "Write patches for these failing nodes:\n" + json.dumps(payload, indent=1),
                                       FixOut, lambda: mock_fix([{**n, "attempt": attempt} for n in batch]))

        by_id = {n["node_id"]: n for n in batch}
        patches, failures = {}, {}
        for raw in out.patches:
            try:
                p = Patch.model_validate(raw)
            except ValidationError as e:
                log.info("invalid patch from model: %s", e)
                continue
            if not p.node_id and len(batch) == 1:
                p.node_id = first["node_id"]
            node = by_id.get(p.node_id)
            if not node or p.node_id in patches:
                continue
            p.rule_id = rule
            p.selector = p.selector or node["selector"]
            p.wcag = p.wcag or node["wcag"]
            if rule == "image-alt" or "alt" in p.attributes:
                p.needs_review = True
            clean, why = sanitize(p)
            if clean:
                patches[p.node_id] = clean
                failures.pop(p.node_id, None)
            else:
                failures[p.node_id] = f"Sanitizer rejected the patch: {why}"
                node["last_patch"] = p.model_dump(include={"selector", "action", "attributes", "remove_attributes", "style", "html"})
                node["last_failure"] = failures[p.node_id]
        return patches, failures

    async def verify(self, round_patches: dict[str, Patch]) -> dict:
        """Apply patches on fresh loads, re-run axe, and decide which nodes are fixed."""
        by_page = defaultdict(dict)
        for nid, p in round_patches.items():
            by_page[self.nodes[nid]["page"]][nid] = p

        async def scan_page(pi):
            page = self.pages[pi]
            marks = [{"node_id": n["node_id"], "selector": n["selector"]} for n in self.nodes.values()
                     if n["page"] == pi and n["selector"]]
            payload = [p.model_dump() for p in by_page[pi].values()]
            return pi, await self._timed_scan(page["url"], "verify", marks=marks, patches=payload)

        scans = {}
        for res in await asyncio.gather(*(scan_page(pi) for pi in by_page), return_exceptions=True):
            if isinstance(res, Exception):
                raise PipelineError(f"Verification scan failed: {res}")
            scans[res[0]] = res[1]

        fixed, failing, regressions = {}, {}, []
        for pi, scan in scans.items():
            applied = {a["node_id"]: a for a in scan["applied"]}
            after_by_rule = defaultdict(list)
            for v in scan["violations"]:
                for vn in v["nodes"]:
                    after_by_rule[v["id"]].append({**vn, "rule": v["id"], "help": v["help"]})

            for nid, p in by_page[pi].items():
                node = self.nodes[nid]
                a = applied.get(nid)
                if not a or not a["applied"]:
                    failing[nid] = {"reason": f"Patch couldn't be applied: {(a or {}).get('error') or 'unknown error'}"}
                    continue
                still = [vn for vn in after_by_rule.get(node["rule"], [])
                         if nid in vn["afx"] or (not vn["afx"] and vn["target"] == node["target"])]
                if still:
                    failing[nid] = {"reason": still[0]["failureSummary"] or "Still fails axe", "data": still[0]["data"], "applied": a}
                else:
                    fixed[nid] = a

            # Regression check: any violation that wasn't there before is blamed on the patch on (or around) its element.
            originals = defaultdict(set)
            original_targets = defaultdict(list)
            for n in self.nodes.values():
                if n["page"] == pi:
                    originals[n["rule"]].add(n["node_id"])
                    original_targets[n["rule"]].append(n["target"])
            for rule, vns in after_by_rule.items():
                for vn in vns:
                    if set(vn["afx"]) & originals[rule] or vn["target"] in original_targets[rule]:
                        continue
                    culprits = [x for x in vn["afx"] + vn["afx_anc"] if x in by_page[pi]]
                    regressions.append({"rule": rule, "target": vn["target"], "culprits": culprits})
                    for c in culprits:
                        fixed.pop(c, None)
                        failing[c] = {"reason": f"This patch caused a new '{rule}' violation: {vn['failureSummary'][:300]}"}
        return {"fixed": fixed, "failing": failing, "regressions": regressions, "scans": scans}

    def page_after(self, scans: dict) -> None:
        for pi, page in enumerate(self.pages):
            scan = scans.get(pi)
            if scan:
                page["score_after"] = scoring.score(scan["violations"])
                page["issues_after"] = scoring.issue_count(scan["violations"])
                page["screenshot_after"] = scan["screenshot"]
            else:
                page["score_after"], page["issues_after"] = page["score_before"], page["issues_before"]
                page["screenshot_after"] = page["screenshot_before"]

    # ---------- the loop ----------

    async def events(self):
        t0 = time.perf_counter()
        cfg = self.cfg

        # Step 1: discover pages with Tavily Map
        yield step("discover", "Discover pages", "running", "Tavily Map is looking for key user-facing pages", "tavily")
        discovered = []
        try:
            discovered = await self.research.map(self.url)
        except Exception as e:
            yield note(f"Tavily Map couldn't crawl this URL ({str(e)[:120]}); scanning just the input page.")
        candidates = choose_pages(self.url, discovered, cfg.max_pages)
        chosen = [candidates[0]] + [u for u in candidates[1:] if await security.url_allowed(u)]
        yield step("discover", "Discover pages", "done",
                   f"Tavily Map found {len(discovered)} URL(s); scanning {len(chosen)} unique page(s)", "tavily")
        yield note("Pages chosen", discovered=discovered[:20], chosen=chosen)

        # Step 2: scan "before"
        yield step("scan", "Scan with axe-core", "running", f"Loading {len(chosen)} page(s) in Chromium and running axe-core")
        results = await asyncio.gather(*(self._timed_scan(u, "before") for u in chosen), return_exceptions=True)
        for u, r in zip(chosen, results):
            if isinstance(r, Exception):
                if not self.pages and u == chosen[0]:
                    raise PipelineError(str(r))
                yield note(f"Skipped {u}: {r}")
                continue
            pi = len(self.pages)
            self.pages.append({"index": pi, "url": u, "title": r["title"], "violations_before": r["violations"],
                               "score_before": scoring.score(r["violations"]), "issues_before": scoring.issue_count(r["violations"]),
                               "screenshot_before": r["screenshot"]})
            for v in r["violations"]:
                for ni, vn in enumerate(v["nodes"]):
                    nid = f"p{pi}-{v['id']}-{ni}"
                    simple = len(vn["target"]) == 1 and isinstance(vn["target"][0], str)
                    self.nodes[nid] = {
                        "node_id": nid, "page": pi, "page_url": u, "rule": v["id"], "impact": vn.get("impact") or v["impact"],
                        "help": v["help"], "helpUrl": v["helpUrl"], "wcag": wcag_criterion(v["tags"]),
                        "selector": vn["target"][0] if simple else None, "target": vn["target"], "html": vn["html"],
                        "failureSummary": vn.get("failureSummary") or "", "data": vn["data"], "context": vn["context"],
                        "status": "pending", "attempt": 0, "reason": "",
                    }
        issues_before = sum(p["issues_before"] for p in self.pages)
        score_before = _avg(p["score_before"] for p in self.pages)
        yield step("scan", "Scan with axe-core", "done",
                   f"{issues_before} issues across {len(self._rule_summary())} rule types · score {score_before}/100")

        # Step 3: triage with Nemotron Nano
        rules = self._rule_summary()
        verdicts: dict[str, TriageItem] = {}
        if rules:
            yield step("triage", "Triage issues", "running", f"Classifying {len(rules)} rule types: auto-fix or needs a human", "nano")
            try:
                tri = await self.llm.call_json("nano", f"Triage {len(rules)} rules", prompts.TRIAGE_SYSTEM,
                                               json.dumps({"violations": rules}, indent=1), TriageOut, lambda: mock_triage(rules))
                verdicts = {t.rule_id: t for t in tri.items}
            except LLMError as e:
                yield note(f"Triage failed ({str(e)[:160]}); treating every rule as auto-fixable.")
        fixable = []
        for n in self.nodes.values():
            verdict = verdicts.get(n["rule"])
            if not n["selector"]:
                n["status"], n["reason"] = "needs_human", "Inside an iframe or shadow DOM, so it can't be patched from the top-level page"
            elif n["rule"] == "color-contrast" and any(isinstance(d, dict) and d.get("bgColor") for d in n["data"]):
                fixable.append((n, 1))  # solid background: the deterministic contrast tool handles it
            elif verdict and verdict.fixable == "needs_human":
                n["status"], n["reason"] = "needs_human", verdict.reason or "Needs human judgment"
            else:
                fixable.append((n, verdict.priority if verdict else 3))
        fixable.sort(key=lambda x: (scoring.IMPACT_RANK.get(x[0]["impact"], 4), x[1]))
        selected = [n for n, _ in fixable[:cfg.max_nodes]]
        for n, _ in fixable[cfg.max_nodes:]:
            n["status"], n["reason"] = "skipped", f"Over the {cfg.max_nodes}-node limit for one run"
        human = [n for n in self.nodes.values() if n["status"] == "needs_human"]
        if rules:
            yield step("triage", "Triage issues", "done", f"{len(selected)} to auto-fix · {len(human)} need a human", "nano")

        # Step 4: research with Tavily Search
        if selected:
            rule_ids = list(dict.fromkeys(n["rule"] for n in selected))
            yield step("research", "Research fixes", "running", f"Tavily Search: current fix guidance for {len(rule_ids)} rule(s)", "tavily")
            sem = asyncio.Semaphore(4)
            sample = {n["rule"]: n for n in selected}

            async def one(rid):
                async with sem:
                    return rid, await self.research.search(rid, sample[rid]["help"], sample[rid]["wcag"])

            self.sources = dict(await asyncio.gather(*(one(r) for r in rule_ids)))
            total = sum(len(s) for s in self.sources.values())
            yield step("research", "Research fixes", "done", f"{total} source(s) found for {len(rule_ids)} rule(s)", "tavily")

        # Steps 5-7: write → verify → retry
        verified: dict[str, Patch] = {}
        last_scans: dict = {}
        last_round_clean = True
        if selected:
            n_contrast = sum(1 for n in selected if n["rule"] == "color-contrast")
            yield step("fix", "Write fixes", "running",
                       f"Nemotron Super is writing {len(selected) - n_contrast} patch(es); the contrast tool is fixing {n_contrast}", "super")
            new_patches, write_failures = await self.write_fixes(selected, attempt=1)
            for n in selected:
                n["attempt"] = 1
            yield step("fix", "Write fixes", "done",
                       f"{len(new_patches)} patch(es) written" + (f", {len(write_failures)} unusable" if write_failures else ""), "super")

            attempt = 1
            while True:
                round_patches = {**verified, **new_patches}
                vid = f"verify-{attempt}"
                yield step(vid, f"Verify with axe (attempt {attempt})", "running",
                           f"Fresh page load, applying {len(round_patches)} patch(es), re-running axe-core")
                outcome = await self.verify(round_patches) if round_patches else {"fixed": {}, "failing": {}, "regressions": [], "scans": {}}
                last_scans = outcome["scans"]

                failing_now = {}
                for nid, why in write_failures.items():
                    failing_now[nid] = {"reason": why}
                newly_fixed = []
                for nid, info in outcome["failing"].items():
                    failing_now[nid] = info
                    verified.pop(nid, None)
                for nid, applied in outcome["fixed"].items():
                    node = self.nodes[nid]
                    node["before_html"], node["after_html"] = applied["before_html"], applied["after_html"]
                    if nid in new_patches:
                        verified[nid] = new_patches[nid]
                        node["status"], node["fixed_attempt"] = "fixed", node["attempt"]
                        newly_fixed.append(nid)
                for nid, info in failing_now.items():
                    node = self.nodes[nid]
                    node["status"], node["reason"] = "failed", info["reason"]
                    node["last_failure"], node["last_failure_data"] = info["reason"], info.get("data") or []
                    p = round_patches.get(nid) or new_patches.get(nid)
                    if p:
                        node["last_patch"] = p.model_dump(include={"selector", "action", "attributes", "remove_attributes", "style", "html"})
                        node["failed_patch"] = p
                        if info.get("applied"):
                            node["before_html"], node["after_html"] = info["applied"]["before_html"], info["applied"]["after_html"]
                for r in outcome["regressions"]:
                    yield note(f"Regression: a patch introduced a new '{r['rule']}' violation; dropping it and retrying.", **r)
                for nid in newly_fixed:
                    yield {"type": "fix", "patch": self._patch_record(nid, verified[nid])}

                if attempt == 1:
                    detail = f"{len(newly_fixed)} of {len(new_patches) + len(write_failures)} verified fixed by axe"
                else:
                    detail = f"Attempt {attempt}: {len(new_patches) + len(write_failures)} still failing → {len(newly_fixed)} fixed"
                if failing_now:
                    detail += f" · {len(failing_now)} still failing"
                yield step(vid, f"Verify with axe (attempt {attempt})", "done", detail)

                last_round_clean = not failing_now
                if not failing_now or attempt > cfg.max_retries:
                    break

                # Step 7: retry the failures with a different fix
                retry_nodes = [self.nodes[nid] for nid in failing_now]
                rid = f"retry-{attempt}"
                yield step(rid, f"Retry round {attempt}", "running",
                           f"{len(retry_nodes)} still failing → asking Nemotron Super for a different fix", "super")
                attempt += 1
                for n in retry_nodes:
                    n["attempt"] = attempt
                new_patches, write_failures = await self.write_fixes(retry_nodes, attempt=attempt)
                yield step(rid, f"Retry round {attempt - 1}", "done", f"{len(new_patches)} new patch(es) written", "super")

            if not last_round_clean:
                # The last round included patches that failed; re-verify with only the verified set so "after" is exact.
                yield step("final", "Final check", "running", f"Re-running axe with only the {len(verified)} verified patch(es)")
                outcome = await self.verify(verified) if verified else {"fixed": {}, "failing": {}, "scans": {}}
                last_scans = outcome["scans"]
                for nid in outcome["failing"]:
                    verified.pop(nid, None)
                    self.nodes[nid]["status"] = "failed"
                    self.nodes[nid]["reason"] = outcome["failing"][nid]["reason"]
                yield step("final", "Final check", "done", f"{len(verified)} patch(es) confirmed together")

        self.page_after(last_scans)
        score_after = _avg(p["score_after"] for p in self.pages)
        issues_after = sum(p["issues_after"] for p in self.pages)

        # Step 8: report with Nemotron Ultra
        patches_out = [self._patch_record(nid, p) for nid, p in verified.items()]
        unfixed = [n for n in self.nodes.values() if n["status"] in ("failed", "skipped")]
        patches_out += [self._patch_record(n["node_id"], n["failed_patch"], verified=False)
                        for n in unfixed if n.get("failed_patch")]
        fixed_by_rule = defaultdict(int)
        for p in patches_out:
            if p["verified"]:
                fixed_by_rule[p["rule_id"]] += 1
        summary = {
            "pages": [{k: p[k] for k in ("url", "score_before", "score_after", "issues_before", "issues_after")} for p in self.pages],
            "score_before": score_before, "score_after": score_after,
            "issues_before": issues_before, "issues_after": issues_after,
            "fixed_count": len(verified),
            "fixed_by_rule": [{"rule_id": r, "count": c} for r, c in fixed_by_rule.items()],
            "unfixed": [{"rule_id": n["rule"], "selector": n["selector"], "reason": n["reason"][:200]} for n in unfixed],
            "needs_human": [{"rule_id": n["rule"], "reason": n["reason"]} for n in human],
            "alt_text_needs_review": sum(1 for p in patches_out if p["verified"] and p["needs_review"]),
        }
        yield step("report", "Write report", "running", "Nemotron Ultra is writing the site-owner report", "ultra")
        try:
            report = await self.llm.call_text("ultra", "Final report", prompts.REPORT_SYSTEM,
                                              json.dumps(summary, indent=1), lambda: mock_report(summary))
            if "guarantee" not in report.lower():
                report += "\n\n" + DISCLAIMER
            yield step("report", "Write report", "done", f"{len(report.split())} words", "ultra")
        except LLMError as e:
            report = mock_report(summary).replace("_(Mock report: canned text, no model was called.)_",
                                                  "_(Nemotron Ultra was unavailable, so this report was generated from a template.)_")
            yield step("report", "Write report", "error", f"Ultra unavailable ({str(e)[:100]}); used a template", "ultra")

        # Step 9: result
        yield {
            "type": "result",
            "url": self.url,
            "score_before": score_before, "score_after": score_after,
            "issues_before": issues_before, "issues_after": issues_after,
            "patches": patches_out,
            "needs_human": [{"node_id": n["node_id"], "rule_id": n["rule"], "help": n["help"], "helpUrl": n["helpUrl"],
                             "wcag": n["wcag"], "target": n["target"], "html": n["html"], "reason": n["reason"],
                             "page_url": n["page_url"]} for n in human],
            "unfixed": [{"node_id": n["node_id"], "rule_id": n["rule"], "help": n["help"], "selector": n["selector"],
                         "html": n["html"], "reason": n["reason"], "attempts": n["attempt"]} for n in unfixed],
            "report_md": report,
            "model_calls": self.llm.calls,
            "tool_calls": self.research.calls + self.tool_calls,
            "pages": [{k: v for k, v in p.items() if k != "violations_before"} for p in self.pages],
            "mock": {"llm": self.llm.mock, "tavily": self.research.mock},
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    def _patch_record(self, nid: str, p: Patch, verified: bool = True) -> dict:
        n = self.nodes[nid]
        return {
            **p.model_dump(),
            "page_url": n["page_url"], "help": n["help"], "helpUrl": n["helpUrl"], "impact": n["impact"],
            "verified": verified, "attempt": n.get("fixed_attempt") if verified else n["attempt"],
            "reason": "" if verified else n["reason"],
            "before_html": n.get("before_html") or n["html"], "after_html": n.get("after_html", ""),
            "sources": [{"title": s["title"], "url": s["url"]} for s in self.sources.get(n["rule"], [])],
        }


async def run_pipeline(url: str, scanner: Scanner, llm: LLM | None = None, research: Research | None = None,
                       cfg: Settings | None = None):
    run = AgentRun(url, scanner, llm or LLM(), research or Research(), cfg or Settings.from_env())
    try:
        async for event in run.events():
            yield event
    except Exception as e:
        log.exception("pipeline failed")
        yield {"type": "error", "message": str(e)[:500]}
