"""Generación de formularios de carga a partir de la configuración.

Doc 02 §57: cada usuario debería ver qué tiene que hacer, dónde, con qué datos,
qué errores tiene y qué sigue. Y nada más: el sistema no pide información que
la configuración no haya requerido.

Los formularios de esta pantalla no están escritos a mano en ninguna plantilla:
se derivan de la configuración de la versión, igual que las planillas de carga
masiva. Si el CFO no configuró Stock, la pantalla de Stock no existe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..domain.config import Configuration, SalesMode
from ..domain.inputs import ChangeType, Concept, InputValue
from ..domain.periods import Frequency, Period


@dataclass
class Field_:
    name: str                      # identificador del campo en el HTML
    label: str
    value: str = ""
    currency: str = ""
    hint: str = ""
    kind: str = "number"


@dataclass
class Row:
    label: str
    detail: str
    fields: list[Field_] = field(default_factory=list)


@dataclass
class FormSpec:
    title: str
    intro: str
    columns: list[str]
    rows: list[Row]
    capability: str
    extra: dict = field(default_factory=dict)


def _existing(version, **criteria) -> dict[str, Decimal]:
    out = {}
    for iv in version.inputs.values:
        ok = all(getattr(iv, k, None) == v for k, v in criteria.items() if k != "concept")
        if ok and iv.concept is criteria["concept"]:
            key = "|".join(str(getattr(iv, f) or "") for f in
                           ("branch_id", "product_id", "expense_id", "family_id",
                            "area_id", "balance_item_id", "period"))
            out[key] = iv.value
    return out


def _fmt(v: Optional[Decimal]) -> str:
    if v is None:
        return ""
    v = Decimal(v)
    return str(int(v)) if v == v.to_integral_value() else str(v)


# ==========================================================================
# Especificaciones por concepto
# ==========================================================================
def build_form(version, task) -> FormSpec:
    cfg = version.configuration
    if task.concept == "SALES":
        return _sales_form(cfg, version, task)
    if task.concept == "EXPENSES":
        return _expenses_form(cfg, version)
    if task.concept == "PAYROLL_HEADCOUNT":
        return _payroll_form(cfg, version, task)
    if task.concept == "CAPEX":
        return _capex_form(cfg, version)
    if task.concept == "OPENING_STOCK":
        return _stock_form(cfg, version)
    if task.concept == "BALANCE":
        return _balance_form(cfg, version)
    return FormSpec(task.label, "Concepto sin formulario definido.", [], [], "budget.read")


def _sales_form(cfg: Configuration, version, task) -> FormSpec:
    branch_id = task.scope_key.split(":", 1)[1]
    unit = cfg.branch_owner(branch_id)
    branch = unit.branch(branch_id)
    fy = cfg.fiscal_year
    unit_based = unit.sales_mode is SalesMode.UNIT_BASED
    concept = Concept.SALES_QTY if unit_based else Concept.SALES_AMOUNT
    current = _existing(version, concept=concept, branch_id=branch_id)

    rows: list[Row] = []
    for product in unit.products:
        for head, bucket in fy.iter_buckets(product.sales_frequency):
            if not any(cfg.is_active(branch, p) for p in bucket):
                continue
            key = f"{branch_id}|{product.id}||||" + f"|{head.code}"
            detail = (f"precio {product.price} {product.price_currency} · "
                      f"margen {product.margin * 100:.0f}%" if unit_based
                      else f"margen {product.margin * 100:.0f}% · {unit.sales_currency}")
            rows.append(Row(
                label=f"{product.code} — {product.name}",
                detail=f"{head.code} · {product.sales_frequency.value.lower()} · {detail}",
                fields=[Field_(name=f"S:{product.id}:{head.code}",
                               label="Cantidad" if unit_based else "Monto",
                               value=_fmt(current.get(key)),
                               currency="unidades" if unit_based else unit.sales_currency)]))
    intro = ("Cargá la cantidad por producto y período. El precio y el margen los define la "
             "configuración: el sistema calcula ventas, costo y margen."
             if unit_based else
             f"Cargá el monto de ventas en {unit.sales_currency}. El sistema calcula el costo "
             "con el margen configurado.")
    return FormSpec(f"Ventas — {unit.name} / {branch.name}",
                    intro + " Si un producto no se vende, cargá 0 (vacío es error).",
                    ["Producto", "Período"], rows, "budget.sales.load")


def _expenses_form(cfg: Configuration, version) -> FormSpec:
    fy = cfg.fiscal_year
    current = _existing(version, concept=Concept.EXPENSE_AMOUNT)
    rows: list[Row] = []
    for ed in cfg.expenses:
        target = {"COMPANY": "Empresa", "DISTRIBUTED": "Distribuido por porcentaje"}.get(
            ed.level.value, f"{ed.level.value} {ed.target_id}")
        for head, _ in fy.iter_buckets(ed.frequency):
            key = f"||{ed.id}||||{head.code}"
            rows.append(Row(
                label=ed.name,
                detail=f"{head.code} · {ed.frequency.value.lower()} · {target}"
                       + (" · se distribuye a sucursales" if ed.distribute_to_branches else ""),
                fields=[Field_(name=f"E:{ed.id}:{head.code}", label="Importe",
                               value=_fmt(current.get(key)), currency=ed.currency)]))
    return FormSpec("Gastos", "El nivel de imputación y la moneda los define el CFO en la "
                    "configuración; acá sólo se carga el importe total.",
                    ["Gasto", "Período"], rows, "budget.expense.load")


def _payroll_form(cfg: Configuration, version, task) -> FormSpec:
    scope = task.scope_key
    current = _existing(version, concept=Concept.INITIAL_HEADCOUNT)
    rows: list[Row] = []
    for area in cfg.payroll.areas:
        key = f"{scope.split(':')[-1] if scope.startswith('BR') else ''}||||{area.id}||"
        rows.append(Row(
            label=area.name,
            detail=f"sueldo base {area.base_salary} {area.currency} · "
                   f"cargas {((cfg.payroll.charges_factor - 1) * 100):.0f}%",
            fields=[Field_(name=f"H:{area.id}", label="Dotación inicial",
                           value=_fmt(current.get(key)), currency="personas")]))
    movements = [iv for iv in version.inputs.values
                 if iv.concept is Concept.HEADCOUNT_CHANGE and iv.scope_key == scope]
    return FormSpec(
        task.label,
        "Las unidades informan cantidad, área y fecha estimada. El valor salarial lo define "
        "Nómina en la configuración; el sistema calcula el costo aplicando los aumentos que "
        "correspondan según la fecha de ingreso de cada persona.",
        ["Área"], rows, "budget.payroll.load",
        extra={"movements": movements, "areas": cfg.payroll.areas,
               "fy_start": cfg.fiscal_year_start, "fy_end": cfg.fiscal_year_end})


def _capex_form(cfg: Configuration, version) -> FormSpec:
    items = [iv for iv in version.inputs.values if iv.concept is Concept.CAPEX_AMOUNT]
    scopes = ([("CO", "Empresa")]
              + [(f"BU:{u.id}", u.name) for u in cfg.business_units]
              + [(f"BR:{b.id}", f"{u.name} / {b.name}") for u in cfg.business_units
                 for b in u.branches]
              + [(f"SU:{u.id}", u.name) for u in cfg.support_units])
    return FormSpec("CAPEX", "Sin depreciación en V1: se carga la inversión y el período.",
                    [], [], "budget.expense.load",
                    extra={"capex_items": items, "categories": cfg.capex.categories,
                           "scopes": scopes, "periods": [p.code for p in cfg.periods],
                           "currencies": cfg.enabled_currencies})


def _stock_form(cfg: Configuration, version) -> FormSpec:
    from ..domain.validation import _families_for_scope
    fy = cfg.fiscal_year
    level = cfg.inventory.level.value
    scopes = {"COMPANY": [("CO", "Empresa")],
              "BUSINESS_UNIT": [(f"BU:{u.id}", u.name) for u in cfg.business_units],
              "BRANCH": [(f"BR:{b.id}", f"{u.name} / {b.name}")
                         for u in cfg.business_units for b in u.branches]}[level]
    open_cur = _existing(version, concept=Concept.OPENING_STOCK)
    pur_cur = _existing(version, concept=Concept.PURCHASES)
    fam_names = {f.id: f.name for u in cfg.business_units for f in u.families}

    rows: list[Row] = []
    for scope_key, scope_label in scopes:
        for fam in _families_for_scope(cfg, scope_key):
            br = scope_key.split(":")[1] if scope_key.startswith("BR") else ""
            okey = f"{br}|||{fam}|||"
            fields = [Field_(name=f"O:{scope_key}:{fam}", label="Stock inicial",
                             value=_fmt(open_cur.get(okey)), currency=cfg.inventory.currency)]
            if cfg.inventory.purchases_enabled:
                for head, _ in fy.iter_buckets(cfg.inventory.frequency):
                    pkey = f"{br}|||{fam}|||{head.code}"
                    fields.append(Field_(name=f"P:{scope_key}:{fam}:{head.code}",
                                         label=f"Compras {head.code}",
                                         value=_fmt(pur_cur.get(pkey)),
                                         currency=cfg.inventory.currency))
            rows.append(Row(label=f"{fam_names.get(fam, fam)}", detail=scope_label, fields=fields))
    return FormSpec("Stock y compras",
                    f"El stock se administra por familia a nivel {level.lower()}, en "
                    f"{cfg.inventory.currency}. El stock final no se carga: lo calcula el "
                    "sistema como stock anterior + compras − costo de venta.",
                    ["Familia", "Ámbito"], rows, "budget.expense.load")


def _balance_form(cfg: Configuration, version) -> FormSpec:
    op = _existing(version, concept=Concept.BALANCE_OPENING)
    pr = _existing(version, concept=Concept.BALANCE_PROJECTED)
    section_label = {"ASSET": "Activo", "LIABILITY": "Pasivo", "EQUITY": "Patrimonio"}
    rows: list[Row] = []
    for item in cfg.balance.items:
        key = f"|||||{item.id}|"
        detail = (f"{section_label[item.section.value]} "
                  f"{'corriente' if item.current else 'no corriente'}")
        if item.source.value == "CALCULATED":
            rows.append(Row(item.name, detail + " · calculado, no se carga", []))
            continue
        rows.append(Row(item.name, detail, [
            Field_(name=f"BO:{item.id}", label="Inicial", value=_fmt(op.get(key)),
                   currency=cfg.balance.currency),
            Field_(name=f"BP:{item.id}", label="Proyectado", value=_fmt(pr.get(key)),
                   currency=cfg.balance.currency),
        ]))
    return FormSpec("Balance",
                    f"El balance inicial corresponde al {cfg.fiscal_year.opening_balance_date}. "
                    "El patrimonio total es calculado (activo − pasivo): si los rubros de "
                    "patrimonio cargados no coinciden con ese total, la carga se rechaza entera.",
                    ["Rubro", "Sección"], rows, "budget.balance.load")


# ==========================================================================
# Aplicación del formulario
# ==========================================================================
def _dec(raw: str) -> Optional[Decimal]:
    raw = (raw or "").strip().replace(",", ".")
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"'{raw}' no es un número")


def apply_form(service, actor: str, version, task, formdata) -> int:
    """Convierte el formulario en inputs y los manda por el servicio.

    Devuelve la cantidad de valores cargados. Cualquier error de validación
    sale como BudgetError y no deja nada a medias en el pedido.
    """
    cfg = version.configuration
    spec = build_form(version, task)
    pending: list[InputValue] = []

    for key, raw in formdata.items():
        parts = key.split(":")
        value = _dec(raw)
        if value is None:
            continue

        if parts[0] == "S":
            branch_id = task.scope_key.split(":", 1)[1]
            unit = cfg.branch_owner(branch_id)
            product_id, period = parts[1], parts[2]
            unit_based = unit.sales_mode is SalesMode.UNIT_BASED
            pending.append(InputValue(
                concept=Concept.SALES_QTY if unit_based else Concept.SALES_AMOUNT,
                period=period, value=value,
                currency=None if unit_based else unit.sales_currency,
                business_unit_id=unit.id, branch_id=branch_id, product_id=product_id))
        elif parts[0] == "E":
            ed = cfg.expense(parts[1])
            pending.append(InputValue(concept=Concept.EXPENSE_AMOUNT, period=parts[2],
                                      value=value, currency=ed.currency, expense_id=ed.id))
        elif parts[0] == "H":
            scope = task.scope_key
            iv = InputValue(concept=Concept.INITIAL_HEADCOUNT, value=value, area_id=parts[1])
            _set_scope(iv, scope, cfg)
            pending.append(iv)
        elif parts[0] == "O":
            iv = InputValue(concept=Concept.OPENING_STOCK, value=value,
                            currency=cfg.inventory.currency, family_id=parts[2])
            _set_scope(iv, parts[1], cfg)
            pending.append(iv)
        elif parts[0] == "P":
            iv = InputValue(concept=Concept.PURCHASES, value=value, period=parts[3],
                            currency=cfg.inventory.currency, family_id=parts[2])
            _set_scope(iv, parts[1], cfg)
            pending.append(iv)
        elif parts[0] in ("BO", "BP"):
            pending.append(InputValue(
                concept=Concept.BALANCE_OPENING if parts[0] == "BO" else Concept.BALANCE_PROJECTED,
                value=value, currency=cfg.balance.currency, balance_item_id=parts[1]))

    for iv in pending:
        service.submit_input(actor, version, iv, spec.capability)
    return len(pending)


def _set_scope(iv: InputValue, scope_key: str, cfg: Configuration) -> None:
    if scope_key == "CO":
        return
    kind, _id = scope_key.split(":", 1)
    if kind == "BR":
        iv.branch_id = _id
        iv.business_unit_id = cfg.branch_owner(_id).id
    elif kind == "BU":
        iv.business_unit_id = _id
    elif kind == "SU":
        iv.support_unit_id = _id
    elif kind == "CC":
        iv.cost_center_id = _id


def add_headcount_change(service, actor: str, version, task, form) -> None:
    from datetime import date as _date
    iv = InputValue(
        concept=Concept.HEADCOUNT_CHANGE,
        value=Decimal(form["quantity"]),
        change_type=ChangeType(form["change_type"]),
        effective_date=_date.fromisoformat(form["effective_date"]),
        area_id=form["area_id"])
    _set_scope(iv, task.scope_key, version.configuration)
    service.submit_input(actor, version, iv, "budget.payroll.load")


def add_capex(service, actor: str, version, form) -> None:
    iv = InputValue(concept=Concept.CAPEX_AMOUNT, period=form["period"],
                    value=Decimal(form["amount"]), currency=form["currency"],
                    capex_category_id=form["category_id"])
    _set_scope(iv, form["scope"], version.configuration)
    service.submit_input(actor, version, iv, "budget.expense.load")
