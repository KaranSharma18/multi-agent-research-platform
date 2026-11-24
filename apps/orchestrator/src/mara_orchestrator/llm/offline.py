"""A deterministic, rule-based stand-in for Gemini.

`LLM_PROVIDER=offline` makes every "model" call go here. It plans with keyword
rules, calls the first sensible tool, writes summaries from tool results and
passes verification when citations exist. The answers are crude; that is
fine. Its job is to exercise the real graph, tokens, MCP servers, executor and
approval flow end to end with no API key and no cost, in CI and on a fresh
Windows install. Never use it to judge answer quality.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import date, timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool

EV_RE = re.compile(r'id="(ev_[A-Za-z0-9_]+)"')
BLOCK_RE = re.compile(r'<untrusted id="(ev_[A-Za-z0-9_]+)"([^>]*)>(.*?)</untrusted>', re.S)


def _content(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in message.content)


def _text(messages: list[BaseMessage]) -> str:
    return "\n".join(_content(m) for m in messages)


def _digest(body: str) -> str:
    """A one-line summary of one evidence block, good enough to read."""
    body = html.unescape(body).strip()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("incidents"), list):
            return (
                "; ".join(
                    f"{i['id']} ({i['service']}, {i['severity']}): {i['title']}, started {i['started_at'][:10]}"
                    for i in data["incidents"][:6]
                )
                or "no incidents in that window"
            )
        if "customers_affected" in data:
            names = ", ".join(c["name"] for c in data["customers_affected"][:12])
            return f"{data.get('id')} ({data.get('service')}, {data.get('severity')}) affected {names}"
        if isinstance(data.get("items"), list):
            return (
                "; ".join(
                    f"{i.get('repo', '')}#{i.get('number')} {i.get('title')} ({i.get('state')})"
                    for i in data["items"][:5]
                )
                or "no matching issues"
            )
        return json.dumps(data)[:220]
    if body.startswith("SQL:"):
        rows = body.splitlines()[1:7]
        return "warehouse rows: " + " / ".join(r.strip() for r in rows)
    cause = re.search(r"root cause was ([^\n]+?)\.(?:\s|$)", body, re.I)
    if cause:
        return "Root cause: " + cause.group(1)
    text = re.sub(r"[#*`>-]+", " ", body)
    return " ".join(text.split())[:220]


def _field(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else default


def _as_of(text: str) -> date:
    raw = _field(r"Today is (\d{4}-\d{2}-\d{2})", text)
    return date.fromisoformat(raw) if raw else date.today()


def _question(text: str) -> str:
    return _field(r"(?:Question|User request):\s*(.+)", text) or text.strip().splitlines()[-1]


class OfflineChatModel(BaseChatModel):
    tier: str = "small"
    tools: list[dict] = []

    @property
    def _llm_type(self) -> str:
        return "offline-heuristic"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> OfflineChatModel:  # type: ignore[override]
        return self.model_copy(update={"tools": [convert_to_openai_tool(t) for t in tools]})

    def with_structured_output(self, schema: Any, **kwargs: Any):  # type: ignore[override]
        return RunnableLambda(lambda value: self._structured(schema, value))

    # --------------------------------------------------------------- generate
    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        names = {t["function"]["name"] for t in self.tools}
        message = self._agent_step(messages, names) if names else self._write_answer(messages)
        message.response_metadata = {"model_name": "offline-heuristic"}
        message.usage_metadata = {
            "input_tokens": len(_text(messages)) // 4,
            "output_tokens": len(str(message.content)) // 4 + 20,
            "total_tokens": 0,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _call(self, name: str, args: dict) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}])

    def _agent_step(self, messages: list[BaseMessage], names: set[str]) -> AIMessage:
        text = _text(messages)
        goal = _field(r"Task:\s*(.+)", text, _question(text))
        inputs = _field(r"Inputs:\s*(.+)", text, "{}")
        today = _as_of(text)
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        evidence_results = [m for m in tool_results if m.name != "find_tools"]

        if evidence_results or len(tool_results) >= 3:
            facts = []
            for m in evidence_results:
                for ev_id, attrs, body in BLOCK_RE.findall(_content(m))[:4]:
                    if "possible-injection" not in attrs:  # even the stand-in model skips flagged text
                        facts.append(f"{_digest(body)} [{ev_id}]")
            errors = [_content(m)[:160] for m in evidence_results if getattr(m, "status", "") == "error"]
            summary = " ".join(facts[:2])[:600] or "No evidence was returned."
            return self._call(
                "WorkerAnswer",
                {"summary": summary, "facts": facts[:5], "gaps": errors or ([] if facts else ["no evidence returned"])},
            )

        if "run_sql" in names:
            return self._call("run_sql", {"sql": self._sql_for(goal + " " + inputs, today)})
        fx = re.search(r"exchange rate|\bfx\b|usd to (\w{3})|currency", goal, re.I)
        if fx:
            quote = (re.search(r"\b(EUR|GBP|JPY|INR|CAD|AUD)\b", goal.upper()) or re.search("(EUR)", "EUR")).group(1)
            if "get_fx_rate" in names:
                return self._call("get_fx_rate", {"base": "USD", "quote": quote})
            if "find_tools" in names and not tool_results:
                return self._call("find_tools", {"need": "currency exchange rate"})
        if "search_docs" in names:
            return self._call("search_docs", {"query": goal[:200]})
        incident = _field(r"(INC-\d+)", goal + inputs)
        if incident:
            if "get_incident" in names:
                return self._call("get_incident", {"incident_id": incident})
            if "find_tools" in names and not tool_results:
                return self._call("find_tools", {"need": "details of one incident including affected customers"})
        about_incidents = re.search(r"incident|outage|down|status", goal, re.I)
        if re.search(r"issue|github|pull request", goal, re.I) and not about_incidents and "search_issues" in names:
            words = [
                w
                for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", goal)
                if w.lower()
                not in {"find", "list", "open", "github", "issues", "issue", "that", "mention", "with", "from", "their"}
            ]
            return self._call("search_issues", {"query": " ".join(words[:2]), "state": "all"})
        if "list_incidents" in names:
            since = _field(r'"since":\s*"([^"]+)"', inputs, (today - timedelta(days=7)).isoformat())
            until = _field(r'"until":\s*"([^"]+)"', inputs, (today - timedelta(days=1)).isoformat())
            service = _field(
                r"(payments-api|checkout|auth-service|api-gateway|search|notifications|billing-worker|reporting)", goal
            )
            args = {"since": since, "until": until}
            if service:
                args["service"] = service
            return self._call("list_incidents", args)
        return self._call("WorkerAnswer", {"summary": "No suitable tool for this task.", "gaps": ["no tool"]})

    @staticmethod
    def _sql_for(goal: str, today: date) -> str:
        g = goal.lower()
        since = _field(r'"since":\s*"(\d{4}-\d{2}-\d{2})', goal, (today - timedelta(days=7)).isoformat())
        until = _field(r'"until":\s*"(\d{4}-\d{2}-\d{2})', goal, today.isoformat())
        segment = next((s for s in ("enterprise", "mid-market", "smb") if s in g), None)
        region = next((r for r in ("na", "emea", "apac") if re.search(rf"\b{r}\b", g)), None)
        where = " and ".join(
            ([f"segment = '{segment}'"] if segment else []) + ([f"region = '{region}'"] if region else [])
        )
        where = f" where {where}" if where else ""
        if "globex" in g:
            return "select id, name, segment, region, arr_usd, owner_github from analytics.customers where name ilike '%globex%'"
        inc = re.search(r"(INC-\d+)", goal)
        if inc and ("arr" in g or "how many" in g):
            return (
                "select count(distinct c.id) as customers, sum(c.arr_usd) as total_arr from analytics.customers c "
                f"where c.id in (select customer_id from analytics.incident_impacts where incident_id = '{inc.group(1)}')"
            )
        if "churn" in g and "total" in g:
            return "select count(*) as churned from analytics.subscriptions where status = 'churned'"
        if "how many" in g and "customer" in g and "churn" not in g:
            return f"select count(*) as customers from analytics.customers{where}"
        if "average" in g and "arr" in g:
            return f"select round(avg(arr_usd), 2) as average_arr from analytics.customers{where}"
        if "total arr" in g and not re.search(r"affected|hit|incident", g):
            return f"select sum(arr_usd) as total_arr from analytics.customers{where}"
        if "churn" in g:
            day = (today - timedelta(days=1)).isoformat()
            return f"select count(*) as churned from analytics.subscriptions where status = 'churned' and churned_at = '{day}'"
        if "arr" in g and ("incident" in g or "affected" in g or "hit" in g or "outage" in g):
            hit = (
                "select distinct i.customer_id from analytics.incident_impacts i join analytics.incidents n on "
                f"n.id = i.incident_id where n.started_at >= '{since}' and n.started_at < '{until}'"
                + (" and n.service = 'payments-api'" if "payment" in g else "")
            )
            seg = " and c.segment = 'enterprise'" if "enterprise" in g else ""
            return (
                f"select c.name, c.arr_usd, c.owner_github, sum(c.arr_usd) over () as total_arr "
                f"from analytics.customers c where c.id in ({hit}){seg} order by c.arr_usd desc"
            )
        if "revenue" in g or "invoice" in g:
            q_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
            prev_end = q_start
            prev_start = date(
                prev_end.year if prev_end.month > 3 else prev_end.year - 1, ((prev_end.month - 4) % 12) + 1, 1
            )
            return (
                f"select sum(amount_usd) as revenue from analytics.invoices where issued_at >= '{prev_start}' "
                f"and issued_at < '{prev_end}'"
            )
        if "error" in g or "usage" in g:
            return (
                "select c.name, sum(u.errors) as errors from analytics.usage_daily u join analytics.customers c on "
                f"c.id = u.customer_id where u.day >= '{today - timedelta(days=30)}' group by c.name order by errors desc limit 5"
            )
        return "select id, name, segment, arr_usd, region from analytics.customers order by arr_usd desc limit 10"

    def _write_answer(self, messages: list[BaseMessage]) -> AIMessage:
        text = _text(messages)
        findings = text.split("Worker findings:", 1)[-1].split("Evidence:", 1)[0].strip()
        facts = [ln.strip().lstrip("- ").strip() for ln in findings.splitlines() if ln.startswith("    - ")]
        gaps = [f for f in facts if f.startswith("could not get")]
        facts = [f for f in facts if not f.startswith("could not get")] or [
            ln.split(":", 1)[-1].strip() for ln in findings.splitlines() if ln.startswith("- ")
        ]
        body = "\n".join(f"- {f}" for f in dict.fromkeys(facts[:8])) or "- No findings were returned."
        if gaps:
            body += "\n" + "\n".join(f"- Could not get: {g.split(':', 1)[-1].strip()}" for g in dict.fromkeys(gaps[:4]))
        return AIMessage(content=f"Here is what the sources show (offline model, no LLM was used):\n{body}")

    # ------------------------------------------------------------- structured
    def _structured(self, schema: Any, value: Any) -> Any:
        text = (
            value
            if isinstance(value, str)
            else _text(value if isinstance(value, list) else getattr(value, "messages", [value]))
        )
        name = schema.__name__
        if name == "TaskPlan":
            return schema.model_validate(self._plan(text))
        if name == "Verdict":
            draft = text.split("Draft:", 1)[-1].split("Evidence:", 1)[0]
            ok = "[ev_" in draft
            return schema(passed=ok, issues=[] if ok else ["the draft cites no evidence"])
        if name == "ActionProposals":
            return schema.model_validate({"actions": self._actions(text)})
        raise ValueError(f"offline model has no rule for {name}")

    def _plan(self, text: str) -> dict:
        question = text.split("User request:")[-1] if "User request:" in text else text.splitlines()[-1]
        q = question.lower()
        today = _as_of(text)
        since, until = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat()
        tasks: list[dict] = []
        if re.search(
            r"incident|inc-\d|outage|status|degraded|right now|github|issue|pull request|exchange rate|\bfx\b", q
        ):
            tasks.append(
                {
                    "id": "t1",
                    "kind": "live_api",
                    "goal": f"Find the incidents and live data relevant to: {question.strip()[:200]}",
                    "inputs": [{"name": "since", "value": since}, {"name": "until", "value": until}],
                }
            )
        if re.search(r"\barr\b|revenue|customer|churn|invoice|usage|errors|enterprise|average", q):
            deps = ["t1"] if tasks and re.search(r"affected|hit by|impacted", q) else []
            tasks.append(
                {
                    "id": f"t{len(tasks) + 1}",
                    "kind": "sql",
                    "goal": f"Answer from the warehouse: {question.strip()[:200]}",
                    "inputs": [{"name": "since", "value": since}, {"name": "until", "value": until}],
                    "depends_on": deps,
                }
            )
        if (
            re.search(r"postmortem|runbook|caus|why|procedure|rollback|policy|sla|docs|summar|explain|checklist", q)
            or not tasks
        ):
            tasks.append(
                {
                    "id": f"t{len(tasks) + 1}",
                    "kind": "docs",
                    "goal": f"Find documentation about: {question.strip()[:200]}",
                }
            )
        if re.search(r"\bweb\b|news|coverage|public", q):
            tasks.append({"id": f"t{len(tasks) + 1}", "kind": "web_research", "goal": question.strip()[:200]})
        needs_action = bool(re.search(r"\b(open|create|file|post|send|notify|email)\b", q))
        return {
            "tasks": tasks[:8],
            "answer_shape": "a direct answer with citations",
            "needs_action": needs_action,
            "assumptions": [f"'last week' means {since} to {until}"] if "last week" in q else [],
        }

    def _actions(self, text: str) -> list[dict]:
        question = _field(r"User request:\s*(.+)", text).lower()
        repos = re.findall(r"repos allowed: \[?([^\]\n]+)", text)
        repo = repos[0].split(",")[0].strip(" '\"") if repos else "acme-cloud/support-followups"
        channel_match = re.search(r"(#[a-z0-9-]+)", question)
        actions = []
        if "issue" in question:
            owner = _field(r"owner_github[\"']?:\s*[\"']?(@?[A-Za-z0-9-]+)", text) or None
            actions.append(
                {
                    "tool": "create_github_issue",
                    "reason": "user asked for a follow-up issue",
                    "repo": repo,
                    "title": "Follow-up from incident review",
                    "body": "See the cited answer in MARA.",
                    "labels": ["follow-up"],
                    "assignee": owner,
                }
            )
        if re.search(r"slack|post|notify", question) or channel_match:
            actions.append(
                {
                    "tool": "post_slack_message",
                    "reason": "user asked to post a note",
                    "channel": channel_match.group(1) if channel_match else "#incident-review",
                    "text": "Summary from MARA: see the cited answer for details.",
                }
            )
        if "email" in question:
            actions.append(
                {
                    "tool": "send_email",
                    "reason": "user asked for an email",
                    "to": "ops@acme-cloud.example",
                    "subject": "Update",
                    "body": "See MARA.",
                }
            )
        return actions
