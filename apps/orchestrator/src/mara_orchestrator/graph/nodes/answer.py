"""Synthesis and verification.

The synthesizer writes the answer from worker findings and spotlighted
evidence. The verifier then checks it in two layers:

1. Code checks, which are cheap and never wrong about what they check: every
   cited id exists, an answer built on evidence cites something, and every
   "Calculation:" line adds up, using numbers that appear in the evidence.
2. A model check (mid tier) for what code cannot judge: does the evidence
   actually support each claim, and is anything required missing?

A failed check sends the draft back once with the problems listed. If it still
fails, the answer goes out with the problems attached, and no write actions are
proposed from it.
"""

from __future__ import annotations

import ast
import operator
import re

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from mara_core.logging import get_logger
from mara_core.settings import get_settings
from mara_core.spotlight import UNTRUSTED_RULE

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.state import RunState, Verdict
from mara_orchestrator.llm.cost import BudgetExceeded
from mara_orchestrator.llm.models import chat_model, structured
from mara_orchestrator.prompts import render, split
from mara_orchestrator.tools.evidence import EvidenceBuilder

log = get_logger(__name__)
CITE_RE = re.compile(r"ev_[A-Za-z0-9]+_\d+")
CALC_RE = re.compile(r"^\s*[-*]?\s*\**calculation\**\s*:\s*(.+)$", re.I | re.M)
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
MAX_EVIDENCE_CHARS = 24_000
NOT_FOUND_HINTS = (
    "could not find",
    "couldn't find",
    "no evidence",
    "not found",
    "no results",
    "unable to find",
    "did not find",
    "nothing to answer",
)


def text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
    return "".join(parts).strip()


def findings_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines.append(f"- {r['task_id']} ({r['kind']}, {r['status']}): {r['summary']}")
        lines.extend(f"    - {f}" for f in r.get("facts", [])[:8])
        lines.extend(f"    - could not get: {g}" for g in r.get("gaps", [])[:4])
    return "\n".join(lines) or "- (no findings)"


def pick_evidence(evidence: list[dict], results: list[dict], extra_text: str = "") -> list[dict]:
    """Cited evidence first, then the rest, up to a character budget."""
    cited_text = " ".join(r.get("summary", "") + " " + " ".join(r.get("facts", [])) for r in results) + extra_text
    cited = list(dict.fromkeys(CITE_RE.findall(cited_text)))
    by_id = {e["id"]: e for e in evidence}
    ordered = [by_id[i] for i in cited if i in by_id] + [e for e in evidence if e["id"] not in cited]
    picked, used = [], 0
    for item in ordered:
        size = len(item["content"]) + 120
        if used + size > MAX_EVIDENCE_CHARS and picked:
            continue
        picked.append(item)
        used += size
    return picked


async def synthesize(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    results = state.get("results") or []
    evidence = state.get("evidence") or []
    issues = state.get("verify_issues") or []
    revisions = state.get("revisions", 0) + (1 if issues else 0)
    plan = state.get("plan") or {}

    if not evidence and not any(r["status"] == "done" for r in results):
        gaps = [g for r in results for g in r.get("gaps", [])][:5]
        answer = "I could not find anything to answer this." + ("".join(f"\n- {g}" for g in gaps) if gaps else "")
        return {"answer": answer, "revisions": revisions}
    if ctx.budget.exceeded():
        answer = (
            "The run's budget ran out before a full answer could be written. Here is what the workers found:\n"
            + findings_text(results)
        )
        return {"answer": answer, "revisions": revisions, "stop_reason": state.get("stop_reason") or "budget"}

    assumptions = plan.get("assumptions") or []
    rendered, _ = render(
        "synthesizer",
        question=state["question"],
        as_of=state["as_of"],
        answer_shape=plan.get("answer_shape") or "a direct answer with citations",
        assumptions=("Assumptions made while planning: " + "; ".join(assumptions)) if assumptions else "",
        untrusted_rule=UNTRUSTED_RULE,
        findings=findings_text(results),
        evidence=EvidenceBuilder.render(pick_evidence(evidence, results)),
    )
    system, human = split(rendered, "Worker findings:")
    if issues:
        human += (
            "\n\nA checker found problems in your previous draft. Fix all of them:\n"
            + "\n".join(f"- {i}" for i in issues)
            + f"\n\nPrevious draft:\n{state.get('answer', '')}"
        )
    try:
        ctx.budget.check()
        message = await chat_model("small").ainvoke([SystemMessage(system), HumanMessage(human)])
        answer = text_of(message)
    except BudgetExceeded:
        answer = (
            "The run's budget ran out before a full answer could be written. Here is what the workers found:\n"
            + findings_text(results)
        )
        return {"answer": answer, "revisions": revisions, "stop_reason": "budget"}
    except Exception as exc:
        log.exception("synthesizer_failed")
        answer = (
            "The answer could not be written because the model call failed. What the workers found:\n"
            + findings_text(results)
        )
        return {"answer": answer, "revisions": revisions, "error": str(exc)[:300]}
    await ctx.emit("answer_draft", {"revision": revisions, "chars": len(answer)})
    return {"answer": answer or "The model returned an empty answer.", "revisions": revisions}


# ----------------------------------------------------------- code checks
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand)
    raise ValueError("unsupported expression")


def _clean_expr(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)  # citations
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # thousands separators
    text = re.sub(r"[$€£%]|\b(?:USD|EUR|GBP|ARR|k)\b", " ", text, flags=re.I)
    return re.sub(r"[^0-9.+\-*/() ]", " ", text)


def check_calculations(answer: str, evidence_text: str) -> list[str]:
    issues = []
    evidence_numbers = {n.replace(",", "") for n in NUMBER_RE.findall(evidence_text)}
    for line in CALC_RE.findall(answer):
        if "=" not in line:
            continue
        left, _, right = line.rpartition("=")
        try:
            lhs = _eval(ast.parse(_clean_expr(left).strip(), mode="eval"))
            rhs_numbers = NUMBER_RE.findall(_clean_expr(right))
            rhs = float(rhs_numbers[0].replace(",", "")) if rhs_numbers else None
        except (SyntaxError, ValueError, ZeroDivisionError):
            continue  # not an expression we can check; the model check still reads it
        if rhs is None:
            continue
        if abs(lhs - rhs) > max(0.01, abs(rhs) * 0.005):
            issues.append(f"calculation is wrong: '{line.strip()[:120]}' (the left side is {lhs:,.2f})")
        for number in NUMBER_RE.findall(_clean_expr(left)):
            plain = number.replace(",", "")
            if (
                plain not in evidence_numbers
                and plain.rstrip("0").rstrip(".") not in evidence_numbers
                and abs(float(plain)) >= 10
            ):
                issues.append(f"calculation uses {number}, which does not appear in the evidence")
    return issues


def code_checks(answer: str, evidence: list[dict]) -> list[str]:
    by_id = {e["id"]: e for e in evidence}
    cited = list(dict.fromkeys(CITE_RE.findall(answer)))
    issues = []
    unknown = [i for i in cited if i not in by_id]
    if unknown:
        issues.append(f"cites evidence that does not exist: {', '.join(unknown[:5])}")
    if evidence and not cited and not any(h in answer.lower() for h in NOT_FOUND_HINTS):
        issues.append("no claim is cited: put evidence ids like [ev_t1_1] after each fact")
    evidence_text = " ".join(by_id[i]["content"] for i in cited if i in by_id) or " ".join(
        e["content"] for e in evidence
    )
    issues += check_calculations(answer, evidence_text)
    return issues


async def verify(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    answer = state.get("answer") or ""
    evidence = state.get("evidence") or []
    issues = code_checks(answer, evidence)
    checked_by = ["code"]
    model_verdict: Verdict | None = None
    if not issues and evidence and not state.get("stop_reason") and not ctx.budget.exceeded():
        by_id = {e["id"]: e for e in evidence}
        cited = [by_id[i] for i in dict.fromkeys(CITE_RE.findall(answer)) if i in by_id] or evidence[:8]
        plan = state.get("plan") or {}
        rendered, _ = render(
            "verifier",
            question=state["question"],
            answer_shape=plan.get("answer_shape") or "a direct answer with citations",
            untrusted_rule=UNTRUSTED_RULE,
            answer=answer,
            evidence=EvidenceBuilder.render(cited),
        )
        system, human = split(rendered, "Draft:")
        try:
            model_verdict = await structured("mid", Verdict).ainvoke([SystemMessage(system), HumanMessage(human)])
            checked_by.append("model")
        except BudgetExceeded:
            pass
        except Exception:
            log.exception("verifier_failed")  # code checks passed; do not block the answer on a verifier outage
    if model_verdict is not None and not model_verdict.passed:
        issues += model_verdict.issues or ["the verifier rejected the draft without details"]
    passed = not issues
    verdict = {"passed": passed, "issues": issues[:8], "checked_by": checked_by}
    await ctx.emit("verification", verdict)
    return {"verdict": verdict, "verify_issues": [] if passed else issues[:8]}


def route_after_verify(state: RunState) -> str:
    verdict = state.get("verdict") or {}
    if (
        not verdict.get("passed")
        and state.get("revisions", 0) < get_settings().max_revisions
        and not state.get("stop_reason")
    ):
        return "synthesize"
    plan = state.get("plan") or {}
    guard = state.get("guard") or {}
    if (
        plan.get("needs_action")
        and guard.get("actions_allowed")
        and verdict.get("passed")
        and not state.get("stop_reason")
    ):
        return "propose"
    return "finalize"
