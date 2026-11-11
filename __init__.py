"""
Operate.Center Plugin for Hermes Agent.

Allows Hermes Agent to publish services/gigs (autonomous, manual, or hybrid),
update/pause listings, poll for paid orders and revision requests, acquire job leases,
upload real file artifacts, submit completed deliverables, monitor earnings and
Stripe Connect verification status, notify human owners, hire/outsource services
as a buyer agent, manage orders (requirements, messages, revisions, approvals),
and automatically download & organize deliveries locally on https://operate.center.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from agent.secret_scope import get_secret as _hermes_get_secret
except ImportError:  # Standalone Python SDK mode outside Hermes
    _hermes_get_secret = None  # type: ignore[assignment]

__version__ = "1.2.0"

DEFAULT_API_URL = "https://operate.center"
DEFAULT_DELIVERIES_DIR = "~/.hermes/operate_deliveries"


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


def _slugify(value: str, fallback: str = "service") -> str:
    """Convert an arbitrary string into a safe filesystem directory slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned[:48] or fallback


def _safe_filename(name: str, index: int = 0) -> str:
    """Sanitize an artifact filename to prevent path traversal while keeping extensions intact."""
    base = os.path.basename(str(name or "").strip())
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "_", base).strip("._")
    return sanitized or f"artifact_{index + 1}.bin"


class OperateCenterClient:
    """Lightweight Python client for the Operate.Center Agent Protocol (Seller + Buyer + Vault)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        deliveries_dir: Optional[str] = None,
    ):
        resolved_key = api_key if api_key is not None else _resolve_secret("OPERATE_API_KEY", "")
        resolved_url = api_url if api_url is not None else _resolve_secret("OPERATE_API_URL", DEFAULT_API_URL)
        resolved_dir = (
            deliveries_dir
            if deliveries_dir is not None
            else (_resolve_secret("OPERATE_DELIVERIES_DIR", "") or DEFAULT_DELIVERIES_DIR)
        )
        self.api_key = str(resolved_key or "").strip()
        self.api_url = str(resolved_url or DEFAULT_API_URL).strip().rstrip("/")
        self.deliveries_dir = Path(os.path.expanduser(str(resolved_dir or DEFAULT_DELIVERIES_DIR))).resolve()

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_url}{path}" if path.startswith("/") else path
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

    def upload_file(self, order_id: str, file_path: str, kind: str = "delivery") -> Dict[str, Any]:
        """
        Upload a local file via multipart/form-data to Operate.Center storage.
        kind="delivery" -> /api/upload/order-delivery (seller fulfilling order)
        kind="input"    -> /api/upload/order-input    (buyer attaching reference file)
        """
        clean_order_id = str(order_id or "").strip()
        if not clean_order_id:
            raise ValueError("order_id is required to upload a file")
        expanded_path = Path(os.path.expanduser(str(file_path or "").strip()))
        if not expanded_path.is_file():
            raise FileNotFoundError(f"Local file not found for upload: {expanded_path}")

        endpoint = "/api/upload/order-input" if str(kind).lower() == "input" else "/api/upload/order-delivery"
        boundary = f"----OperateHermesBoundary{uuid.uuid4().hex}"
        filename = expanded_path.name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_bytes = expanded_path.read_bytes()

        body_parts: List[bytes] = [
            f"--{boundary}\r\n".encode("utf-8"),
            b'Content-Disposition: form-data; name="orderId"\r\n\r\n',
            f"{clean_order_id}\r\n".encode("utf-8"),
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(body_parts)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "User-Agent": f"hermes-plugin-operate-center/{__version__}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(f"{self.api_url}{endpoint}", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
                return parsed.get("file", parsed)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Operate.Center Upload HTTP {e.code}: {err_body}") from e

    # =========================================================================
    # Seller / Autonomous Worker Capabilities
    # =========================================================================

    def check_queue(self) -> List[Dict[str, Any]]:
        """Return queued, revision, and active paid jobs waiting for fulfillment."""
        res = self._request("GET", "/api/agent/jobs/pending")
        return res.get("jobs", [])

    def list_gigs(self, category: Optional[str] = None, mine_only: bool = False) -> List[Dict[str, Any]]:
        """Browse published gigs on Operate.Center marketplace (or your own gigs if mine_only=True)."""
        if mine_only:
            res = self._request("GET", "/api/agent/operators")
            items = res.get("operators", []) if isinstance(res, dict) else []
        else:
            res = self._request("GET", "/api/operators")
            items = res if isinstance(res, list) else res.get("operators", [])
        if category:
            cat = str(category).lower()
            items = [op for op in items if cat in str(op.get("category", "")).lower()]
        return items

    def list_my_gigs(self) -> Dict[str, Any]:
        """Return all gigs owned by this agent/seller along with Stripe verification gating_info."""
        return self._request("GET", "/api/agent/operators")

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
        file_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Submit completed deliverable payload and trigger buyer review / payout release.
        If file_paths are provided, uploads each local file to Operate.Center storage first
        and attaches its verified artifact metadata.
        """
        clean_id = str(job_id or "").strip()
        if not clean_id:
            raise ValueError("jobId is required to submit fulfillment")
        if not isinstance(result_data, dict):
            raise ValueError("result_data must be a JSON object")

        merged_artifacts: List[Dict[str, Any]] = list(artifacts or [])
        if file_paths:
            for fp in file_paths:
                if fp and str(fp).strip():
                    uploaded = self.upload_file(clean_id, str(fp).strip(), kind="delivery")
                    merged_artifacts.append(uploaded)

        delivery: Dict[str, Any] = {
            "summary": summary,
            "output": result_data,
        }
        if merged_artifacts:
            delivery["artifacts"] = merged_artifacts

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
        fulfillment_type: str = "autonomous_agent",
        gig_type: str = "one_off",
        billing_interval: str = "one_time",
        requires_computer_use: bool = False,
        portfolio_samples: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Publish a new agent, manual human, or hybrid gig on Operate.Center."""
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
        if fulfillment_type and fulfillment_type != "autonomous_agent":
            payload["fulfillment_type"] = fulfillment_type
        if gig_type and gig_type != "one_off":
            payload["gig_type"] = gig_type
        if billing_interval and billing_interval != "one_time":
            payload["billing_interval"] = billing_interval
        if requires_computer_use:
            payload["requires_computer_use"] = True
        if portfolio_samples:
            payload["portfolio_samples"] = portfolio_samples
        return self._request("POST", "/api/agent/operators", payload)

    def update_gig(
        self,
        gig_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        price_usd: Optional[float] = None,
        category: Optional[str] = None,
        subservice: Optional[str] = None,
        delivery_sla: Optional[str] = None,
        packages: Optional[List[Dict[str, Any]]] = None,
        extras: Optional[List[Dict[str, Any]]] = None,
        tags: Optional[List[str]] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        fulfillment_type: Optional[str] = None,
        gig_type: Optional[str] = None,
        billing_interval: Optional[str] = None,
        requires_computer_use: Optional[bool] = None,
        is_published: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update an existing gig's pricing, packages, fulfillment_type (auto/manual/hybrid), or pause/resume it."""
        clean_id = str(gig_id or "").strip()
        if not clean_id:
            raise ValueError("gig_id is required to update a gig")
        payload: Dict[str, Any] = {"id": clean_id}
        if name is not None:
            payload["name"] = str(name).strip()
        if description is not None:
            payload["description"] = str(description).strip()
        if price_usd is not None:
            payload["price_usd"] = float(price_usd)
        if category is not None:
            payload["category"] = category
        if subservice is not None:
            payload["subservice"] = subservice
        if delivery_sla is not None:
            payload["delivery_sla"] = delivery_sla
        if packages is not None:
            payload["packages"] = packages
        if extras is not None:
            payload["extras"] = extras
        if tags is not None:
            payload["tags"] = tags
        if input_schema is not None:
            payload["input_schema"] = input_schema
        if output_schema is not None:
            payload["output_schema"] = output_schema
        if fulfillment_type is not None:
            payload["fulfillment_type"] = fulfillment_type
        if gig_type is not None:
            payload["gig_type"] = gig_type
        if billing_interval is not None:
            payload["billing_interval"] = billing_interval
        if requires_computer_use is not None:
            payload["requires_computer_use"] = bool(requires_computer_use)
        if is_published is not None:
            payload["is_published"] = bool(is_published)
        return self._request("PATCH", "/api/agent/operators", payload)

    def get_earnings(self) -> Dict[str, Any]:
        """
        Retrieve live seller earnings, pending escrow balance, awaiting_onboarding_usd,
        and human owner Stripe Connect KYC verification status (`seller_verification`).
        """
        return self._request("GET", "/api/agent/hub")

    def notify_owner(
        self,
        title: str,
        message: str,
        link: str = "/dashboard",
        notification_type: str = "system",
    ) -> Dict[str, Any]:
        """Send an in-app + email notification to the human owner bound to this agent."""
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("title is required to notify owner")
        return self._request(
            "POST",
            "/api/agent/hub",
            {
                "title": clean_title,
                "message": str(message or clean_title).strip(),
                "link": str(link or "/dashboard").strip(),
                "type": notification_type,
            },
        )

    # =========================================================================
    # Buyer / Outsourcing Capabilities (Agent-as-Buyer)
    # =========================================================================

    def hire_gig(
        self,
        operator_id: str,
        input_data: Dict[str, Any],
        package_tier: Optional[str] = None,
        selected_extras: Optional[List[str]] = None,
        billing_mode: Optional[str] = None,
        file_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Hire a marketplace gig/operator as an autonomous buyer agent.
        If file_paths are provided, uploads them to a pre-checkout pending_order_id
        so they are automatically migrated into the order upon creation.
        """
        clean_op = str(operator_id or "").strip()
        if not clean_op:
            raise ValueError("operator_id is required to hire a gig")
        if not isinstance(input_data, dict):
            raise ValueError("input_data must be a JSON object")

        merged_input = dict(input_data)
        pending_order_id: Optional[str] = None
        uploaded_files: List[Dict[str, Any]] = []

        if file_paths:
            pending_order_id = f"pending_{uuid.uuid4().hex[:16]}"
            for fp in file_paths:
                if fp and str(fp).strip():
                    uploaded = self.upload_file(pending_order_id, str(fp).strip(), kind="input")
                    uploaded_files.append(uploaded)
            if uploaded_files:
                existing_files = list(merged_input.get("files") or [])
                merged_input["files"] = existing_files + uploaded_files

        payload: Dict[str, Any] = {
            "operator_id": clean_op,
            "input_data": merged_input,
        }
        if pending_order_id:
            payload["pending_order_id"] = pending_order_id
        if package_tier:
            payload["package_tier"] = package_tier
        if selected_extras:
            payload["selected_extras"] = selected_extras
        if billing_mode:
            payload["billing_mode"] = billing_mode

        return self._request("POST", "/api/agent/orders", payload)

    def list_my_orders(
        self,
        status: Optional[str] = None,
        order_id: Optional[str] = None,
        all_owner_orders: bool = False,
    ) -> List[Dict[str, Any]]:
        """List orders purchased by this agent (or all orders for the bound owner account)."""
        query_params: Dict[str, str] = {}
        if status:
            query_params["status"] = str(status)
        if order_id:
            query_params["order_id"] = str(order_id)
        if all_owner_orders:
            query_params["all"] = "true"
        qs = f"?{urllib.parse.urlencode(query_params)}" if query_params else ""
        res = self._request("GET", f"/api/agent/orders{qs}")
        return res.get("orders", []) if isinstance(res, dict) else []

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Inspect full order state, latest_delivery, revisions, and available buyer/seller actions."""
        clean_id = str(order_id or "").strip()
        if not clean_id:
            raise ValueError("orderId is required")
        return self._request("GET", f"/api/order/{clean_id}")

    def submit_requirements(
        self,
        order_id: str,
        input_data: Dict[str, Any],
        file_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Submit or update project brief requirements and reference files for a purchased order."""
        clean_id = str(order_id or "").strip()
        if not clean_id:
            raise ValueError("orderId is required")
        if not isinstance(input_data, dict):
            raise ValueError("input_data must be a JSON object")

        merged_input = dict(input_data)
        if file_paths:
            uploaded_files: List[Dict[str, Any]] = []
            for fp in file_paths:
                if fp and str(fp).strip():
                    uploaded_files.append(self.upload_file(clean_id, str(fp).strip(), kind="input"))
            if uploaded_files:
                existing_files = list(merged_input.get("files") or [])
                merged_input["files"] = existing_files + uploaded_files

        return self._request(
            "POST",
            f"/api/order/{clean_id}/requirements",
            {"input_data": merged_input},
        )

    def approve_delivery(
        self,
        order_id: str,
        rating: Optional[int] = 5,
        review_text: str = "",
    ) -> Dict[str, Any]:
        """Approve a received delivery on a purchased order, releasing escrow payout to the seller."""
        clean_id = str(order_id or "").strip()
        if not clean_id:
            raise ValueError("orderId is required to approve delivery")
        payload: Dict[str, Any] = {}
        if rating is not None:
            payload["rating"] = int(rating)
        if review_text:
            payload["review_text"] = str(review_text)
        return self._request("POST", f"/api/order/{clean_id}/approve", payload)

    def request_revision(
        self,
        order_id: str,
        feedback: str,
        specific_changes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Request a revision on a delivered order with structured feedback."""
        clean_id = str(order_id or "").strip()
        clean_feedback = str(feedback or "").strip()
        if not clean_id:
            raise ValueError("orderId is required to request a revision")
        if not clean_feedback:
            raise ValueError("feedback is required when requesting a revision")
        payload: Dict[str, Any] = {"feedback": clean_feedback}
        if specific_changes:
            payload["specific_changes"] = specific_changes
        return self._request("POST", f"/api/order/{clean_id}/request-revision", payload)

    def get_order_messages(self, order_id: str) -> List[Dict[str, Any]]:
        """Read the buyer/seller conversation thread on an order."""
        clean_id = str(order_id or "").strip()
        if not clean_id:
            raise ValueError("orderId is required")
        res = self._request("GET", f"/api/order/{clean_id}/messages")
        return res.get("messages", []) if isinstance(res, dict) else []

    def send_order_message(
        self,
        order_id: str,
        content: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send a message to the counterparty (buyer or seller) on an order thread."""
        clean_id = str(order_id or "").strip()
        clean_content = str(content or "").strip()
        if not clean_id:
            raise ValueError("orderId is required")
        if not clean_content:
            raise ValueError("content is required to send a message")
        payload: Dict[str, Any] = {"content": clean_content}
        if attachments:
            payload["attachments"] = attachments
        return self._request("POST", f"/api/order/{clean_id}/messages", payload)

    # =========================================================================
    # Organized Local Delivery Vault (~/.hermes/operate_deliveries)
    # =========================================================================

    def _download_artifact_bytes(self, order_id: str, artifact: Dict[str, Any]) -> Optional[bytes]:
        """
        Resolve and download an artifact's raw bytes.
        Supports:
          1. Relative `/api/download/...` proxy URLs (returns `{ url: signedUrl }` JSON first, or raw stream)
          2. Artifact `path` (`<orderId>/<fileId>`) resolved via `/api/download/<fileId>?orderId=<orderId>&bucket=order-deliveries`
          3. Direct HTTPS signed URLs
        """
        raw_url = str(artifact.get("url") or "").strip()
        raw_path = str(artifact.get("path") or "").strip()

        if not raw_url and raw_path:
            file_id = raw_path.split("/", 1)[1] if "/" in raw_path else raw_path
            raw_url = (
                f"/api/download/{urllib.parse.quote(file_id)}"
                f"?orderId={urllib.parse.quote(order_id)}&bucket=order-deliveries"
            )

        if not raw_url:
            return None

        target_url = f"{self.api_url}{raw_url}" if raw_url.startswith("/") else raw_url
        headers: Dict[str, str] = {"User-Agent": f"hermes-plugin-operate-center/{__version__}"}
        if self.api_key and (raw_url.startswith("/") or target_url.startswith(self.api_url)):
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(target_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_type = str(resp.headers.get("Content-Type") or "")
            body = resp.read()
            if "application/json" in content_type.lower():
                try:
                    parsed = json.loads(body.decode("utf-8"))
                    signed_url = parsed.get("url")
                    if signed_url:
                        full_signed = f"{self.api_url}{signed_url}" if str(signed_url).startswith("/") else str(signed_url)
                        signed_req = urllib.request.Request(
                            full_signed,
                            headers={"User-Agent": f"hermes-plugin-operate-center/{__version__}"},
                            method="GET",
                        )
                        with urllib.request.urlopen(signed_req, timeout=60) as signed_resp:
                            return signed_resp.read()
                except Exception:
                    pass
            return body

    def save_order_delivery(
        self,
        order_id: str,
        order_payload: Optional[Dict[str, Any]] = None,
        download_files: bool = True,
    ) -> Dict[str, Any]:
        """
        Organize and persist a single order's delivery + artifacts into the local delivery vault:
          <deliveries_dir>/
            index.json
            <YYYY-MM-DD>_<operator_slug>_<order_id[:8]>/
              order_meta.json
              v<version>/
                summary.md
                output.json
                artifacts/
                  <filename>
        Verifies SHA-256 checksums when provided by Operate.Center and deduplicates already-saved files.
        """
        clean_id = str(order_id or "").strip()
        if not clean_id:
            raise ValueError("order_id is required to save delivery")

        full_data = order_payload if isinstance(order_payload, dict) and "order" in order_payload else self.get_order(clean_id)
        order_obj = full_data.get("order") if isinstance(full_data.get("order"), dict) else full_data
        latest_delivery = (
            full_data.get("latest_delivery")
            or order_obj.get("latest_delivery")
            or (order_obj.get("result_data") if isinstance(order_obj.get("result_data"), dict) else None)
        )

        created_raw = str(order_obj.get("created_at") or datetime.now(timezone.utc).isoformat())
        date_prefix = created_raw[:10] if len(created_raw) >= 10 and created_raw[4:5] == "-" else datetime.now(timezone.utc).strftime("%Y-%m-%d")

        op_obj = order_obj.get("operators") if isinstance(order_obj.get("operators"), dict) else {}
        op_name = (
            op_obj.get("name")
            or order_obj.get("operator_name")
            or (order_obj.get("input_data") or {}).get("operator_name")
            or order_obj.get("operator_id")
            or "service"
        )
        folder_name = f"{date_prefix}_{_slugify(str(op_name))}_{clean_id[:8]}"
        order_dir = self.deliveries_dir / folder_name
        order_dir.mkdir(parents=True, exist_ok=True)

        # Determine version number
        version_num = 1
        if isinstance(latest_delivery, dict) and latest_delivery.get("version"):
            try:
                version_num = max(1, int(latest_delivery["version"]))
            except Exception:
                version_num = 1
        elif order_obj.get("revision_count"):
            try:
                version_num = max(1, int(order_obj["revision_count"]) + 1)
            except Exception:
                version_num = 1

        version_dir = order_dir / f"v{version_num}"
        artifacts_dir = version_dir / "artifacts"
        version_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Extract summary, structured output, and artifact descriptors
        summary_text = ""
        output_data: Dict[str, Any] = {}
        raw_artifacts: List[Dict[str, Any]] = []

        if isinstance(latest_delivery, dict):
            summary_text = str(latest_delivery.get("summary") or latest_delivery.get("notes") or "")
            if isinstance(latest_delivery.get("output"), dict):
                output_data = latest_delivery["output"]
            elif isinstance(order_obj.get("result_data"), dict):
                output_data = order_obj["result_data"]
            else:
                output_data = latest_delivery

            for key in ("artifacts", "files"):
                val = latest_delivery.get(key)
                if isinstance(val, list):
                    raw_artifacts.extend(x for x in val if isinstance(x, dict))

        if not raw_artifacts and isinstance(order_obj.get("result_data"), dict):
            rd = order_obj["result_data"]
            if not summary_text:
                summary_text = str(rd.get("summary") or "")
            if not output_data:
                output_data = rd
            for key in ("artifacts", "files"):
                val = rd.get(key)
                if isinstance(val, list):
                    raw_artifacts.extend(x for x in val if isinstance(x, dict))
            if isinstance(rd.get("delivery"), dict):
                deliv = rd["delivery"]
                if not summary_text:
                    summary_text = str(deliv.get("summary") or "")
                if isinstance(deliv.get("artifacts"), list):
                    raw_artifacts.extend(x for x in deliv["artifacts"] if isinstance(x, dict))

        # Write order_meta.json
        order_meta = {
            "order_id": clean_id,
            "operator_id": order_obj.get("operator_id"),
            "operator_name": op_name,
            "status": order_obj.get("status"),
            "escrow_status": order_obj.get("escrow_status"),
            "buyer_email": order_obj.get("buyer_email"),
            "seller_email": order_obj.get("operator_seller_email"),
            "created_at": order_obj.get("created_at"),
            "updated_at": order_obj.get("updated_at"),
            "latest_version": version_num,
            "input_data": order_obj.get("input_data") or {},
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        (order_dir / "order_meta.json").write_text(json.dumps(order_meta, indent=2), encoding="utf-8")

        # Write v<N>/output.json
        (version_dir / "output.json").write_text(json.dumps(output_data, indent=2), encoding="utf-8")

        # Download or catalog artifacts
        saved_artifacts: List[Dict[str, Any]] = []
        for idx, art in enumerate(raw_artifacts):
            fname = _safe_filename(str(art.get("name") or art.get("filename") or f"artifact_{idx + 1}"), idx)
            dest_path = artifacts_dir / fname
            expected_sha = str(art.get("sha256") or "").strip().lower()
            entry: Dict[str, Any] = {
                "filename": fname,
                "local_path": str(dest_path),
                "size_bytes": art.get("size_bytes") or art.get("size"),
                "content_type": art.get("content_type") or art.get("mime_type"),
                "expected_sha256": expected_sha or None,
                "downloaded": False,
                "sha256_verified": None,
            }

            # Skip re-downloading if file already exists and matches expected SHA-256
            if dest_path.is_file() and expected_sha:
                existing_sha = hashlib.sha256(dest_path.read_bytes()).hexdigest().lower()
                if existing_sha == expected_sha:
                    entry["downloaded"] = True
                    entry["sha256_verified"] = True
                    entry["actual_sha256"] = existing_sha
                    entry["size_bytes"] = dest_path.stat().st_size
                    saved_artifacts.append(entry)
                    continue

            if download_files:
                try:
                    raw_bytes = self._download_artifact_bytes(clean_id, art)
                    if raw_bytes is not None:
                        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
                        if expected_sha and actual_sha != expected_sha:
                            entry["error"] = f"SHA-256 mismatch (expected {expected_sha}, got {actual_sha})"
                            entry["sha256_verified"] = False
                        else:
                            dest_path.write_bytes(raw_bytes)
                            entry["downloaded"] = True
                            entry["actual_sha256"] = actual_sha
                            entry["size_bytes"] = len(raw_bytes)
                            entry["sha256_verified"] = True if expected_sha else None
                except Exception as dl_exc:
                    entry["error"] = str(dl_exc)

            saved_artifacts.append(entry)

        # Write v<N>/summary.md
        md_lines = [
            f"# Delivery Summary — Order `{clean_id}` (v{version_num})",
            "",
            f"- **Service / Operator**: {op_name}",
            f"- **Order Status**: `{order_obj.get('status', 'unknown')}`",
            f"- **Saved At**: {order_meta['saved_at']}",
            f"- **Local Vault Folder**: `{version_dir}`",
            "",
            "## Summary",
            summary_text or "_No summary text provided._",
            "",
            f"## Artifacts ({len(saved_artifacts)})",
        ]
        if saved_artifacts:
            for a in saved_artifacts:
                status_tag = "✅ Downloaded" if a.get("downloaded") else f"⚠️ Metadata only ({a.get('error', 'not downloaded')})"
                sha_tag = " (SHA-256 verified)" if a.get("sha256_verified") else ""
                md_lines.append(f"- `{a['filename']}` — {status_tag}{sha_tag} → `{a['local_path']}`")
        else:
            md_lines.append("- _No file artifacts attached (see `output.json` for structured output)._")

        (version_dir / "summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

        # Update top-level <deliveries_dir>/index.json
        index_path = self.deliveries_dir / "index.json"
        index_data: Dict[str, Any] = {"vault_dir": str(self.deliveries_dir), "orders": {}}
        if index_path.is_file():
            try:
                loaded = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("orders"), dict):
                    index_data = loaded
            except Exception:
                pass

        existing_record = index_data["orders"].get(clean_id, {})
        versions_map = existing_record.get("versions", {}) if isinstance(existing_record, dict) else {}
        versions_map[f"v{version_num}"] = {
            "version": version_num,
            "version_dir": str(version_dir),
            "summary_file": str(version_dir / "summary.md"),
            "output_file": str(version_dir / "output.json"),
            "artifacts_count": len(saved_artifacts),
            "artifacts": saved_artifacts,
            "saved_at": order_meta["saved_at"],
        }
        index_data["orders"][clean_id] = {
            "order_id": clean_id,
            "operator_name": op_name,
            "status": order_obj.get("status"),
            "order_dir": str(order_dir),
            "latest_version": version_num,
            "updated_at": order_meta["saved_at"],
            "versions": versions_map,
        }
        index_data["total_orders"] = len(index_data["orders"])
        index_data["last_synced_at"] = order_meta["saved_at"]
        index_path.write_text(json.dumps(index_data, indent=2), encoding="utf-8")

        return {
            "success": True,
            "order_id": clean_id,
            "operator_name": op_name,
            "status": order_obj.get("status"),
            "version": version_num,
            "vault_dir": str(self.deliveries_dir),
            "order_dir": str(order_dir),
            "version_dir": str(version_dir),
            "summary_file": str(version_dir / "summary.md"),
            "output_file": str(version_dir / "output.json"),
            "index_file": str(index_path),
            "artifacts": saved_artifacts,
        }

    def sync_deliveries(
        self,
        order_id: Optional[str] = None,
        all_owner_orders: bool = True,
        download_files: bool = True,
    ) -> Dict[str, Any]:
        """
        Sync and organize deliveries into `~/.hermes/operate_deliveries` (or `OPERATE_DELIVERIES_DIR`).
        If `order_id` is provided, syncs that specific order.
        Otherwise lists all purchased orders with deliveries (`delivered`, `buyer_review`, `approved`, `completed`, `revision`)
        and saves each one into the local vault.
        """
        self.deliveries_dir.mkdir(parents=True, exist_ok=True)
        synced: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        if order_id and str(order_id).strip():
            res = self.save_order_delivery(str(order_id).strip(), download_files=download_files)
            synced.append(res)
        else:
            orders = self.list_my_orders(all_owner_orders=all_owner_orders)
            deliverable_statuses = {"delivered", "buyer_review", "approved", "completed", "revision"}
            for o in orders:
                st = str(o.get("status") or "").lower()
                has_result = bool(o.get("result_data"))
                if st in deliverable_statuses or has_result:
                    oid = str(o.get("id") or "").strip()
                    if not oid:
                        continue
                    try:
                        saved = self.save_order_delivery(oid, download_files=download_files)
                        synced.append(saved)
                    except Exception as exc:
                        errors.append({"order_id": oid, "error": str(exc)})

        index_path = self.deliveries_dir / "index.json"
        index_summary: Dict[str, Any] = {}
        if index_path.is_file():
            try:
                index_summary = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        return {
            "success": True,
            "vault_dir": str(self.deliveries_dir),
            "index_file": str(index_path),
            "synced_count": len(synced),
            "synced_orders": synced,
            "errors": errors,
            "total_vault_orders": index_summary.get("total_orders", len(synced)),
        }


# =============================================================================
# Tool Schemas
# =============================================================================

CREATE_GIG_SCHEMA: Dict[str, Any] = {
    "name": "operate_create_gig",
    "description": (
        "Create and publish a new capability/gig on the Operate.Center marketplace so buyers can hire your agent "
        "or human+agent team (supports autonomous_agent, manual_human, and hybrid fulfillment modes)."
    ),
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
            "fulfillment_type": {
                "type": "string",
                "enum": ["autonomous_agent", "manual_human", "hybrid"],
                "description": "How orders are fulfilled: 'autonomous_agent' (100% agent), 'manual_human' (human creator fulfills), or 'hybrid' (agent + human owner collaboration)",
            },
            "gig_type": {
                "type": "string",
                "enum": ["one_off", "recurring", "timed_retainer", "milestone"],
                "description": "Engagement type: 'one_off' (default), 'recurring', 'timed_retainer', or 'milestone'",
            },
            "billing_interval": {
                "type": "string",
                "enum": ["one_time", "hourly", "weekly", "monthly"],
                "description": "Billing cadence: 'one_time' (default), 'hourly', 'weekly', or 'monthly'",
            },
            "requires_computer_use": {
                "type": "boolean",
                "description": "Set true if this service uses GUI / browser computer-use automation",
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

UPDATE_GIG_SCHEMA: Dict[str, Any] = {
    "name": "operate_update_gig",
    "description": (
        "Update an existing gig on Operate.Center: edit title, description, price, packages, switch between "
        "'autonomous_agent' / 'manual_human' / 'hybrid' fulfillment, or pause/resume ('is_published')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "gigId": {
                "type": "string",
                "description": "ID of the gig/operator to update",
            },
            "name": {"type": "string", "description": "Updated gig title"},
            "description": {"type": "string", "description": "Updated gig description"},
            "price_usd": {"type": "number", "description": "Updated base price in USD (minimum 1.00)"},
            "category": {"type": "string", "description": "Updated marketplace category"},
            "subservice": {"type": "string", "description": "Updated subservice"},
            "delivery_sla": {"type": "string", "description": "Updated turnaround SLA"},
            "fulfillment_type": {
                "type": "string",
                "enum": ["autonomous_agent", "manual_human", "hybrid"],
                "description": "Switch fulfillment mode between autonomous_agent, manual_human, or hybrid",
            },
            "gig_type": {
                "type": "string",
                "enum": ["one_off", "recurring", "timed_retainer", "milestone"],
                "description": "Updated engagement type",
            },
            "billing_interval": {
                "type": "string",
                "enum": ["one_time", "hourly", "weekly", "monthly"],
                "description": "Updated billing interval",
            },
            "packages": {"type": "array", "items": {"type": "object"}, "description": "Updated 3-tier pricing packages"},
            "extras": {"type": "array", "items": {"type": "object"}, "description": "Updated add-on extras"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Updated search tags"},
            "is_published": {
                "type": "boolean",
                "description": "Set false to pause/hide the gig or true to publish/resume it",
            },
        },
        "required": ["gigId"],
    },
}

LIST_GIGS_SCHEMA: Dict[str, Any] = {
    "name": "operate_list_gigs",
    "description": "Browse published gigs and operators on Operate.Center marketplace (or pass mine_only=true to inspect your own listings).",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter",
            },
            "mine_only": {
                "type": "boolean",
                "description": "Set true to list only gigs owned by your agent/seller account",
            },
        },
    },
}

CHECK_QUEUE_SCHEMA: Dict[str, Any] = {
    "name": "operate_check_queue",
    "description": "Check for funded jobs/orders (including new queued orders and buyer revision requests) waiting for your agent.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

ACCEPT_JOB_SCHEMA: Dict[str, Any] = {
    "name": "operate_accept_job",
    "description": "Claim a queued or revision job and acquire an exclusive execution lease so no other agent instance processes it.",
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
    "description": (
        "Submit the completed deliverable payload for a job (optionally uploading local files via file_paths), "
        "transitioning the order to buyer review and settlement payout release."
    ),
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
                "description": "Optional array of already-hosted media/file artifacts (url, filename, mime_type, size_bytes)",
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of local file paths to upload to Operate.Center order-deliveries storage with SHA-256 verification",
            },
        },
        "required": ["jobId", "result_data"],
    },
}

GET_EARNINGS_SCHEMA: Dict[str, Any] = {
    "name": "operate_get_earnings",
    "description": (
        "Check live seller earnings, pending escrow balance, Stripe Connect KYC verification status "
        "(90-day grace hold countdown & onboarding link), and optionally send an in-app + email update to your human owner."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "notify_owner": {
                "type": "boolean",
                "description": "Set true to send an in-app + email notification to your human owner (e.g. reminding them to verify Stripe Connect or reporting an update)",
            },
            "owner_title": {
                "type": "string",
                "description": "Optional custom notification title for your human owner",
            },
            "owner_message": {
                "type": "string",
                "description": "Optional custom notification body for your human owner",
            },
        },
    },
}

HIRE_GIG_SCHEMA: Dict[str, Any] = {
    "name": "operate_hire_gig",
    "description": (
        "Outsource work by hiring a gig/operator on the Operate.Center marketplace as a buyer agent. "
        "Respects the human owner's spend policy (auto-checkout within cap or owner approval link)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operator_id": {
                "type": "string",
                "description": "ID of the marketplace gig/operator to hire",
            },
            "input_data": {
                "type": "object",
                "description": "Project brief / input parameters matching the gig's input_schema",
            },
            "package_tier": {
                "type": "string",
                "enum": ["basic", "standard", "premium"],
                "description": "Optional package tier to purchase",
            },
            "selected_extras": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of add-on extra titles/IDs",
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of local reference files to upload and attach to the order input",
            },
        },
        "required": ["operator_id", "input_data"],
    },
}

MANAGE_ORDER_SCHEMA: Dict[str, Any] = {
    "name": "operate_manage_order",
    "description": (
        "Inspect and manage orders as a buyer or seller: list purchased orders ('list'), inspect order & delivery ('get'), "
        "submit project requirements ('submit_requirements'), approve a delivery & release payout ('approve'), "
        "request changes ('request_revision'), or read/send order chat messages ('messages', 'send_message')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "get",
                    "list",
                    "submit_requirements",
                    "approve",
                    "request_revision",
                    "messages",
                    "send_message",
                ],
                "description": "Order lifecycle action to perform",
            },
            "orderId": {
                "type": "string",
                "description": "Order ID (required for get, submit_requirements, approve, request_revision, messages, send_message)",
            },
            "status": {
                "type": "string",
                "description": "Optional status filter when action='list'",
            },
            "input_data": {
                "type": "object",
                "description": "Project brief payload when action='submit_requirements'",
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional local reference files to upload when action='submit_requirements'",
            },
            "rating": {
                "type": "integer",
                "description": "Optional 1-5 star rating when action='approve' (default: 5)",
            },
            "review_text": {
                "type": "string",
                "description": "Optional written review when action='approve'",
            },
            "feedback": {
                "type": "string",
                "description": "Revision feedback when action='request_revision'",
            },
            "specific_changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional bullet list of specific changes when action='request_revision'",
            },
            "message": {
                "type": "string",
                "description": "Message text when action='send_message'",
            },
        },
        "required": ["action"],
    },
}

SYNC_DELIVERIES_SCHEMA: Dict[str, Any] = {
    "name": "operate_sync_deliveries",
    "description": (
        "Download, verify (SHA-256), and organize received deliveries and file artifacts from outsourced/purchased orders "
        "into a structured local vault (~/.hermes/operate_deliveries/ with index.json, per-order folders, v1/v2 versions, summary.md, output.json, and artifacts/)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "orderId": {
                "type": "string",
                "description": "Optional specific orderId to sync. If omitted, syncs all delivered/completed orders.",
            },
            "deliveries_dir": {
                "type": "string",
                "description": "Optional custom local directory path (defaults to OPERATE_DELIVERIES_DIR or ~/.hermes/operate_deliveries)",
            },
            "download_files": {
                "type": "boolean",
                "description": "Whether to download binary/media artifact files (default: true)",
            },
        },
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
        "orders_hired": 0,
        "deliveries_synced": 0,
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
        hired_delta: int = 0,
        synced_delta: int = 0,
        earned_delta_usd: float = 0.0,
        heartbeat_now: bool = False,
        activity: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = _get_telemetry()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        data["gigs_created"] = int(data.get("gigs_created", 0)) + gigs_delta
        data["jobs_claimed"] = int(data.get("jobs_claimed", 0)) + claimed_delta
        data["jobs_fulfilled"] = int(data.get("jobs_fulfilled", 0)) + fulfilled_delta
        data["orders_hired"] = int(data.get("orders_hired", 0)) + hired_delta
        data["deliveries_synced"] = int(data.get("deliveries_synced", 0)) + synced_delta
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

    def _build_client(custom_deliveries_dir: Optional[str] = None) -> OperateCenterClient:
        cfg_key: Optional[str] = None
        cfg_url: Optional[str] = None
        cfg_dir: Optional[str] = None
        if hasattr(ctx, "get_config"):
            try:
                raw_key = ctx.get_config("api_key")
                if isinstance(raw_key, str) and raw_key.strip():
                    cfg_key = raw_key.strip()
                raw_url = ctx.get_config("api_url")
                if isinstance(raw_url, str) and raw_url.strip():
                    cfg_url = raw_url.strip()
                raw_dir = ctx.get_config("deliveries_dir")
                if isinstance(raw_dir, str) and raw_dir.strip():
                    cfg_dir = raw_dir.strip()
            except Exception:
                pass
        api_key = _resolve_secret("OPERATE_API_KEY", "") or cfg_key or ""
        api_url = cfg_url or _resolve_secret("OPERATE_API_URL", DEFAULT_API_URL) or DEFAULT_API_URL
        deliv_dir = (
            custom_deliveries_dir
            or cfg_dir
            or _resolve_secret("OPERATE_DELIVERIES_DIR", DEFAULT_DELIVERIES_DIR)
            or DEFAULT_DELIVERIES_DIR
        )
        return OperateCenterClient(api_key=api_key, api_url=api_url, deliveries_dir=deliv_dir)

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
                fulfillment_type=str(p.get("fulfillment_type") or "autonomous_agent"),
                gig_type=str(p.get("gig_type") or "one_off"),
                billing_interval=str(p.get("billing_interval") or "one_time"),
                requires_computer_use=bool(p.get("requires_computer_use", False)),
                portfolio_samples=p.get("portfolio_samples"),
            )
            mode = str(p.get("fulfillment_type") or "autonomous_agent")
            _record_telemetry(
                gigs_delta=1,
                activity=f"🚀 Published {mode} gig '{clean_name}' (${price:.2f} USD)",
            )
            return res

        return _call_mcp_or_client("operate_create_gig", args or {}, _fb)

    def handle_update_gig(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            gig_id = str(p.get("gigId") or p.get("gig_id") or p.get("id") or "").strip()
            if not gig_id:
                raise ValueError("Missing required parameter 'gigId'")
            res = client.update_gig(
                gig_id=gig_id,
                name=p.get("name"),
                description=p.get("description"),
                price_usd=p.get("price_usd"),
                category=p.get("category"),
                subservice=p.get("subservice"),
                delivery_sla=p.get("delivery_sla"),
                packages=p.get("packages"),
                extras=p.get("extras"),
                tags=p.get("tags"),
                input_schema=p.get("input_schema"),
                output_schema=p.get("output_schema"),
                fulfillment_type=p.get("fulfillment_type"),
                gig_type=p.get("gig_type"),
                billing_interval=p.get("billing_interval"),
                requires_computer_use=p.get("requires_computer_use"),
                is_published=p.get("is_published"),
            )
            _record_telemetry(activity=f"🛠️ Updated gig #{gig_id}")
            return res

        return _call_mcp_or_client("operate_update_gig", args or {}, _fb)

    def handle_list_gigs(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        return _call_mcp_or_client(
            "operate_list_gigs",
            args or {},
            lambda client, p: client.list_gigs(
                category=p.get("category"),
                mine_only=bool(p.get("mine_only", False)),
            ),
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
                file_paths=p.get("file_paths"),
            )
            payout = float(res.get("payout_usd", 0.0) or res.get("expected_payout_usd", 0.0) or 0.0)
            _record_telemetry(
                fulfilled_delta=1,
                earned_delta_usd=payout,
                activity=f"💰 Fulfilled job #{job_id}" + (f" (+${payout:.2f} USD)" if payout else ""),
            )
            return res

        # If local file_paths are passed, always run through the Python client so multipart upload executes locally
        if isinstance(args, dict) and args.get("file_paths"):
            try:
                return json.dumps(_fb(_build_client(), args), indent=2)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        return _call_mcp_or_client("operate_submit_fulfillment", args or {}, _fb)

    def handle_get_earnings(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            hub_data = client.get_earnings()
            if p.get("notify_owner"):
                sv = hub_data.get("seller_verification") or {}
                default_title = (
                    "Action Required: Verify Stripe Connect for Operate.Center Payouts"
                    if not sv.get("stripe_onboarding_complete")
                    else "Operate.Center Agent Earnings & Status Update"
                )
                default_msg = str(
                    p.get("owner_message")
                    or sv.get("owner_message")
                    or "Your Hermes Agent checked live earnings and payout verification status on Operate.Center."
                )
                notify_res = client.notify_owner(
                    title=str(p.get("owner_title") or default_title),
                    message=default_msg,
                    link="/dashboard/earnings",
                    notification_type=(
                        "payout_awaiting_onboarding"
                        if not sv.get("stripe_onboarding_complete")
                        else "system"
                    ),
                )
                hub_data["owner_notified"] = notify_res
                _record_telemetry(activity="📊 Checked seller earnings & notified human owner")
            else:
                _record_telemetry(activity="📊 Checked seller earnings & Stripe verification status")
            return hub_data

        return _call_mcp_or_client("operate_get_earnings", args or {}, _fb)

    def handle_hire_gig(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            operator_id = str(p.get("operator_id") or p.get("operatorId") or "").strip()
            if not operator_id:
                raise ValueError("Missing required parameter 'operator_id'")
            input_data = p.get("input_data") if isinstance(p.get("input_data"), dict) else p.get("inputData")
            if not isinstance(input_data, dict):
                raise ValueError("Missing or invalid required parameter 'input_data' (expected object)")
            res = client.hire_gig(
                operator_id=operator_id,
                input_data=input_data,
                package_tier=p.get("package_tier") or p.get("packageTier"),
                selected_extras=p.get("selected_extras") or p.get("selectedExtras"),
                billing_mode=p.get("billing_mode") or p.get("billingMode"),
                file_paths=p.get("file_paths"),
            )
            oid = res.get("order_id") or "pending"
            _record_telemetry(
                hired_delta=1,
                activity=f"🛒 Hired gig '{operator_id}' (order #{oid})",
            )
            return res

        if isinstance(args, dict) and args.get("file_paths"):
            try:
                return json.dumps(_fb(_build_client(), args), indent=2)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        return _call_mcp_or_client("operate_hire_gig", args or {}, _fb)

    def handle_manage_order(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, p: Dict[str, Any]) -> Any:
            action = str(p.get("action") or "get").strip().lower()
            order_id = str(p.get("orderId") or p.get("order_id") or "").strip()

            if action == "list":
                return {
                    "orders": client.list_my_orders(
                        status=p.get("status"),
                        order_id=order_id or None,
                        all_owner_orders=bool(p.get("all_owner_orders", True)),
                    )
                }

            if not order_id:
                raise ValueError(f"Missing required parameter 'orderId' for action '{action}'")

            if action == "get":
                return client.get_order(order_id)

            if action == "submit_requirements":
                input_data = p.get("input_data")
                if not isinstance(input_data, dict):
                    raise ValueError("Missing required parameter 'input_data' (object) for submit_requirements")
                res = client.submit_requirements(order_id, input_data, file_paths=p.get("file_paths"))
                _record_telemetry(activity=f"📦 Submitted project requirements for order #{order_id}")
                return res

            if action == "approve":
                res = client.approve_delivery(
                    order_id=order_id,
                    rating=p.get("rating", 5),
                    review_text=str(p.get("review_text") or ""),
                )
                _record_telemetry(activity=f"✅ Approved delivery for order #{order_id}")
                return res

            if action == "request_revision":
                feedback = str(p.get("feedback") or "").strip()
                if not feedback:
                    raise ValueError("Missing required parameter 'feedback' for request_revision")
                res = client.request_revision(
                    order_id=order_id,
                    feedback=feedback,
                    specific_changes=p.get("specific_changes"),
                )
                _record_telemetry(activity=f"🔄 Requested revision on order #{order_id}")
                return res

            if action == "messages":
                return {"order_id": order_id, "messages": client.get_order_messages(order_id)}

            if action == "send_message":
                msg = str(p.get("message") or p.get("content") or "").strip()
                if not msg:
                    raise ValueError("Missing required parameter 'message' for send_message")
                res = client.send_order_message(order_id, msg, attachments=p.get("attachments"))
                _record_telemetry(activity=f"💬 Sent message on order #{order_id}")
                return res

            raise ValueError(
                f"Unsupported action '{action}'. Expected one of: get, list, submit_requirements, approve, request_revision, messages, send_message"
            )

        if isinstance(args, dict) and args.get("file_paths"):
            try:
                return json.dumps(_fb(_build_client(), args), indent=2)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        return _call_mcp_or_client("operate_manage_order", args or {}, _fb)

    def handle_sync_deliveries(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs
        p = args or {}
        try:
            client = _build_client(custom_deliveries_dir=p.get("deliveries_dir"))
            order_id = str(p.get("orderId") or p.get("order_id") or "").strip() or None
            download_files = bool(p.get("download_files", True))
            res = client.sync_deliveries(order_id=order_id, download_files=download_files)
            count = int(res.get("synced_count", 0))
            _record_telemetry(
                synced_delta=count,
                activity=f"🗂️ Synced {count} delivery package(s) to {client.deliveries_dir}",
            )
            return json.dumps(res, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def handle_heartbeat(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        del kwargs

        def _fb(client: OperateCenterClient, _p: Dict[str, Any]) -> Any:
            res = client.heartbeat()
            _record_telemetry(heartbeat_now=True, activity="🟢 Sent Fleet Ready heartbeat")
            return res

        return _call_mcp_or_client("operate_heartbeat", args or {}, _fb)

    tools_to_register = [
        ("operate_create_gig", CREATE_GIG_SCHEMA, handle_create_gig, "Publish a new autonomous, manual, or hybrid gig on Operate.Center", "🚀"),
        ("operate_update_gig", UPDATE_GIG_SCHEMA, handle_update_gig, "Update gig details, fulfillment mode, or pause/resume a listing", "🛠️"),
        ("operate_list_gigs", LIST_GIGS_SCHEMA, handle_list_gigs, "Browse published gigs on Operate.Center", "🏪"),
        ("operate_check_queue", CHECK_QUEUE_SCHEMA, handle_check_queue, "Check queued paid orders and revision requests on Operate.Center", "📥"),
        ("operate_accept_job", ACCEPT_JOB_SCHEMA, handle_accept_job, "Accept a queued job and claim an execution lease", "🔒"),
        ("operate_submit_fulfillment", SUBMIT_FULFILLMENT_SCHEMA, handle_submit_fulfillment, "Upload artifacts and submit completed deliverable to trigger payout", "💰"),
        ("operate_get_earnings", GET_EARNINGS_SCHEMA, handle_get_earnings, "Check live earnings, Stripe Connect KYC status, and notify human owner", "📊"),
        ("operate_hire_gig", HIRE_GIG_SCHEMA, handle_hire_gig, "Hire a marketplace gig/operator to outsource work as a buyer agent", "🛒"),
        ("operate_manage_order", MANAGE_ORDER_SCHEMA, handle_manage_order, "Inspect orders, submit requirements, approve deliveries, request revisions, or message", "📦"),
        ("operate_sync_deliveries", SYNC_DELIVERIES_SCHEMA, handle_sync_deliveries, "Download, verify (SHA-256), and organize received deliveries into local vault", "🗂️"),
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
                            description="Operate.Center marketplace workflow for auto/manual gigs, queue fulfillment, Stripe KYC, outsourcing, and delivery organization.",
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
                stripe_line = "  🏦 Stripe KYC Status    : 🟡 Connect OPERATE_API_KEY to check live payout status"
                if has_key:
                    try:
                        hub = client.get_earnings()
                        sv = hub.get("seller_verification") or {}
                        summary = hub.get("summary") or {}
                        if sv.get("stripe_onboarding_complete"):
                            stripe_line = "  🏦 Stripe KYC Status    : 🟢 Verified (80% Creator Payouts Active)"
                        else:
                            days = sv.get("grace_period_days_left", 90)
                            stripe_line = (
                                f"  🏦 Stripe KYC Status    : 🟡 Unverified ({days}d grace window left — verify at https://operate.center/dashboard/earnings)"
                            )
                        if summary.get("total_earnings_usd") is not None:
                            stats["total_earned_usd"] = max(
                                float(stats.get("total_earned_usd", 0.0)),
                                float(summary.get("total_earnings_usd", 0.0)),
                            )
                    except Exception:
                        stripe_line = "  🏦 Stripe KYC Status    : ⚪ Offline (using cached local telemetry)"

                recent = stats.get("recent_activity") or ["No recent events recorded yet — run `/operate demo` to test!"]
                recent_lines = "\n".join(f"  • {item}" for item in recent[:5])
                return "\n".join([
                    "╭──────────────────────────────────────────────────────────────╮",
                    f"│  ⚡ Operate.Center × Hermes Agent Marketplace (v{__version__})       │",
                    "╰──────────────────────────────────────────────────────────────╯",
                    f"  🌐 Marketplace Endpoint : {client.api_url}",
                    f"  🔑 Agent Auth Status    : {key_badge}",
                    stripe_line,
                    f"  🗂️ Local Delivery Vault : {client.deliveries_dir}",
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
                    "  💡 Commands: /operate status | /operate demo | /operate gigs [category] | /operate queue | /operate orders | /operate deliveries | /operate heartbeat",
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
                        f"📥 Pending Funded & Revision Orders ({len(jobs)}):",
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

            if subcmd == "orders":
                if not has_key:
                    return "🔑 OPERATE_API_KEY is required to list purchased/outsourced buyer orders."
                try:
                    orders = client.list_my_orders(status=arg or None, all_owner_orders=True)
                    if not orders:
                        return "📦 No purchased/outsourced orders found."
                    rows = [
                        f"📦 Purchased / Outsourced Orders ({len(orders)}):",
                        "  ┌──────────────────────┬──────────────────────────────┬────────────┬──────────────┐",
                        "  │ Order ID             │ Operator ID                  │ Gross USD  │ Status       │",
                        "  ├──────────────────────┼──────────────────────────────┼────────────┼──────────────┤",
                    ]
                    for o in orders[:10]:
                        oid = str(o.get("id", ""))[:20]
                        op_id = str(o.get("operator_id", ""))[:28]
                        cents = o.get("amount_cents")
                        usd = (float(cents) / 100.0) if cents is not None else float((o.get("input_data") or {}).get("price_usd", 0.0) or 0.0)
                        st = str(o.get("status", ""))[:12]
                        rows.append(f"  │ {oid:<20} │ {op_id:<28} │ ${usd:<9.2f} │ {st:<12} │")
                    rows.append("  └──────────────────────┴──────────────────────────────┴────────────┴──────────────┘")
                    return "\n".join(rows)
                except Exception as exc:
                    return f"⚠️ Error listing buyer orders: {exc}"

            if subcmd == "deliveries":
                try:
                    if has_key:
                        sync_res = client.sync_deliveries(order_id=arg or None)
                        _record_telemetry(
                            synced_delta=int(sync_res.get("synced_count", 0)),
                            activity=f"🗂️ Synced {sync_res.get('synced_count', 0)} deliveries via /operate deliveries",
                        )
                    index_path = client.deliveries_dir / "index.json"
                    if not index_path.is_file():
                        return (
                            f"🗂️ Local Delivery Vault (`{client.deliveries_dir}`): 0 deliveries saved yet.\n"
                            "  • Hire services with `operate_hire_gig` and run `operate_sync_deliveries` (or `/operate deliveries`) when orders are delivered."
                        )
                    idx_data = json.loads(index_path.read_text(encoding="utf-8"))
                    orders_map = idx_data.get("orders") or {}
                    rows = [
                        f"🗂️ Organized Local Delivery Vault (`{client.deliveries_dir}`) — {len(orders_map)} Order(s):",
                    ]
                    for oid, meta in list(orders_map.items())[:10]:
                        rows.append(
                            f"  • [{oid[:8]}] {meta.get('operator_name', 'Service')} (v{meta.get('latest_version', 1)}, status={meta.get('status')}) → {meta.get('order_dir')}"
                        )
                    return "\n".join(rows)
                except Exception as exc:
                    return f"⚠️ Error syncing deliveries vault: {exc}"

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
                "Available: /operate status, /operate demo, /operate gigs [category], /operate queue, /operate orders, /operate deliveries, /operate heartbeat"
            )

        try:
            ctx.register_command(
                "operate",
                handle_operate_command,
                description="Inspect Operate.Center earnings & Stripe KYC status, run a demo, poll queued orders, browse gigs, inspect buyer orders, sync local deliveries, or send a heartbeat",
                args_hint="[status|demo|queue|gigs|orders|deliveries|heartbeat]",
            )
        except Exception:
            pass

    # Register Automation Blueprint for autonomous queue polling & delivery syncing
    if hasattr(ctx, "register_automation_blueprint"):
        try:
            ctx.register_automation_blueprint(
                "autonomous-worker",
                title="Operate.Center Autonomous Worker",
                description="Periodically send a Fleet Ready heartbeat, poll Operate.Center for funded orders & revisions, fulfill queued jobs, check Stripe KYC status, and sync incoming buyer deliveries.",
                schedule_template="*/15 * * * *",
                prompt_template=(
                    "1. Call `operate_heartbeat` to keep Fleet Ready status online.\n"
                    "2. Call `operate_check_queue` to check for funded orders or buyer revision requests. "
                    "For each autonomous or hybrid job, claim a lease with `operate_accept_job`, complete the requested work "
                    "(or notify the human owner if a manual_human step is required), and deliver using `operate_submit_fulfillment`.\n"
                    "3. Call `operate_get_earnings` to check if the human owner needs to complete Stripe Connect KYC verification, "
                    "and if `action_required` is true, notify them.\n"
                    "4. Call `operate_sync_deliveries` to download and organize any completed deliveries from services this agent has purchased."
                ),
                category="productivity",
                skills=("operate-hermes-plugin:operate-center",),
                tags=("marketplace", "operate-center", "autonomous"),
            )
        except Exception:
            pass
