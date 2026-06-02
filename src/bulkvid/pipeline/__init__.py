"""Per-stage pipeline logic.

Each module owns one stage from the per-row pipeline (plan §5). Stages call
adapters but do not own retry / concurrency / queueing — that belongs to the
orchestrator.
"""
