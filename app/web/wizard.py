"""Wizard de configuración — el primer paso del CFO y del COO.

Doc 01 §5: el sistema usa un wizard persistente y multiusuario. La
configuración puede guardarse y retomarse, y no puede empezar la carga hasta
que esté validada.

Doc 01 §6: la configuración es multiusuario y cada elemento tiene su
responsable. Acá eso se traduce en qué pasos ve y puede editar cada rol:
la estructura financiera, los gastos, los ratios y el workflow son del CFO;
los productos, las familias y la modalidad de venta son del COO.

Este módulo sólo arma y modifica la Configuration. No calcula nada y no
decide nada que el dominio no haya decidido ya.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from ..domain.config import (
    AllocationMode, BalanceItem, BalanceSection, BalanceSource, Branch, BusinessUnit,
    CapexCategory, ConfigStatus, Configuration, CostCenter, ExpenseDefinition, ExpenseTarget,
    ExpenseTargetType, InventoryLevel, MarginFormula, Objective, ObjectiveType, Operation,
    PayrollArea, PayrollPercentageConcept, Product, ProductFamily, RatioSelection, Role,
    SalaryIncreaseRule, SalesMode, SupportUnit, WorkflowStep,
)
from ..domain.money import FXTable
from ..domain.periods import Frequency
from ..services.budget import BudgetError


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    owner: Role
    blurb: str


STEPS: list[Step] = [
    Step("general", "Datos generales", Role.CFO,
         "Empresa, ejercicio, monedas habilitadas y tipos de cambio estimados."),
    Step("estructura", "Estructura", Role.CFO,
         "Sucursales y unidades de negocio se dan de alta por separado; después se crea "
         "cada operación: la combinación de una unidad operando en una sucursal. Una unidad "
         "puede estar en varias sucursales y una sucursal alojar varias unidades. Cada "
         "combinación necesita su centro de costo, porque ahí se registran sus gastos. "
         "Todo puede tener fecha de inicio y de cierre dentro del ejercicio."),
    Step("productos", "Productos y familias", Role.COO,
         "El COO define el catálogo de cada unidad. La modalidad de venta y la fórmula de "
         "margen son de cada producto: una unidad puede vender mercadería por unidades y "
         "servicios por monto."),
    Step("gastos", "Gastos", Role.CFO,
         "Qué gastos existen, a qué destinos se imputan, con qué frecuencia y en qué moneda. "
         "Un mismo gasto puede ir a varias sucursales y centros de costo a la vez."),
    Step("nomina", "Nómina", Role.CFO,
         "Áreas con su sueldo base, reglas de aumento y conceptos porcentuales. "
         "Las unidades informan personas; Nómina pone los valores."),
    Step("modulos", "CAPEX, Stock y Balance", Role.CFO,
         "Módulos opcionales. Lo que no se configura, no se pide."),
    Step("ratios", "Ratios y objetivos", Role.CFO,
         "Se eligen del catálogo. Cada ratio arrastra las dependencias que necesita."),
    Step("workflow", "Workflow", Role.CFO,
         "Quién carga, quién revisa y quién aprueba cada concepto."),
    Step("cierre", "Validación y cierre", Role.CFO,
         "Configuración cerrada = configuración bloqueada. A partir de acá se puede cargar."),
]

STEP_BY_KEY = {s.key: s for s in STEPS}


def can_edit_step(user, step: Step) -> bool:
    if Role.CFO in user.roles:
        return True
    return step.owner in user.roles


def assert_open(version) -> None:
    """Doc 01 §7: una vez cerrada, la configuración no se modifica."""
    version.assert_mutable()
    if version.configuration.status is ConfigStatus.LOCKED:
        raise BudgetError(
            "CONFIGURATION_LOCKED",
            "La configuración está cerrada. Los cambios estructurales requieren una nueva versión.")


def _next_id(existing: list[str], prefix: str, width: int = 2) -> str:
    n = 1
    used = set(existing)
    while f"{prefix}-{n:0{width}d}" in used:
        n += 1
    return f"{prefix}-{n:0{width}d}"


def _name(form, campo: str = "name") -> str:
    """Un nombre vacío no crea nada.

    Sin esto, reenviar un formulario de alta sin escribir nada da de alta una
    entidad sin nombre — el mismo tipo de error que el valor por defecto
    destructivo en un selector.
    """
    valor = (form.get(campo) or "").strip()
    if not valor:
        raise BudgetError("MISSING_NAME", "El nombre no puede quedar vacío.")
    return valor


def _opt_date(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def _dec(raw, default="0") -> Decimal:
    raw = str(raw if raw not in (None, "") else default).strip().replace(",", ".")
    return Decimal(raw)


def _pct(raw, default="0") -> Decimal:
    """Los porcentajes se escriben como 25, se guardan como 0.25."""
    return _dec(raw, default) / Decimal(100)


# ==========================================================================
# 1. Datos generales
# ==========================================================================
def new_configuration(form) -> Configuration:
    currencies = [c.strip().upper() for c in form.get("currencies", "").split(",") if c.strip()]
    presentation = form["presentation_currency"].strip().upper()
    if presentation not in currencies:
        currencies.insert(0, presentation)
    return Configuration(
        company_name=form["company_name"].strip(),
        fiscal_year_start=date.fromisoformat(form["fiscal_year_start"]),
        fiscal_year_end=date.fromisoformat(form["fiscal_year_end"]),
        presentation_currency=presentation,
        enabled_currencies=currencies,
    )


def update_general(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.company_name = form["company_name"].strip()
    cfg.fiscal_year_start = date.fromisoformat(form["fiscal_year_start"])
    cfg.fiscal_year_end = date.fromisoformat(form["fiscal_year_end"])
    currencies = [c.strip().upper() for c in form.get("currencies", "").split(",") if c.strip()]
    if cfg.presentation_currency not in currencies:
        currencies.insert(0, cfg.presentation_currency)
    cfg.enabled_currencies = currencies
    version.fx.enabled = set(currencies) | {cfg.presentation_currency}
    version.invalidate()


def set_fx_rate(version, form) -> None:
    """Doc 02 §30: el TC se carga para cada día del ejercicio.

    Acá se pide el TC estimado de inicio y, opcionalmente, el de cierre: el
    sistema interpola linealmente día por día. Es una comodidad de carga, no
    una regla del modelo — la tabla que queda guardada es diaria igual, y
    después se puede sobreescribir cualquier día puntual.
    """
    assert_open(version)
    cfg = version.configuration
    currency = form["currency"].strip().upper()
    if currency == cfg.presentation_currency:
        raise BudgetError("INVALID_CURRENCY",
                          "La moneda de presentación no necesita tipo de cambio.")
    if currency not in cfg.enabled_currencies:
        raise BudgetError("INVALID_CURRENCY", f"{currency} no está habilitada.")

    start_units = _dec(form["start_rate"])
    if start_units <= 0:
        raise BudgetError("INVALID_FX_RATE", "El tipo de cambio debe ser mayor a cero.")
    end_units = _dec(form.get("end_rate") or form["start_rate"])

    fy = cfg.fiscal_year
    total_days = (fy.end - fy.start).days or 1
    day = fy.start
    while day <= fy.end:
        t = Decimal((day - fy.start).days) / Decimal(total_days)
        units = start_units + (end_units - start_units) * t
        version.fx.add_inverse(currency, day, units)
        day += timedelta(days=1)
    version.invalidate()


def fx_summary(version) -> list[dict]:
    cfg = version.configuration
    out = []
    for c in cfg.enabled_currencies:
        if c == cfg.presentation_currency:
            continue
        try:
            start = version.fx.rate_on(c, cfg.fiscal_year_start)
            end = version.fx.rate_on(c, cfg.fiscal_year_end)
            out.append({"currency": c, "loaded": True,
                        "start": (Decimal(1) / start) if start else None,
                        "end": (Decimal(1) / end) if end else None})
        except Exception:
            out.append({"currency": c, "loaded": False, "start": None, "end": None})
    return out


# ==========================================================================
# 2. Estructura
# ==========================================================================
def add_business_unit(version, form) -> BusinessUnit:
    assert_open(version)
    cfg = version.configuration
    unit = BusinessUnit(
        id=_next_id([u.id for u in cfg.business_units], "BU"),
        name=_name(form),
        effective_from=_opt_date(form.get("effective_from")),
        effective_to=_opt_date(form.get("effective_to")),
    )
    cfg.business_units.append(unit)
    version.invalidate()
    return unit


def add_branch(version, form) -> Branch:
    """Alta de la sucursal en el catálogo de la empresa, todavía sin unidad."""
    assert_open(version)
    cfg = version.configuration
    name = _name(form)
    if any(b.name.strip().lower() == name.lower() for b in cfg.branches):
        raise BudgetError("DUPLICATE_BRANCH_NAME", f"ya existe una sucursal llamada {name}")
    branch = Branch(
        id=_next_id([b.id for b in cfg.branches], "BR"),
        name=name,
        effective_from=_opt_date(form.get("effective_from")),
        effective_to=_opt_date(form.get("effective_to")),
    )
    cfg.branches.append(branch)
    version.invalidate()
    return branch


def _cost_center_ids(cfg: Configuration) -> list[str]:
    return ([o.cost_center.id for o in cfg.operations]
            + [c.id for u in cfg.support_units for c in u.cost_centers])


def _check_cost_center_name(cfg: Configuration, nombre: str, donde: str) -> str:
    """El nombre del centro de costo es su identificación, y es único en toda
    la empresa: no hace falta un código aparte."""
    nombre = (nombre or "").strip()
    if not nombre:
        raise BudgetError(
            "MISSING_COST_CENTER",
            f"Indicá el centro de costo: es donde se van a registrar los gastos de {donde}.")
    if any(cc.name.strip().lower() == nombre.lower() for cc, _k, _l in cfg.cost_centers()):
        raise BudgetError("DUPLICATE_COST_CENTER_NAME",
                          f"ya existe un centro de costo llamado {nombre}")
    return nombre


def add_operation(version, form) -> Operation:
    """Crea la operación: una unidad de negocio operando en una sucursal.

    Es la unidad mínima del presupuesto. La relación es de muchos a muchos:
    una unidad puede operar en varias sucursales y una sucursal alojar varias
    unidades. Cada combinación se crea con su propio centro de costo, porque
    es contra él que se imputan sus gastos.
    """
    assert_open(version)
    cfg = version.configuration
    unit_id = (form.get("business_unit_id") or "").strip()
    branch_id = (form.get("branch_id") or "").strip()
    if not unit_id or not branch_id:
        raise BudgetError("INVALID_OPERATION",
                          "Elegí la unidad de negocio y la sucursal de la combinación.")
    unit = cfg.unit(unit_id)
    branch = cfg.branch(branch_id)
    if cfg.operation_for(unit_id, branch_id):
        raise BudgetError("DUPLICATE_OPERATION",
                          f"{unit.name} ya opera en {branch.name}.")
    nombre = _check_cost_center_name(cfg, form.get("cost_center_name"),
                                     f"{unit.name} en {branch.name}")
    op = Operation(
        id=_next_id([o.id for o in cfg.operations], "OP"),
        business_unit_id=unit_id,
        branch_id=branch_id,
        cost_center=CostCenter(id=_next_id(_cost_center_ids(cfg), "CC"), name=nombre),
        effective_from=_opt_date(form.get("effective_from")),
        effective_to=_opt_date(form.get("effective_to")),
    )
    cfg.operations.append(op)
    version.invalidate()
    return op


def add_support_unit(version, form) -> SupportUnit:
    """El área de soporte nace con su primer centro de costo.

    Un área sin centro de costo no tiene contra qué imputar sus gastos, así que
    no tiene sentido que exista en ese estado ni por un momento: las dos cosas
    se piden en el mismo formulario.
    """
    assert_open(version)
    cfg = version.configuration
    nombre_area = _name(form)
    nombre_cc = _check_cost_center_name(cfg, form.get("cost_center_name"), nombre_area)
    su = SupportUnit(id=_next_id([u.id for u in cfg.support_units], "SU"),
                     name=nombre_area,
                     cost_centers=[CostCenter(id=_next_id(_cost_center_ids(cfg), "CC"),
                                              name=nombre_cc)],
                     effective_from=_opt_date(form.get("effective_from")),
                     effective_to=_opt_date(form.get("effective_to")))
    cfg.support_units.append(su)
    version.invalidate()
    return su


def add_cost_center(version, form) -> CostCenter:
    """Centros de costo adicionales de un área que ya existe."""
    assert_open(version)
    cfg = version.configuration
    su = cfg.support_unit(form["support_unit_id"])
    nombre = _check_cost_center_name(cfg, form.get("name"), su.name)
    cc = CostCenter(id=_next_id(_cost_center_ids(cfg), "CC"), name=nombre)
    su.cost_centers.append(cc)
    version.invalidate()
    return cc


# ==========================================================================
# 3. Productos y familias
# ==========================================================================
def add_family(version, form) -> ProductFamily:
    assert_open(version)
    cfg = version.configuration
    unit = cfg.unit(form["business_unit_id"])
    fam = ProductFamily(id=_next_id([f.id for u in cfg.business_units for f in u.families], "FAM"),
                        name=_name(form))
    unit.families.append(fam)
    version.invalidate()
    return fam


def add_product(version, form) -> Product:
    assert_open(version)
    cfg = version.configuration
    unit = cfg.unit(form["business_unit_id"])
    if not unit.families:
        raise BudgetError("INVALID_FAMILY",
                          "Definí al menos una familia antes de cargar productos: "
                          "cada producto pertenece a una única familia.")
    is_other = bool(form.get("is_other"))
    family_id = form["family_id"]
    margin_formula = MarginFormula(form.get("margin_formula", "PERCENTAGE_OF_SALES"))
    # El código identifica al producto en toda la empresa: es lo que se escribe
    # en las planillas de carga, donde no hay unidad ni familia que lo aclare.
    codigo = _name(form, "code").upper()
    repetido = next((p for u in cfg.business_units for p in u.products
                     if p.code.strip().upper() == codigo), None)
    if repetido is not None:
        raise BudgetError(
            "DUPLICATE_PRODUCT_CODE",
            f"el código {codigo} ya lo usa el producto {repetido.name}. "
            "Los códigos son únicos en toda la empresa, aunque estén en otra "
            "familia o en otra unidad de negocio.")
    product = Product(
        id=_next_id([p.id for u in cfg.business_units for p in u.products], "P", 3),
        code=codigo,
        name=_name(form),
        family_id=family_id,
        sales_mode=SalesMode(form.get("sales_mode", "UNIT_BASED")),
        margin_formula=margin_formula,
        price=_dec(form.get("price"), "0"),
        currency=form["currency"].strip().upper(),
        margin=Decimal(1) if margin_formula is MarginFormula.NO_COST else _pct(form["margin"]),
        sales_frequency=Frequency(form["sales_frequency"]),
        is_other=is_other,
        commission_rate=(_pct(form["commission_rate"])
                         if form.get("commission_rate", "").strip() else None),
    )
    # El "Otros" es por familia: cada familia necesita el suyo, y sólo uno.
    if is_other and any(p.is_other and p.family_id == family_id for p in unit.products):
        raise BudgetError(
            "DUPLICATE_OTHER_PRODUCT",
            f"la familia {unit.family(family_id).name} ya tiene su producto 'Otros'")
    unit.products.append(product)
    version.invalidate()
    return product


# ==========================================================================
# 4. Gastos
# ==========================================================================
def expense_target_options(cfg: Configuration) -> list[tuple[str, str, str]]:
    """(clave de destino, etiqueta, grupo) para el formulario de gastos."""
    out = [("COMPANY:", "Empresa (corporativo)", "Empresa")]
    out += [(f"BUSINESS_UNIT:{u.id}", u.name, "Unidades de negocio") for u in cfg.business_units]
    out += [(f"BRANCH:{b.id}", b.name, "Sucursales") for b in cfg.branches]
    out += [(f"COST_CENTER:{o.cost_center.id}",
             f"{o.cost_center.name} — {cfg.operation_label(o.id)}",
             "Centros de costo de operaciones") for o in cfg.operations]
    out += [(f"COST_CENTER:{c.id}", f"{c.name} — {s.name}", "Centros de costo de soporte")
            for s in cfg.support_units for c in s.cost_centers]
    return out


def add_expense(version, form) -> ExpenseDefinition:
    """Un gasto puede imputarse a varios destinos a la vez.

    Internet va a todas las sucursales y a algunos centros de costo; alquiler
    va sólo a las sucursales que no son propias. En modo PER_TARGET cada destino
    recibe su propio importe y donde no corresponde se carga 0.
    """
    assert_open(version)
    cfg = version.configuration
    mode = AllocationMode(form.get("allocation_mode", "PER_TARGET"))
    selected = form.getlist("target")
    if not selected:
        raise BudgetError("INVALID_EXPENSE", "Elegí al menos un destino para el gasto.")

    targets: list[ExpenseTarget] = []
    for raw in selected:
        ttype, _, tid = raw.partition(":")
        pct = None
        if mode is AllocationMode.PERCENTAGE:
            pct = _pct(form.get(f"pct_{raw}", "0"))
        targets.append(ExpenseTarget(
            target_type=ExpenseTargetType(ttype),
            target_id=tid or None,
            percentage=pct,
            distribute_to_branches=(ttype in ("BUSINESS_UNIT", "BRANCH")
                                    and bool(form.get(f"split_{raw}"))),
        ))

    ed = ExpenseDefinition(
        id=_next_id([e.id for e in cfg.expenses], "EXP"),
        name=_name(form),
        allocation_mode=mode,
        targets=targets,
        currency=form["currency"].strip().upper(),
        frequency=Frequency(form["frequency"]),
        responsible_role=Role(form.get("responsible_role", Role.ADMIN_AREA.value)),
    )
    cfg.expenses.append(ed)
    version.invalidate()
    return ed


# ==========================================================================
# 5. Nómina
# ==========================================================================
def add_payroll_area(version, form) -> PayrollArea:
    assert_open(version)
    cfg = version.configuration
    area = PayrollArea(id=_next_id([a.id for a in cfg.payroll.areas], "AR"),
                       name=_name(form),
                       base_salary=_dec(form["base_salary"]),
                       currency=form["currency"].strip().upper())
    cfg.payroll.areas.append(area)
    version.invalidate()
    return area


def add_increase_rule(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    if not (form.get("effective_date") or "").strip():
        raise BudgetError("MISSING_DATE", "Indicá desde cuándo rige el aumento.")
    cfg.payroll.increase_rules.append(SalaryIncreaseRule(
        effective_date=date.fromisoformat(form["effective_date"]),
        percentage=_pct(form["percentage"])))
    cfg.payroll.increase_rules.sort(key=lambda r: r.effective_date)
    version.invalidate()


def add_percentage_concept(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.payroll.percentage_concepts.append(PayrollPercentageConcept(
        concept=_name(form, "concept"), percentage=_pct(form["percentage"])))
    version.invalidate()


# ==========================================================================
# 6. Módulos opcionales
# ==========================================================================
def update_modules(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.capex.enabled = bool(form.get("capex_enabled"))
    if cfg.capex.enabled:
        cfg.capex.frequency = Frequency(form.get("capex_frequency", "MONTHLY"))

    cfg.inventory.enabled = bool(form.get("inventory_enabled"))
    if cfg.inventory.enabled:
        cfg.inventory.level = InventoryLevel(form["inventory_level"])
        cfg.inventory.frequency = Frequency(form["inventory_frequency"])
        cfg.inventory.currency = form["inventory_currency"].strip().upper()
        cfg.inventory.purchases_enabled = bool(form.get("purchases_enabled"))

    cfg.balance.enabled = bool(form.get("balance_enabled"))
    if cfg.balance.enabled:
        cfg.balance.currency = form["balance_currency"].strip().upper()
    version.invalidate()


def add_capex_category(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.capex.categories.append(CapexCategory(
        id=_next_id([c.id for c in cfg.capex.categories], "CAT"),
        name=_name(form)))
    version.invalidate()


def add_balance_item(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.balance.items.append(BalanceItem(
        id=_next_id([i.id for i in cfg.balance.items], "BI"),
        name=_name(form),
        section=BalanceSection(form["section"]),
        current=form.get("current") == "1",
        source=BalanceSource(form.get("source", "MANUAL")),
    ))
    version.invalidate()


DEFAULT_BALANCE_ITEMS = [
    ("Caja y bancos", "ASSET", True), ("Créditos por ventas", "ASSET", True),
    ("Bienes de cambio", "ASSET", True), ("Bienes de uso", "ASSET", False),
    ("Deudas comerciales", "LIABILITY", True), ("Deuda financiera", "LIABILITY", False),
    ("Capital", "EQUITY", True), ("Resultados acumulados", "EQUITY", True),
]


def add_default_balance_items(version) -> None:
    assert_open(version)
    cfg = version.configuration
    for name, section, current in DEFAULT_BALANCE_ITEMS:
        if any(i.name == name for i in cfg.balance.items):
            continue
        cfg.balance.items.append(BalanceItem(
            id=_next_id([i.id for i in cfg.balance.items], "BI"),
            name=name, section=BalanceSection(section), current=current))
    version.invalidate()


# ==========================================================================
# 7. Ratios
# ==========================================================================
def update_ratios(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    selected = form.getlist("ratio")
    out: list[RatioSelection] = []
    for code in selected:
        raw = form.get(f"objective_{code}", "").strip()
        objective = None
        if raw:
            otype = ObjectiveType(form.get(f"objective_type_{code}", "MINIMUM"))
            from ..domain.ratios import RATIO_CATALOG, RatioUnit
            unit = RATIO_CATALOG[code].unit
            value = _pct(raw) if unit is RatioUnit.PERCENTAGE else _dec(raw)
            objective = Objective(type=otype, value=value)
        out.append(RatioSelection(ratio_code=code, objective=objective))
    # Asignar una lista no vuelve a pasar por el validador del modelo, así que
    # el orden canónico se aplica también acá.
    from ..domain.ratios import CATALOG
    orden = {r.code: i for i, r in enumerate(CATALOG)}
    cfg.ratios = sorted(out, key=lambda s: orden.get(s.ratio_code, 999))
    version.invalidate()


# ==========================================================================
# 8. Workflow
# ==========================================================================
WORKFLOW_CONCEPTS = [
    ("SALES", "Ventas"), ("EXPENSES", "Gastos"), ("PAYROLL_HEADCOUNT", "Nómina"),
    ("CAPEX", "CAPEX"), ("OPENING_STOCK", "Stock y compras"), ("BALANCE", "Balance"),
]


def update_workflow(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    steps: list[WorkflowStep] = []
    for concept, _label in WORKFLOW_CONCEPTS:
        loader = form.get(f"loader_{concept}")
        if not loader:
            continue
        steps.append(WorkflowStep(
            concept=concept, loader_role=Role(loader),
            reviewer_role=Role(form.get(f"reviewer_{concept}", Role.CFO.value)),
            approver_role=Role(form.get(f"approver_{concept}", Role.CFO.value))))
    cfg.workflow.steps = steps
    version.invalidate()


def default_workflow(version) -> None:
    assert_open(version)
    defaults = {
        "SALES": (Role.UNIT_MANAGER, Role.COO, Role.CFO),
        "EXPENSES": (Role.ADMIN_AREA, Role.CFO, Role.CFO),
        "PAYROLL_HEADCOUNT": (Role.PAYROLL_AREA, Role.CFO, Role.CFO),
        "CAPEX": (Role.ADMIN_AREA, Role.CFO, Role.CFO),
        "OPENING_STOCK": (Role.ADMIN_AREA, Role.CFO, Role.CFO),
        "BALANCE": (Role.FINANCE_AREA, Role.CFO, Role.CFO),
    }
    cfg = version.configuration
    cfg.workflow.steps = [
        WorkflowStep(concept=c, loader_role=l, reviewer_role=r, approver_role=a)
        for c, (l, r, a) in defaults.items()
    ]
    version.invalidate()


# ==========================================================================
# Responsables
# ==========================================================================
def scope_options(cfg: Configuration) -> list[tuple[str, str]]:
    out = [("", "Toda la empresa")]
    out += [(f"BU:{u.id}", f"Unidad — {u.name}") for u in cfg.business_units]
    out += [(f"BR:{b.id}", f"Sucursal — {b.name}") for b in cfg.branches]
    out += [(f"OP:{o.id}", f"Operación — {cfg.operation_label(o.id)}") for o in cfg.operations]
    out += [(f"SU:{u.id}", f"Soporte — {u.name}") for u in cfg.support_units]
    return out


def add_user(service, version, form):
    """Doc 02 §56: cada usuario ve y carga sólo lo que tiene asignado.

    El alcance vacío significa transversal — es lo del CFO. Un gerente de
    sucursal se define con alcance BR:<id> y por eso no puede tocar otra.
    """
    from ..services.budget import User

    name = _name(form)
    roles = {Role(r) for r in form.getlist("role")}
    if not roles:
        raise BudgetError("INVALID_USER", "Elegí al menos un rol.")
    scopes = {s for s in form.getlist("scope") if s}

    base = "u." + "".join(ch for ch in name.lower().split()[0] if ch.isalnum())[:10]
    uid, n = base, 1
    while uid in service.users:
        n += 1
        uid = f"{base}{n}"
    user = User(id=uid, name=name, roles=roles, scopes=scopes)
    service.register_user(user)
    service.audit.record(actor="cfo", action="UserRegistered", entity_type="USER",
                         entity_id=uid, version_id=version.id, after=name)
    return user


def remove_user(service, user_id: str) -> None:
    service.users.pop(user_id, None)


# ==========================================================================
# Borrado
# ==========================================================================
def remove(version, kind: str, entity_id: str, parent_id: Optional[str] = None) -> None:
    assert_open(version)
    cfg = version.configuration
    def _borrar_operaciones(ops: list) -> None:
        """Al irse una unidad o una sucursal se van sus operaciones, y con ellas
        sus centros de costo: los gastos imputados ahí quedan sin destino."""
        muertas = {o.id for o in ops}
        ccs = {o.cost_center.id for o in ops}
        cfg.operations = [o for o in cfg.operations if o.id not in muertas]
        for e in cfg.expenses:
            e.targets = [t for t in e.targets if t.target_id not in ccs]

    if kind == "business_unit":
        _borrar_operaciones(cfg.unit_operations(entity_id))
        cfg.business_units = [u for u in cfg.business_units if u.id != entity_id]
        for e in cfg.expenses:
            e.targets = [t for t in e.targets if t.target_id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.targets]
    elif kind == "branch":
        _borrar_operaciones(cfg.branch_operations(entity_id))
        cfg.branches = [b for b in cfg.branches if b.id != entity_id]
        for e in cfg.expenses:
            e.targets = [t for t in e.targets if t.target_id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.targets]
    elif kind == "operation":
        _borrar_operaciones([cfg.operation(entity_id)])
        cfg.expenses = [e for e in cfg.expenses if e.targets]
    elif kind == "support_unit":
        cfg.support_units = [u for u in cfg.support_units if u.id != entity_id]
    elif kind == "cost_center":
        for u in cfg.support_units:
            u.cost_centers = [c for c in u.cost_centers if c.id != entity_id]
        for e in cfg.expenses:
            e.targets = [t for t in e.targets if t.target_id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.targets]
    elif kind == "product":
        for u in cfg.business_units:
            u.products = [p for p in u.products if p.id != entity_id]
    elif kind == "family":
        for u in cfg.business_units:
            if any(p.family_id == entity_id for p in u.products):
                raise BudgetError("INVALID_FAMILY",
                                  "No se puede borrar una familia que tiene productos.")
            u.families = [f for f in u.families if f.id != entity_id]
    elif kind == "expense":
        cfg.expenses = [e for e in cfg.expenses if e.id != entity_id]
    elif kind == "payroll_area":
        cfg.payroll.areas = [a for a in cfg.payroll.areas if a.id != entity_id]
    elif kind == "increase_rule":
        cfg.payroll.increase_rules.pop(int(entity_id))
    elif kind == "percentage_concept":
        cfg.payroll.percentage_concepts.pop(int(entity_id))
    elif kind == "capex_category":
        cfg.capex.categories = [c for c in cfg.capex.categories if c.id != entity_id]
    elif kind == "balance_item":
        cfg.balance.items = [i for i in cfg.balance.items if i.id != entity_id]
    else:
        raise BudgetError("NOT_FOUND", f"no sé borrar {kind}")
    version.invalidate()


# ==========================================================================
# Estado del wizard
# ==========================================================================
def step_state(version) -> dict[str, dict]:
    """Qué está listo y qué falta, por paso. Alimenta el índice del wizard."""
    cfg = version.configuration
    fx_ok = all(r["loaded"] for r in fx_summary(version))
    sin_operacion = cfg.unassigned_branches()
    unidades_sueltas = cfg.units_without_operations()
    products_ok = bool(cfg.business_units) and all(
        u.products and u.families and not u.missing_other_products()
        for u in cfg.business_units)
    modules_ok = (not cfg.inventory.enabled or any(u.families for u in cfg.business_units)) and \
                 (not cfg.balance.enabled or bool(cfg.balance.items))
    return {
        "general": {"ready": fx_ok,
                    "detail": (f"{cfg.company_name} · {cfg.fiscal_year_start:%d/%m/%Y}–"
                               f"{cfg.fiscal_year_end:%d/%m/%Y} · "
                               f"{', '.join(cfg.enabled_currencies)}"
                               + ("" if fx_ok else " · faltan tipos de cambio"))},
        "estructura": {"ready": bool(cfg.operations) and not sin_operacion
                                and not unidades_sueltas,
                       "detail": f"{len(cfg.business_units)} unidades · "
                                 f"{len(cfg.branches)} sucursales · "
                                 f"{len(cfg.operations)} operaciones"
                                 + (f" · {len(sin_operacion)} sucursales sin unidad"
                                    if sin_operacion else "")
                                 + (f" · {len(unidades_sueltas)} unidades sin sucursal"
                                    if unidades_sueltas else "")
                                 + f" · {len(cfg.support_units)} áreas de soporte"},
        "productos": {"ready": products_ok,
                      "detail": f"{sum(len(u.products) for u in cfg.business_units)} productos · "
                                f"{sum(len(u.families) for u in cfg.business_units)} familias"},
        "gastos": {"ready": bool(cfg.expenses), "detail": f"{len(cfg.expenses)} conceptos"},
        "nomina": {"ready": bool(cfg.payroll.areas),
                   "detail": f"{len(cfg.payroll.areas)} áreas · "
                             f"{len(cfg.payroll.increase_rules)} aumentos"},
        "modulos": {"ready": modules_ok,
                    "detail": " · ".join(filter(None, [
                        "CAPEX" if cfg.capex.enabled else "",
                        ("Stock por " + {"COMPANY": "empresa",
                                          "BUSINESS_UNIT": "unidad de negocio",
                                          "OPERATION": "operación"}[cfg.inventory.level.value])
                        if cfg.inventory.enabled else "",
                        "Balance" if cfg.balance.enabled else ""])) or "ninguno habilitado"},
        "ratios": {"ready": bool(cfg.ratios),
                   "detail": f"{len(cfg.ratios)} seleccionados · "
                             f"{sum(1 for r in cfg.ratios if r.objective)} con objetivo"},
        "workflow": {"ready": bool(cfg.workflow.steps),
                     "detail": f"{len(cfg.workflow.steps)} circuitos"},
        "cierre": {"ready": cfg.status is ConfigStatus.LOCKED,
                   "detail": cfg.status.value},
    }
