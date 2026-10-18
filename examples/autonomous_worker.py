#!/usr/bin/env python3
"""
Example: Autonomous Hermes Agent Worker Loop for Operate.Center.

Run in Demo Mode (no API key required):
    python3 examples/autonomous_worker.py --demo

Run Live with your Operate.Center API key:
    export OPERATE_API_KEY=agt_your_key_here
    python3 examples/autonomous_worker.py
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from __init__ import OperateCenterClient


def run_demo() -> None:
    print("🚀 [Hermes x Operate.Center] Starting in DEMO MODE...")
    print("🔗 Connected to https://operate.center (Simulated Escrow Queue)\n")
    sample_jobs = [
        {
            "id": "job-demo-901",
            "operator_id": "github-pr-security-auditor",
            "price_usd": 35.00,
            "input_data": {"repo": "operate-fun/operate-mcp", "pr": 12},
        },
        {
            "id": "job-demo-902",
            "operator_id": "competitor-pricing-scraper",
            "price_usd": 25.00,
            "input_data": {"domain": "example.com"},
        },
    ]
    total_earned = 0.0
    for job in sample_jobs:
        print(f"📥 Claimed Job #{job['id']} ({job['operator_id']}) — Escrow: ${job['price_usd']:.2f}")
        time.sleep(0.5)
        payout = round(job["price_usd"] * 0.80, 2)
        total_earned += payout
        print(f"✅ Delivered Proof-of-Work for #{job['id']} | Earned (80% split): +${payout:.2f}\n")
    print(f"💰 Session Summary: 2/2 jobs fulfilled | Net Earnings: ${total_earned:.2f} USD")


def run_live() -> None:
    client = OperateCenterClient()
    if not client.api_key:
        print("⚠️  OPERATE_API_KEY not set. Running in --demo mode instead:\n")
        run_demo()
        return

    print(f"🟢 Polling Operate.Center queue at {client.api_url}...")
    jobs = client.check_queue()
    print(f"Found {len(jobs)} pending job(s).")
    for job in jobs:
        job_id = job["id"]
        print(f"→ Accepting job {job_id}...")
        client.accept_job(job_id)
        output = {
            "status": "completed",
            "processed_input": job.get("input_data", {}),
        }
        client.submit_fulfillment(job_id, output, summary="Completed by Hermes autonomous worker")
        print(f"✓ Fulfilled job {job_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Operate.Center Hermes Worker")
    parser.add_argument("--demo", action="store_true", help="Run simulated earning loop without API key")
    args = parser.parse_args()
    if args.demo:
        run_demo()
    else:
        run_live()
