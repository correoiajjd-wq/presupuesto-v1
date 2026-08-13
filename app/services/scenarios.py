"""Scenario Engine: overlay virtual sobre los inputs de una versión.

Doc 02 §39 / doc 03 §44:
    Input -> variación -> cálculo -> resultado.
    Nunca se modifica un valor calculado (no existe "EBITDA +1M").
    El presupuesto base no cambia: el escenario es una capa.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..domain.config import Configuration, MarginFormula
from ..domain.engine import BudgetEngine, FY
from ..domain.graph import nk
from ..domain.inputs import Concept, InputSet, InputSource
from .budget import BudgetError, BudgetVersion, Scenario, ScenarioAdjustment

#: Qué inputs toca cada concepto de escenario.
CONCEPT_TARGETS: dict[str, tuple[Concept, ...]] = {
    "SALES": (Concept.SALES_QTY, Concept.SALES_AMOUNT),
    "EXPENSES": (Concept.EXPENSE_AMOUNT,),
    "PAYROLL": (Concept.INITIAL_HEADCOUNT, Concept.HEADCOUNT_CHANGE, Concept.COMMISSION_AMOUNT),
    "PURCHASES": (Concept.PURCHASES,),
    "CAPEX": (Concept.CAPEX_AMOUNT,),
    "COST": (),  # actúa sobre el supuesto de margen, no sobre un input cargado
}


def _in_scope(iv, adj: ScenarioAdjustment) -> bool:
    if adj.branch_id:
        return iv.branch_id == adj.branch_id
    if adj.business_unit_id:
        return iv.business_unit_id == adj.business_unit_id
    return True


def apply_overlay(cfg: Configuration, inputs: InputSet,
                  adjustments: list[ScenarioAdjustment]) -> tuple[Configuration, InputSet]:
    """Devuelve (configuración, inputs) virtuales. No muta los originales."""
    new_cfg = cfg.model_copy(deep=True)
    new_inputs = inputs.model_copy(deep=True)

    # los inputs de ventas no siempre traen business_unit_id resuelto por sucursal
    branch_to_unit = {b.id: u.id for u, b in cfg.all_branches()}
    for iv in new_inputs.values:
        if iv.branch_id and not iv.business_unit_id:
            iv.business_unit_id = branch_to_unit.get(iv.branch_id)

    for adj in adjustments:
        if adj.concept not in CONCEPT_TARGETS:
            raise BudgetError("INVALID_SCENARIO_CONCEPT",
                              f"{adj.concept} no es un input sobre el que se pueda simular")
        if adj.concept == "COST":
            # Subir el costo 5% = mover el margen para que el costo quede 5% arriba.
            # Cada producto tiene su fórmula, así que se opera sobre la proporción
            # de costo y después se vuelve al margen que corresponda.
            factor = Decimal(1) + adj.variation
            for unit in new_cfg.business_units:
                if adj.business_unit_id and unit.id != adj.business_unit_id:
                    continue
                for p in unit.products:
                    if p.margin_formula is MarginFormula.NO_COST:
                        continue          # sin costo no hay nada que mover
                    new_ratio = p.cost_ratio * factor
                    if new_ratio >= 1:
                        raise BudgetError(
                            "INVALID_SCENARIO",
                            f"la variación deja el costo por encima de las ventas en {p.code}")
                    if p.margin_formula is MarginFormula.MARKUP_ON_COST:
                        p.margin = Decimal(1) / new_ratio - Decimal(1)
                    else:
                        p.margin = Decimal(1) - new_ratio
            continue

        for iv in new_inputs.values:
            if iv.concept not in CONCEPT_TARGETS[adj.concept]:
                continue
            if not _in_scope(iv, adj):
                continue
            if adj.variation_type == "PERCENTAGE":
                iv.value = iv.value * (Decimal(1) + adj.variation)
            else:
                iv.value = iv.value + adj.variation
            iv.source = InputSource.SCENARIO
    return new_cfg, new_inputs


def run_scenario(version: BudgetVersion, scenario: Scenario) -> dict:
    cfg, inputs = apply_overlay(version.configuration, version.inputs, scenario.adjustments)
    graph = BudgetEngine(cfg, version.fx, inputs).build()
    scenario._values = graph.evaluate()
    return scenario._values


def compare(version: BudgetVersion, scenario: Scenario, scope: str = "CO",
            metrics: Optional[list[str]] = None, period_code: str = FY) -> list[dict]:
    base = version.calculate()
    sim = scenario._values or run_scenario(version, scenario)
    metrics = metrics or ["SALES", "COGS", "GROSS_MARGIN", "EXPENSES", "PAYROLL", "EBITDA"]
    out = []
    for m in metrics:
        key = nk(m, scope, period_code)
        b = base.get(key)
        s = sim.get(key)
        delta = None if b is None or s is None else Decimal(s) - Decimal(b)
        pct = None if not b else (Decimal(s) - Decimal(b)) / abs(Decimal(b))
        out.append({"metric": m, "base": b, "scenario": s, "delta": delta, "delta_pct": pct})
    return out
