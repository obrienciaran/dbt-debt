"""Load and parse dbt's manifest.json into domain objects.

We read the artifact as plain JSON rather than importing dbt: it is lighter and avoids
coupling the tool to a dbt version. Unknown keys are ignored so minor schema changes do not
break loading; the schema version is captured so a breaking change is at least visible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dbt_debt.artifacts._json import as_dict, load_artifact
from dbt_debt.domain import (
    ColumnRef,
    Exposure,
    Manifest,
    Model,
    Relation,
    SemanticConsumer,
    Test,
    relation_key,
)
from dbt_debt.sqlparse import expression_columns

logger = logging.getLogger(__name__)

KNOWN_SCHEMA_PREFIX = "https://schemas.getdbt.com/dbt/manifest/v"


def load_manifest(path: str | Path) -> Manifest:
    """Read manifest.json from disk and parse it into a Manifest.

    Raises `ArtifactError` (with the path in the message) when the file cannot be read or is
    not valid artifact JSON.
    """

    return parse_manifest(load_artifact(path))


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Parse an already-loaded manifest dict into domain objects."""

    metadata = as_dict(data.get("metadata"))
    schema_version = str(metadata.get("dbt_schema_version", ""))
    _check_schema_version(schema_version)

    models: dict[str, Model] = {}
    tests: dict[str, Test] = {}
    relations: dict[str, Relation] = {}
    for unique_id, node in as_dict(data.get("nodes")).items():
        resource_type = node.get("resource_type")
        if resource_type in ("model", "seed", "snapshot"):
            models[unique_id] = _parse_model(unique_id, node, resource_type)
        elif resource_type == "test":
            tests[unique_id] = _parse_test(unique_id, node)

    for unique_id, node in as_dict(data.get("sources")).items():
        relations[unique_id] = _parse_relation(unique_id, node)

    exposures = {
        unique_id: _parse_exposure(unique_id, node)
        for unique_id, node in as_dict(data.get("exposures")).items()
    }

    # The semantic layer's three node kinds (dbt 1.6+, manifest v12 top-level keys). Their
    # shapes follow the published manifest schema; the parsing stays lenient (`get` everywhere)
    # since we have not verified them against a populated real-world manifest.
    semantic_consumers: dict[str, SemanticConsumer] = {}
    for unique_id, node in as_dict(data.get("semantic_models")).items():
        semantic_consumers[unique_id] = _parse_semantic_model(unique_id, node)
    for key, kind in (("metrics", "metric"), ("saved_queries", "saved_query")):
        for unique_id, node in as_dict(data.get(key)).items():
            semantic_consumers[unique_id] = SemanticConsumer(
                unique_id=unique_id,
                name=node.get("name", ""),
                kind=kind,
                depends_on=_depends_on(node),
            )

    return Manifest(
        project_name=str(metadata.get("project_name", "")),
        dbt_schema_version=schema_version,
        dbt_version=metadata.get("dbt_version"),
        models=models,
        tests=tests,
        exposures=exposures,
        relations=relations,
        semantic_consumers=semantic_consumers,
    )


def _parse_model(unique_id: str, node: dict[str, Any], resource_type: str = "model") -> Model:
    # Column names are lowercased here (and in the catalog and test parsers) so every layer
    # compares them the same way, matching the relation_key normalization. Seeds and snapshots
    # share the node shape; a seed simply has no compiled_code and no dependencies.
    columns = tuple(name.lower() for name in as_dict(node.get("columns")))
    return Model(
        unique_id=unique_id,
        name=node.get("name", ""),
        database=node.get("database"),
        schema=node.get("schema"),
        alias=node.get("alias"),
        original_file_path=node.get("original_file_path"),
        depends_on=_depends_on(node),
        columns=columns,
        contract_enforced=bool(as_dict(node.get("contract")).get("enforced", False)),
        compiled_code=node.get("compiled_code"),
        resource_type=resource_type,
    )


def _parse_test(unique_id: str, node: dict[str, Any]) -> Test:
    column_name = node.get("column_name")
    return Test(
        unique_id=unique_id,
        name=node.get("name", ""),
        depends_on=_depends_on(node),
        attached_node=node.get("attached_node"),
        column_name=column_name.lower() if column_name else None,
    )


def _parse_relation(unique_id: str, node: dict[str, Any]) -> Relation:
    """Parse a source into a `Relation`.

    A source keys off `identifier` (falling back to `name`). The relation_key is built the same
    way as a model's so both sides of the orphan subtraction compare equal.
    """

    database = node.get("database")
    schema = node.get("schema")
    identifier = node.get("identifier") or node.get("name")
    return Relation(
        unique_id=unique_id,
        relation_key=relation_key(database, schema, identifier),
        schema=schema,
    )


def _parse_semantic_model(unique_id: str, node: dict[str, Any]) -> SemanticConsumer:
    """Parse one semantic model, resolving its entity/dimension/measure exprs to column refs.

    Each element's `expr` (falling back to its `name`) names the columns it reads; those are
    paired with every model the semantic model depends on — usually exactly one. Expressions
    that fail to parse contribute no refs, which is conservative: the model-grain flag still
    protects the model itself.
    """

    depends_on = _depends_on(node)
    model_deps = tuple(dep for dep in depends_on if dep.startswith("model."))
    columns: set[str] = set()
    for section in ("entities", "dimensions", "measures"):
        elements = node.get(section) or []
        for element in elements:
            if not isinstance(element, dict):
                continue
            # `expr` may be null (the column is just `name`) or a non-string (e.g. YAML
            # `expr: true`), in which case the name is the best available column reference.
            expr = element.get("expr")
            if not isinstance(expr, str):
                expr = element.get("name")
            if isinstance(expr, str):
                columns.update(expression_columns(expr))
    column_refs: tuple[ColumnRef, ...] = tuple(
        sorted((dep, column) for dep in model_deps for column in columns)
    )
    return SemanticConsumer(
        unique_id=unique_id,
        name=node.get("name", ""),
        kind="semantic_model",
        depends_on=depends_on,
        column_refs=column_refs,
    )


def _parse_exposure(unique_id: str, node: dict[str, Any]) -> Exposure:
    return Exposure(
        unique_id=unique_id,
        name=node.get("name", ""),
        depends_on=_depends_on(node),
    )


def _depends_on(node: dict[str, Any]) -> tuple[str, ...]:
    nodes = as_dict(node.get("depends_on")).get("nodes") or []
    return tuple(nodes)


def _check_schema_version(schema_version: str) -> None:
    if not schema_version:
        logger.warning("manifest has no dbt_schema_version; parsing best-effort")
    elif not schema_version.startswith(KNOWN_SCHEMA_PREFIX):
        logger.warning(
            "unrecognized manifest schema version %r; parsing best-effort", schema_version
        )
