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
    """Doc 02 §10. Se define por producto: una misma unidad puede vender
    mercadería por unidades y servicios por monto."""

    UNIT_BASED = "UNIT_BASED"      # el gerente carga cantidad
    AMOUNT_BASED = "AMOUNT_BASED"  # el gerente carga monto


class MarginFormula(str, Enum):
    """Doc 02 §11: la fórmula de margen es configuración, no elección del que carga."""

    PERCENTAGE_OF_SALES = "PERCENTAGE_OF_SALES"  # costo = ventas x (1 - margen)
    MARKUP_ON_COST = "MARKUP_ON_COST"            # costo = ventas / (1 + margen)
    NO_COST = "NO_COST"                          # costo = 0; el precio es todo margen


class ExpenseTargetType(str, Enum):
    COMPANY = "COMPANY"
    BUSINESS_UNIT = "BUSINESS_UNIT"   # se reparte entre sus operaciones
    BRANCH = "BRANCH"                 # se reparte entre las operaciones de la sucursal
    COST_CENTER = "COST_CENTER"       # el de una operación o el de un área de soporte


class AllocationMode(str, Enum):
    """Cómo llega el importe de un gasto a cada destino."""

    #: Se carga un importe por cada destino. Si no corresponde, se carga 0.
    #: Es el modo natural cuando el mismo concepto existe en varios lugares
    #: con montos distintos: internet en cada sucursal y en administración.
    PER_TARGET = "PER_TARGET"
    #: Se carga un único total y el sistema lo reparte con porcentajes fijos.
    PERCENTAGE = "PERCENTAGE"


class InventoryLevel(str, Enum):
    COMPANY = "COMPANY"
    BUSINESS_UNIT = "BUSINESS_UNIT"
    OPERATION = "OPERATION"           # por combinación unidad x sucursal


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
    """Sucursal: una ubicación física de la empresa.

    Existe por sí misma. Qué unidades de negocio operan en ella se define
    en las operaciones (ver Operation): una sucursal puede alojar varias
    unidades, y una unidad puede estar en varias sucursales.
    """

    id: str
    name: str


class CostCenter(BaseModel):
    """Centro de costo: el lugar donde se registran los gastos de algo.

    Lo tiene cada combinación unidad x sucursal y cada área de soporte.
    El nombre es la clave: es único en toda la empresa, así que alcanza para
    identificarlo sin necesidad de un código aparte.

    El perfil responsable es quien carga los valores del presupuesto de este
    centro de costo. Se define acá y no en el workflow porque es propio de cada
    centro: administración carga los suyos, sistemas los de sistemas.
    """

    id: str
    name: str
    responsible_role: Role = Role.ADMIN_AREA


class Operation(Effectivity):
    """La combinación de una unidad de negocio operando en una sucursal.

    Es la unidad mínima del presupuesto: acá se cargan las ventas y la
    dotación, y contra su centro de costo se registran sus gastos. Las
    unidades y las sucursales son dos formas de agrupar operaciones, no
    dos niveles de una jerarquía.
    """

    id: str
    business_unit_id: str
    branch_id: str
    cost_center: CostCenter


class ProductFamily(BaseModel):
    id: str
    name: str


class Product(BaseModel):
    """La modalidad de venta y la fórmula de margen viven acá, no en la unidad:
    una misma unidad puede vender mercadería por unidades y servicios por monto."""

    id: str
    code: str
    name: str
    family_id: str
    sales_mode: SalesMode = SalesMode.UNIT_BASED
    margin_formula: MarginFormula = MarginFormula.PERCENTAGE_OF_SALES
    unit_of_measure: Literal["UNIT"] = "UNIT"   # V1: sólo "unidad"
    price: Decimal = Decimal(0)                 # constante en el ejercicio (V1)
    currency: str                               # del precio y de la carga por monto
    margin: Decimal                             # constante en el ejercicio (V1)
    sales_frequency: Frequency = Frequency.MONTHLY
    is_other: bool = False                      # el obligatorio "XX — Otros" de la familia
    #: Doc 01 §19: la comisión se calcula sobre las ventas de este producto.
    #: Cada producto puede comisionar distinto, o no comisionar.
    commission_rate: Optional[Decimal] = None

    @model_validator(mode="after")
    def _check(self) -> "Product":
        if self.margin_formula is MarginFormula.NO_COST:
            object.__setattr__(self, "margin", Decimal(1))
        elif not (Decimal(-1) < self.margin < Decimal(1)):
            raise ConfigurationError(
                "INVALID_MARGIN",
                f"producto {self.code}: el margen se expresa como fracción (0.25 = 25%). "
                "Si el precio es todo margen, usá la fórmula 'sin costo'.")
        if self.sales_mode is SalesMode.UNIT_BASED and self.price <= 0:
            raise ConfigurationError(
                "INVALID_PRODUCT", f"producto {self.code}: precio requerido en venta por unidades")
        return self

    @property
    def cost_ratio(self) -> Decimal:
        """Qué proporción de la venta es costo."""
        if self.margin_formula is MarginFormula.NO_COST:
            return Decimal(0)
        if self.margin_formula is MarginFormula.MARKUP_ON_COST:
            return Decimal(1) / (Decimal(1) + self.margin)
        return Decimal(1) - self.margin


class BusinessUnit(Effectivity):
    id: str
    name: str
    families: list[ProductFamily] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "BusinessUnit":
        fam_ids = {f.id for f in self.families}
        for p in self.products:
            if p.family_id not in fam_ids:
                raise ConfigurationError(
                    "INVALID_FAMILY",
                    f"producto {p.code} referencia familia inexistente {p.family_id}")
        # Doc 02 §8/§9: el "Otros" es por familia, no por unidad: cada familia
        # necesita su propio cajón para lo que no está en el catálogo.
        for fam in self.families:
            others = [p for p in self.products if p.family_id == fam.id and p.is_other]
            if len(others) > 1:
                raise ConfigurationError(
                    "DUPLICATE_OTHER_PRODUCT",
                    f"la familia {fam.name} tiene más de un producto 'Otros'")
        return self

    def product(self, product_id: str) -> Product:
        for p in self.products:
            if p.id == product_id:
                return p
        raise ConfigurationError("INVALID_PRODUCT", f"{product_id} no existe en {self.id}")

    def family(self, family_id: str) -> ProductFamily:
        for f in self.families:
            if f.id == family_id:
                return f
        raise ConfigurationError("INVALID_FAMILY", f"{family_id} no existe en {self.id}")

    def missing_other_products(self) -> list[ProductFamily]:
        return [f for f in self.families
                if not any(p.family_id == f.id and p.is_other for p in self.products)]


class SupportUnit(Effectivity):
    id: str
    name: str
    cost_centers: list[CostCenter] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Gastos
# --------------------------------------------------------------------------
class ExpenseTarget(BaseModel):
    """Un destino de imputación de un gasto (doc 03 §12).

    Un mismo gasto puede tener varios: internet va a todas las sucursales y a
    algunos centros de costo. En modo PER_TARGET cada destino recibe su propio
    importe; en modo PERCENTAGE recibe una porción del total.
    """

    target_type: ExpenseTargetType
    target_id: Optional[str] = None          # None sólo para COMPANY
    percentage: Optional[Decimal] = None     # sólo en PERCENTAGE
    #: Doc 02 §22: mostrar el gasto de una unidad repartido entre sus sucursales,
    #: proporcional al volumen de ventas.
    distribute_to_branches: bool = False

    @property
    def scope_key(self) -> str:
        if self.target_type is ExpenseTargetType.COMPANY:
            return "CO"
        prefix = {"BUSINESS_UNIT": "BU", "BRANCH": "BR", "COST_CENTER": "CC"}[
            self.target_type.value]
        return f"{prefix}:{self.target_id}"


class ExpenseDefinition(BaseModel):
    id: str
    name: str
    allocation_mode: AllocationMode = AllocationMode.PER_TARGET
    targets: list[ExpenseTarget] = Field(default_factory=list)
    currency: str
    frequency: Frequency = Frequency.MONTHLY
    responsible_role: Role = Role.ADMIN_AREA

    @model_validator(mode="after")
    def _check(self) -> "ExpenseDefinition":
        if not self.targets:
            raise ConfigurationError(
                "INVALID_EXPENSE", f"gasto {self.name or self.id}: elegí al menos un destino")
        for t in self.targets:
            if t.target_type is not ExpenseTargetType.COMPANY and not t.target_id:
                raise ConfigurationError("INVALID_EXPENSE_TARGET",
                                         f"gasto {self.name}: destino sin identificar")
        if self.allocation_mode is AllocationMode.PERCENTAGE:
            total = sum((t.percentage or Decimal(0) for t in self.targets), Decimal(0))
            if total != Decimal(1):
                raise ConfigurationError(
                    "INVALID_ALLOCATION",
                    f"gasto {self.name}: la distribución suma {total * 100}%, debe sumar 100%")
        return self

    def target_for(self, scope_key: str) -> Optional[ExpenseTarget]:
        for t in self.targets:
            if t.scope_key == scope_key:
                return t
        return None


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


class PayrollConfig(BaseModel):
    """Doc 01 §16: las unidades informan personas, Nómina pone los valores.

    Nómina carga la foto inicial de cada centro de costo —cuánta gente hay y
    cuánto suma por mes— a valores de hoy. Después, cada solicitud de un área
    (alta, baja o ajuste) vuelve a Nómina para que le ponga su nominal. El
    sistema aplica los aumentos del ejercicio a cada movimiento desde su propia
    fecha, y por eso quien entra en abril no cobra el aumento de marzo.

    El nominal es mensual y se anualiza: no hay frecuencia que elegir.
    """

    increase_rules: list[SalaryIncreaseRule] = Field(default_factory=list)
    percentage_concepts: list[PayrollPercentageConcept] = Field(default_factory=list)
    currency: str = "USD"

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
    level: InventoryLevel = InventoryLevel.OPERATION
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
    branches: list[Branch] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)  # unidad x sucursal
    support_units: list[SupportUnit] = Field(default_factory=list)
    expenses: list[ExpenseDefinition] = Field(default_factory=list)
    payroll: PayrollConfig = Field(default_factory=PayrollConfig)
    capex: CapexConfig = Field(default_factory=CapexConfig)
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    balance: BalanceConfig = Field(default_factory=BalanceConfig)
    ratios: list[RatioSelection] = Field(default_factory=list)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)

    status: ConfigStatus = ConfigStatus.DRAFT

    @model_validator(mode="after")
    def _canonical_order(self) -> "Configuration":
        """Los ratios se guardan siempre en el orden del catálogo.

        Así la configuración no depende del orden en que llegaron los campos
        de un formulario, y volver a guardar la misma selección no produce
        una configuración distinta.
        """
        from .ratios import CATALOG

        orden = {r.code: i for i, r in enumerate(CATALOG)}
        self.ratios.sort(key=lambda s: orden.get(s.ratio_code, 999))
        return self

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

    def branch(self, branch_id: str) -> Branch:
        for b in self.branches:
            if b.id == branch_id:
                return b
        raise ConfigurationError("INVALID_BRANCH", branch_id)

    # -- operaciones: la unidad mínima del presupuesto ---------------------
    def operation(self, operation_id: str) -> Operation:
        for o in self.operations:
            if o.id == operation_id:
                return o
        raise ConfigurationError("INVALID_OPERATION", operation_id)

    def operation_for(self, unit_id: str, branch_id: str) -> Optional[Operation]:
        for o in self.operations:
            if o.business_unit_id == unit_id and o.branch_id == branch_id:
                return o
        return None

    def unit_operations(self, unit_id: str) -> list[Operation]:
        return [o for o in self.operations if o.business_unit_id == unit_id]

    def branch_operations(self, branch_id: str) -> list[Operation]:
        return [o for o in self.operations if o.branch_id == branch_id]

    def operation_unit(self, operation_id: str) -> BusinessUnit:
        return self.unit(self.operation(operation_id).business_unit_id)

    def operation_branch(self, operation_id: str) -> Branch:
        return self.branch(self.operation(operation_id).branch_id)

    def operation_label(self, operation_id: str) -> str:
        o = self.operation(operation_id)
        return f"{self.unit(o.business_unit_id).name} / {self.branch(o.branch_id).name}"

    def unit_branches(self, unit_id: str) -> list[Branch]:
        """Las sucursales donde opera la unidad."""
        return [self.branch(o.branch_id) for o in self.unit_operations(unit_id)]

    def branch_units(self, branch_id: str) -> list[BusinessUnit]:
        """Las unidades que operan en la sucursal."""
        return [self.unit(o.business_unit_id) for o in self.branch_operations(branch_id)]

    def unassigned_branches(self) -> list[Branch]:
        """Sucursales donde todavía no opera ninguna unidad."""
        return [b for b in self.branches if not self.branch_operations(b.id)]

    def units_without_operations(self) -> list[BusinessUnit]:
        return [u for u in self.business_units if not self.unit_operations(u.id)]

    # -- centros de costo --------------------------------------------------
    def cost_centers(self) -> list[tuple[CostCenter, str, str]]:
        """(centro de costo, tipo de dueño, etiqueta)."""
        out = [(o.cost_center, "OPERATION", self.operation_label(o.id))
               for o in self.operations]
        out += [(cc, "SUPPORT_UNIT", f"{su.name} / {cc.name}")
                for su in self.support_units for cc in su.cost_centers]
        return out

    def cost_center(self, cc_id: str) -> CostCenter:
        for cc, _kind, _label in self.cost_centers():
            if cc.id == cc_id:
                return cc
        raise ConfigurationError("INVALID_COST_CENTER", cc_id)

    def cost_center_owner(self, cc_id: str):
        """Devuelve ('OPERATION', Operation) o ('SUPPORT_UNIT', SupportUnit)."""
        for o in self.operations:
            if o.cost_center.id == cc_id:
                return "OPERATION", o
        for su in self.support_units:
            for cc in su.cost_centers:
                if cc.id == cc_id:
                    return "SUPPORT_UNIT", su
        raise ConfigurationError("INVALID_COST_CENTER", cc_id)

    def scope_label(self, scope_key: str) -> str:
        if scope_key == "CO":
            return "Empresa"
        kind, _id = scope_key.split(":", 1)
        try:
            if kind == "BU":
                return self.unit(_id).name
            if kind == "BR":
                return self.branch(_id).name
            if kind == "OP":
                return self.operation_label(_id)
            if kind == "SU":
                return self.support_unit(_id).name
            if kind == "CC":
                kind_owner, owner = self.cost_center_owner(_id)
                cc = self.cost_center(_id)
                if kind_owner == "OPERATION":
                    return f"{self.operation_label(owner.id)} ({cc.name})"
                return f"{owner.name} / {cc.name}"
        except Exception:
            pass
        return scope_key

    def scope_ancestors(self, scope_key: str) -> set[str]:
        """Ámbitos que contienen a este. Una operación pertenece a la vez a su
        unidad de negocio y a su sucursal: son dos agrupaciones distintas."""
        out = {"CO"}
        if scope_key == "CO" or ":" not in scope_key:
            return out
        kind, _id = scope_key.split(":", 1)
        try:
            if kind == "OP":
                o = self.operation(_id)
                out |= {f"BU:{o.business_unit_id}", f"BR:{o.branch_id}",
                        f"CC:{o.cost_center.id}"}
            elif kind == "CC":
                kind_owner, owner = self.cost_center_owner(_id)
                if kind_owner == "OPERATION":
                    out |= {f"OP:{owner.id}", f"BU:{owner.business_unit_id}",
                            f"BR:{owner.branch_id}"}
                else:
                    out.add(f"SU:{owner.id}")
        except ConfigurationError:
            pass
        return out

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
                "no está en las monedas habilitadas")
        if not self.business_units:
            errors.append("INCOMPLETE_CONFIGURATION: no hay unidades de negocio")
        if not self.branches:
            errors.append("INCOMPLETE_CONFIGURATION: no hay sucursales")
        if not self.operations:
            errors.append("INCOMPLETE_CONFIGURATION: no hay ninguna unidad operando en "
                          "ninguna sucursal")

        for b in self.unassigned_branches():
            errors.append(
                f"BRANCH_WITHOUT_OPERATION: en la sucursal {b.name} no opera ninguna unidad")
        for u in self.units_without_operations():
            errors.append(
                f"UNIT_WITHOUT_OPERATION: la unidad {u.name} no opera en ninguna sucursal")

        seen_ids: set[str] = set()

        def uniq(kind: str, _id: str) -> None:
            key = f"{kind}:{_id}"
            if key in seen_ids:
                errors.append(f"DUPLICATE_ID: {key}")
            seen_ids.add(key)

        seen_names: set[str] = set()
        for b in self.branches:
            uniq("BR", b.id)
            low = b.name.strip().lower()
            if low in seen_names:
                errors.append(f"DUPLICATE_BRANCH_NAME: hay más de una sucursal llamada {b.name}")
            seen_names.add(low)
            for f, label in ((b.effective_from, "inicio"), (b.effective_to, "cierre")):
                if f and not (fy.start <= f <= fy.end):
                    errors.append(
                        f"INVALID_PERIOD: sucursal {b.name} fecha de {label} fuera del ejercicio")

        pares: set[tuple[str, str]] = set()
        nombres_cc: set[str] = set()
        for o in self.operations:
            uniq("OP", o.id)
            par = (o.business_unit_id, o.branch_id)
            if par in pares:
                errors.append(
                    f"DUPLICATE_OPERATION: {self.operation_label(o.id)} está más de una vez")
            pares.add(par)
            try:
                self.unit(o.business_unit_id)
                self.branch(o.branch_id)
            except ConfigurationError as exc:
                errors.append(str(exc))
                continue
            uniq("CC", o.cost_center.id)
            nombre = o.cost_center.name.strip().lower()
            if nombre in nombres_cc:
                errors.append(f"DUPLICATE_COST_CENTER_NAME: hay más de un centro de costo "
                              f"llamado {o.cost_center.name}")
            nombres_cc.add(nombre)
            for f, label in ((o.effective_from, "inicio"), (o.effective_to, "cierre")):
                if f and not (fy.start <= f <= fy.end):
                    errors.append(f"INVALID_PERIOD: {self.operation_label(o.id)} "
                                  f"fecha de {label} fuera del ejercicio")

        codigos_producto: dict[str, str] = {}
        for u in self.business_units:
            uniq("BU", u.id)
            if not u.products:
                errors.append(f"INCOMPLETE_CONFIGURATION: la unidad {u.name} no tiene productos")
            for fam in u.missing_other_products():
                errors.append(
                    f"MISSING_OTHER_PRODUCT: la familia {fam.name} de {u.name} necesita su "
                    "producto 'Otros'")
            for p in u.products:
                uniq("PROD", p.id)
                # El código identifica al producto en toda la empresa: es lo que
                # se escribe en las planillas de carga, y ahí no hay unidad ni
                # familia que lo desambigüe.
                code = p.code.strip().lower()
                if code in codigos_producto:
                    errors.append(
                        f"DUPLICATE_PRODUCT_CODE: el código {p.code} ya lo usa "
                        f"{codigos_producto[code]}")
                codigos_producto[code] = f"{p.name} ({u.name})"
                if p.currency not in self.enabled_currencies:
                    errors.append(f"INVALID_CURRENCY: {p.currency} en producto {p.code}")
            for f in u.families:
                uniq("FAM", f.id)

        for su in self.support_units:
            uniq("SU", su.id)
            # Un área de soporte sin centro de costo no tiene contra qué imputar
            # sus gastos: no puede existir.
            if not su.cost_centers:
                errors.append(f"SUPPORT_UNIT_WITHOUT_COST_CENTER: el área {su.name} no tiene "
                              "ningún centro de costo")
            for cc in su.cost_centers:
                uniq("CC", cc.id)
                nombre = cc.name.strip().lower()
                if nombre in nombres_cc:
                    errors.append(f"DUPLICATE_COST_CENTER_NAME: hay más de un centro de costo "
                                  f"llamado {cc.name}")
                nombres_cc.add(nombre)

        known = seen_ids
        prefix = {"BUSINESS_UNIT": "BU", "BRANCH": "BR", "COST_CENTER": "CC"}
        for e in self.expenses:
            uniq("EXP", e.id)
            if e.currency not in self.enabled_currencies:
                errors.append(f"INVALID_CURRENCY: {e.currency} en gasto {e.name}")
            for tg in e.targets:
                if tg.target_type is ExpenseTargetType.COMPANY:
                    continue
                key = f"{prefix[tg.target_type.value]}:{tg.target_id}"
                if key not in known:
                    errors.append(
                        f"INVALID_EXPENSE_TARGET: el gasto {e.name} apunta a {key}, que no existe")

        if self.payroll.currency not in self.enabled_currencies:
            errors.append(f"INVALID_CURRENCY: {self.payroll.currency} en nómina")
        for r in self.payroll.increase_rules:
            if not (fy.start <= r.effective_date <= fy.end):
                errors.append(
                    f"INVALID_PERIOD: regla de aumento {r.effective_date} fuera del ejercicio")

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

        from .ratios import RATIO_CATALOG

        for r in self.ratios:
            if r.ratio_code not in RATIO_CATALOG:
                errors.append(f"INVALID_RATIO: {r.ratio_code} no está en el catálogo V1")

        return errors

    def required_concepts(self) -> set[str]:
        """Doc 02 §42: la obligatoriedad surge del modelo, no de una lista rígida."""
        from .ratios import RATIO_CATALOG

        required = {"SALES", "EXPENSES", "PAYROLL_HEADCOUNT", "PAYROLL_SALARY"}
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
                    f"PENDING_DEPENDENCY: el ratio {ratio.name} requiere Stock, que no está "
                    "configurado")
            if "BALANCE" in ratio.required_inputs and not self.balance.enabled:
                out.append(
                    f"PENDING_DEPENDENCY: el ratio {ratio.name} requiere Balance, que no está "
                    "configurado")
            if "CAPEX" in ratio.required_inputs and not self.capex.enabled:
                out.append(
                    f"PENDING_DEPENDENCY: el ratio {ratio.name} requiere CAPEX, que no está "
                    "configurado")
        return out
