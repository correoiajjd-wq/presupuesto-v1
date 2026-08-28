"""Generación de formularios de carga a partir de la configuración.

Doc 02 §57: cada usuario debería ver qué tiene que hacer, dónde, con qué datos,
qué errores tiene y qué sigue. Y nada más: el sistema no pide información que
la configuración no haya requerido.

Los formularios de esta pantalla no están escritos a mano en ninguna plantilla:
se derivan de la configuración de la versión, igual que las planillas de carga
masiva. Si el CFO no configuró Stock, la pantalla de Stock no existe.

Los nombres de campo usan "~" como separador porque los ámbitos ya contienen
":" (OP:OP-01).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..domain.config import AllocationMode, Configuration, SalesMode
from ..domain.inputs import ChangeType, Concept, InputValue

SEP = "~"


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


def movement_totals(version) -> dict[str, Decimal]:
    """El importe total de cada solicitud: nominal de una persona por cuántas
    quedaron autorizadas."""
    porque = {iv.movement_id: iv for iv in version.inputs.values
              if iv.concept is Concept.HEADCOUNT_CHANGE}
    out: dict[str, Decimal] = {}
    for iv in version.inputs.values:
        if iv.concept is Concept.NOMINAL_SALARY and iv.movement_id in porque:
            out[iv.movement_id] = iv.value * movement_quantity(porque[iv.movement_id])
    return out


def movement_quantity(movimiento: InputValue) -> Decimal:
    """Por cuántas personas se valoriza una solicitud.

    Un ajuste no mueve gente: su importe es el total, así que cuenta como uno.
    """
    if movimiento.change_type is ChangeType.ADJUSTMENT:
        return Decimal(1)
    return Decimal(movimiento.value)


def _index(version, concept: Concept) -> dict[str, Decimal]:
    """Los valores ya cargados, indexados por una clave estable."""
    out: dict[str, Decimal] = {}
    for iv in version.inputs.values:
        if iv.concept is not concept:
            continue
        key = SEP.join([iv.scope_key, iv.product_id or "", iv.expense_id or "",
                        iv.family_id or "", iv.balance_item_id or "",
                        iv.period or ""])
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
        return _expenses_form(cfg, version, task)
    if task.concept == "PAYROLL_HEADCOUNT":
        return _payroll_form(cfg, version, task)
    if task.concept == "PAYROLL_SALARY":
        return _salary_form(cfg, version)
    if task.concept == "CAPEX":
        return _capex_form(cfg, version)
    if task.concept == "OPENING_STOCK":
        return _stock_form(cfg, version)
    if task.concept == "BALANCE":
        return _balance_form(cfg, version)
    return FormSpec(task.label, "Concepto sin formulario definido.", [], [], "budget.read")


def _sales_form(cfg: Configuration, version, task) -> FormSpec:
    operation_id = task.scope_key.split(":", 1)[1]
    op = cfg.operation(operation_id)
    unit = cfg.unit(op.business_unit_id)
    branch = cfg.branch(op.branch_id)
    fy = cfg.fiscal_year
    qty = _index(version, Concept.SALES_QTY)
    amt = _index(version, Concept.SALES_AMOUNT)

    rows: list[Row] = []
    for product in unit.products:
        unit_based = product.sales_mode is SalesMode.UNIT_BASED
        current = qty if unit_based else amt
        for head, bucket in fy.iter_buckets(product.sales_frequency):
            if not any(cfg.is_active(op, p) and cfg.is_active(unit, p)
                       and cfg.is_active(branch, p) for p in bucket):
                continue
            key = SEP.join([task.scope_key, product.id, "", "", "", head.code])
            detail = (f"precio {product.price} {product.currency}" if unit_based
                      else f"monto en {product.currency}")
            margen = ("sin costo" if product.margin_formula.value == "NO_COST"
                      else f"margen {product.margin * 100:.0f}%")
            rows.append(Row(
                label=f"{product.code} — {product.name}"
                      + ("  (Otros)" if product.is_other else ""),
                detail=f"{head.code} · {product.sales_frequency.value.lower()} · {detail} · {margen}",
                fields=[Field_(name=SEP.join(["S", product.id, head.code]),
                               label="Cantidad" if unit_based else "Monto",
                               value=_fmt(current.get(key)),
                               currency="unidades" if unit_based else product.currency)]))
    # Doc 02 §48: quien autoriza los objetivos de venta tiene que ver, en la
    # misma pantalla, la gente que se pidió para esa operación. Es parte de lo
    # que está aprobando; si no la quiere, rechaza y vuelve al que la pidió.
    solicitudes = [iv for iv in version.inputs.values
                   if iv.concept is Concept.HEADCOUNT_CHANGE
                   and iv.cost_center_id == op.cost_center.id]
    solicitudes.sort(key=lambda iv: (iv.effective_date or cfg.fiscal_year_start))
    return FormSpec(
        f"Ventas — {unit.name} / {branch.name}",
        f"Se carga la operación de {unit.name} en {branch.name}; los gastos propios de esta "
        f"combinación van al centro de costo {op.cost_center.name}. "
        "Cada producto se carga como lo define la configuración: por cantidad o por monto. "
        "El precio y el margen no se tocan acá; el sistema calcula ventas, costo y margen. "
        "Si un producto no se vende, cargá 0 (vacío es error).",
        ["Producto", "Período"], rows, "budget.sales.load",
        extra={"solicitudes": solicitudes, "valores": movement_totals(version),
               "currency": cfg.payroll.currency,
               "centro": op.cost_center.name})


def _expenses_form(cfg: Configuration, version, task) -> FormSpec:
    """Cada quien carga los gastos del ámbito que tiene asignado.

    Un centro de costo con responsable propio es su propia tarea: el que carga
    ve sus conceptos y nada más.
    """
    fy = cfg.fiscal_year
    current = _index(version, Concept.EXPENSE_AMOUNT)
    rows: list[Row] = []
    for ed in cfg.expenses:
        per_target = ed.allocation_mode is AllocationMode.PER_TARGET
        todos = [t.scope_key for t in ed.targets] if per_target else ["CO"]
        scopes = [sc for sc in todos if sc == task.scope_key]
        for head, _ in fy.iter_buckets(ed.frequency):
            for scope in scopes:
                key = SEP.join([scope, "", ed.id, "", "", head.code])
                destino = (cfg.scope_label(scope) if per_target
                           else "total, se reparte por porcentajes")
                rows.append(Row(
                    label=f"{ed.name} — {destino}",
                    detail=f"{head.code} · {ed.frequency.value.lower()} · {ed.currency}",
                    fields=[Field_(name=SEP.join(["E", ed.id, scope, head.code]),
                                   label="Importe", value=_fmt(current.get(key)),
                                   currency=ed.currency)]))
    return FormSpec(
        f"Gastos — {cfg.scope_label(task.scope_key)}",
        "El nivel de imputación, la moneda y la frecuencia los define el CFO; acá sólo se "
        "carga el importe. Un mismo gasto puede existir en varios lugares con montos "
        "distintos: donde no corresponde, se carga 0.",
        ["Gasto y destino", "Período"], rows, "budget.expense.load")


def _payroll_form(cfg: Configuration, version, task) -> FormSpec:
    """Las solicitudes de dotación de un centro de costo: altas, bajas y ajustes.

    Acá no se carga plata. Cada solicitud le llega a Nómina, que le pone el
    nominal; mientras no lo haga, la versión no se puede aprobar.
    """
    cc_id = task.scope_key.split(":", 1)[1]
    solicitudes = [iv for iv in version.inputs.values
                   if iv.concept is Concept.HEADCOUNT_CHANGE and iv.cost_center_id == cc_id]
    valores = movement_totals(version)
    solicitudes.sort(key=lambda iv: (iv.effective_date or cfg.fiscal_year_start))
    return FormSpec(
        task.label,
        "Pedí las altas, las bajas y los ajustes de este centro de costo, con su fecha y el "
        "puesto. El importe lo pone Nómina: cada solicitud le llega como pendiente de "
        "valorizar, y hasta que no le ponga el número la versión no cierra. Si en la revisión "
        "se autoriza una cantidad distinta, el importe se ajusta solo en esa proporción.",
        [], [], "budget.headcount.load",
        extra={"solicitudes": solicitudes, "valores": valores,
               "tipos": [("HIRED", "Alta"), ("TERMINATED", "Baja"),
                         ("ADJUSTMENT", "Ajuste (ascenso, cambio de jornada)")],
               "currency": cfg.payroll.currency,
               "fy_start": cfg.fiscal_year_start, "fy_end": cfg.fiscal_year_end})


def _salary_form(cfg: Configuration, version) -> FormSpec:
    """Doc 01 §16: Nómina pone los valores.

    Dos cosas: la foto inicial de cada centro de costo —cuánta gente hay y
    cuánto suma por mes— y el nominal de cada solicitud que pidieron las áreas.
    Todo a valores de hoy: los aumentos del ejercicio los aplica el sistema.
    """
    personas = _index(version, Concept.INITIAL_HEADCOUNT)
    inicial = {iv.cost_center_id: iv.value for iv in version.inputs.values
               if iv.concept is Concept.NOMINAL_SALARY and not iv.movement_id}
    por_solicitud = {iv.movement_id: iv.value for iv in version.inputs.values
                     if iv.concept is Concept.NOMINAL_SALARY and iv.movement_id}

    rows: list[Row] = []
    for cc, kind, label in cfg.cost_centers():
        key = SEP.join([f"CC:{cc.id}", "", "", "", "", ""])
        rows.append(Row(
            label=cc.name,
            detail=("operación" if kind == "OPERATION" else "área de soporte") + f" · {label}",
            fields=[
                Field_(name=SEP.join(["IH", cc.id]), label="Personas al inicio",
                       value=_fmt(personas.get(key)), currency="personas"),
                Field_(name=SEP.join(["IN", cc.id]), label="Nominal mensual",
                       value=_fmt(inicial.get(cc.id)), currency=cfg.payroll.currency),
            ]))

    etiqueta = {"HIRED": "Alta", "TERMINATED": "Baja", "ADJUSTMENT": "Ajuste"}
    solicitudes = [iv for iv in version.inputs.values
                   if iv.concept is Concept.HEADCOUNT_CHANGE]
    solicitudes.sort(key=lambda iv: (iv.effective_date or cfg.fiscal_year_start))
    for iv in solicitudes:
        cantidad = movement_quantity(iv)
        gente = ("" if iv.change_type is ChangeType.ADJUSTMENT
                 else f" · {_fmt(iv.value)} personas")
        unitario = por_solicitud.get(iv.movement_id)
        rows.append(Row(
            label=f"{etiqueta[iv.change_type.value]} — {iv.comment or 'sin detalle'}",
            detail=f"{cfg.scope_label('CC:' + (iv.cost_center_id or ''))} · "
                   f"desde {iv.effective_date}{gente}",
            fields=[Field_(name=SEP.join(["MV", iv.movement_id or ""]),
                           label="Nominal mensual",
                           value="" if unitario is None else _fmt(unitario * cantidad),
                           currency=cfg.payroll.currency)]))

    aumentos = " · ".join(f"{r.effective_date:%d/%m} +{r.percentage * 100:.0f}%"
                          for r in cfg.payroll.increase_rules) or "sin aumentos configurados"
    return FormSpec(
        "Nómina — foto inicial y valor de las solicitudes",
        f"Todo en {cfg.payroll.currency} y a valores de hoy: el sistema aplica las cargas "
        f"({((cfg.payroll.charges_factor - 1) * 100):.0f}%) y los aumentos del ejercicio "
        f"({aumentos}) a cada movimiento desde su propia fecha. Un centro sin gente lleva 0; "
        "vacío es un faltante. En una baja, poné lo que se deja de pagar por esa persona. "
        "Si después se autoriza una cantidad distinta de personas, el importe se ajusta solo "
        "en la misma proporción: no hay que volver a cargarlo.",
        ["Centro de costo o solicitud", "Detalle"], rows, "budget.payroll.load")


def _capex_form(cfg: Configuration, version) -> FormSpec:
    items = [iv for iv in version.inputs.values if iv.concept is Concept.CAPEX_AMOUNT]
    scopes = ([("CO", "Empresa")]
              + [(f"BU:{u.id}", u.name) for u in cfg.business_units]
              + [(f"BR:{b.id}", b.name) for b in cfg.branches]
              + [(f"OP:{o.id}", cfg.operation_label(o.id)) for o in cfg.operations]
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
              "OPERATION": [(f"OP:{o.id}", cfg.operation_label(o.id))
                            for o in cfg.operations]}[level]
    open_cur = _index(version, Concept.OPENING_STOCK)
    pur_cur = _index(version, Concept.PURCHASES)
    fam_names = {f.id: f.name for u in cfg.business_units for f in u.families}

    rows: list[Row] = []
    for scope_key, scope_label in scopes:
        for fam in _families_for_scope(cfg, scope_key):
            okey = SEP.join([scope_key, "", "", fam, "", ""])
            fields = [Field_(name=SEP.join(["O", scope_key, fam]), label="Stock inicial",
                             value=_fmt(open_cur.get(okey)), currency=cfg.inventory.currency)]
            if cfg.inventory.purchases_enabled:
                for head, _ in fy.iter_buckets(cfg.inventory.frequency):
                    pkey = SEP.join([scope_key, "", "", fam, "", head.code])
                    fields.append(Field_(name=SEP.join(["P", scope_key, fam, head.code]),
                                         label=f"Compras {head.code}",
                                         value=_fmt(pur_cur.get(pkey)),
                                         currency=cfg.inventory.currency))
            rows.append(Row(label=f"{fam_names.get(fam, fam)}", detail=scope_label, fields=fields))
    nivel = {"COMPANY": "empresa", "BUSINESS_UNIT": "unidad de negocio",
             "OPERATION": "operación (unidad x sucursal)"}[level]
    return FormSpec("Stock y compras",
                    f"El stock se administra por familia a nivel {nivel}, en "
                    f"{cfg.inventory.currency}. El stock final no se carga: lo calcula el "
                    "sistema como stock anterior + compras − costo de venta.",
                    ["Familia", "Ámbito"], rows, "budget.expense.load")


def _balance_form(cfg: Configuration, version) -> FormSpec:
    op = _index(version, Concept.BALANCE_OPENING)
    pr = _index(version, Concept.BALANCE_PROJECTED)
    section_label = {"ASSET": "Activo", "LIABILITY": "Pasivo", "EQUITY": "Patrimonio"}
    rows: list[Row] = []
    for item in cfg.balance.items:
        key = SEP.join(["CO", "", "", "", item.id, ""])
        detail = (f"{section_label[item.section.value]} "
                  f"{'corriente' if item.current else 'no corriente'}")
        if item.source.value == "CALCULATED":
            rows.append(Row(item.name, detail + " · calculado, no se carga", []))
            continue
        rows.append(Row(item.name, detail, [
            Field_(name=SEP.join(["BO", item.id]), label="Inicial", value=_fmt(op.get(key)),
                   currency=cfg.balance.currency),
            Field_(name=SEP.join(["BP", item.id]), label="Proyectado", value=_fmt(pr.get(key)),
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
    """Convierte el formulario en inputs y los manda por el servicio."""
    cfg = version.configuration
    spec = build_form(version, task)
    pending: list[InputValue] = []

    for key, raw in formdata.items():
        if SEP not in key:
            continue
        parts = key.split(SEP)
        value = _dec(raw)
        if value is None:
            continue

        if parts[0] == "S":
            operation_id = task.scope_key.split(":", 1)[1]
            op = cfg.operation(operation_id)
            unit = cfg.unit(op.business_unit_id)
            product = unit.product(parts[1])
            unit_based = product.sales_mode is SalesMode.UNIT_BASED
            pending.append(InputValue(
                concept=Concept.SALES_QTY if unit_based else Concept.SALES_AMOUNT,
                period=parts[2], value=value,
                currency=None if unit_based else product.currency,
                operation_id=op.id, business_unit_id=unit.id, branch_id=op.branch_id,
                product_id=product.id))
        elif parts[0] == "E":
            ed = cfg.expense(parts[1])
            iv = InputValue(concept=Concept.EXPENSE_AMOUNT, period=parts[3],
                            value=value, currency=ed.currency, expense_id=ed.id)
            _set_scope(iv, parts[2], cfg)
            pending.append(iv)
        elif parts[0] == "IH":
            iv = InputValue(concept=Concept.INITIAL_HEADCOUNT, value=value)
            _set_scope(iv, f"CC:{parts[1]}", cfg)
            pending.append(iv)
        elif parts[0] == "IN":
            iv = InputValue(concept=Concept.NOMINAL_SALARY, value=value,
                            currency=cfg.payroll.currency)
            _set_scope(iv, f"CC:{parts[1]}", cfg)
            pending.append(iv)
        elif parts[0] == "MV":
            solicitud = next((x for x in version.inputs.values
                              if x.concept is Concept.HEADCOUNT_CHANGE
                              and x.movement_id == parts[1]), None)
            if solicitud is None:
                raise ValueError("La solicitud que se intenta valorizar ya no existe.")
            if movement_quantity(solicitud) <= 0:
                raise ValueError("Esa solicitud no tiene personas: corregila antes de valorizarla.")
            # Se guarda el nominal de una persona. Así, si en la revisión se
            # autoriza una cantidad distinta, el importe se recalcula solo.
            pending.append(InputValue(
                concept=Concept.NOMINAL_SALARY, value=value / movement_quantity(solicitud),
                currency=cfg.payroll.currency, movement_id=parts[1],
                cost_center_id=solicitud.cost_center_id))
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

    cambios = 0
    for iv in pending:
        if version.inputs.unchanged(iv):
            continue
        service.submit_input(actor, version, iv, spec.capability)
        cambios += 1
    return cambios


def _set_scope(iv: InputValue, scope_key: str, cfg: Configuration) -> None:
    if not scope_key or scope_key == "CO":
        return
    kind, _id = scope_key.split(":", 1)
    if kind == "OP":
        op = cfg.operation(_id)
        iv.operation_id = op.id
        iv.business_unit_id = op.business_unit_id
        iv.branch_id = op.branch_id
    elif kind == "BR":
        iv.branch_id = _id
    elif kind == "BU":
        iv.business_unit_id = _id
    elif kind == "SU":
        iv.support_unit_id = _id
    elif kind == "CC":
        iv.cost_center_id = _id


def add_headcount_change(service, actor: str, version, task, form) -> None:
    """Una solicitud de un área. Dos solicitudes iguales el mismo día son dos
    cosas distintas, así que cada una lleva su propio identificador: es lo que
    ata el importe que después le pone Nómina."""
    import uuid
    from datetime import date as _date

    tipo = ChangeType(form["change_type"])
    cantidad = Decimal(0) if tipo is ChangeType.ADJUSTMENT else Decimal(form["quantity"])
    iv = InputValue(
        concept=Concept.HEADCOUNT_CHANGE,
        value=cantidad,
        change_type=tipo,
        effective_date=_date.fromisoformat(form["effective_date"]),
        comment=(form.get("comment") or "").strip() or None,
        movement_id=f"MOV-{uuid.uuid4().hex[:8]}")
    _set_scope(iv, task.scope_key, version.configuration)
    service.submit_input(actor, version, iv, "budget.headcount.load")


def remove_headcount_change(service, actor: str, version, movement_id: str) -> None:
    """Al borrar una solicitud se va también su valorización: un importe sin
    solicitud no es de nadie."""
    version.assert_mutable()
    quedan = [iv for iv in version.inputs.values if iv.movement_id != movement_id]
    if len(quedan) == len(version.inputs.values):
        raise ValueError("Esa solicitud ya no existe.")
    version.inputs.values = quedan
    version.invalidate()
    service.audit.record(actor=actor, action="MovementRemoved", entity_type="HEADCOUNT_CHANGE",
                         entity_id=movement_id, version_id=version.id, before=movement_id)


def add_capex(service, actor: str, version, form) -> None:
    iv = InputValue(concept=Concept.CAPEX_AMOUNT, period=form["period"],
                    value=Decimal(form["amount"]), currency=form["currency"],
                    capex_category_id=form["category_id"])
    _set_scope(iv, form["scope"], version.configuration)
    service.submit_input(actor, version, iv, "budget.expense.load")
