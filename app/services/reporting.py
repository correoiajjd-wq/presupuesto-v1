"""Reporting: read models sobre los valores calculados.

Doc 03 §28: el reporting no contiene fórmulas financieras. Sólo lee, ordena
y presenta lo que produjo el Calculation Engine, e informa explícitamente
supuestos, faltantes y alertas (doc 02 §41).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..domain.config import Configuration
from ..domain.engine import FY, scope_br, scope_bu, scope_co, scope_su
from ..domain.graph import DependencyGraph, nk
from ..domain.ratios import RATIO_CATALOG, format_ratio

ZERO = Decimal(0)

PNL_LINES = [
    ("SALES", "Ventas", 1),
    ("COGS", "Costo de ventas", -1),
    ("GROSS_MARGIN", "Margen bruto", 0),
    ("EXPENSES", "Gastos", -1),
    ("PAYROLL", "Nómina", -1),
    ("EBITDA", "EBITDA", 0),
    ("ALLOCATED_EXPENSES", "Gastos corporativos asignados", -1),
    ("RESULT_AFTER_ALLOCATION", "Resultado después de asignación", 0),
]

SUBTOTALS = {"GROSS_MARGIN", "EBITDA", "RESULT_AFTER_ALLOCATION"}


def scopes_of(cfg: Configuration) -> list[tuple[str, str, str]]:
    """(scope_key, etiqueta, nivel)"""
    out = [(scope_co(), cfg.company_name, "COMPANY")]
    for u in cfg.business_units:
        out.append((scope_bu(u.id), u.name, "BUSINESS_UNIT"))
        for b in u.branches:
            out.append((scope_br(b.id), f"{u.name} / {b.name}", "BRANCH"))
    for su in cfg.support_units:
        out.append((scope_su(su.id), su.name, "SUPPORT_UNIT"))
    return out


def pnl(cfg: Configuration, values: dict, scope: str, period_code: str = FY) -> list[dict]:
    rows = []
    for metric, label, sign in PNL_LINES:
        key = nk(metric, scope, period_code)
        if key not in values:
            continue
        v = values.get(key)
        rows.append({
            "metric": metric,
            "label": label,
            "value": None if v is None else Decimal(v),
            "display": None if v is None else Decimal(v) * (Decimal(-1) if sign < 0 else Decimal(1)),
            "sign": sign,
            "subtotal": metric in SUBTOTALS,
        })
    return rows


def pnl_by_period(cfg: Configuration, values: dict, scope: str) -> dict:
    periods = [p.code for p in cfg.periods]
    table = {}
    for metric, label, sign in PNL_LINES:
        row = []
        for pc in periods:
            v = values.get(nk(metric, scope, pc))
            row.append(None if v is None else Decimal(v))
        total = values.get(nk(metric, scope, FY))
        table[metric] = {
            "label": label,
            "periods": row,
            "total": None if total is None else Decimal(total),
            "subtotal": metric in SUBTOTALS,
        }
    return {"periods": periods, "lines": table}


def headcount_summary(cfg: Configuration, values: dict, scope: str) -> dict:
    """Doc 02 §54: dotación inicial + altas - bajas = dotación final."""
    periods = [p.code for p in cfg.periods]
    first = values.get(nk("HEADCOUNT", scope, periods[0]))
    last = values.get(nk("HEADCOUNT", scope, periods[-1]))
    monthly = [values.get(nk("HEADCOUNT", scope, pc)) for pc in periods]
    delta = (Decimal(last) if last is not None else ZERO) - (Decimal(first) if first is not None else ZERO)
    return {
        "initial": first,
        "final": last,
        "net_change": delta,
        "monthly": monthly,
        "payroll_cost": values.get(nk("PAYROLL", scope, FY)),
        "avg": values.get(nk("HEADCOUNT_AVG", scope, FY)),
    }


def ratio_report(cfg: Configuration, values: dict, scope: str, period_code: str = FY) -> list[dict]:
    out = []
    for sel in cfg.ratios:
        ratio = RATIO_CATALOG.get(sel.ratio_code)
        if ratio is None:
            continue
        key = nk(f"RATIO:{ratio.code}", scope, period_code)
        if key not in values:
            continue
        v = values.get(key)
        objective_met: Optional[bool] = None
        if sel.objective is not None and v is not None:
            objective_met = sel.objective.met(Decimal(v))
        out.append({
            "code": ratio.code,
            "name": ratio.name,
            "group": ratio.group.value,
            "formula": ratio.formula_text,
            "value": None if v is None else Decimal(v),
            "display": format_ratio(None if v is None else Decimal(v), ratio.unit),
            "unit": ratio.unit.value,
            "direction": ratio.direction.value,
            "objective": None if not sel.objective else {
                "type": sel.objective.type.value,
                "value": sel.objective.value,
                "display": format_ratio(sel.objective.value, ratio.unit),
            },
            "objective_met": objective_met,
            "computable": v is not None,
        })
    return out


def inventory_report(cfg: Configuration, values: dict, graph: DependencyGraph) -> list[dict]:
    if not cfg.inventory.enabled:
        return []
    from ..domain.engine import BudgetEngine  # noqa: F401
    out = []
    for key in sorted(values):
        if not key.startswith("CLOSING_STOCK|") or "#FAM:" not in key:
            continue
        metric, scope, period = key.split("|", 2)
        if period != FY:
            continue
        base, fam = scope.split("#FAM:")
        out.append({
            "scope": base,
            "family": fam,
            "opening": values.get(nk("OPENING_STOCK", scope, FY)),
            "purchases": values.get(nk("PURCHASES", scope, FY)),
            "cogs": values.get(nk("COGS_FAMILY", scope, FY)),
            "closing": values.get(key),
        })
    return out


def configuration_checklist(cfg: Configuration) -> list[dict]:
    """Doc 02 §58: vista tipo checklist del estado de la configuración."""
    def state(ok: bool, detail: str = "") -> dict:
        return {"status": "Completo" if ok else "No configurado", "detail": detail}

    items = [
        ("Empresa", state(bool(cfg.company_name and cfg.presentation_currency))),
        ("Ejercicio", state(cfg.fiscal_year_end > cfg.fiscal_year_start,
                            f"{cfg.fiscal_year_start} a {cfg.fiscal_year_end}")),
        ("Unidades de negocio", state(bool(cfg.business_units), f"{len(cfg.business_units)}")),
        ("Sucursales", state(bool(cfg.all_branches()), f"{len(cfg.all_branches())}")),
        ("Unidades de soporte", state(bool(cfg.support_units), f"{len(cfg.support_units)}")),
        ("Productos", state(all(u.products for u in cfg.business_units),
                            f"{sum(len(u.products) for u in cfg.business_units)}")),
        ("Familias", state(all(u.families for u in cfg.business_units),
                           f"{sum(len(u.families) for u in cfg.business_units)}")),
        ("Ventas", state(bool(cfg.business_units))),
        ("Gastos", state(bool(cfg.expenses), f"{len(cfg.expenses)}")),
        ("Nómina", state(bool(cfg.payroll.areas), f"{len(cfg.payroll.areas)} áreas")),
        ("CAPEX", state(cfg.capex.enabled, f"{len(cfg.capex.categories)} categorías")),
        ("Stock", state(cfg.inventory.enabled, cfg.inventory.level.value if cfg.inventory.enabled else "")),
        ("Balance", state(cfg.balance.enabled, f"{len(cfg.balance.items)} rubros")),
        ("Ratios", state(bool(cfg.ratios), f"{len(cfg.ratios)} seleccionados")),
        ("Objetivos", state(any(r.objective for r in cfg.ratios),
                            f"{sum(1 for r in cfg.ratios if r.objective)}")),
        ("Monedas", state(bool(cfg.enabled_currencies), ", ".join(cfg.enabled_currencies))),
        ("Workflow", state(bool(cfg.workflow.steps), f"{len(cfg.workflow.steps)} circuitos")),
    ]
    return [{"module": name, **st} for name, st in items]
