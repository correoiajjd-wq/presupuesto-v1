"""Persistencia.

Nota de implementación honesta: el destino es PostgreSQL (ver schema_postgres.sql
y doc 03 §32). Este repositorio usa sqlite3 de la stdlib para que el prototipo
corra sin dependencias externas. Todo el acceso a datos está aislado acá: el
dominio, el motor de cálculo y el workflow no conocen la base. Cambiar a
PostgreSQL con SQLAlchemy/psycopg es reemplazar este módulo.

Modelo de almacenamiento:
    budget           -> cabecera y versión vigente
    budget_version   -> estado + SNAPSHOT de configuración + TC congelados
    input_value      -> una fila por input (auditable, filtrable)
    audit_event      -> append-only, nunca se sobrescribe
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..domain.config import Configuration
from ..domain.inputs import InputSet, InputValue
from ..domain.money import FXTable
from .budget import (
    AuditEvent, Budget, BudgetService, BudgetVersion, Task, TaskStatus, VersionStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_version_id TEXT
);
CREATE TABLE IF NOT EXISTS budget_version (
    id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES budget(id),
    number INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_version_id TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    configuration_snapshot TEXT NOT NULL,
    fx_rates TEXT NOT NULL,
    tasks TEXT NOT NULL,
    UNIQUE (budget_id, number)
);
CREATE TABLE IF NOT EXISTS input_value (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES budget_version(id),
    identity TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (version_id, identity)
);
CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, version_id TEXT,
    before TEXT, after TEXT, comment TEXT, correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_input_version ON input_value(version_id);
CREATE INDEX IF NOT EXISTS ix_audit_version ON audit_event(version_id);
"""


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def dumps(obj) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def fx_to_dict(fx: FXTable) -> dict:
    return {
        "presentation": fx.presentation,
        "enabled": sorted(fx.enabled),
        "rates": {c: {d.isoformat(): str(r) for d, r in table.items()}
                  for c, table in fx._rates.items()},
    }


def fx_from_dict(data: dict) -> FXTable:
    fx = FXTable(data["presentation"], data["enabled"])
    for c, table in data["rates"].items():
        for day, rate in table.items():
            fx.add(c, date.fromisoformat(day), Decimal(rate))
    return fx


class Repository:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- escritura ---------------------------------------------------------
    def save_budget(self, budget: Budget, audit: Optional[list[AuditEvent]] = None) -> None:
        """Guarda presupuesto + versiones + inputs en una sola transacción."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO budget (id, name, current_version_id) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "current_version_id=excluded.current_version_id",
                (budget.id, budget.name, budget.current_version_id),
            )
            for v in budget.versions.values():
                self.conn.execute(
                    "INSERT INTO budget_version (id, budget_id, number, status, "
                    "source_version_id, created_at, approved_at, configuration_snapshot, "
                    "fx_rates, tasks) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                    "approved_at=excluded.approved_at, tasks=excluded.tasks",
                    (v.id, v.budget_id, v.number, v.status.value, v.source_version_id,
                     v.created_at.isoformat(),
                     v.approved_at.isoformat() if v.approved_at else None,
                     v.configuration.model_dump_json(), dumps(fx_to_dict(v.fx)),
                     dumps({t.id: {"concept": t.concept, "scope_key": t.scope_key,
                                   "label": t.label, "status": t.status.value,
                                   "loader_role": t.loader_role.value,
                                   "reviewer_role": t.reviewer_role.value,
                                   "approver_role": t.approver_role.value}
                            for t in v.tasks.values()})),
                )
                for iv in v.inputs.values:
                    self.conn.execute(
                        "INSERT INTO input_value (version_id, identity, payload) VALUES (?,?,?) "
                        "ON CONFLICT(version_id, identity) DO UPDATE SET payload=excluded.payload",
                        (v.id, iv.identity(), iv.model_dump_json()),
                    )
            for ev in (audit or []):
                self.conn.execute(
                    "INSERT INTO audit_event (at, actor, action, entity_type, entity_id, "
                    "version_id, before, after, comment, correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ev.at.isoformat(), ev.actor, ev.action, ev.entity_type, ev.entity_id,
                     ev.version_id, ev.before, ev.after, ev.comment, ev.correlation_id),
                )

    # -- lectura -----------------------------------------------------------
    def load_budget(self, budget_id: str) -> Budget:
        row = self.conn.execute("SELECT * FROM budget WHERE id=?", (budget_id,)).fetchone()
        if row is None:
            raise KeyError(budget_id)
        budget = Budget(id=row["id"], name=row["name"],
                        current_version_id=row["current_version_id"])
        for vr in self.conn.execute(
                "SELECT * FROM budget_version WHERE budget_id=? ORDER BY number", (budget_id,)):
            from ..domain.config import Role
            tasks = {}
            for tid, t in json.loads(vr["tasks"]).items():
                tasks[tid] = Task(id=tid, concept=t["concept"], scope_key=t["scope_key"],
                                  label=t["label"], status=TaskStatus(t["status"]),
                                  loader_role=Role(t["loader_role"]),
                                  reviewer_role=Role(t["reviewer_role"]),
                                  approver_role=Role(t["approver_role"]))
            version = BudgetVersion(
                id=vr["id"], number=vr["number"], budget_id=budget_id,
                configuration=Configuration.model_validate_json(vr["configuration_snapshot"]),
                fx=fx_from_dict(json.loads(vr["fx_rates"])),
                status=VersionStatus(vr["status"]),
                source_version_id=vr["source_version_id"],
                created_at=datetime.fromisoformat(vr["created_at"]),
                approved_at=datetime.fromisoformat(vr["approved_at"]) if vr["approved_at"] else None,
                tasks=tasks,
            )
            inputs = InputSet()
            for ir in self.conn.execute(
                    "SELECT payload FROM input_value WHERE version_id=?", (vr["id"],)):
                inputs.values.append(InputValue.model_validate_json(ir["payload"]))
            version.inputs = inputs
            budget.versions[version.id] = version
        return budget

    def audit_events(self, version_id: Optional[str] = None, entity_type: Optional[str] = None,
                     limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM audit_event WHERE 1=1"
        args: list = []
        if version_id:
            sql += " AND version_id=?"; args.append(version_id)
        if entity_type:
            sql += " AND entity_type=?"; args.append(entity_type)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]
