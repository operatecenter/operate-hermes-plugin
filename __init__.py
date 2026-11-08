"""
Operate.Center Plugin for Hermes Agent.

Allows Hermes Agent to publish services/gigs, poll for paid orders,
acquire job leases, and submit completed deliverables on https://operate.center.
"""

import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__version__ = "1.1.4"

DEFAULT_API_URL = "https://operate.center"


class OperateCenterClient:
    """Lightweight Python client for Operate.Center Agent Protocol."""

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPERATE_API_KEY", "")
        self.api_url = (api_url or os.environ.get("OPERATE_API_URL", DEFAULT_API_URL)).rstrip("/")

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
        return self._request("POST", f"/api/agent/jobs/{job_id}/accept")

    def submit_fulfillment(
        self,
        job_id: str,
        result_data: Dict[str, Any],
        summary: str = "Fulfilled autonomously by Hermes Agent",
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Submit completed deliverable payload and trigger buyer review / payout release."""
        delivery: Dict[str, Any] = {
            "summary": summary,
            "output": result_data,
        }
        if artifacts is not None:
            delivery["artifacts"] = artifacts
        return self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/fulfill",
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
    ) -> Dict[str, Any]:
        """Publish a new agent or creator gig on Operate.Center."""
        payload: Dict[str, Any] = {
            "name": name,
            "description": description,
            "price_usd": price_usd,
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "compute_provider": "hermes",
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


def run() -> None:
    """Entrypoint invoked by Hermes plugin loader."""
    # The MCP server process is booted by Hermes based on manifest.yaml.
    pass


def register(ctx: Any) -> None:
    """Native Hermes Agent plugin entrypoint registering the 6 Operate.Center tools."""
    create_gig_schema = {
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
                    "description": "Optional 3-tier pricing packages (basic, standard, premium) with price_usd, delivery_sla, revisions, description, and parameters[]",
                },
                "extras": {
                    "type": "array",
                    "description": "Optional add-on extras (e.g. Express Turnaround, Additional Revisions) with title, price_usd, description",
                },
                "tags": {
                    "type": "array",
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

    list_gigs_schema = {
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

    check_queue_schema = {
        "name": "operate_check_queue",
        "description": "Check for new funded jobs/orders that buyers have purchased from your agent.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    }

    accept_job_schema = {
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

    submit_fulfillment_schema = {
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
                    "description": "Optional array of delivered media/file artifacts (url, filename, mime_type, size_bytes)",
                },
            },
            "required": ["jobId", "result_data"],
        },
    }

    heartbeat_schema = {
        "name": "operate_heartbeat",
        "description": "Send a health heartbeat to keep your agent's Fleet Ready status online on Operate.Center.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    }

    def _call_mcp_or_client(tool_name: str, args: Dict[str, Any], fallback_fn: Any) -> str:
        params = args or {}
        if hasattr(ctx, "call_mcp"):
            for srv in ("operate-mcp", "operate_mcp", "operate-center"):
                try:
                    res = ctx.call_mcp(srv, tool_name, params)
                    if res is not None:
                        return res if isinstance(res, str) else json.dumps(res, indent=2)
                except Exception:
                    pass
        try:
            data = fallback_fn(OperateCenterClient(), params)
            return json.dumps(data, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def handle_create_gig(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            return client.create_gig(
                name=p["name"],
                description=p["description"],
                price_usd=float(p["price_usd"]),
                category=p.get("category"),
                subservice=p.get("subservice"),
                delivery_sla=p.get("delivery_sla"),
                packages=p.get("packages"),
                extras=p.get("extras"),
                tags=p.get("tags"),
                input_schema=p.get("input_schema"),
                output_schema=p.get("output_schema"),
            )
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
        return _call_mcp_or_client(
            "operate_accept_job",
            args or {},
            lambda client, p: client.accept_job(str(p.get("jobId") or p.get("job_id") or "")),
        )

    def handle_submit_fulfillment(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            return client.submit_fulfillment(
                job_id=str(p.get("jobId") or p.get("job_id") or ""),
                result_data=p.get("result_data") or {},
                summary=p.get("summary") or "Fulfilled autonomously by Hermes Agent",
                artifacts=p.get("artifacts"),
            )
        return _call_mcp_or_client("operate_submit_fulfillment", args or {}, _fb)

    def handle_heartbeat(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        return _call_mcp_or_client(
            "operate_heartbeat",
            args or {},
            lambda client, _p: client.heartbeat(),
        )

    tools_to_register = [
        ("operate_create_gig", create_gig_schema, handle_create_gig, "Publish a new gig on Operate.Center"),
        ("operate_list_gigs", list_gigs_schema, handle_list_gigs, "Browse published gigs on Operate.Center"),
        ("operate_check_queue", check_queue_schema, handle_check_queue, "Check queued paid orders on Operate.Center"),
        ("operate_accept_job", accept_job_schema, handle_accept_job, "Accept a queued job and claim an execution lease"),
        ("operate_submit_fulfillment", submit_fulfillment_schema, handle_submit_fulfillment, "Submit completed deliverable and trigger payout"),
        ("operate_heartbeat", heartbeat_schema, handle_heartbeat, "Send a health heartbeat to keep Fleet Ready status online"),
    ]

    for name, schema, handler, desc in tools_to_register:
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

