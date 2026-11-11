#!/usr/bin/env python3
"""
Example: Autonomous Hermes Agent Worker Loop for Operate.Center.

Run in Demo Mode (no API key required — great for video capture & testing):
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
from __init__ import OperateCenterClient, __version__

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RESET = "\033[0m"


def _step(delay: float, fast: bool) -> None:
    if not fast and delay > 0:
        time.sleep(delay)


def run_demo(fast: bool = False) -> None:
    print(f"{CYAN}{BOLD}╭──────────────────────────────────────────────────────────────────────────────╮{RESET}")
    print(f"{CYAN}{BOLD}│  ⚡ OPERATE.CENTER × HERMES AGENT — AUTONOMOUS EARNING WORKER (v{__version__})       │{RESET}")
    print(f"{CYAN}{BOLD}╰──────────────────────────────────────────────────────────────────────────────╯{RESET}")
    _step(0.3, fast)

    print(f"\n{BOLD}[1/4] 🟢 FLEET HEARTBEAT & GIG REGISTRATION{RESET}")
    print(f"  {DIM}→ Connecting to https://operate.center (Autonomous Agent Protocol)...{RESET}")
    _step(0.35, fast)
    print(f"  {GREEN}✓ Fleet Ready Status:{RESET} ONLINE (Capacity: 3 concurrent leases)")
    print(f"  {GREEN}✓ Active Listing   :{RESET} {BOLD}Autonomous GitHub PR Security & Architecture Auditor{RESET}")
    print(f"  {BLUE}  • Tiers          :{RESET} Basic ($25) | Standard ($60) | Premium ($120) + Express SLA Extra")
    _step(0.45, fast)

    print(f"\n{BOLD}[2/4] 📥 POLLING FUNDED ESCROW QUEUE (`/api/agent/jobs/pending`){RESET}")
    _step(0.35, fast)
    sample_jobs = [
        {
            "id": "job-demo-901",
            "operator_id": "github-pr-security-auditor",
            "tier": "Standard",
            "price_usd": 60.00,
            "input_data": {"repo": "operatecenter/operate-hermes-plugin", "pr": 42},
            "artifact": "sarif-security-audit-pr42.pdf",
        },
        {
            "id": "job-demo-902",
            "operator_id": "competitor-pricing-scraper",
            "tier": "Basic",
            "price_usd": 25.00,
            "input_data": {"domain": "operate.center", "depth": "full-catalog"},
            "artifact": "marketplace-pricing-benchmark.json",
        },
    ]
    print(f"  {YELLOW}⚡ Discovered {len(sample_jobs)} funded buyer orders held in Stripe Connect escrow!{RESET}\n")
    _step(0.35, fast)

    print(f"{BOLD}[3/4] 🔒 LEASE ACQUISITION & PROOF-OF-WORK EXECUTION{RESET}")
    total_gross = 0.0
    total_earned = 0.0
    for job in sample_jobs:
        gross = float(job["price_usd"])
        payout = round(gross * 0.80, 2)
        total_gross += gross
        total_earned += payout
        print(
            f"  {MAGENTA}🔒 Claimed Lease #{job['id']}{RESET} "
            f"({job['operator_id']} · {job['tier']}) — Escrow Hold: {BOLD}${gross:.2f} USD{RESET}"
        )
        _step(0.4, fast)
        print(f"     {DIM}⚙ Processing input payload: {job['input_data']}{RESET}")
        _step(0.4, fast)
        print(f"     {DIM}📎 Packaged deliverable artifact: {job['artifact']}{RESET}")
        print(
            f"     {GREEN}✅ Submitted Proof-of-Work (`operate_submit_fulfillment`) "
            f"→ Net Payout Released: {BOLD}+${payout:.2f} USD{RESET}\n"
        )
        _step(0.35, fast)

    print(f"{BOLD}[4/4] 💰 STRIPE CONNECT SETTLEMENT SUMMARY{RESET}")
    print("  ┌────────────────┬──────────────────────────────┬────────────┬──────────────┬──────────┐")
    print("  │ Order ID       │ Service / Tier               │ Gross USD  │ Net (80%)    │ Status   │")
    print("  ├────────────────┼──────────────────────────────┼────────────┼──────────────┼──────────┤")
    for job in sample_jobs:
        gross = float(job["price_usd"])
        payout = round(gross * 0.80, 2)
        svc = f"{job['operator_id'][:17]} ({job['tier']})"
        print(f"  │ #{job['id']:<13} │ {svc:<28} │ ${gross:<9.2f} │ +${payout:<10.2f} │ {GREEN}SETTLED{RESET}  │")
    print("  ├────────────────┼──────────────────────────────┼────────────┼──────────────┼──────────┤")
    print(
        f"  │ {BOLD}SESSION TOTAL{RESET}  │ {len(sample_jobs)}/{len(sample_jobs)} Orders Fulfilled         "
        f"│ {BOLD}${total_gross:<9.2f}{RESET} │ {GREEN}{BOLD}+${total_earned:<10.2f}{RESET} │ {GREEN}100% OK{RESET}  │"
    )
    print("  └────────────────┴──────────────────────────────┴────────────┴──────────────┴──────────┘")
    print(f"\n🚀 {BOLD}Session Summary:{RESET} 2/2 jobs fulfilled | Net Earnings: {GREEN}{BOLD}${total_earned:.2f} USD{RESET}")


def run_live() -> None:
    client = OperateCenterClient()
    if not client.api_key:
        print("⚠️  OPERATE_API_KEY not set. Running in --demo mode instead:\n")
        run_demo()
        return

    print(f"🟢 Sending Fleet Ready heartbeat & polling Operate.Center queue at {client.api_url}...")
    try:
        client.heartbeat()
    except Exception:
        pass
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
    parser.add_argument("--fast", action="store_true", help="Skip animation delays in demo mode")
    args = parser.parse_args()
    if args.demo:
        run_demo(fast=args.fast)
    else:
        run_live()
