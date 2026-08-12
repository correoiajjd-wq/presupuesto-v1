"""Esquema de configuración: el contrato del modelo presupuestario.

Doc 01 §46.1: "La configuración es el contrato del modelo. Una vez cerrada,
define qué existe, quién carga, qué se calcula y qué se valida."

Este módulo es la representación ejecutable de esa configuración. Todo el
resto del sistema (dependencias, cálculo, validación, workflow, plantillas
de carga) se deriva de acá; nada se hardcodea aguas abajo.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .periods import FiscalYear, Frequency


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class SalesMode(str, Enum):
    UNIT_BASED = "UNIT_BASED"      # el gerente carga cantidad
    AMOUNT_BASED = "AMOUNT_BASED"  # el gerente carga monto


class MarginFormula(str, Enum):
    """Doc 02 §11: la fórmula de margen es configuración, no elección del que carga."""

    PERCENTAGE_OF_SALES = "PERCENTAGE_OF_SALES"  # costo = ventas * (1 - margen)
    MARKUP_ON_COST = "MARKUP_ON_COST"            # costo = ventas / (1 + margen)


class ExpenseLevel(str, Enum):
    COMPANY = "COMPANY"
    BUSINESS_UNIT = "BUSINESS_UNIT"
    BRANCH = "BRANCH"
    COST_CENTER = "COST_CENTER"
    DISTRIBUTED = "DISTRIBUTED"


class InventoryLevel(str, Enum):
    COMPANY = "COMPANY"
    BUSINESS_UNIT = "BUSINESS_UNIT"
    BRANCH = "BRANCH"


class BalanceSection(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"


class BalanceSource(str, Enum):
    MANUAL = "MANUAL"
    CALCULATED = "CALCULATED"


class ObjectiveType(str, Enum):
    MINIMUM = "MINIMUM"
    MAXIMUM = "MAXIMUM"
    RANGE = "RANGE"
    EXACT = "EXACT"


class ConfigStatus(str, Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    LOCKED = "LOCKED"


class Role(str, Enum):
    CFO = "CFO"
    COO = "COO"
    ADMIN_AREA = "ADMIN_AREA"
    PAYROLL_AREA = "PAYROLL_AREA"
    UNIT_MANAGER = "UNIT_MANAGER"
    FINANCE_AREA = "FINANCE_AREA"
    REVIEWER = "REVIEWER"
    APPROVER = "APPROVER"
    ADMINISTRATOR = "ADMINISTRATOR"


class ConfigurationError(ValueError):
    """Error estructural de configuración. Bloquea (doc 02 §62)."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


# --------------------------------------------------------------------------
# Estructura organizacional
# --------------------------------------------------------------------------
class Effectivity(BaseModel):
    """Vigencia (doc 02 §7). None = vigente todo el ejercicio."""

    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class Branch(Effectivity):
    id: str
    name: str


class ProductFamily(BaseModel):
    id: str
    name: str


class Product(BaseModel):
    id: str
    code: str
    name: str
    family_id: str
    unit_of_measure: Literal["UNIT"] = "UNIT"   # V1: sólo "unidad"
    price: Decimal = Decimal(0)                 # constante en el ejercicio (V1)
    price_currency: str
    margin: Decimal                             # constante en el ejercicio (V1)
    sales_frequency: Frequency = Frequency.MONTHLY
    is_other: bool = False                      # el obligatorio "XX — Otros"

    @field_validator("margin")
    @classmethod
    def _margin_range(cls, v: Decimal) -> Decimal:
        if not (Decimal(-1) < v < Decimal(1)):
            raise ValueError("margin debe expresarse como fracción (0.25 = 25%)")
        return v


class BusinessUnit(Effectivity):
    id: str
    name: str
    sales_mode: SalesMode
    margin_formula: MarginFormula = MarginFormula.PERCENTAGE_OF_SALES
    sales_currency: str                        # moneda de la carga AMOUNT_BASED
    branches: list[Branch] = Field(default_factory=list)
    families: list[ProductFamily] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    commission_rate: Optional[Decimal] = None  # si se define, nómina lo calcula (doc 01 §19)

    @model_validator(mode="after")
    def _check(self) -> "BusinessUnit":
        fam_ids = {f.id for f in self.families}
        for p in self.products:
            if p.family_id not in fam_ids:
                raise ConfigurationError(
                    "INVALID_FAMILY", f"producto {p.code} referencia familia inexistente {p.family_id}"
                )
        if self.products and not any(p.is_other for p in self.products):
            raise ConfigurationError(
                "MISSING_OTHER_PRODUCT",
                f"la unidad {self.id} debe tener el producto obligatorio 'XX — Otros'",
            )
        if self.sales_mode is SalesMode.UNIT_BASED:
            for p in self.products:
                if p.price <= 0:
                    raise ConfigurationError(
                        "INVALID_PRODUCT", f"producto {p.code}: precio requerido en modalidad por unidades"
                    )
        return self

    def product(self, product_id: str) -> Product:
        for p in self.products:
            if p.id == product_id:
                return p
        raise ConfigurationError("INVALID_PRODUCT", f"{product_id} no existe en {self.id}")

    def branch(self, branch_id: str) -> Branch:
        for b in self.branches:
            if b.id == branch_id:
                return b
        raise ConfigurationError("INVALID_BRANCH", f"{branch_id} no existe en {self.id}")


class CostCenter(BaseModel):
    id: str
    name: str


class SupportUnit(Effectivity):
    id: str
    name: str
    cost_centers: list[CostCenter] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Gastos
# --------------------------------------------------------------------------
class Allocation(BaseModel):
    """Distribución explícita de un gasto (doc 03 §12)."""

    target_type: Literal["BUSINESS_UNIT", "BRANCH", "COST_CENTER"]
    target_id: str
    percentage: Decimal


class ExpenseDefinition(BaseModel):
    id: str
    name: str
    level: ExpenseLevel
    target_id: Optional[str] = None                 # requerido salvo COMPANY/DISTRIBUTED
    allocations: list[Allocation] = Field(default_factory=list)
    currency: str
    frequency: Frequency = Frequency.MONTHLY
    responsible_role: Role = Role.ADMIN_AREA
    distribute_to_branches: bool = False            # doc 02 §22: proporcional a ventas
    corporate: bool = False                         # se muestra bajo EBITDA de la unidad

    @model_validator(mode="after")
    def _check(self) -> "ExpenseDefinition":
        if self.level is ExpenseLevel.DISTRIBUTED:
            if not self.allocations:
                raise ConfigurationError("INVALID_ALLOCATION", f"gasto {self.id} sin distribución")
            total = sum((a.percentage for a in self.allocations), Decimal(0))
            if total != Decimal(1):
                raise ConfigurationError(
                    "INVALID_ALLOCATION",
                    f"gasto {self.id}: la distribución suma {total * 100}%, debe sumar 100%",
                )
        elif self.level in (ExpenseLevel.BUSINESS_UNIT, ExpenseLevel.BRANCH, ExpenseLevel.COST_CENTER):
            if not self.target_id:
                raise ConfigurationError("INVALID_EXPENSE", f"gasto {self.id} sin target_id")
        return self


# --------------------------------------------------------------------------
# Nómina
# --------------------------------------------------------------------------
class SalaryIncreaseRule(BaseModel):
    effective_date: date
    percentage: Decimal


class PayrollPercentageConcept(BaseModel):
    """Conceptos que son % del sueldo (cargas sociales, beneficios)."""

    concept: str
    percentage: Decimal


class PayrollArea(BaseModel):
    """Área/sector de nómina con su salario base de referencia (lo define Nómina)."""

    id: str
    name: str
    base_salary: Decimal
    currency: str


class PayrollConfig(BaseModel):
    areas: list[PayrollArea] = Field(default_factory=list)
    increase_rules: list[SalaryIncreaseRule] = Field(default_factory=list)
    percentage_concepts: list[PayrollPercentageConcept] = Field(default_factory=list)
    frequency: Frequency = Frequency.MONTHLY

    def area(self, area_id: str) -> PayrollArea:
        for a in self.areas:
            if a.id == area_id:
                return a
        raise ConfigurationError("INVALID_PAYROLL_AREA", area_id)

    @property
    def charges_factor(self) -> Decimal:
        return Decimal(1) + sum((c.percentage for c in self.percentage_concepts), Decimal(0))


# --------------------------------------------------------------------------
# CAPEX / Inventario / Balance
# --------------------------------------------------------------------------
class CapexCategory(BaseModel):
    id: str
    name: str


class CapexConfig(BaseModel):
    enabled: bool = False
    categories: list[CapexCategory] = Field(default_factory=list)
    frequency: Frequency = Frequency.MONTHLY


class InventoryConfig(BaseModel):
    enabled: bool = False
    level: InventoryLevel = InventoryLevel.BRANCH
    frequency: Frequency = Frequency.MONTHLY
    currency: str = "USD"
    purchases_enabled: bool = True


class BalanceItem(BaseModel):
    id: str
    name: str
    section: BalanceSection
    current: bool = True                       # corriente / no corriente
    source: BalanceSource = BalanceSource.MANUAL
    #: Doc 01 §26: el TOTAL de patrimonio siempre es calculado (Activo - Pasivo).
    #: Los componentes del patrimonio (capital, resultados acumulados) sí se
    #: cargan, y su suma debe coincidir con ese total: si no, el balance no cierra.


class BalanceConfig(BaseModel):
    enabled: bool = False
    currency: str = "USD"
    items: list[BalanceItem] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Ratios, objetivos, workflow
# --------------------------------------------------------------------------
class Objective(BaseModel):
    type: ObjectiveType
    value: Decimal
    value_max: Optional[Decimal] = None

    @model_validator(mode="after")
    def _check(self) -> "Objective":
        if self.type is ObjectiveType.RANGE and self.value_max is None:
            raise ConfigurationError("INVALID_OBJECTIVE", "RANGE requiere value_max")
        return self

    def met(self, actual: Decimal) -> bool:
        if self.type is ObjectiveType.MINIMUM:
            return actual >= self.value
        if self.type is ObjectiveType.MAXIMUM:
            return actual <= self.value
        if self.type is ObjectiveType.EXACT:
            return actual == self.value
        return self.value <= actual <= (self.value_max or self.value)


class RatioSelection(BaseModel):
    ratio_code: str
    objective: Optional[Objective] = None


class WorkflowStep(BaseModel):
    concept: str                 # SALES, EXPENSES, PAYROLL, CAPEX, INVENTORY, BALANCE
    loader_role: Role
    reviewer_role: Role
    approver_role: Role


class WorkflowConfig(BaseModel):
    steps: list[WorkflowStep] = Field(default_factory=list)

    def step(self, concept: str) -> Optional[WorkflowStep]:
        for s in self.steps:
            if s.concept == concept:
                return s
        return None


# --------------------------------------------------------------------------
# Configuración completa
# --------------------------------------------------------------------------
class Configuration(BaseModel):
    """Snapshot inmutable una vez LOCKED. Una versión apunta a un snapshot."""

    company_name: str
    fiscal_year_start: date
    fiscal_year_end: date
    presentation_currency: str
    enabled_currencies: list[str] = Field(default_factory=list)

    business_units: list[BusinessUnit] = Field(default_factory=list)
    support_units: list[SupportUnit] = Field(default_factory=list)
    expenses: list[ExpenseDefinition] = Field(default_factory=list)
    payroll: PayrollConfig = Field(default_factory=PayrollConfig)
    capex: CapexConfig = Field(default_factory=CapexConfig)
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    balance: BalanceConfig = Field(default_factory=BalanceConfig)
    ratios: list[RatioSelection] = Field(default_factory=list)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)

    status: ConfigStatus = ConfigStatus.DRAFT

    # -- derivados ---------------------------------------------------------
    @property
    def fiscal_year(self) -> FiscalYear:
        return FiscalYear(self.fiscal_year_start, self.fiscal_year_end)

    @property
    def periods(self):
        return self.fiscal_year.periods

    def unit(self, unit_id: str) -> BusinessUnit:
        for u in self.business_units:
            if u.id == unit_id:
                return u
        raise ConfigurationError("INVALID_BUSINESS_UNIT", unit_id)

    def support_unit(self, unit_id: str) -> SupportUnit:
        for u in self.support_units:
            if u.id == unit_id:
                return u
        raise ConfigurationError("INVALID_SUPPORT_UNIT", unit_id)

    def expense(self, expense_id: str) -> ExpenseDefinition:
        for e in self.expenses:
            if e.id == expense_id:
                return e
        raise ConfigurationError("INVALID_EXPENSE", expense_id)

    def branch_owner(self, branch_id: str) -> BusinessUnit:
        for u in self.business_units:
            for b in u.branches:
                if b.id == branch_id:
                    return u
        raise ConfigurationError("INVALID_BRANCH", branch_id)

    def all_branches(self) -> list[tuple[BusinessUnit, Branch]]:
        return [(u, b) for u in self.business_units for b in u.branches]

    def cost_center_owner(self, cc_id: str) -> SupportUnit:
        for u in self.support_units:
            for cc in u.cost_centers:
                if cc.id == cc_id:
                    return u
        raise ConfigurationError("INVALID_COST_CENTER", cc_id)

    def is_active(self, entity: Effectivity, period) -> bool:
        """Vigencia por período (doc 02 §7)."""
        if entity.effective_from and period.last_day < entity.effective_from:
            return False
        if entity.effective_to and period.first_day > entity.effective_to:
            return False
        return True

    # -- validación estructural -------------------------------------------
    def validate_structure(self) -> list[str]:
        """Errores bloqueantes de configuración. Vacío = se puede cerrar."""
        errors: list[str] = []
        fy = self.fiscal_year

        if self.presentation_currency not in self.enabled_currencies:
            errors.append(
                f"INVALID_CURRENCY: la moneda de presentación {self.presentation_currency} "
                "no está en las monedas habilitadas"
            )
        if not self.business_units:
            errors.append("INCOMPLETE_CONFIGURATION: no hay unidades de negocio")

        seen_ids: set[str] = set()

        def uniq(kind: str, _id: str) -> None:
            key = f"{kind}:{_id}"
            if key in seen_ids:
                errors.append(f"DUPLICATE_ID: {key}")
            seen_ids.add(key)

        for u in self.business_units:
            uniq("BU", u.id)
            if not u.branches:
                errors.append(f"INCOMPLETE_CONFIGURATION: unidad {u.id} sin sucursales")
            if not u.products:
                errors.append(f"INCOMPLETE_CONFIGURATION: unidad {u.id} sin productos")
            if u.sales_currency not in self.enabled_currencies:
                errors.append(f"INVALID_CURRENCY: {u.sales_currency} en unidad {u.id}")
            for b in u.branches:
                uniq("BR", b.id)
                for f, label in ((b.effective_from, "inicio"), (b.effective_to, "cierre")):
                    if f and not (fy.start <= f <= fy.end):
                        errors.append(
                            f"INVALID_PERIOD: sucursal {b.id} fecha de {label} fuera del ejercicio"
                        )
            for p in u.products:
                uniq("PROD", p.id)
                if p.price_currency not in self.enabled_currencies:
                    errors.append(f"INVALID_CURRENCY: {p.price_currency} en producto {p.code}")
            for f in u.families:
                uniq("FAM", f.id)

        for su in self.support_units:
            uniq("SU", su.id)
            for cc in su.cost_centers:
                uniq("CC", cc.id)

        known = seen_ids
        for e in self.expenses:
            uniq("EXP", e.id)
            if e.currency not in self.enabled_currencies:
                errors.append(f"INVALID_CURRENCY: {e.currency} en gasto {e.id}")
            targets = (
                [(a.target_type, a.target_id) for a in e.allocations]
                if e.level is ExpenseLevel.DISTRIBUTED
                else ([(e.level.value, e.target_id)] if e.target_id else [])
            )
            prefix = {"BUSINESS_UNIT": "BU", "BRANCH": "BR", "COST_CENTER": "CC"}
            for ttype, tid in targets:
                key = f"{prefix.get(ttype, ttype)}:{tid}"
                if ttype in prefix and key not in known:
                    errors.append(f"INVALID_EXPENSE_TARGET: gasto {e.id} apunta a {key} inexistente")

        for a in self.payroll.areas:
            if a.currency not in self.enabled_currencies:
                errors.append(f"INVALID_CURRENCY: {a.currency} en área de nómina {a.id}")
        for r in self.payroll.increase_rules:
            if not (fy.start <= r.effective_date <= fy.end):
                errors.append(
                    f"INVALID_PERIOD: regla de aumento {r.effective_date} fuera del ejercicio"
                )

        if self.inventory.enabled:
            if self.inventory.currency not in self.enabled_currencies:
                errors.append(f"INVALID_CURRENCY: {self.inventory.currency} en inventario")
            if not any(u.families for u in self.business_units):
                errors.append("INCOMPLETE_CONFIGURATION: stock configurado sin familias definidas")

        if self.balance.enabled:
            if not self.balance.items:
                errors.append("INCOMPLETE_CONFIGURATION: balance habilitado sin rubros")
            if not any(i.section is BalanceSection.EQUITY for i in self.balance.items):
                errors.append("INCOMPLETE_CONFIGURATION: balance sin rubro de patrimonio")

        from .ratios import RATIO_CATALOG  # import local para evitar ciclo

        for r in self.ratios:
            if r.ratio_code not in RATIO_CATALOG:
                errors.append(f"INVALID_RATIO: {r.ratio_code} no está en el catálogo V1")

        return errors

    def required_concepts(self) -> set[str]:
        """Doc 02 §42: la obligatoriedad surge del modelo, no de una lista rígida.

        Devuelve los conceptos de input que el modelo exige: los del P&L mínimo
        más los que arrastren los ratios seleccionados y los módulos habilitados.
        """
        from .ratios import RATIO_CATALOG

        required = {"SALES", "EXPENSES", "PAYROLL_HEADCOUNT"}
        if self.capex.enabled:
            required.add("CAPEX")
        if self.inventory.enabled:
            required.add("OPENING_STOCK")
            if self.inventory.purchases_enabled:
                required.add("PURCHASES")
        if self.balance.enabled:
            required.add("BALANCE")
        for sel in self.ratios:
            ratio = RATIO_CATALOG.get(sel.ratio_code)
            if ratio:
                required |= set(ratio.required_inputs)
        return required

    def missing_modules_for_ratios(self) -> list[str]:
        """Doc 02 §37: seleccionar un ratio puede generar nuevos requerimientos."""
        from .ratios import RATIO_CATALOG

        out: list[str] = []
        for sel in self.ratios:
            ratio = RATIO_CATALOG.get(sel.ratio_code)
            if not ratio:
                continue
            if "OPENING_STOCK" in ratio.required_inputs and not self.inventory.enabled:
                out.append(
                    f"PENDING_DEPENDENCY: el ratio {ratio.code} requiere Stock, que no está configurado"
                )
            if "BALANCE" in ratio.required_inputs and not self.balance.enabled:
                out.append(
                    f"PENDING_DEPENDENCY: el ratio {ratio.code} requiere Balance, que no está configurado"
                )
            if "CAPEX" in ratio.required_inputs and not self.capex.enabled:
                out.append(
                    f"PENDING_DEPENDENCY: el ratio {ratio.code} requiere CAPEX, que no está configurado"
                )
        return out
