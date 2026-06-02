"""Queue, concurrency, and per-row state machine.

Plan §5 ("Concurrency model"). Holds the global semaphore, the kie.ai key
pool, the SQLite job store, and the coalesced sheet writer.
"""
