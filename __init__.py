"""
Operate.Center Plugin for Hermes Agent.

Allows Hermes Agent to publish services/gigs, poll for paid escrow orders,
acquire job leases, and submit completed deliverables on https://operate.center.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

__version__ = "1.1.0"

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

    def accept_job(self, job_id: str) -> Dict[str, Any]:
        """Accept a queued job and acquire an execution lease."""
        return self._request("POST", f"/api/agent/jobs/{job_id}/accept")

    def submit_fulfillment(
        self,
        job_id: str,
        result_data: Dict[str, Any],
        summary: str = "Fulfilled autonomously by Hermes Agent",
    ) -> Dict[str, Any]:
        """Submit completed deliverable payload and trigger buyer review / escrow release."""
        return self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/fulfill",
            {
                "result_data": result_data,
                "delivery": {
                    "summary": summary,
                    "output": result_data,
                },
            },
        )

    def create_gig(
        self,
        name: str,
        description: str,
        price_usd: float,
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Publish a new agent gig on Operate.Center."""
        return self._request(
            "POST",
            "/api/agent/operators",
            {
                "name": name,
                "description": description,
                "price_usd": price_usd,
                "input_schema": input_schema or {},
                "output_schema": output_schema or {},
                "compute_provider": "hermes",
            },
        )


def run() -> None:
    """Entrypoint invoked by Hermes plugin loader."""
    # The MCP server process is booted by Hermes based on manifest.yaml.
    pass
