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
    Allocation, BalanceItem, BalanceSection, BalanceSource, Branch, BusinessUnit,
    CapexCategory, ConfigStatus, Configuration, CostCenter, ExpenseDefinition, ExpenseLevel,
    InventoryLevel, MarginFormula, Objective, ObjectiveType, PayrollArea,
    PayrollPercentageConcept, Product, ProductFamily, RatioSelection, Role,
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
         "Unidades de negocio, sucursales, unidades de soporte y centros de costo. "
         "Cada una puede tener fecha de inicio y de cierre dentro del ejercicio."),
    Step("productos", "Productos y familias", Role.COO,
         "El COO define el catálogo de cada unidad y la modalidad de venta. "
         "Todas las sucursales de la unidad usan el mismo catálogo."),
    Step("gastos", "Gastos", Role.CFO,
         "Qué gastos existen, dónde se imputan, con qué frecuencia y en qué moneda. "
         "El que carga no decide el nivel."),
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
        name=form["name"].strip(),
        sales_mode=SalesMode(form["sales_mode"]),
        margin_formula=MarginFormula(form["margin_formula"]),
        sales_currency=form["sales_currency"].strip().upper(),
        commission_rate=(_pct(form["commission_rate"])
                         if form.get("commission_rate", "").strip() else None),
        effective_from=_opt_date(form.get("effective_from")),
        effective_to=_opt_date(form.get("effective_to")),
    )
    cfg.business_units.append(unit)
    version.invalidate()
    return unit


def add_branch(version, form) -> Branch:
    assert_open(version)
    cfg = version.configuration
    unit = cfg.unit(form["business_unit_id"])
    branch = Branch(
        id=_next_id([b.id for _, b in cfg.all_branches()], "BR"),
        name=form["name"].strip(),
        effective_from=_opt_date(form.get("effective_from")),
        effective_to=_opt_date(form.get("effective_to")),
    )
    unit.branches.append(branch)
    version.invalidate()
    return branch


def add_support_unit(version, form) -> SupportUnit:
    assert_open(version)
    cfg = version.configuration
    su = SupportUnit(id=_next_id([u.id for u in cfg.support_units], "SU"),
                     name=form["name"].strip(),
                     effective_from=_opt_date(form.get("effective_from")),
                     effective_to=_opt_date(form.get("effective_to")))
    cfg.support_units.append(su)
    version.invalidate()
    return su


def add_cost_center(version, form) -> CostCenter:
    assert_open(version)
    cfg = version.configuration
    su = cfg.support_unit(form["support_unit_id"])
    cc = CostCenter(id=_next_id([c.id for u in cfg.support_units for c in u.cost_centers], "CC"),
                    name=form["name"].strip())
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
                        name=form["name"].strip())
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
    product = Product(
        id=_next_id([p.id for u in cfg.business_units for p in u.products], "P", 3),
        code=form["code"].strip().upper(),
        name=form["name"].strip(),
        family_id=form["family_id"],
        price=_dec(form.get("price"), "0"),
        price_currency=form.get("price_currency", unit.sales_currency).strip().upper(),
        margin=_pct(form["margin"]),
        sales_frequency=Frequency(form["sales_frequency"]),
        is_other=is_other,
    )
    if is_other and any(p.is_other for p in unit.products):
        raise BudgetError("INVALID_PRODUCT",
                          "La unidad ya tiene su producto 'Otros'. Sólo puede haber uno.")
    unit.products.append(product)
    version.invalidate()
    return product


# ==========================================================================
# 4. Gastos
# ==========================================================================
def add_expense(version, form) -> ExpenseDefinition:
    assert_open(version)
    cfg = version.configuration
    level = ExpenseLevel(form["level"])
    allocations: list[Allocation] = []
    target_id = None

    if level is ExpenseLevel.DISTRIBUTED:
        for unit in cfg.business_units:
            raw = form.get(f"alloc_{unit.id}", "").strip()
            if raw:
                allocations.append(Allocation(target_type="BUSINESS_UNIT", target_id=unit.id,
                                              percentage=_pct(raw)))
        if not allocations:
            raise BudgetError("INVALID_ALLOCATION",
                              "Indicá los porcentajes de distribución (deben sumar 100%).")
    elif level is not ExpenseLevel.COMPANY:
        target_id = form.get("target_id") or None
        if not target_id:
            raise BudgetError("INVALID_EXPENSE", "Elegí a qué ámbito se imputa el gasto.")

    ed = ExpenseDefinition(
        id=_next_id([e.id for e in cfg.expenses], "EXP"),
        name=form["name"].strip(),
        level=level,
        target_id=target_id,
        allocations=allocations,
        currency=form["currency"].strip().upper(),
        frequency=Frequency(form["frequency"]),
        responsible_role=Role(form.get("responsible_role", Role.ADMIN_AREA.value)),
        distribute_to_branches=bool(form.get("distribute_to_branches")),
        corporate=level in (ExpenseLevel.COMPANY, ExpenseLevel.COST_CENTER),
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
                       name=form["name"].strip(),
                       base_salary=_dec(form["base_salary"]),
                       currency=form["currency"].strip().upper())
    cfg.payroll.areas.append(area)
    version.invalidate()
    return area


def add_increase_rule(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.payroll.increase_rules.append(SalaryIncreaseRule(
        effective_date=date.fromisoformat(form["effective_date"]),
        percentage=_pct(form["percentage"])))
    cfg.payroll.increase_rules.sort(key=lambda r: r.effective_date)
    version.invalidate()


def add_percentage_concept(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.payroll.percentage_concepts.append(PayrollPercentageConcept(
        concept=form["concept"].strip(), percentage=_pct(form["percentage"])))
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
        name=form["name"].strip()))
    version.invalidate()


def add_balance_item(version, form) -> None:
    assert_open(version)
    cfg = version.configuration
    cfg.balance.items.append(BalanceItem(
        id=_next_id([i.id for i in cfg.balance.items], "BI"),
        name=form["name"].strip(),
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
    cfg.ratios = out
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
    out += [(f"BR:{b.id}", f"Sucursal — {u.name} / {b.name}")
            for u in cfg.business_units for b in u.branches]
    out += [(f"SU:{u.id}", f"Soporte — {u.name}") for u in cfg.support_units]
    return out


def add_user(service, version, form):
    """Doc 02 §56: cada usuario ve y carga sólo lo que tiene asignado.

    El alcance vacío significa transversal — es lo del CFO. Un gerente de
    sucursal se define con alcance BR:<id> y por eso no puede tocar otra.
    """
    from ..services.budget import User

    name = form["name"].strip()
    if not name:
        raise BudgetError("INVALID_USER", "El nombre es obligatorio.")
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
    if kind == "business_unit":
        cfg.business_units = [u for u in cfg.business_units if u.id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.target_id != entity_id]
    elif kind == "branch":
        for u in cfg.business_units:
            u.branches = [b for b in u.branches if b.id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.target_id != entity_id]
    elif kind == "support_unit":
        cfg.support_units = [u for u in cfg.support_units if u.id != entity_id]
    elif kind == "cost_center":
        for u in cfg.support_units:
            u.cost_centers = [c for c in u.cost_centers if c.id != entity_id]
        cfg.expenses = [e for e in cfg.expenses if e.target_id != entity_id]
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
    branches = cfg.all_branches()
    products_ok = bool(cfg.business_units) and all(
        u.products and u.families and any(p.is_other for p in u.products)
        for u in cfg.business_units)
    modules_ok = (not cfg.inventory.enabled or any(u.families for u in cfg.business_units)) and \
                 (not cfg.balance.enabled or bool(cfg.balance.items))
    return {
        "general": {"ready": fx_ok,
                    "detail": (f"{cfg.company_name} · {cfg.fiscal_year_start:%d/%m/%Y}–"
                               f"{cfg.fiscal_year_end:%d/%m/%Y} · "
                               f"{', '.join(cfg.enabled_currencies)}"
                               + ("" if fx_ok else " · faltan tipos de cambio"))},
        "estructura": {"ready": bool(cfg.business_units) and bool(branches),
                       "detail": f"{len(cfg.business_units)} unidades · {len(branches)} "
                                 f"sucursales · {len(cfg.support_units)} de soporte"},
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
                        f"Stock por {cfg.inventory.level.value.lower()}" if cfg.inventory.enabled else "",
                        "Balance" if cfg.balance.enabled else ""])) or "ninguno habilitado"},
        "ratios": {"ready": bool(cfg.ratios),
                   "detail": f"{len(cfg.ratios)} seleccionados · "
                             f"{sum(1 for r in cfg.ratios if r.objective)} con objetivo"},
        "workflow": {"ready": bool(cfg.workflow.steps),
                     "detail": f"{len(cfg.workflow.steps)} circuitos"},
        "cierre": {"ready": cfg.status is ConfigStatus.LOCKED,
                   "detail": cfg.status.value},
    }
