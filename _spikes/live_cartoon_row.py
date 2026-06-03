"""Live end-to-end smoke: one real cartoon row through the real providers.

Builds the SAME PipelineClients the worker uses (build_pipeline_clients) and runs
process_cartoon_row on one CartoonRow against a real article. This exercises the
full live wiring: article fetch -> OpenAI (language/classify/plan) -> kie
(nano-banana-2 x4 + Seedance x4) -> Rendi concat x2 -> storage. ZapCap left off.

Run (from project root, .venv active):
    python _spikes/live_cartoon_row.py

Estimated spend: ~$0.48. Prints the final status, the two video URLs, and the
cost breakdown.
"""

from __future__ import annotations

import asyncio
import json

from bulkvid.config import get_settings
from bulkvid.models.row import CartoonRow
from bulkvid.orchestrator.row_processor_cartoon import process_cartoon_row
from bulkvid.worker import build_pipeline_clients


async def main() -> None:
    settings = get_settings()
    clients = build_pipeline_clients(settings)

    row = CartoonRow(
        row_num=2,
        country="US",
        vertical="automotive",
        article_url="https://en.wikipedia.org/wiki/Used_car",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )

    print("Running one live cartoon row (this calls real paid APIs)...")
    result = await process_cartoon_row(row, clients, job_id="live-test")

    print("\n=== RESULT ===")
    print("status      :", result.status)
    print("video_urls  :", result.video_urls)
    print("cost_usd    :", result.cost_usd)
    print("elapsed (s) :", result.elapsed_seconds)
    if result.error:
        print("error       :", result.error)
    print("metadata    :", json.dumps(result.metadata, indent=2, default=str))

    for closer in (clients.kie, clients.rendi, clients.openai):
        try:
            await closer.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
