"""Consumption layer: learn what the warehouse actually queried.

It turns warehouse facts (query-history usage, storage bytes) into the model-grain inputs the
verdict layer needs. The warehouse is reached only through the `WarehouseClient` Protocol so the
engine can be exercised with a fake and no credentials. The SQL builders and row parsers (`jobs`
for BigQuery, `snowflake_queries` for Snowflake) are pure and tested directly; each real client
(`bigquery`, `snowflake`) is the only module that imports its warehouse SDK.
"""
