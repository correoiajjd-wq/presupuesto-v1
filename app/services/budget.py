"""Budget / Versioning / Workflow / Approval / Audit.

Reglas duras del spec:
    - Una versión aprobada es inmutable (doc 01 §9, doc 04 §1.5/§44).
    - La versión guarda un snapshot de configuración: no depende de la
      configuración actual (doc 03 §45).
    - Aprobar no significa poder aprobar: hace falta permiso + estado de
      workflow (doc 04 §47/§51).
    - Una aprobación sobrevive si su dependencia no fue afectada (doc 04 §47).
    - Ninguna transición ocurre sin evento de auditoría, en la misma
      transacción (doc 04 §38).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

from ..domain.config import AllocationMode, ConfigStatus, Configuration, Role
from ..domain.engine import BudgetEngine, FY
from ..domain.graph import DependencyGraph
from ..domain.inputs import Concept, InputSet, InputStatus, InputValue
from ..domain.money import FXTable
from ..domain.validation import (
    Alert,
    Finding,
    Severity,
    collect_assumptions,
    evaluate_objectives,
    missing_required_inputs,
    validate_balance,
    validate_configuration,
    validate_inputs,
)


class BudgetError(Exception):
    """Error funcional con código estable (doc 04 §54)."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ==========================================================================
# Auditoría
# ==========================================================================
@dataclass
class AuditEvent:
    actor: str
    action: str
    entity_type: str
    entity_id: str
    version_id: Optional[str] = None
    before: Optional[str] = None
    after: Optional[str] = None
    comment: Optional[str] = None
    at: datetime = field(default_factory=_now)
    correlation_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "version_id": self.version_id,
            "before": self.before,
            "after": self.after,
            "comment": self.comment,
            "correlation_id": self.correlation_id,
        }


class AuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, **kwargs) -> AuditEvent:
        ev = AuditEvent(**kwargs)
        self.events.append(ev)
        return ev

    def filter(self, **criteria) -> list[AuditEvent]:
        out = self.events
        for k, v in criteria.items():
            if v is None:
                continue
            out = [e for e in out if getattr(e, k, None) == v]
        return out


# ==========================================================================
# Workflow
# ==========================================================================
class TaskStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


#: Qué tarea depende de cuál. Si cambia lo de arriba, lo de abajo vuelve a
#: revisión: Nómina no puede quedar aprobada sobre una dotación que cambió.
TASK_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "PAYROLL_HEADCOUNT": ("PAYROLL_SALARY",),
}


_TRANSITIONS = {
    TaskStatus.NOT_STARTED: {TaskStatus.DRAFT},
    TaskStatus.DRAFT: {TaskStatus.DRAFT, TaskStatus.SUBMITTED},
    TaskStatus.SUBMITTED: {TaskStatus.IN_REVIEW, TaskStatus.REJECTED},
    TaskStatus.IN_REVIEW: {TaskStatus.APPROVED, TaskStatus.REJECTED},
    TaskStatus.REJECTED: {TaskStatus.DRAFT},
    TaskStatus.APPROVED: {TaskStatus.IN_REVIEW},  # sólo si una dependencia cambia
}


@dataclass
class Task:
    id: str
    concept: str                 # SALES, EXPENSES, PAYROLL_HEADCOUNT, CAPEX, ...
    scope_key: str
    label: str
    loader_role: Role
    reviewer_role: Role
    approver_role: Role
    status: TaskStatus = TaskStatus.NOT_STARTED
    assignee: Optional[str] = None
    due_date: Optional[date] = None
    history: list[dict] = field(default_factory=list)

    def can_transition(self, new: TaskStatus) -> bool:
        return new in _TRANSITIONS[self.status]


@dataclass
class User:
    id: str
    name: str
    roles: set[Role]
    scopes: set[str] = field(default_factory=set)  # vacío = transversal (CFO)

    def has_scope(self, scope_key: str, cfg: Optional[Configuration] = None) -> bool:
        """Tener alcance sobre algo alcanza también a lo que ese algo contiene.

        Quien tiene la sucursal tiene las operaciones de esa sucursal; quien
        tiene la unidad, las de la unidad. Como una operación pertenece a la vez
        a una unidad y a una sucursal, ambos caminos son válidos.
        """
        if not self.scopes:
            return True
        if scope_key in self.scopes:
            return True
        if scope_key == "CO" and Role.CFO in self.roles:
            return True
        if cfg is not None and self.scopes & cfg.scope_ancestors(scope_key):
            return True
        return False


class AuthorizationProvider:
    """Capabilities + scope, sin `if role == CFO` desperdigado (doc 03 §3)."""

    CAPABILITIES: dict[Role, set[str]] = {
        Role.CFO: {
            "budget.configuration.edit", "budget.configuration.close", "budget.expense.load",
            "budget.expense.review", "budget.review", "budget.approve", "budget.version.create",
            "budget.version.approve", "budget.version.set_current", "budget.fx.edit",
            "budget.scenario.run", "budget.alert.resolve", "budget.read",
        },
        Role.COO: {"budget.configuration.edit", "budget.review", "budget.read"},
        Role.ADMIN_AREA: {"budget.expense.load", "budget.read"},
        # Doc 01 §16: las unidades informan personas, Nómina pone los valores.
        # Son dos permisos distintos porque son dos responsabilidades distintas.
        Role.PAYROLL_AREA: {"budget.payroll.load", "budget.headcount.load", "budget.read"},
        Role.UNIT_MANAGER: {"budget.sales.load", "budget.headcount.load", "budget.read"},
        Role.FINANCE_AREA: {"budget.balance.load", "budget.read"},
        Role.REVIEWER: {"budget.review", "budget.read"},
        Role.APPROVER: {"budget.approve", "budget.read"},
        Role.ADMINISTRATOR: {"budget.read"},
    }

    def capabilities(self, user: User) -> set[str]:
        out: set[str] = set()
        for r in user.roles:
            out |= self.CAPABILITIES.get(r, set())
        return out

    def check(self, user: User, capability: str, scope_key: str = "CO",
              cfg: Optional[Configuration] = None) -> None:
        if capability not in self.capabilities(user):
            raise BudgetError("UNAUTHORIZED", f"{user.name} no tiene la capacidad {capability}")
        if not user.has_scope(scope_key, cfg):
            raise BudgetError(
                "UNAUTHORIZED_SCOPE", f"{user.name} no tiene alcance sobre {scope_key}"
            )


# ==========================================================================
# Versión
# ==========================================================================
class VersionStatus(str, Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class BudgetVersion:
    id: str
    number: int
    budget_id: str
    configuration: Configuration          # snapshot propio de la versión
    fx: FXTable
    inputs: InputSet = field(default_factory=InputSet)
    status: VersionStatus = VersionStatus.DRAFT
    source_version_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    approved_at: Optional[datetime] = None
    tasks: dict[str, Task] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    scenarios: dict[str, "Scenario"] = field(default_factory=dict)
    _graph: Optional[DependencyGraph] = None
    _values: Optional[dict] = None

    # -- inmutabilidad -----------------------------------------------------
    @property
    def mutable(self) -> bool:
        return self.status in (VersionStatus.DRAFT, VersionStatus.REJECTED)

    def assert_mutable(self) -> None:
        if not self.mutable:
            raise BudgetError(
                "VERSION_IMMUTABLE",
                f"La versión V{self.number} está {self.status.value} y no puede modificarse. "
                "Cree una nueva versión.",
            )

    # -- cálculo -----------------------------------------------------------
    def invalidate(self) -> None:
        self._graph = None
        self._values = None

    @property
    def graph(self) -> DependencyGraph:
        if self._graph is None:
            self._graph = BudgetEngine(self.configuration, self.fx, self.inputs).build()
        return self._graph

    def calculate(self, force: bool = False) -> dict:
        if self._values is None or force:
            self._values = self.graph.evaluate()
        return self._values

    def recalculate_from(self, changed_keys: Iterable[str]) -> dict:
        """Recálculo incremental: sólo los nodos afectados (doc 03 §43)."""
        if self._values is None:
            return self.calculate()
        affected = self.graph.impacted_by(changed_keys)
        self._values = self.graph.evaluate(only=affected, previous=self._values)
        return self._values

    def task_for(self, concept: str, scope_key: str) -> Optional[Task]:
        """A qué tarea pertenece un input: a la más específica de su concepto.

        Si hay una tarea para ese ámbito exacto, es esa; si no, la general del
        concepto. Así un gasto imputado a un centro de costo con responsable
        propio no queda además dentro de la tarea general de gastos.
        """
        exact = next((t for t in self.tasks.values()
                      if t.concept == concept and t.scope_key == scope_key), None)
        if exact is not None:
            return exact
        return next((t for t in self.tasks.values()
                     if t.concept == concept and t.scope_key == "CO"), None)

    def inputs_of(self, task: Task) -> list[InputValue]:
        return [iv for iv in self.inputs.values
                if self.task_for(iv.group, iv.scope_key) is task]

    def impact_of(self, changed_keys: Iterable[str]) -> dict:
        affected = self.graph.impacted_by(changed_keys)
        by_metric: dict[str, int] = {}
        for k in affected:
            by_metric[k.split("|", 1)[0]] = by_metric.get(k.split("|", 1)[0], 0) + 1
        return {"affected_count": len(affected), "by_metric": by_metric}


@dataclass
class ScenarioAdjustment:
    concept: str                     # SALES, EXPENSES, PAYROLL_HEADCOUNT, PURCHASES, CAPEX
    variation_type: str              # PERCENTAGE | ABSOLUTE
    variation: Decimal
    business_unit_id: Optional[str] = None
    branch_id: Optional[str] = None
    operation_id: Optional[str] = None


@dataclass
class Scenario:
    """Doc 03 §44: overlay sobre inputs, no copia física del presupuesto."""

    id: str
    name: str
    version_id: str
    adjustments: list[ScenarioAdjustment] = field(default_factory=list)
    _values: Optional[dict] = None


# ==========================================================================
# Servicio de presupuesto
# ==========================================================================
@dataclass
class Budget:
    id: str
    name: str
    versions: dict[str, BudgetVersion] = field(default_factory=dict)
    current_version_id: Optional[str] = None

    def version(self, version_id: str) -> BudgetVersion:
        v = self.versions.get(version_id)
        if v is None:
            raise BudgetError("NOT_FOUND", f"versión {version_id} inexistente")
        return v

    @property
    def latest(self) -> BudgetVersion:
        return max(self.versions.values(), key=lambda v: v.number)


class BudgetService:
    def __init__(self, auth: Optional[AuthorizationProvider] = None):
        self.auth = auth or AuthorizationProvider()
        self.budgets: dict[str, Budget] = {}
        self.audit = AuditLog()
        self.users: dict[str, User] = {}

    # -- usuarios ----------------------------------------------------------
    def register_user(self, user: User) -> User:
        self.users[user.id] = user
        return user

    def user(self, user_id: str) -> User:
        u = self.users.get(user_id)
        if u is None:
            raise BudgetError("UNAUTHORIZED", f"usuario {user_id} desconocido")
        return u

    # -- presupuesto y versión --------------------------------------------
    def create_budget(self, actor: str, name: str, configuration: Configuration,
                      fx: FXTable) -> Budget:
        user = self.user(actor)
        self.auth.check(user, "budget.configuration.edit")
        budget = Budget(id=_uid("BUD"), name=name)
        version = BudgetVersion(
            id=_uid("VER"), number=1, budget_id=budget.id,
            configuration=configuration.model_copy(deep=True), fx=fx,
        )
        budget.versions[version.id] = version
        self.budgets[budget.id] = budget
        self.audit.record(actor=actor, action="BudgetCreated", entity_type="BUDGET",
                          entity_id=budget.id, version_id=version.id, after=name)
        return budget

    def budget(self, budget_id: str) -> Budget:
        b = self.budgets.get(budget_id)
        if b is None:
            raise BudgetError("NOT_FOUND", f"presupuesto {budget_id} inexistente")
        return b

    # -- configuración -----------------------------------------------------
    def close_configuration(self, actor: str, version: BudgetVersion) -> list[Finding]:
        """Doc 01 §7: configuración cerrada = configuración bloqueada."""
        user = self.user(actor)
        self.auth.check(user, "budget.configuration.close")
        version.assert_mutable()
        findings = validate_configuration(version.configuration, version.fx)
        blocking = [f for f in findings if f.blocking]
        if blocking:
            raise BudgetError(
                "INCOMPLETE_CONFIGURATION",
                "La configuración no puede cerrarse: hay errores estructurales.",
                {"errors": [f.message for f in blocking]},
            )
        before = version.configuration.status.value
        version.configuration.status = ConfigStatus.LOCKED
        version.invalidate()
        self._create_tasks(version)
        self.audit.record(actor=actor, action="ConfigurationLocked", entity_type="CONFIGURATION",
                          entity_id=version.id, version_id=version.id,
                          before=before, after=ConfigStatus.LOCKED.value)
        return findings

    def assert_configuration_open(self, version: BudgetVersion) -> None:
        if version.configuration.status is ConfigStatus.LOCKED:
            raise BudgetError("CONFIGURATION_LOCKED",
                              "La configuración está cerrada; los cambios estructurales "
                              "requieren una nueva versión.")

    # -- tareas ------------------------------------------------------------
    def _create_tasks(self, version: BudgetVersion) -> None:
        """Genera las tareas de carga a partir de la configuración (doc 02 §59)."""
        cfg = version.configuration
        wf = cfg.workflow

        def add(concept: str, scope_key: str, label: str,
                loader: Optional[Role] = None) -> None:
            step = wf.step(concept)
            task = Task(
                id=_uid("TSK"), concept=concept, scope_key=scope_key, label=label,
                loader_role=loader or (step.loader_role if step else Role.ADMIN_AREA),
                reviewer_role=step.reviewer_role if step else Role.CFO,
                approver_role=step.approver_role if step else Role.CFO,
            )
            version.tasks[task.id] = task

        # La venta vive en la operación: la combinación unidad x sucursal.
        for o in cfg.operations:
            add("SALES", f"OP:{o.id}", f"Ventas — {cfg.operation_label(o.id)}")
        # Las solicitudes de dotación las hace el responsable de cada centro de
        # costo, el mismo que carga sus gastos.
        for cc, _kind, label in cfg.cost_centers():
            add("PAYROLL_HEADCOUNT", f"CC:{cc.id}", f"Dotación — {label}",
                loader=cc.responsible_role)
        if cfg.cost_centers():
            # Nómina valoriza todo de una sola vez: la foto inicial y las solicitudes.
            add("PAYROLL_SALARY", "CO", "Nómina — foto inicial y valor de las solicitudes")
        if cfg.expenses:
            # Un gasto se carga donde se imputa, y cada centro de costo tiene su
            # responsable: el que definió el CFO al crearlo.
            # Dónde se carga cada gasto: en modo por destino, en cada uno; en
            # modo porcentaje, un único total a nivel empresa.
            destinos = {t.scope_key
                        for e in cfg.expenses if e.allocation_mode is AllocationMode.PER_TARGET
                        for t in e.targets}
            destinos |= {"CO" for e in cfg.expenses
                         if e.allocation_mode is AllocationMode.PERCENTAGE}
            for cc, _kind, label in cfg.cost_centers():
                if f"CC:{cc.id}" in destinos:
                    add("EXPENSES", f"CC:{cc.id}", f"Gastos — {label}",
                        loader=cc.responsible_role)
            for scope in sorted(d for d in destinos if not d.startswith("CC:")):
                add("EXPENSES", scope, f"Gastos — {cfg.scope_label(scope)}")
        if cfg.capex.enabled:
            add("CAPEX", "CO", "CAPEX")
        if cfg.inventory.enabled:
            add("OPENING_STOCK", "CO", "Stock inicial y compras")
        if cfg.balance.enabled:
            add("BALANCE", "CO", "Balance inicial y proyectado")

    def tasks_for(self, version: BudgetVersion, user: User) -> list[Task]:
        out = []
        for t in version.tasks.values():
            if t.loader_role in user.roles or t.reviewer_role in user.roles or \
                    t.approver_role in user.roles:
                if user.has_scope(t.scope_key, version.configuration):
                    out.append(t)
        return out

    def _transition(self, actor: str, version: BudgetVersion, task: Task,
                    new: TaskStatus, capability: str, comment: Optional[str] = None) -> Task:
        user = self.user(actor)
        self.auth.check(user, capability, task.scope_key, version.configuration)
        if not task.can_transition(new):
            raise BudgetError(
                "WORKFLOW_INVALID_TRANSITION",
                f"No se puede pasar de {task.status.value} a {new.value}",
            )
        version.assert_mutable()
        before = task.status
        task.status = new
        task.history.append({"at": _now().isoformat(), "actor": actor,
                             "from": before.value, "to": new.value, "comment": comment})
        self.audit.record(actor=actor, action=f"Task{new.value.title()}", entity_type="TASK",
                          entity_id=task.id, version_id=version.id,
                          before=before.value, after=new.value, comment=comment)
        return task

    def submit_task(self, actor: str, version: BudgetVersion, task_id: str) -> Task:
        task = version.tasks[task_id]
        cap = {"SALES": "budget.sales.load", "EXPENSES": "budget.expense.load",
               "PAYROLL_HEADCOUNT": "budget.headcount.load",
               "PAYROLL_SALARY": "budget.payroll.load",
               "BALANCE": "budget.balance.load"}.get(task.concept, "budget.expense.load")
        t = self._transition(actor, version, task, TaskStatus.SUBMITTED, cap)
        for iv in version.inputs_of(task):
            iv.status = InputStatus.SUBMITTED
        self._transition(actor, version, task, TaskStatus.IN_REVIEW, cap)
        return t

    def approve_task(self, actor: str, version: BudgetVersion, task_id: str) -> Task:
        task = version.tasks[task_id]
        t = self._transition(actor, version, task, TaskStatus.APPROVED, "budget.approve")
        for iv in version.inputs_of(task):
            iv.status = InputStatus.APPROVED
        return t

    def reject_task(self, actor: str, version: BudgetVersion, task_id: str, comment: str) -> Task:
        if not comment:
            raise BudgetError("MISSING_COMMENT", "El rechazo requiere un motivo (doc 02 §48).")
        task = version.tasks[task_id]
        self._transition(actor, version, task, TaskStatus.REJECTED, "budget.review", comment)
        t = self._transition(actor, version, task, TaskStatus.DRAFT, "budget.review", comment)
        for iv in version.inputs_of(task):
            iv.status = InputStatus.REJECTED
        return t

    # -- carga de inputs ---------------------------------------------------
    def submit_input(self, actor: str, version: BudgetVersion, iv: InputValue,
                     capability: str = "budget.expense.load") -> InputValue:
        user = self.user(actor)
        self.auth.check(user, capability, iv.scope_key, version.configuration)
        version.assert_mutable()
        if version.configuration.status is not ConfigStatus.LOCKED:
            raise BudgetError("CONFIGURATION_NOT_CLOSED",
                              "No se puede cargar hasta cerrar la configuración (doc 01 §5).")
        errors = [f for f in validate_inputs(version.configuration, InputSet(values=[iv]))
                  if f.blocking]
        if errors:
            raise BudgetError("INPUT_VALIDATION_FAILED", errors[0].message,
                              {"errors": [e.message for e in errors]})
        iv.loaded_by = actor
        before = next((x for x in version.inputs.values if x.identity() == iv.identity()), None)
        version.inputs.upsert(iv)
        version.invalidate()
        self.audit.record(actor=actor, action="InputSubmitted", entity_type=iv.concept.value,
                          entity_id=iv.identity(), version_id=version.id,
                          before=None if before is None else str(before.value), after=str(iv.value))
        self._invalidate_dependent_approvals(version, iv)
        return iv

    def _invalidate_dependent_approvals(self, version: BudgetVersion, iv: InputValue) -> None:
        """Doc 04 §47: aprobación parcial. Sólo vuelve a revisión lo afectado."""
        afectada = version.task_for(iv.group, iv.scope_key)
        dependientes = TASK_DEPENDENCIES.get(iv.group, ())
        for t in version.tasks.values():
            if t.status is not TaskStatus.APPROVED:
                continue
            if t is afectada or t.concept in dependientes:
                t.status = TaskStatus.IN_REVIEW
                t.history.append({"at": _now().isoformat(), "actor": "system",
                                  "from": "APPROVED", "to": "IN_REVIEW",
                                  "comment": "una dependencia cambió"})
                self.audit.record(actor="system", action="ApprovalInvalidated",
                                  entity_type="TASK", entity_id=t.id, version_id=version.id,
                                  before="APPROVED", after="IN_REVIEW",
                                  comment=f"cambió {iv.concept.value} en {iv.scope_key}")

    # -- FX ----------------------------------------------------------------
    def change_fx_rate(self, actor: str, version: BudgetVersion, currency: str,
                       day: date, rate: Decimal) -> dict:
        user = self.user(actor)
        self.auth.check(user, "budget.fx.edit")
        version.assert_mutable()   # doc 04 §63: si está aprobada -> VERSION_IMMUTABLE
        before = None
        try:
            before = version.fx.rate_on(currency, day)
        except Exception:
            pass
        version.fx.add(currency, day, rate)
        version.invalidate()
        self.audit.record(actor=actor, action="CHANGE_FX_RATE", entity_type="FX_RATE",
                          entity_id=f"{currency}-{day.isoformat()}", version_id=version.id,
                          before=str(before), after=str(rate))
        return {"currency": currency, "date": day.isoformat(), "before": before, "after": rate}

    # -- validación y aprobación de versión --------------------------------
    def validate_version(self, version: BudgetVersion) -> dict:
        cfg = version.configuration
        values = version.calculate()
        findings: list[Finding] = []
        findings += validate_configuration(cfg, version.fx)
        findings += validate_inputs(cfg, version.inputs)
        findings += missing_required_inputs(cfg, version.inputs)
        findings += validate_balance(cfg, values, "OPENING")
        if cfg.balance.enabled:
            findings += validate_balance(cfg, values, FY)
        alerts = evaluate_objectives(cfg, values)
        version.alerts = alerts
        pending_tasks = [t for t in version.tasks.values() if t.status is not TaskStatus.APPROVED]
        return {
            "blocking": [f for f in findings if f.blocking],
            "informative": [f for f in findings if not f.blocking],
            "alerts": alerts,
            "assumptions": collect_assumptions(cfg),
            "pending_tasks": pending_tasks,
        }

    def approve_version(self, actor: str, version: BudgetVersion) -> BudgetVersion:
        user = self.user(actor)
        self.auth.check(user, "budget.version.approve")
        version.assert_mutable()
        report = self.validate_version(version)
        if report["blocking"]:
            raise BudgetError("VERSION_NOT_READY",
                              "La versión tiene validaciones bloqueantes pendientes.",
                              {"errors": [f.message for f in report["blocking"]][:20]})
        if report["pending_tasks"]:
            raise BudgetError("PENDING_APPROVALS",
                              "Hay tareas sin aprobar.",
                              {"tasks": [t.label for t in report["pending_tasks"]]})
        before = version.status.value
        version.status = VersionStatus.APPROVED
        version.approved_at = _now()
        self.audit.record(actor=actor, action="VersionApproved", entity_type="VERSION",
                          entity_id=version.id, version_id=version.id,
                          before=before, after=VersionStatus.APPROVED.value)
        return version

    def set_current(self, actor: str, budget: Budget, version_id: str) -> Budget:
        user = self.user(actor)
        self.auth.check(user, "budget.version.set_current")
        version = budget.version(version_id)
        if version.status is not VersionStatus.APPROVED:
            raise BudgetError("VERSION_NOT_APPROVED",
                              "Sólo una versión aprobada puede ser la vigente.")
        before = budget.current_version_id
        budget.current_version_id = version_id
        self.audit.record(actor=actor, action="VersionSetCurrent", entity_type="BUDGET",
                          entity_id=budget.id, version_id=version_id,
                          before=before, after=version_id)
        return budget

    def create_version(self, actor: str, budget: Budget, source_version_id: str) -> BudgetVersion:
        """Doc 04 §45: clona configuración, inputs, reglas y (opcionalmente) escenarios."""
        user = self.user(actor)
        self.auth.check(user, "budget.version.create")
        source = budget.version(source_version_id)
        new = BudgetVersion(
            id=_uid("VER"),
            number=max(v.number for v in budget.versions.values()) + 1,
            budget_id=budget.id,
            configuration=source.configuration.model_copy(deep=True),
            fx=source.fx,
            inputs=source.inputs.model_copy(deep=True),
            source_version_id=source.id,
        )
        for t in source.tasks.values():
            new.tasks[t.id] = Task(
                id=t.id, concept=t.concept, scope_key=t.scope_key, label=t.label,
                loader_role=t.loader_role, reviewer_role=t.reviewer_role,
                approver_role=t.approver_role, status=t.status,
            )
        budget.versions[new.id] = new
        self.audit.record(actor=actor, action="VersionCreated", entity_type="VERSION",
                          entity_id=new.id, version_id=new.id,
                          before=source.id, after=f"V{new.number}")
        return new

    # -- alertas -----------------------------------------------------------
    def resolve_alert(self, actor: str, version: BudgetVersion, index: int,
                      comment: str, accept: bool = False) -> Alert:
        user = self.user(actor)
        self.auth.check(user, "budget.alert.resolve")
        if not comment:
            raise BudgetError("MISSING_COMMENT", "Resolver o aceptar una alerta requiere comentario.")
        alert = version.alerts[index]
        alert.accept(actor, comment) if accept else alert.resolve(actor, comment)
        self.audit.record(actor=actor, action="AlertAccepted" if accept else "AlertResolved",
                          entity_type="ALERT", entity_id=alert.code, version_id=version.id,
                          comment=comment)
        return alert
