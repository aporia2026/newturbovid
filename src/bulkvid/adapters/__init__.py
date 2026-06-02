"""External-service adapters. One file per service.

Each adapter exposes async functions that return ``(result, cost_usd)`` tuples
so the orchestrator can sum cost per row / per batch (plan §11).
"""
