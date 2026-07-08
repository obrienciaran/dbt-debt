"""Verdict layer: pure functions from domain objects (+ dead sets) to verdicts.

Nothing here performs I/O or talks to BigQuery. The "dead" sets (dead models, dead columns)
are passed in, computed upstream by the consumption/lineage layers. Keeping this layer pure
makes the load-bearing logic unit-testable with in-memory fixtures and no credentials.
"""
