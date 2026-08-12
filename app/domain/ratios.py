"""Catálogo de ratios V1 (cierra el punto pendiente del doc 01 §47).

Cada ratio declara:
    code, name, fórmula, métricas de las que depende, inputs que arrastra,
    unidad, dirección (si más es mejor o peor), niveles donde tiene sentido
    y si se calcula por período, anualizado, o ambos.

El CFO selecciona del catálogo; no escribe fórmulas (V1).
Seleccionar un ratio activa automáticamente sus dependencias
(doc 02 §37): p.ej. elegir STOCK_DAYS exige Stock inicial, y Stock exige
costo de venta, que exige Ventas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional


class RatioUnit(str, Enum):
    PERCENTAGE = "PERCENTAGE"
    TIMES = "TIMES"
    DAYS = "DAYS"
    CURRENCY = "CURRENCY"
    RATIO = "RATIO"


class Direction(str, Enum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    NEUTRAL = "NEUTRAL"


class RatioGroup(str, Enum):
    PROFITABILITY = "PROFITABILITY"
    STRUCTURE = "STRUCTURE"
    PRODUCTIVITY = "PRODUCTIVITY"
    INVENTORY = "INVENTORY"
    INVESTMENT = "INVESTMENT"
    BALANCE = "BALANCE"


ZERO = Decimal(0)


def _div(num: Optional[Decimal], den: Optional[Decimal]) -> Optional[Decimal]:
    """División protegida: sin denominador el ratio NO es 0, es no calculable.

    Doc 02 §41: un faltante no debe interpretarse automáticamente como cero.
    """
    if num is None or den is None or den == 0:
        return None
    return Decimal(num) / Decimal(den)


@dataclass(frozen=True)
class RatioDef:
    code: str
    name: str
    group: RatioGroup
    unit: RatioUnit
    direction: Direction
    formula_text: str
    metrics: tuple[str, ...]                      # métricas calculadas que consume
    required_inputs: tuple[str, ...]              # conceptos de input que arrastra
    fn: Callable[[dict, int], Optional[Decimal]]  # (métricas, días del período)
    levels: tuple[str, ...] = ("COMPANY", "BUSINESS_UNIT", "BRANCH")
    annual_only: bool = False
    notes: str = ""

    def compute(self, metrics: dict, days: int) -> Optional[Decimal]:
        for m in self.metrics:
            if metrics.get(m) is None:
                return None
        return self.fn(metrics, days)


def _r(*args, **kwargs) -> RatioDef:
    return RatioDef(*args, **kwargs)


CATALOG: list[RatioDef] = [
    # ---------------- Rentabilidad ----------------
    _r(
        code="GROSS_MARGIN_PCT",
        name="Margen bruto %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="MARGEN_BRUTO / VENTAS",
        metrics=("GROSS_MARGIN", "SALES"),
        required_inputs=("SALES",),
        fn=lambda m, days: _div(m["GROSS_MARGIN"], m["SALES"]),
    ),
    _r(
        code="EBITDA_MARGIN_PCT",
        name="EBITDA %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="EBITDA / VENTAS",
        metrics=("EBITDA", "SALES"),
        required_inputs=("SALES", "EXPENSES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["EBITDA"], m["SALES"]),
    ),
    _r(
        code="COGS_PCT",
        name="Costo de ventas sobre ventas %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="COSTO / VENTAS",
        metrics=("COGS", "SALES"),
        required_inputs=("SALES",),
        fn=lambda m, days: _div(m["COGS"], m["SALES"]),
    ),
    _r(
        code="EXPENSES_PCT",
        name="Gastos sobre ventas %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="GASTOS / VENTAS",
        metrics=("EXPENSES", "SALES"),
        required_inputs=("SALES", "EXPENSES"),
        fn=lambda m, days: _div(m["EXPENSES"], m["SALES"]),
    ),
    _r(
        code="PAYROLL_PCT",
        name="Nómina sobre ventas %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="NOMINA / VENTAS",
        metrics=("PAYROLL", "SALES"),
        required_inputs=("SALES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["PAYROLL"], m["SALES"]),
    ),
    _r(
        code="OPEX_PCT",
        name="Estructura operativa sobre ventas %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="(GASTOS + NOMINA) / VENTAS",
        metrics=("EXPENSES", "PAYROLL", "SALES"),
        required_inputs=("SALES", "EXPENSES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["EXPENSES"] + m["PAYROLL"], m["SALES"]),
    ),
    _r(
        code="PAYROLL_TO_GROSS_MARGIN",
        name="Nómina sobre margen bruto %",
        group=RatioGroup.PROFITABILITY,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="NOMINA / MARGEN_BRUTO",
        metrics=("PAYROLL", "GROSS_MARGIN"),
        required_inputs=("SALES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["PAYROLL"], m["GROSS_MARGIN"]),
        notes="Cuánto del margen bruto se consume en estructura de personal.",
    ),
    # ---------------- Estructura / asignación ----------------
    _r(
        code="CORPORATE_ALLOCATION_PCT",
        name="Gastos corporativos asignados sobre ventas %",
        group=RatioGroup.STRUCTURE,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="GASTOS_CORPORATIVOS_ASIGNADOS / VENTAS",
        metrics=("ALLOCATED_EXPENSES", "SALES"),
        required_inputs=("SALES", "EXPENSES"),
        fn=lambda m, days: _div(m["ALLOCATED_EXPENSES"], m["SALES"]),
        notes="Doc 02 §53: separa el resultado propio del impacto corporativo.",
    ),
    _r(
        code="RESULT_AFTER_ALLOCATION_PCT",
        name="Resultado después de asignación %",
        group=RatioGroup.STRUCTURE,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="(EBITDA - GASTOS_CORPORATIVOS_ASIGNADOS) / VENTAS",
        metrics=("EBITDA", "ALLOCATED_EXPENSES", "SALES"),
        required_inputs=("SALES", "EXPENSES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["EBITDA"] - m["ALLOCATED_EXPENSES"], m["SALES"]),
    ),
    # ---------------- Productividad ----------------
    _r(
        code="SALES_PER_HEAD",
        name="Ventas por persona",
        group=RatioGroup.PRODUCTIVITY,
        unit=RatioUnit.CURRENCY,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="VENTAS / DOTACION_PROMEDIO",
        metrics=("SALES", "HEADCOUNT_AVG"),
        required_inputs=("SALES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["SALES"], m["HEADCOUNT_AVG"]),
    ),
    _r(
        code="GROSS_MARGIN_PER_HEAD",
        name="Margen bruto por persona",
        group=RatioGroup.PRODUCTIVITY,
        unit=RatioUnit.CURRENCY,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="MARGEN_BRUTO / DOTACION_PROMEDIO",
        metrics=("GROSS_MARGIN", "HEADCOUNT_AVG"),
        required_inputs=("SALES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["GROSS_MARGIN"], m["HEADCOUNT_AVG"]),
    ),
    _r(
        code="EBITDA_PER_HEAD",
        name="EBITDA por persona",
        group=RatioGroup.PRODUCTIVITY,
        unit=RatioUnit.CURRENCY,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="EBITDA / DOTACION_PROMEDIO",
        metrics=("EBITDA", "HEADCOUNT_AVG"),
        required_inputs=("SALES", "EXPENSES", "PAYROLL_HEADCOUNT"),
        fn=lambda m, days: _div(m["EBITDA"], m["HEADCOUNT_AVG"]),
    ),
    _r(
        code="PAYROLL_COST_PER_HEAD",
        name="Costo laboral por persona",
        group=RatioGroup.PRODUCTIVITY,
        unit=RatioUnit.CURRENCY,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="NOMINA / DOTACION_PROMEDIO",
        metrics=("PAYROLL", "HEADCOUNT_AVG"),
        required_inputs=("PAYROLL_HEADCOUNT",),
        fn=lambda m, days: _div(m["PAYROLL"], m["HEADCOUNT_AVG"]),
    ),
    # ---------------- Inventario ----------------
    _r(
        code="STOCK_TURNOVER",
        name="Rotación de stock",
        group=RatioGroup.INVENTORY,
        unit=RatioUnit.TIMES,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="COSTO_DE_VENTA_ANUALIZADO / STOCK_PROMEDIO",
        metrics=("COGS", "STOCK_AVG"),
        required_inputs=("SALES", "OPENING_STOCK"),
        fn=lambda m, days: _div(
            Decimal(m["COGS"]) * Decimal(365) / Decimal(days or 365), m["STOCK_AVG"]
        ),
        notes="Anualizado por días del período para que sea comparable mes a mes.",
    ),
    _r(
        code="STOCK_DAYS",
        name="Días de stock",
        group=RatioGroup.INVENTORY,
        unit=RatioUnit.DAYS,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="STOCK_PROMEDIO / COSTO_DE_VENTA * DIAS_DEL_PERIODO",
        metrics=("STOCK_AVG", "COGS"),
        required_inputs=("SALES", "OPENING_STOCK"),
        fn=lambda m, days: (
            None
            if _div(m["STOCK_AVG"], m["COGS"]) is None
            else _div(m["STOCK_AVG"], m["COGS"]) * Decimal(days or 365)
        ),
    ),
    _r(
        code="STOCK_TO_SALES",
        name="Stock final sobre ventas",
        group=RatioGroup.INVENTORY,
        unit=RatioUnit.RATIO,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="STOCK_FINAL / VENTAS",
        metrics=("CLOSING_STOCK", "SALES"),
        required_inputs=("SALES", "OPENING_STOCK"),
        fn=lambda m, days: _div(m["CLOSING_STOCK"], m["SALES"]),
    ),
    _r(
        code="PURCHASE_TO_COGS",
        name="Cobertura de compras",
        group=RatioGroup.INVENTORY,
        unit=RatioUnit.RATIO,
        direction=Direction.NEUTRAL,
        formula_text="COMPRAS / COSTO_DE_VENTA",
        metrics=("PURCHASES", "COGS"),
        required_inputs=("SALES", "OPENING_STOCK", "PURCHASES"),
        fn=lambda m, days: _div(m["PURCHASES"], m["COGS"]),
        notes=">1 acumula stock, <1 lo consume.",
    ),
    # ---------------- Inversión ----------------
    _r(
        code="CAPEX_TO_SALES",
        name="CAPEX sobre ventas %",
        group=RatioGroup.INVESTMENT,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.NEUTRAL,
        formula_text="CAPEX / VENTAS",
        metrics=("CAPEX", "SALES"),
        required_inputs=("SALES", "CAPEX"),
        fn=lambda m, days: _div(m["CAPEX"], m["SALES"]),
    ),
    _r(
        code="CAPEX_TO_EBITDA",
        name="CAPEX sobre EBITDA",
        group=RatioGroup.INVESTMENT,
        unit=RatioUnit.RATIO,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="CAPEX / EBITDA",
        metrics=("CAPEX", "EBITDA"),
        required_inputs=("SALES", "EXPENSES", "PAYROLL_HEADCOUNT", "CAPEX"),
        fn=lambda m, days: _div(m["CAPEX"], m["EBITDA"]),
        notes="Sin depreciación en V1, mide esfuerzo de inversión contra generación operativa.",
    ),
    # ---------------- Balance (sólo empresa) ----------------
    _r(
        code="CURRENT_RATIO",
        name="Liquidez corriente",
        group=RatioGroup.BALANCE,
        unit=RatioUnit.RATIO,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="ACTIVO_CORRIENTE / PASIVO_CORRIENTE",
        metrics=("CURRENT_ASSETS", "CURRENT_LIABILITIES"),
        required_inputs=("BALANCE",),
        fn=lambda m, days: _div(m["CURRENT_ASSETS"], m["CURRENT_LIABILITIES"]),
        levels=("COMPANY",),
        annual_only=True,
    ),
    _r(
        code="WORKING_CAPITAL",
        name="Capital de trabajo",
        group=RatioGroup.BALANCE,
        unit=RatioUnit.CURRENCY,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="ACTIVO_CORRIENTE - PASIVO_CORRIENTE",
        metrics=("CURRENT_ASSETS", "CURRENT_LIABILITIES"),
        required_inputs=("BALANCE",),
        fn=lambda m, days: Decimal(m["CURRENT_ASSETS"]) - Decimal(m["CURRENT_LIABILITIES"]),
        levels=("COMPANY",),
        annual_only=True,
    ),
    _r(
        code="DEBT_TO_EQUITY",
        name="Pasivo sobre patrimonio",
        group=RatioGroup.BALANCE,
        unit=RatioUnit.RATIO,
        direction=Direction.LOWER_IS_BETTER,
        formula_text="PASIVO_TOTAL / PATRIMONIO",
        metrics=("LIABILITIES", "EQUITY"),
        required_inputs=("BALANCE",),
        fn=lambda m, days: _div(m["LIABILITIES"], m["EQUITY"]),
        levels=("COMPANY",),
        annual_only=True,
    ),
    _r(
        code="EQUITY_RATIO",
        name="Solvencia patrimonial %",
        group=RatioGroup.BALANCE,
        unit=RatioUnit.PERCENTAGE,
        direction=Direction.HIGHER_IS_BETTER,
        formula_text="PATRIMONIO / ACTIVO_TOTAL",
        metrics=("EQUITY", "ASSETS"),
        required_inputs=("BALANCE",),
        fn=lambda m, days: _div(m["EQUITY"], m["ASSETS"]),
        levels=("COMPANY",),
        annual_only=True,
    ),
]

RATIO_CATALOG: dict[str, RatioDef] = {r.code: r for r in CATALOG}


def format_ratio(value: Optional[Decimal], unit: RatioUnit) -> str:
    if value is None:
        return "no calculable"
    if unit is RatioUnit.PERCENTAGE:
        return f"{value * 100:.1f}%"
    if unit is RatioUnit.DAYS:
        return f"{value:.0f} días"
    if unit is RatioUnit.TIMES:
        return f"{value:.2f}x"
    if unit is RatioUnit.CURRENCY:
        return f"{value:,.0f}"
    return f"{value:.2f}"
