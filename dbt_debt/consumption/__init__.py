"""Consumption layer: learn what BigQuery actually queried.

It turns warehouse facts (`INFORMATION_SCHEMA.JOBS` usage, storage bytes) into the model-grain
inputs the verdict layer needs. The warehouse is reached only through the `BigQueryClient`
Protocol so the engine can be exercised with a fake and no credentials. The SQL builders and
row parsers in `jobs` are pure and tested directly; the real client in `bigquery` is the only
module that imports `google-cloud-bigquery`.
"""
