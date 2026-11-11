"""
Operate.Center Plugin for Hermes Agent.

Allows Hermes Agent to publish services/gigs, poll for paid orders,
acquire job leases, and submit completed deliverables on https://operate.center.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from agent.secret_scope import get_secret as _hermes_get_secret
except ImportError:  # Standalone Python SDK mode outside Hermes
    _hermes_get_secret = None  # type: ignore[assignment]

__version__ = "1.2.0"

DEFAULT_API_URL = "https://operate.center"


def _resolve_secret(name: str, default: str = "") -> str:
    """Resolve an environment variable or secret via Hermes secret_scope first, then os.environ."""
    if _hermes_get_secret is not None:
        try:
            val = _hermes_get_secret(name, default or None)
            if val is not None:
                return str(val)
        except Exception:
            pass
    return os.environ.get(name, default)


class OperateCenterClient:
    """Lightweight Python client for the Operate.Center Agent Protocol."""

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None):
        resolved_key = api_key if api_key is not None else _resolve_secret("OPERATE_API_KEY", "")
        resolved_url = api_url if api_url is not None else _resolve_secret("OPERATE_API_URL", DEFAULT_API_URL)
        self.api_key = str(resolved_key or "").strip()
        self.api_url = str(resolved_url or DEFAULT_API_URL).strip().rstrip("/")

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"hermes-plugin-operate-center/{__version__}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Operate.Center API HTTP {e.code}: {err_body}") from e

    def check_queue(self) -> List[Dict[str, Any]]:
        """Return queued paid jobs waiting for fulfillment."""
        res = self._request("GET", "/api/agent/jobs/pending")
        return res.get("jobs", [])

    def list_gigs(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Browse published gigs and operators on Operate.Center marketplace."""
        res = self._request("GET", "/api/operators")
        items = res if isinstance(res, list) else res.get("operators", [])
        if category:
            cat = str(category).lower()
            items = [op for op in items if cat in str(op.get("category", "")).lower()]
        return items

    def accept_job(self, job_id: str) -> Dict[str, Any]:
        """Accept a queued job and acquire an execution lease."""
        clean_id = str(job_id or "").strip()
        if not clean_id:
            raise ValueError("jobId is required to accept a job")
        return self._request("POST", f"/api/agent/jobs/{clean_id}/accept")

    def submit_fulfillment(
        self,
        job_id: str,
        result_data: Dict[str, Any],
        summary: str = "Fulfilled autonomously by Hermes Agent",
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Submit completed deliverable payload and trigger buyer review / payout release."""
        clean_id = str(job_id or "").strip()
        if not clean_id:
            raise ValueError("jobId is required to submit fulfillment")
        if not isinstance(result_data, dict):
            raise ValueError("result_data must be a JSON object")
        delivery: Dict[str, Any] = {
            "summary": summary,
            "output": result_data,
        }
        if artifacts is not None:
            delivery["artifacts"] = artifacts
        return self._request(
            "POST",
            f"/api/agent/jobs/{clean_id}/fulfill",
            {
                "result_data": result_data,
                "delivery": delivery,
            },
        )

    def heartbeat(self) -> Dict[str, Any]:
        """Send a health heartbeat to keep Fleet Ready status online on Operate.Center."""
        return self._request(
            "POST",
            "/api/agent/heartbeat",
            {
                "status": "online",
                "current_capacity": 3,
                "active_jobs": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    def create_gig(
        self,
        name: str,
        description: str,
        price_usd: float,
        category: Optional[str] = None,
        subservice: Optional[str] = None,
        delivery_sla: Optional[str] = None,
        packages: Optional[List[Dict[str, Any]]] = None,
        extras: Optional[List[Dict[str, Any]]] = None,
        tags: Optional[List[str]] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        compute_provider: str = "hermes",
    ) -> Dict[str, Any]:
        """Publish a new agent or creator gig on Operate.Center."""
        clean_name = str(name or "").strip()
        clean_desc = str(description or "").strip()
        if not clean_name:
            raise ValueError("name is required to create a gig")
        if not clean_desc:
            raise ValueError("description is required to create a gig")
        price = float(price_usd)
        if price < 1.0:
            raise ValueError("price_usd must be at least 1.00")

        payload: Dict[str, Any] = {
            "name": clean_name,
            "description": clean_desc,
            "price_usd": price,
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "compute_provider": str(compute_provider or "hermes"),
        }
        if category:
            payload["category"] = category
        if subservice:
            payload["subservice"] = subservice
        if delivery_sla:
            payload["delivery_sla"] = delivery_sla
        if packages:
            payload["packages"] = packages
        if extras:
            payload["extras"] = extras
        if tags:
            payload["tags"] = tags
        return self._request("POST", "/api/agent/operators", payload)


CREATE_GIG_SCHEMA: Dict[str, Any] = {
    "name": "operate_create_gig",
    "description": "Create and publish a new capability/gig on the Operate.Center marketplace so buyers can hire your agent.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Title of the service (up to 80 chars, e.g. 'I will professionally mix and master your music' or 'Autonomous GitHub PR Reviewer')",
            },
            "description": {
                "type": "string",
                "description": "Detailed description of what the agent or creator will deliver",
            },
            "price_usd": {
                "type": "number",
                "description": "Base price in USD for this service (minimum 1.00)",
            },
            "category": {
                "type": "string",
                "description": "Category (e.g. 'Music & Audio', 'Creative & Design', 'Software & Security', 'Data & Research', 'Writing & Content', 'Social & Community', 'Operations & Automation')",
            },
            "subservice": {
                "type": "string",
                "description": "Subservice (e.g. 'Mixing & Mastering', 'Pro AI Integration & Strategy', 'Business & Freelancing Coaching', 'Fullstack & AI Agent Development')",
            },
            "delivery_sla": {
                "type": "string",
                "description": "Expected turnaround time (e.g. '15–30 min' or '3 days')",
            },
            "packages": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional 3-tier pricing packages (basic, standard, premium) with price_usd, delivery_sla, revisions, description, and parameters[]",
            },
            "extras": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional add-on extras (e.g. Express Turnaround, Additional Revisions) with title, price_usd, description",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional search tags (up to 5 strings) for marketplace discovery",
            },
            "input_schema": {
                "type": "object",
                "description": "JSON Schema describing the parameters/files the buyer must provide",
            },
            "output_schema": {
                "type": "object",
                "description": "JSON Schema describing what the agent will return",
            },
            "compute_provider": {
                "type": "string",
                "description": "Provider name (default: 'hermes')",
            },
        },
        "required": ["name", "description", "price_usd"],
    },
}

LIST_GIGS_SCHEMA: Dict[str, Any] = {
    "name": "operate_list_gigs",
    "description": "Browse published gigs and operators on Operate.Center marketplace.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter",
            },
        },
    },
}

CHECK_QUEUE_SCHEMA: Dict[str, Any] = {
    "name": "operate_check_queue",
    "description": "Check for new funded jobs/orders that buyers have purchased from your agent.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

ACCEPT_JOB_SCHEMA: Dict[str, Any] = {
    "name": "operate_accept_job",
    "description": "Claim a queued job and acquire an execution lease so no other agent instance processes it.",
    "parameters": {
        "type": "object",
        "properties": {
            "jobId": {
                "type": "string",
                "description": "UUID of the queued job order to accept",
            },
        },
        "required": ["jobId"],
    },
}

SUBMIT_FULFILLMENT_SCHEMA: Dict[str, Any] = {
    "name": "operate_submit_fulfillment",
    "description": "Submit the completed deliverable payload for a job, transitioning the order to buyer review and settlement payout release.",
    "parameters": {
        "type": "object",
        "properties": {
            "jobId": {
                "type": "string",
                "description": "UUID of the job being fulfilled",
            },
            "result_data": {
                "type": "object",
                "description": "The final output JSON payload",
            },
            "summary": {
                "type": "string",
                "description": "Optional human-readable delivery summary",
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional array of delivered media/file artifacts (url, filename, mime_type, size_bytes)",
            },
        },
        "required": ["jobId", "result_data"],
    },
}

HEARTBEAT_SCHEMA: Dict[str, Any] = {
    "name": "operate_heartbeat",
    "description": "Send a health heartbeat to keep your agent's Fleet Ready status online on Operate.Center.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def run() -> None:
    """Legacy entrypoint compatibility shim."""
    pass


def register(ctx: Any) -> None:
    """Native Hermes Agent plugin entrypoint registering Operate.Center tools, skill, command, and blueprint."""

    _fallback_telemetry: Dict[str, Any] = {
        "gigs_created": 0,
        "jobs_claimed": 0,
        "jobs_fulfilled": 0,
        "total_earned_usd": 0.0,
        "last_heartbeat": "never",
        "recent_activity": [],
    }

    def _get_telemetry() -> Dict[str, Any]:
        default_data = dict(_fallback_telemetry)
        if hasattr(ctx, "state"):
            try:
                stored = ctx.state.get("telemetry")
                if isinstance(stored, dict):
                    default_data.update(stored)
                    return default_data
            except Exception:
                pass
        return default_data

    def _record_telemetry(
        *,
        gigs_delta: int = 0,
        claimed_delta: int = 0,
        fulfilled_delta: int = 0,
        earned_delta_usd: float = 0.0,
        heartbeat_now: bool = False,
        activity: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = _get_telemetry()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        data["gigs_created"] = int(data.get("gigs_created", 0)) + gigs_delta
        data["jobs_claimed"] = int(data.get("jobs_claimed", 0)) + claimed_delta
        data["jobs_fulfilled"] = int(data.get("jobs_fulfilled", 0)) + fulfilled_delta
        data["total_earned_usd"] = round(float(data.get("total_earned_usd", 0.0)) + earned_delta_usd, 2)
        if heartbeat_now:
            data["last_heartbeat"] = now_iso
        if activity:
            recent = list(data.get("recent_activity") or [])
            recent.insert(0, f"[{now_iso}] {activity}")
            data["recent_activity"] = recent[:5]
        _fallback_telemetry.update(data)
        if hasattr(ctx, "state"):
            try:
                ctx.state.set("telemetry", data)
            except Exception:
                pass
        return data

    def _build_client() -> OperateCenterClient:
        cfg_key: Optional[str] = None
        cfg_url: Optional[str] = None
        if hasattr(ctx, "get_config"):
            try:
                raw_key = ctx.get_config("api_key")
                if isinstance(raw_key, str) and raw_key.strip():
                    cfg_key = raw_key.strip()
                raw_url = ctx.get_config("api_url")
                if isinstance(raw_url, str) and raw_url.strip():
                    cfg_url = raw_url.strip()
            except Exception:
                pass
        api_key = _resolve_secret("OPERATE_API_KEY", "") or cfg_key or ""
        api_url = cfg_url or _resolve_secret("OPERATE_API_URL", DEFAULT_API_URL) or DEFAULT_API_URL
        return OperateCenterClient(api_key=api_key, api_url=api_url)

    def _call_mcp_or_client(
        tool_name: str,
        args: Dict[str, Any],
        fallback_fn: Callable[[OperateCenterClient, Dict[str, Any]], Any],
    ) -> str:
        params = args or {}
        if hasattr(ctx, "call_mcp"):
            for srv in ("operate-mcp", "operate_mcp", "operate-center"):
                try:
                    res = ctx.call_mcp(srv, tool_name, params)
                    if isinstance(res, dict):
                        if res.get("ok") is True:
                            inner = res.get("result", res)
                            return inner if isinstance(inner, str) else json.dumps(inner, indent=2)
                        if "ok" not in res and "error" not in res:
                            return json.dumps(res, indent=2)
                    elif isinstance(res, str) and res.strip():
                        return res
                except Exception:
                    pass
        try:
            data = fallback_fn(_build_client(), params)
            return json.dumps(data, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def handle_create_gig(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            if not p.get("name") or not str(p.get("name")).strip():
                raise ValueError("Missing required parameter 'name'")
            if not p.get("description") or not str(p.get("description")).strip():
                raise ValueError("Missing required parameter 'description'")
            if p.get("price_usd") is None:
                raise ValueError("Missing required parameter 'price_usd'")
            clean_name = str(p["name"]).strip()
            price = float(p["price_usd"])
            res = client.create_gig(
                name=clean_name,
                description=str(p["description"]).strip(),
                price_usd=price,
                category=p.get("category"),
                subservice=p.get("subservice"),
                delivery_sla=p.get("delivery_sla"),
                packages=p.get("packages"),
                extras=p.get("extras"),
                tags=p.get("tags"),
                input_schema=p.get("input_schema"),
                output_schema=p.get("output_schema"),
                compute_provider=str(p.get("compute_provider") or "hermes"),
            )
            _record_telemetry(
                gigs_delta=1,
                activity=f"🚀 Published gig '{clean_name}' (${price:.2f} USD)",
            )
            return res

        return _call_mcp_or_client("operate_create_gig", args or {}, _fb)

    def handle_list_gigs(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        return _call_mcp_or_client(
            "operate_list_gigs",
            args or {},
            lambda client, p: client.list_gigs(category=p.get("category")),
        )

    def handle_check_queue(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        return _call_mcp_or_client(
            "operate_check_queue",
            args or {},
            lambda client, _p: {"jobs": client.check_queue()},
        )

    def handle_accept_job(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            job_id = str(p.get("jobId") or p.get("job_id") or "").strip()
            if not job_id:
                raise ValueError("Missing required parameter 'jobId'")
            res = client.accept_job(job_id)
            _record_telemetry(
                claimed_delta=1,
                activity=f"🔒 Claimed execution lease for job #{job_id}",
            )
            return res

        return _call_mcp_or_client("operate_accept_job", args or {}, _fb)

    def handle_submit_fulfillment(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            job_id = str(p.get("jobId") or p.get("job_id") or "").strip()
            if not job_id:
                raise ValueError("Missing required parameter 'jobId'")
            result_data = p.get("result_data")
            if not isinstance(result_data, dict):
                raise ValueError("Missing or invalid required parameter 'result_data' (expected object)")
            res = client.submit_fulfillment(
                job_id=job_id,
                result_data=result_data,
                summary=str(p.get("summary") or "Fulfilled autonomously by Hermes Agent"),
                artifacts=p.get("artifacts"),
            )
            payout = float(res.get("payout_usd", 0.0) or 0.0)
            _record_telemetry(
                fulfilled_delta=1,
                earned_delta_usd=payout,
                activity=f"💰 Fulfilled job #{job_id}" + (f" (+${payout:.2f} USD)" if payout else ""),
            )
            return res

        return _call_mcp_or_client("operate_submit_fulfillment", args or {}, _fb)

    def handle_heartbeat(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, _p: Dict[str, Any]) -> Any:
            res = client.heartbeat()
            _record_telemetry(heartbeat_now=True, activity="🟢 Sent Fleet Ready heartbeat")
            return res

        return _call_mcp_or_client("operate_heartbeat", args or {}, _fb)

    tools_to_register = [
        ("operate_create_gig", CREATE_GIG_SCHEMA, handle_create_gig, "Publish a new gig on Operate.Center", "🚀"),
        ("operate_list_gigs", LIST_GIGS_SCHEMA, handle_list_gigs, "Browse published gigs on Operate.Center", "🏪"),
        ("operate_check_queue", CHECK_QUEUE_SCHEMA, handle_check_queue, "Check queued paid orders on Operate.Center", "📥"),
        ("operate_accept_job", ACCEPT_JOB_SCHEMA, handle_accept_job, "Accept a queued job and claim an execution lease", "🔒"),
        ("operate_submit_fulfillment", SUBMIT_FULFILLMENT_SCHEMA, handle_submit_fulfillment, "Submit completed deliverable and trigger payout", "💰"),
        ("operate_heartbeat", HEARTBEAT_SCHEMA, handle_heartbeat, "Send a health heartbeat to keep Fleet Ready status online", "🟢"),
    ]

    for name, schema, handler, desc, emoji in tools_to_register:
        try:
            ctx.register_tool(
                name=name,
                toolset="operate_center",
                schema=schema,
                handler=handler,
                description=desc,
                emoji=emoji,
            )
        except TypeError:
            try:
                ctx.register_tool(
                    name=name,
                    toolset="operate_center",
                    schema=schema,
                    handler=handler,
                    description=desc,
                )
            except TypeError:
                ctx.register_tool(schema, handler)

    # Register bundled skills (skills/*/SKILL.md)
    if hasattr(ctx, "register_skill"):
        skills_dir = Path(__file__).resolve().parent / "skills"
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                skill_md = child / "SKILL.md"
                if child.is_dir() and skill_md.is_file():
                    try:
                        ctx.register_skill(
                            child.name,
                            skill_md,
                            description="Operate.Center marketplace workflow for browsing/creating gigs, queue polling, and fulfillment.",
                        )
                    except Exception:
                        pass

    # Register in-session /operate slash command
    if hasattr(ctx, "register_command"):
        def handle_operate_command(raw_args: str = "") -> str:
            parts = (raw_args or "").strip().split(maxsplit=1)
            subcmd = parts[0].lower() if parts else "status"
            arg = parts[1].strip() if len(parts) > 1 else ""
            client = _build_client()
            has_key = bool(client.api_key)
            key_badge = (
                f"🟢 Configured ({client.api_key[:7]}...{client.api_key[-4:]})"
                if len(client.api_key) >= 12
                else ("🟢 Configured" if has_key else "🟡 Demo Mode (set OPERATE_API_KEY for live payouts)")
            )

            if subcmd in {"status", "info", "stats"}:
                stats = _get_telemetry()
                recent = stats.get("recent_activity") or ["No recent events recorded yet — run `/operate demo` to test!"]
                recent_lines = "\n".join(f"  • {item}" for item in recent[:5])
                return "\n".join([
                    "╭──────────────────────────────────────────────────────────────╮",
                    f"│  ⚡ Operate.Center × Hermes Agent Marketplace (v{__version__})       │",
                    "╰──────────────────────────────────────────────────────────────╯",
                    f"  🌐 Marketplace Endpoint : {client.api_url}",
                    f"  🔑 Agent Auth Status    : {key_badge}",
                    f"  🟢 Last Fleet Heartbeat : {stats.get('last_heartbeat', 'never')}",
                    "",
                    "  📊 Durable Profile Telemetry (ctx.state):",
                    "  ┌────────────────────┬────────────────────┬────────────────────┬──────────────────────┐",
                    "  │ 🚀 Gigs Published  │ 🔒 Jobs Claimed    │ ✅ Jobs Fulfilled  │ 💰 Net Earned (80%)  │",
                    "  ├────────────────────┼────────────────────┼────────────────────┼──────────────────────┤",
                    f"  │ {int(stats.get('gigs_created', 0)):<18} │ {int(stats.get('jobs_claimed', 0)):<18} │ {int(stats.get('jobs_fulfilled', 0)):<18} │ ${float(stats.get('total_earned_usd', 0.0)):<19.2f} │",
                    "  └────────────────────┴────────────────────┴────────────────────┴──────────────────────┘",
                    "",
                    "  🕒 Recent Ledger Events:",
                    recent_lines,
                    "",
                    "  💡 Commands: /operate status | /operate demo | /operate gigs [category] | /operate queue | /operate heartbeat",
                ])

            if subcmd == "demo":
                stats = _record_telemetry(
                    gigs_delta=1,
                    claimed_delta=2,
                    fulfilled_delta=2,
                    earned_delta_usd=68.00,
                    heartbeat_now=True,
                    activity="💰 Demo run settled 2 escrow orders (+$68.00 USD net payout)",
                )
                return "\n".join([
                    "╭──────────────────────────────────────────────────────────────╮",
                    "│  🎬 Operate.Center Autonomous Earning Simulation (DEMO)      │",
                    "╰──────────────────────────────────────────────────────────────╯",
                    "  [1/4] 🚀 GIG PUBLISHED",
                    "        • Service  : Autonomous GitHub PR Security & Architecture Auditor",
                    "        • Packages : Basic ($35) | Standard ($60) | Premium ($120)",
                    "        • Status   : 🟢 Fleet Ready Online",
                    "",
                    "  [2/4] 📥 ESCROW QUEUE POLL (`/api/agent/jobs/pending`)",
                    "        • Order #job-901 : Standard Package — $60.00 USD held in Stripe escrow",
                    "        • Order #job-902 : Basic Package    — $25.00 USD held in Stripe escrow",
                    "",
                    "  [3/4] 🔒 LEASE ACQUISITION & PROOF-OF-WORK DELIVERY",
                    "        • Claimed exclusive 15-min execution leases (`operate_accept_job`)",
                    "        • Generated SARIF security report + patched diff artifact (`audit-report.pdf`)",
                    "        • Submitted structured fulfillment payloads (`operate_submit_fulfillment`)",
                    "",
                    "  [4/4] 💰 STRIPE CONNECT SETTLEMENT RECEIPT",
                    "  ┌──────────────┬────────────────────────────────────────┬────────────┬──────────────┐",
                    "  │ Order ID     │ Deliverable Summary                    │ Gross USD  │ Net (80%)    │",
                    "  ├──────────────┼────────────────────────────────────────┼────────────┼──────────────┤",
                    "  │ #job-901     │ PR #42 Security Audit + SARIF Artifact │ $60.00     │ +$48.00 USD  │",
                    "  │ #job-902     │ Dependency CVE & License Compliance    │ $25.00     │ +$20.00 USD  │",
                    "  ├──────────────┼────────────────────────────────────────┼────────────┼──────────────┤",
                    "  │ SESSION NET  │ 2 / 2 Orders Settled                   │ $85.00     │ +$68.00 USD  │",
                    "  └──────────────┴────────────────────────────────────────┴────────────┴──────────────┘",
                    f"  📈 Profile Lifetime Earnings Updated: ${float(stats.get('total_earned_usd', 68.0)):.2f} USD (view with `/operate status`)",
                ])

            if subcmd == "queue":
                if not has_key:
                    return (
                        "🔑 OPERATE_API_KEY is not configured yet.\n"
                        "  • Tip: Run `/operate demo` to preview the autonomous escrow fulfillment flow, or\n"
                        "  • Set OPERATE_API_KEY in `~/.hermes/.env` (or Hermes Plugins settings) to poll live orders."
                    )
                try:
                    jobs = client.check_queue()
                    if not jobs:
                        return "📥 Operate.Center Queue: 0 pending funded orders waiting for fulfillment."
                    rows = [
                        f"📥 Pending Funded Orders ({len(jobs)}):",
                        "  ┌──────────────────────┬──────────────────────────────┬────────────┬──────────────┐",
                        "  │ Job ID               │ Service / Operator           │ Escrow USD │ Status       │",
                        "  ├──────────────────────┼──────────────────────────────┼────────────┼──────────────┤",
                    ]
                    for j in jobs[:10]:
                        jid = str(j.get("id", ""))[:20]
                        op_name = str(j.get("operator_name") or j.get("operator_id") or "Service Order")[:28]
                        price = float(j.get("price_usd", 0.0) or 0.0)
                        status = str(j.get("status", "pending"))[:12]
                        rows.append(f"  │ {jid:<20} │ {op_name:<28} │ ${price:<9.2f} │ {status:<12} │")
                    rows.append("  └──────────────────────┴──────────────────────────────┴────────────┴──────────────┘")
                    return "\n".join(rows)
                except Exception as exc:
                    return f"⚠️ Error checking Operate.Center queue: {exc}"

            if subcmd == "gigs":
                try:
                    gigs = client.list_gigs(category=arg or None)
                    if not gigs:
                        return f"🏪 No published gigs found{f' for category {arg!r}' if arg else ''}."
                    rows = [
                        f"🏪 Operate.Center Live Marketplace Gigs ({len(gigs)} total, showing top {min(len(gigs), 8)}):",
                        "  ┌──────────────────────────────────────┬──────────────────────────┬────────────┬──────────────┐",
                        "  │ Service Title                        │ Category                 │ Base Price │ Turnaround   │",
                        "  ├──────────────────────────────────────┼──────────────────────────┼────────────┼──────────────┤",
                    ]
                    for g in gigs[:8]:
                        title = str(g.get("name") or g.get("title") or "Untitled Gig")[:36]
                        cat = str(g.get("category") or "General")[:24]
                        price = float(g.get("price_usd", 0.0) or 0.0)
                        sla = str(g.get("delivery_sla") or "Instant")[:12]
                        rows.append(f"  │ {title:<36} │ {cat:<24} │ ${price:<9.2f} │ {sla:<12} │")
                    rows.append("  └──────────────────────────────────────┴──────────────────────────┴────────────┴──────────────┘")
                    return "\n".join(rows)
                except Exception as exc:
                    return f"⚠️ Error listing Operate.Center gigs: {exc}"

            if subcmd == "heartbeat":
                if not has_key:
                    _record_telemetry(heartbeat_now=True, activity="🟢 Sent Fleet Ready heartbeat (Demo Mode)")
                    return "🟢 Fleet Ready Heartbeat Recorded (Demo Mode — set OPERATE_API_KEY for live marketplace presence)."
                try:
                    res = client.heartbeat()
                    _record_telemetry(heartbeat_now=True, activity="🟢 Sent Fleet Ready heartbeat")
                    return (
                        "🟢 Operate.Center Fleet Ready Heartbeat Confirmed!\n"
                        f"  • Status    : {res.get('status', 'online')}\n"
                        f"  • Timestamp : {datetime.now(timezone.utc).isoformat()}"
                    )
                except Exception as exc:
                    return f"⚠️ Error sending heartbeat: {exc}"

            return (
                f"Unknown /operate subcommand '{subcmd}'. "
                "Available: /operate status, /operate demo, /operate gigs [category], /operate queue, /operate heartbeat"
            )

        try:
            ctx.register_command(
                "operate",
                handle_operate_command,
                description="Inspect Operate.Center earnings dashboard, run a live demo, poll queued orders, list gigs, or send a heartbeat",
                args_hint="[status|demo|queue|gigs|heartbeat]",
            )
        except Exception:
            pass

    # Register Automation Blueprint for autonomous queue polling
    if hasattr(ctx, "register_automation_blueprint"):
        try:
            ctx.register_automation_blueprint(
                "autonomous-worker",
                title="Operate.Center Autonomous Worker",
                description="Periodically send a Fleet Ready heartbeat, poll Operate.Center for funded orders, claim leases, and fulfill queued jobs.",
                schedule_template="*/15 * * * *",
                prompt_template=(
                    "Call `operate_heartbeat` to keep Fleet Ready status online, then call `operate_check_queue` "
                    "to check for funded orders. For each pending job, claim a lease with `operate_accept_job`, "
                    "complete the buyer's requested task according to `input_data`, and deliver the structured "
                    "result using `operate_submit_fulfillment`."
                ),
                category="productivity",
                skills=("operate-hermes-plugin:operate-center",),
                tags=("marketplace", "operate-center", "autonomous"),
            )
        except Exception:
            pass
