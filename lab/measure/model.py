"""Read Auto-Ontology's exported model into qualified names that can be compared.

The export refers to everything by opaque id, and `foreign_keys`, `joins` and `semantic_fks`
come back in no fixed order (UPSTREAM_ARCHITECTURE.md §0.7). Everything here is expressed as
`schema.table` or `schema.table.column`, and relationships as unordered pairs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from lab.common.errors import LabError
from lab.common.gateway import Gateway
from lab.common.pg import Psql

Pair = frozenset[str]


@dataclass(frozen=True)
class Ontology:
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)
    tables: set[str] = field(default_factory=set)
    term_tables: set[str] = field(default_factory=set)
    terms_by_table: dict[str, dict[str, Any]] = field(default_factory=dict)
    attributes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fks: set[Pair] = field(default_factory=set)
    semantic_fks: set[Pair] = field(default_factory=set)
    sql_attributes: list[dict[str, Any]] = field(default_factory=list)
    manual_sql_attributes: list[dict[str, Any]] = field(default_factory=list)


def fetch_model(admin: Gateway, psql: Psql, catalog_db: str, database: str) -> Ontology:
    """Export one database. The API takes only ids, and no route maps a name to its id, so read the catalog."""
    literal = database.replace("'", "''")
    database_id = psql.scalar(catalog_db, f"SELECT id FROM catalog_database WHERE name = '{literal}'")
    if not database_id:
        raise LabError(f"database {database!r} is not registered in the catalog")
    return parse_model(admin.post_text("/api/model/export", {"databases": [database_id], "format": "auto_ontology"}))


def parse_model(text: str) -> Ontology:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise LabError(f"exported model is not valid YAML: {exc}") from exc

    data = doc.get("data_layer") or {}
    semantic = doc.get("semantic_layer") or {}
    table_names: dict[str, str] = {}
    column_names: dict[str, str] = {}
    columns: dict[str, dict[str, Any]] = {}
    for database in data.get("databases") or []:
        for schema in database.get("schemas") or []:
            for table in schema.get("tables") or []:
                qualified = f"{schema.get('name')}.{table.get('name')}"
                table_names[str(table["id"])] = qualified
                for column in table.get("columns") or []:
                    name = f"{qualified}.{column.get('name')}"
                    column_names[str(column["id"])] = name
                    columns[name] = column

    term_tables: set[str] = set()
    terms_by_table: dict[str, dict[str, Any]] = {}
    attributes: dict[str, list[dict[str, Any]]] = {}
    attribute_columns: dict[str, str] = {}
    for term in semantic.get("terms") or []:
        for table_id in term.get("represents") or []:
            table = table_names.get(str(table_id))
            if table:
                term_tables.add(table)
                terms_by_table[table] = term
        for attribute in term.get("columns_attributes") or []:
            column = column_names.get(str(attribute.get("column_id") or ""))
            if column:
                attribute_columns[str(attribute["id"])] = column
                attributes.setdefault(column, []).append({**attribute, "term_id": term.get("id")})

    fks = _pairs(
        (column_names.get(str(fk.get("source_column_id"))), column_names.get(str(fk.get("target_column_id"))))
        for fk in data.get("foreign_keys") or []
    )
    semantic_fks = _pairs(
        (attribute_columns.get(str(fk.get("column_attribute_id"))), column_names.get(str(fk.get("column_id"))))
        for fk in semantic.get("semantic_fks") or []
    )
    by_source = semantic.get("sql_attributes") or {}
    return Ontology(
        columns=columns,
        tables=set(table_names.values()),
        term_tables=term_tables,
        terms_by_table=terms_by_table,
        attributes=attributes,
        fks=fks,
        semantic_fks=semantic_fks,
        sql_attributes=[attr for group in by_source.values() for attr in group or []],
        manual_sql_attributes=list(by_source.get("manual") or []),
    )


def pair_as_list(pair: Pair) -> list[str]:
    return sorted(pair)


def _pairs(rows) -> set[Pair]:
    return {frozenset(row) for row in rows if row[0] and row[1] and row[0] != row[1]}
