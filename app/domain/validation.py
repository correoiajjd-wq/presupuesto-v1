"""Validation Engine + Alert Engine.

Doc 02 §62: no todo error bloquea.
    BLOQUEAN: configuración incompleta, dato obligatorio ausente, formato
              inválido, balance que no cierra, planilla con errores,
              dependencia estructural inexistente, modificar versión aprobada.
    NO BLOQUEAN: objetivo incumplido, alerta informativa, supuesto,
              indicador desfavorable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from .config import AllocationMode, Configuration, BalanceSection, BalanceSource, ExpenseTargetType
from .engine import FY, scope_br, scope_bu, scope_co, scope_su
from .graph import nk
from .inputs import Concept, InputSet
from .money import FXTable
from .periods import Period
from .ratios import RATIO_CATALOG, format_ratio

ZERO = Decimal(0)


class Severity(str, Enum):
    BLOCKING = "BLOCKING"
    INFORMATIVE = "INFORMATIVE"


class AlertStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    ACCEPTED = "ACCEPTED"


@dataclass
class Finding:
    code: str
    message: str
    severity: Severity = Severity.INFORMATIVE
    entity: Optional[str] = None

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCKING


@dataclass
class Alert:
    code: str
    message: str
    entity: Optional[str] = None
    severity: Severity = Severity.INFORMATIVE
    status: AlertStatus = AlertStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    comment: Optional[str] = None

    def resolve(self, user: str, comment: str) -> None:
        self.status = AlertStatus.RESOLVED
        self.resolved_by, self.comment = user, comment
        self.resolved_at = datetime.now(timezone.utc)

    def accept(self, user: str, comment: str) -> None:
        self.status = AlertStatus.ACCEPTED
        self.resolved_by, self.comment = user, comment
        self.resolved_at = datetime.now(timezone.utc)


# ==========================================================================
# Configuración
# ==========================================================================
def validate_configuration(cfg: Configuration, fx: Optional[FXTable] = None) -> list[Finding]:
    out = [Finding(e.split(":")[0], e, Severity.BLOCKING) for e in cfg.validate_structure()]
    out += [Finding("PENDING_DEPENDENCY", m, Severity.BLOCKING)
            for m in cfg.missing_modules_for_ratios()]
    if fx is not None:
        for gap in fx.coverage_gaps(cfg.enabled_currencies, cfg.fiscal_year_start, cfg.fiscal_year_end):
            out.append(Finding("MISSING_FX_RATE", f"MISSING_FX_RATE: {gap}", Severity.BLOCKING))
    return out


# ==========================================================================
# Inputs
# ==========================================================================
def validate_inputs(cfg: Configuration, inputs: InputSet) -> list[Finding]:
    """Validación de formato, ámbito, moneda y período de cada input."""
    out: list[Finding] = []
    fy = cfg.fiscal_year
    valid_periods = {p.code for p in fy.periods}

    for iv in inputs.values:
        tag = f"{iv.concept.value} {iv.scope_key}"
        if iv.period is not None:
            if iv.period not in valid_periods:
                out.append(Finding("INVALID_PERIOD",
                                   f"{tag}: período {iv.period} fuera del ejercicio",
                                   Severity.BLOCKING, tag))
                continue
        if iv.currency and iv.currency not in cfg.enabled_currencies:
            out.append(Finding("INVALID_CURRENCY", f"{tag}: moneda {iv.currency} no habilitada",
                               Severity.BLOCKING, tag))

        if iv.concept in (Concept.SALES_QTY, Concept.SALES_AMOUNT):
            try:
                unit = cfg.branch_owner(iv.branch_id or "")
                product = unit.product(iv.product_id or "")
            except Exception as exc:
                out.append(Finding("INVALID_PRODUCT", f"{tag}: {exc}", Severity.BLOCKING, tag))
                continue
            head = fy.bucket_head(Period.parse(iv.period), product.sales_frequency)
            if Period.parse(iv.period) != head:
                out.append(Finding(
                    "INVALID_FREQUENCY",
                    f"{tag}: el producto {product.code} se carga con frecuencia "
                    f"{product.sales_frequency.value}; el período de carga debe ser {head.code}",
                    Severity.BLOCKING, tag))
            if iv.value < 0:
                out.append(Finding("INVALID_VALUE", f"{tag}: valor negativo", Severity.BLOCKING, tag))

        if iv.concept is Concept.EXPENSE_AMOUNT:
            try:
                ed = cfg.expense(iv.expense_id or "")
            except Exception as exc:
                out.append(Finding("INVALID_EXPENSE", f"{tag}: {exc}", Severity.BLOCKING, tag))
                continue
            head = fy.bucket_head(Period.parse(iv.period), ed.frequency)
            if Period.parse(iv.period) != head:
                out.append(Finding(
                    "INVALID_FREQUENCY",
                    f"gasto {ed.name}: frecuencia {ed.frequency.value}, "
                    f"el período de carga debe ser {head.code}",
                    Severity.BLOCKING, tag))

        if iv.concept in (Concept.OPENING_STOCK, Concept.PURCHASES):
            if not cfg.inventory.enabled:
                out.append(Finding("MODULE_NOT_CONFIGURED",
                                   f"{tag}: stock no está configurado", Severity.BLOCKING, tag))
            elif iv.currency and iv.currency != cfg.inventory.currency:
                out.append(Finding(
                    "INVALID_CURRENCY",
                    f"{tag}: stock y compras deben usar {cfg.inventory.currency}",
                    Severity.BLOCKING, tag))

        if iv.concept is Concept.HEADCOUNT_CHANGE:
            if iv.effective_date is None:
                out.append(Finding("MISSING_REQUIRED_INPUT", f"{tag}: falta fecha estimada",
                                   Severity.BLOCKING, tag))
            elif not (fy.start <= iv.effective_date <= fy.end):
                out.append(Finding("INVALID_PERIOD", f"{tag}: fecha fuera del ejercicio",
                                   Severity.BLOCKING, tag))

        if iv.concept in (Concept.BALANCE_OPENING, Concept.BALANCE_PROJECTED):
            item = next((i for i in cfg.balance.items if i.id == iv.balance_item_id), None)
            if item is None:
                out.append(Finding("INVALID_BALANCE_ITEM", f"{tag}: rubro inexistente",
                                   Severity.BLOCKING, tag))
            elif item.source is BalanceSource.CALCULATED:
                out.append(Finding("CALCULATED_VALUE_NOT_EDITABLE",
                                   f"el rubro {item.name} es calculado y no admite carga",
                                   Severity.BLOCKING, tag))
    return out


def missing_required_inputs(cfg: Configuration, inputs: InputSet) -> list[Finding]:
    """Doc 02 §42: lo que se configura y es necesario, debe cargarse."""
    out: list[Finding] = []
    fy = cfg.fiscal_year
    required = cfg.required_concepts()
    present = {(iv.concept, iv.scope_key, iv.product_id, iv.expense_id, iv.family_id, iv.period)
               for iv in inputs.values}

    if "SALES" in required:
        for unit, b in cfg.all_branches():
            for product in unit.products:
                concept = (Concept.SALES_QTY if product.sales_mode.value == "UNIT_BASED"
                           else Concept.SALES_AMOUNT)
                for head, bucket in fy.iter_buckets(product.sales_frequency):
                    if not any(cfg.is_active(b, p) for p in bucket):
                        continue
                    key = (concept, scope_br(b.id), product.id, None, None, head.code)
                    if key not in present:
                        out.append(Finding(
                            "MISSING_REQUIRED_INPUT",
                            f"Ventas: falta {product.code} en {b.name} para {head.code}",
                            Severity.BLOCKING, scope_br(b.id)))

    if "EXPENSES" in required:
        # En PER_TARGET hay que cargar un importe por cada destino — 0 si no
        # corresponde. En PERCENTAGE alcanza con el total.
        for ed in cfg.expenses:
            scopes = ([t.scope_key for t in ed.targets]
                      if ed.allocation_mode is AllocationMode.PER_TARGET else ["CO"])
            for head, _ in fy.iter_buckets(ed.frequency):
                for scope in scopes:
                    if not any(iv.concept is Concept.EXPENSE_AMOUNT and iv.expense_id == ed.id
                               and iv.period == head.code and iv.scope_key == scope
                               for iv in inputs.values):
                        detalle = (f" en {cfg.scope_label(scope)}"
                                   if ed.allocation_mode is AllocationMode.PER_TARGET else "")
                        out.append(Finding(
                            "MISSING_REQUIRED_INPUT",
                            f"Gastos: falta {ed.name}{detalle} para {head.code}",
                            Severity.BLOCKING, f"EXP:{ed.id}"))

    if "OPENING_STOCK" in required and cfg.inventory.enabled:
        from .engine import BudgetEngine  # noqa: F401  (sólo para tipar mentalmente)
        levels = {
            "COMPANY": [scope_co()],
            "BUSINESS_UNIT": [scope_bu(u.id) for u in cfg.business_units],
            "BRANCH": [scope_br(b.id) for _, b in cfg.all_branches()],
        }[cfg.inventory.level.value]
        for scope in levels:
            fams = _families_for_scope(cfg, scope)
            for fam in fams:
                if not any(iv.concept is Concept.OPENING_STOCK and iv.scope_key == scope
                           and iv.family_id == fam for iv in inputs.values):
                    out.append(Finding("MISSING_REQUIRED_INPUT",
                                       f"Stock inicial: falta familia {fam} en {scope}",
                                       Severity.BLOCKING, scope))

    if "BALANCE" in required and cfg.balance.enabled:
        for item in cfg.balance.items:
            if item.source is BalanceSource.CALCULATED:
                continue
            if not any(iv.concept is Concept.BALANCE_OPENING and iv.balance_item_id == item.id
                       for iv in inputs.values):
                out.append(Finding("MISSING_REQUIRED_INPUT",
                                   f"Balance inicial: falta el rubro {item.name}",
                                   Severity.BLOCKING, f"BI:{item.id}"))
    return out


def _families_for_scope(cfg: Configuration, scope: str) -> list[str]:
    if scope == "CO":
        return sorted({f.id for u in cfg.business_units for f in u.families})
    kind, _id = scope.split(":", 1)
    if kind == "BU":
        return sorted({f.id for f in cfg.unit(_id).families})
    unit = cfg.branch_owner(_id)
    return sorted({f.id for f in unit.families})


# ==========================================================================
# Balance
# ==========================================================================
def validate_balance(cfg: Configuration, values: dict, tag: str = "OPENING") -> list[Finding]:
    """Doc 02 §34: si no cierra, la carga se rechaza. No se incorpora parcialmente."""
    if not cfg.balance.enabled:
        return []
    assets = values.get(nk("ASSETS", scope_co(), tag))
    liabs = values.get(nk("LIABILITIES", scope_co(), tag))
    equity_calc = values.get(nk("EQUITY", scope_co(), tag))       # Activo - Pasivo
    equity_loaded = values.get(nk("EQUITY_LOADED", scope_co(), tag))
    if assets is None or liabs is None or equity_loaded is None:
        return [Finding("MISSING_REQUIRED_INPUT", f"Balance {tag}: sin datos suficientes",
                        Severity.BLOCKING, "BALANCE")]
    diff = (equity_calc or ZERO) - equity_loaded
    if diff != ZERO:
        return [Finding(
            "BALANCE_NOT_BALANCED",
            f"El balance {tag} no cierra: Activo {assets} = Pasivo {liabs} + Patrimonio "
            f"{equity_loaded} deja una diferencia de {diff}",
            Severity.BLOCKING, "BALANCE")]
    return []


# ==========================================================================
# Objetivos -> alertas
# ==========================================================================
def evaluate_objectives(cfg: Configuration, values: dict, period_code: str = FY) -> list[Alert]:
    """Doc 02 §38: si hay objetivo y no se cumple, se genera alerta. No bloquea."""
    alerts: list[Alert] = []
    scopes = (
        [("empresa", scope_co())]
        + [(u.name, scope_bu(u.id)) for u in cfg.business_units]
        + [(b.name, scope_br(b.id)) for _, b in cfg.all_branches()]
    )
    for sel in cfg.ratios:
        if not sel.objective:
            continue
        ratio = RATIO_CATALOG.get(sel.ratio_code)
        if ratio is None:
            continue
        for label, scope in scopes:
            key = nk(f"RATIO:{ratio.code}", scope, period_code)
            if key not in values:
                continue
            actual = values.get(key)
            if actual is None:
                alerts.append(Alert(
                    "RATIO_NOT_COMPUTABLE",
                    f"{ratio.name} en {label}: no calculable con los datos cargados",
                    entity=scope))
                continue
            if not sel.objective.met(Decimal(actual)):
                alerts.append(Alert(
                    "OBJECTIVE_NOT_MET",
                    f"{ratio.name} en {label}: {format_ratio(Decimal(actual), ratio.unit)} "
                    f"vs objetivo {format_ratio(sel.objective.value, ratio.unit)}",
                    entity=scope))
    return alerts


def collect_assumptions(cfg: Configuration) -> list[str]:
    """Doc 02 §41: los reportes deben explicitar los supuestos utilizados."""
    out = [
        "Precio y margen constantes durante el ejercicio (V1).",
        "Los valores cargados con frecuencia menor a la mensual se distribuyen en partes iguales.",
        "Los flujos se convierten con el TC promedio del período; los stocks, con el TC de cierre.",
    ]
    if any(p.margin_formula.value == "NO_COST" for u in cfg.business_units for p in u.products):
        out.append(
            "Hay productos configurados sin costo: su precio de venta es todo margen. "
            "Es el caso de los intangibles.")
    if any(t.distribute_to_branches for e in cfg.expenses for t in e.targets):
        out.append(
            "Los gastos de unidad distribuidos a sucursales usan como driver las ventas "
            "anuales de cada sucursal; una sucursal sin ventas no recibe asignación."
        )
    if any(t.corporate for e in cfg.expenses for t in e.targets):
        out.append(
            "Los gastos de empresa y de las áreas de soporte se muestran asignados a las "
            "unidades por debajo del EBITDA propio; no forman parte del EBITDA de la unidad."
        )
    if not cfg.inventory.enabled:
        out.append("Stock: no configurado. Los ratios de inventario no se calculan.")
    if not cfg.balance.enabled:
        out.append("Balance: no configurado.")
    if not cfg.capex.enabled:
        out.append("CAPEX: no configurado.")
    out.append("Sin depreciación, intereses ni impuestos: el resultado llega hasta EBITDA (V1).")
    return out
