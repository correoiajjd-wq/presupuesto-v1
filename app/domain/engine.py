"""Calculation Engine: construye el grafo de dependencias del presupuesto.

Doc 03 §9: es el componente técnico más importante. No contiene reglas de UI
ni de persistencia; recibe configuración + inputs + TC y devuelve un grafo
evaluable. Todo valor calculado tiene una única fuente (Single Source of Truth)
y una cadena de dependencias explicable.

Unidad de cuenta interna: la moneda de presentación. Cada input se convierte
en el momento de entrar al grafo, con el TC del período (promedio para flujos,
cierre para stocks).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from .config import (
    AllocationMode,
    Configuration,
    ExpenseTargetType,
    InventoryLevel,
    MarginFormula,
    SalesMode,
    BalanceSection,
    BalanceSource,
)
from .graph import DependencyGraph, Node, nk, sum_of
from .inputs import ChangeType, Concept, InputSet, InputValue
from .money import FXConvention, FXTable, Money, d
from .periods import Frequency, Period, spread
from .ratios import RATIO_CATALOG

FY = "FY"  # pseudo-período: el ejercicio completo
ZERO = Decimal(0)


def scope_co() -> str:
    return "CO"


def scope_bu(bu_id: str) -> str:
    return f"BU:{bu_id}"


def scope_br(branch_id: str) -> str:
    return f"BR:{branch_id}"


def scope_su(su_id: str) -> str:
    return f"SU:{su_id}"


def scope_prod(branch_id: str, product_id: str) -> str:
    return f"BR:{branch_id}#P:{product_id}"


def scope_stock(base: str, family_id: str) -> str:
    return f"{base}#FAM:{family_id}"


class BudgetEngine:
    def __init__(self, config: Configuration, fx: FXTable, inputs: InputSet):
        self.cfg = config
        self.fx = fx
        self.inputs = inputs
        self.fy = config.fiscal_year
        self.periods: list[Period] = self.fy.periods
        self.assumptions: list[str] = []
        self.missing: list[str] = []
        self.g = DependencyGraph()

    # ==================================================================
    # utilidades
    # ==================================================================
    def _to_pres(self, amount: Decimal, currency: str, period: Period,
                 conv: FXConvention = FXConvention.AVERAGE) -> Decimal:
        return self.fx.to_presentation(Money(d(amount), currency), period, conv).amount

    def _expand(self, value: Decimal, load_period: Period, freq: Frequency) -> dict[Period, Decimal]:
        """Distribución temporal según frecuencia (doc 01 §23)."""
        bucket = self.fy.bucket_for(load_period, freq)
        return spread(d(value), bucket)

    def _days(self, period_code: str) -> int:
        if period_code == FY:
            return (self.fy.end - self.fy.start).days + 1
        p = Period.parse(period_code)
        return p.last_day.day

    def _all_period_codes(self) -> list[str]:
        return [p.code for p in self.periods]

    # ==================================================================
    # construcción del grafo
    # ==================================================================
    def build(self) -> DependencyGraph:
        self._build_sales()
        self._build_payroll()
        self._build_expenses()
        self._build_capex()
        self._build_aggregates()
        self._build_allocations()
        self._build_inventory()
        self._build_balance()
        self._build_annual()
        self._build_ratios()
        return self.g

    # ------------------------------------------------------------------
    # 1. Ventas y costo, por producto y sucursal
    # ------------------------------------------------------------------
    def _build_sales(self) -> None:
        cfg = self.cfg
        # inputs expandidos a mensual: (branch, product) -> period -> valor
        qty: dict[tuple[str, str], dict[Period, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        amt: dict[tuple[str, str], dict[Period, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

        for iv in self.inputs.of(Concept.SALES_QTY, Concept.SALES_AMOUNT):
            branch = cfg.branch(iv.branch_id)
            unit = cfg.branch_owner(iv.branch_id)
            product = unit.product(iv.product_id)
            lp = Period.parse(iv.period)
            # La distribución sólo alcanza los meses en que la sucursal está vigente:
            # una sucursal que abre en junio no puede recibir 1/3 de un trimestre abr-jun.
            bucket = [p for p in self.fy.bucket_for(lp, product.sales_frequency)
                      if cfg.is_active(branch, p) and cfg.is_active(unit, p)]
            if not bucket:
                self.missing.append(
                    f"OUT_OF_EFFECTIVITY: carga de {product.code} en {branch.name} "
                    f"para {iv.period}: la sucursal no está vigente en ese período"
                )
                continue
            for p, v in spread(d(iv.value), bucket).items():
                if iv.concept is Concept.SALES_QTY:
                    qty[(iv.branch_id, iv.product_id)][p] += v
                else:
                    amt[(iv.branch_id, iv.product_id)][p] += self._to_pres(
                        v, iv.currency or product.currency, p
                    )

        for unit in cfg.business_units:
            for branch in cfg.unit_branches(unit.id):
                for product in unit.products:
                    for p in self.periods:
                        s_key = nk("SALES", scope_prod(branch.id, product.id), p.code)
                        c_key = nk("COGS", scope_prod(branch.id, product.id), p.code)
                        active = cfg.is_active(branch, p) and cfg.is_active(unit, p)

                        if not active:
                            self.g.constant(s_key, ZERO, kind="CALCULATED", reason="fuera de vigencia")
                            self.g.constant(c_key, ZERO, kind="CALCULATED", reason="fuera de vigencia")
                            continue

                        if product.sales_mode is SalesMode.UNIT_BASED:
                            q = qty[(branch.id, product.id)].get(p, ZERO)
                            sales = self._to_pres(q * product.price, product.currency, p)
                            self.g.constant(
                                s_key, sales, kind="INPUT",
                                formula="cantidad x precio", quantity=str(q),
                                price=str(product.price), currency=product.currency,
                            )
                        else:
                            sales = amt[(branch.id, product.id)].get(p, ZERO)
                            self.g.constant(
                                s_key, sales, kind="INPUT", formula="monto cargado",
                                currency=product.currency,
                            )

                        # La modalidad y la fórmula de margen son del producto:
                        # una unidad puede vender mercadería y servicios a la vez.
                        ratio = product.cost_ratio
                        formula = {
                            MarginFormula.PERCENTAGE_OF_SALES: "ventas x (1 - margen)",
                            MarginFormula.MARKUP_ON_COST: "ventas / (1 + margen)",
                            MarginFormula.NO_COST: "sin costo: el precio es todo margen",
                        }[product.margin_formula]
                        self.g.calc(
                            c_key, [s_key],
                            lambda v, k=s_key, r=ratio: None if v.get(k) is None else v[k] * r,
                            formula=formula, margin=str(product.margin))

                # agregación producto -> sucursal
                for p in self.periods:
                    for metric in ("SALES", "COGS"):
                        keys = [nk(metric, scope_prod(branch.id, pr.id), p.code) for pr in unit.products]
                        self.g.calc(nk(metric, scope_br(branch.id), p.code), keys, sum_of(keys),
                                    formula="suma de productos")
                    s = nk("SALES", scope_br(branch.id), p.code)
                    c = nk("COGS", scope_br(branch.id), p.code)
                    self.g.calc(
                        nk("GROSS_MARGIN", scope_br(branch.id), p.code), [s, c],
                        lambda v, s=s, c=c: None if v.get(s) is None or v.get(c) is None else v[s] - v[c],
                        formula="ventas - costo",
                    )

    # ------------------------------------------------------------------
    # 2. Nómina
    # ------------------------------------------------------------------
    def _payroll_cohorts(self) -> dict[tuple[str, str], list[tuple[date, Decimal]]]:
        """(scope_key, area_id) -> [(fecha_de_ingreso, cantidad)] antes de bajas."""
        cohorts: dict[tuple[str, str], list[tuple[date, Decimal]]] = defaultdict(list)
        for iv in self.inputs.of(Concept.INITIAL_HEADCOUNT):
            cohorts[(iv.scope_key, iv.area_id)].append((self.fy.start, d(iv.value)))
        for iv in self.inputs.of(Concept.HEADCOUNT_CHANGE):
            if iv.change_type is ChangeType.HIRED:
                cohorts[(iv.scope_key, iv.area_id)].append(
                    (iv.effective_date or self.fy.start, d(iv.value))
                )
        for k in cohorts:
            cohorts[k].sort(key=lambda t: t[0])
        return cohorts

    def _terminations(self) -> dict[tuple[str, str], list[tuple[date, Decimal]]]:
        out: dict[tuple[str, str], list[tuple[date, Decimal]]] = defaultdict(list)
        for iv in self.inputs.of(Concept.HEADCOUNT_CHANGE):
            if iv.change_type is ChangeType.TERMINATED:
                out[(iv.scope_key, iv.area_id)].append(
                    (iv.effective_date or self.fy.start, d(iv.value))
                )
        for k in out:
            out[k].sort(key=lambda t: t[0])
        return out

    def _increase_factor(self, entry: date, period: Period) -> Decimal:
        """Doc 01 §17: sólo aplican los aumentos posteriores al ingreso."""
        factor = Decimal(1)
        for rule in self.cfg.payroll.increase_rules:
            if rule.effective_date >= entry and rule.effective_date <= period.last_day:
                factor *= (Decimal(1) + rule.percentage)
        return factor

    def _build_payroll(self) -> None:
        cfg = self.cfg
        cohorts = self._payroll_cohorts()
        terms = self._terminations()
        charges = cfg.payroll.charges_factor

        # En V1 la nómina vive a nivel sucursal o unidad de soporte.
        scopes: set[str] = {scope_br(b.id) for _, b in cfg.all_branches()}
        scopes |= {scope_su(su.id) for su in cfg.support_units}
        for (scope_key, _area) in list(cohorts.keys()) + list(terms.keys()):
            if scope_key not in scopes:
                self.missing.append(
                    f"INVALID_PAYROLL_SCOPE: dotación cargada en {scope_key}, "
                    "que no es sucursal ni unidad de soporte; se ignora"
                )

        # Comisiones como % de ventas (doc 01 §19). La tasa es de cada producto:
        # dentro de una misma sucursal, unos comisionan y otros no.
        commission_products: dict[str, list[tuple[str, Decimal]]] = {}
        for unit in cfg.business_units:
            con_comision = [(p.id, p.commission_rate) for p in unit.products
                            if p.commission_rate]
            if con_comision:
                for b in cfg.unit_branches(unit.id):
                    commission_products[scope_br(b.id)] = con_comision

        manual_commissions: dict[str, dict[Period, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for iv in self.inputs.of(Concept.COMMISSION_AMOUNT):
            lp = Period.parse(iv.period)
            for p, v in self._expand(iv.value, lp, cfg.payroll.frequency).items():
                manual_commissions[iv.scope_key][p] += self._to_pres(
                    v, iv.currency or self.fx.presentation, p
                )

        for scope_key in sorted(scopes):
            for p in self.periods:
                head = ZERO
                cost = ZERO
                for (sk, area_id), chs in cohorts.items():
                    if sk != scope_key:
                        continue
                    area = cfg.payroll.area(area_id)
                    remaining = [[entry, q] for entry, q in chs if entry <= p.last_day]
                    # bajas FIFO sobre las cohortes más antiguas
                    for t_date, t_qty in terms.get((sk, area_id), []):
                        if t_date > p.last_day:
                            continue
                        left = t_qty
                        for row in remaining:
                            if left <= 0:
                                break
                            take = min(row[1], left)
                            row[1] -= take
                            left -= take
                    for entry, q in remaining:
                        if q <= 0:
                            continue
                        salary = area.base_salary * self._increase_factor(entry, p)
                        cost += self._to_pres(q * salary * charges, area.currency, p)
                        head += q

                self.g.constant(nk("HEADCOUNT", scope_key, p.code), head, kind="INPUT",
                                formula="dotación vigente en el período")
                self.g.constant(nk("PAYROLL_BASE", scope_key, p.code), cost, kind="INPUT",
                                formula="dotación x sueldo con aumentos x (1 + cargas)")

                # comisiones
                comm_key = nk("COMMISSION", scope_key, p.code)
                productos = commission_products.get(scope_key)
                if productos:
                    branch_id = scope_key.split(":", 1)[1]
                    deps = [nk("SALES", scope_prod(branch_id, pid), p.code)
                            for pid, _ in productos]
                    tasas = [rate for _, rate in productos]

                    def _comision(v, deps=deps, tasas=tasas):
                        total = ZERO
                        for key, rate in zip(deps, tasas):
                            total += (v.get(key) or ZERO) * rate
                        return total

                    self.g.calc(comm_key, deps, _comision,
                                formula="suma de ventas por producto x su tasa de comisión")
                else:
                    self.g.constant(comm_key, manual_commissions[scope_key].get(p, ZERO),
                                    kind="INPUT", formula="comisión cargada por Nómina")

                base_key = nk("PAYROLL_BASE", scope_key, p.code)
                self.g.calc(
                    nk("PAYROLL", scope_key, p.code), [base_key, comm_key],
                    lambda v, b=base_key, c=comm_key: (
                        None if v.get(b) is None else v[b] + (v.get(c) or ZERO)
                    ),
                    formula="costo laboral + comisiones",
                )

    # ------------------------------------------------------------------
    # 3. Gastos
    # ------------------------------------------------------------------
    def _build_expenses(self) -> None:
        cfg = self.cfg
        # total cargado por definición de gasto y período (en moneda de presentación)
        loaded: dict[tuple[str, str], dict[Period, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal))
        for iv in self.inputs.of(Concept.EXPENSE_AMOUNT):
            ed = cfg.expense(iv.expense_id)
            lp = Period.parse(iv.period)
            for p, v in self._expand(iv.value, lp, ed.frequency).items():
                loaded[(ed.id, iv.scope_key)][p] += self._to_pres(
                    v, iv.currency or ed.currency, p)

        # nodos por gasto
        branch_parts: dict[str, list[str]] = defaultdict(list)   # scope_br -> keys
        unit_parts: dict[str, list[str]] = defaultdict(list)     # scope_bu (no distribuido)
        support_parts: dict[str, list[str]] = defaultdict(list)  # scope_su
        company_parts: list[str] = []

        def route(target, part_key: str, p: Period) -> None:
            """Lleva la porción de un gasto al ámbito que corresponde."""
            tt = target.target_type
            if tt is ExpenseTargetType.BRANCH:
                branch_parts[scope_br(target.target_id)].append(part_key)
            elif tt is ExpenseTargetType.COST_CENTER:
                su = cfg.cost_center_owner(target.target_id)
                support_parts[scope_su(su.id)].append(part_key)
            elif tt is ExpenseTargetType.BUSINESS_UNIT:
                tid = target.target_id
                if not target.distribute_to_branches:
                    unit_parts[scope_bu(tid)].append(part_key)
                    return
                sfy_bu = nk("SALES", scope_bu(tid), FY)
                for b in cfg.unit_branches(tid):
                    sub = nk("EXPENSE_PART", f"{part_key.split('|')[1]}>BR:{b.id}", p.code)
                    sfy_br = nk("SALES", scope_br(b.id), FY)
                    self.g.calc(sub, [part_key, sfy_br, sfy_bu],
                                self._proportional(part_key, sfy_br, sfy_bu),
                                formula="gasto de unidad distribuido a sucursal "
                                        "proporcional a ventas anuales")
                    branch_parts[scope_br(b.id)].append(sub)
                # si la unidad no tiene ventas, el gasto queda corporativo de la unidad
                residual = nk("EXPENSE_PART", f"{part_key.split('|')[1]}>CORP", p.code)
                self.g.calc(residual, [part_key, sfy_bu],
                            lambda v, k=part_key, s=sfy_bu: (
                                (v.get(k) or ZERO) if not (v.get(s) or ZERO) else ZERO),
                            formula="residual corporativo de la unidad (sin ventas)")
                unit_parts[scope_bu(tid)].append(residual)
            else:  # COMPANY
                company_parts.append(part_key)

        for ed in cfg.expenses:
            for p in self.periods:
                if ed.allocation_mode is AllocationMode.PER_TARGET:
                    # Un importe por destino. El mismo concepto puede existir en
                    # varias sucursales y centros de costo con montos distintos;
                    # donde no corresponde se carga 0.
                    for t in ed.targets:
                        key = nk("EXPENSE_INPUT", f"EXP:{ed.id}@{t.scope_key}", p.code)
                        self.g.constant(
                            key, loaded[(ed.id, t.scope_key)].get(p, ZERO), kind="INPUT",
                            expense=ed.name, target=cfg.scope_label(t.scope_key),
                            currency=ed.currency, frequency=ed.frequency.value)
                        route(t, key, p)
                else:
                    total_key = nk("EXPENSE_INPUT", f"EXP:{ed.id}", p.code)
                    self.g.constant(total_key, loaded[(ed.id, "CO")].get(p, ZERO), kind="INPUT",
                                    expense=ed.name, currency=ed.currency,
                                    frequency=ed.frequency.value)
                    for t in ed.targets:
                        part_key = nk("EXPENSE_PART", f"EXP:{ed.id}@{t.scope_key}", p.code)
                        pct = t.percentage or ZERO
                        self.g.calc(
                            part_key, [total_key],
                            lambda v, k=total_key, q=pct: None if v.get(k) is None else v[k] * q,
                            formula="gasto x % de distribución", percentage=str(pct),
                            target=cfg.scope_label(t.scope_key))
                        route(t, part_key, p)

        # gastos propios por ámbito
        for unit in cfg.business_units:
            for b in cfg.unit_branches(unit.id):
                keys = branch_parts.get(scope_br(b.id), [])
                for p in self.periods:
                    pk = [k for k in keys if k.endswith(f"|{p.code}")]
                    self.g.calc(nk("EXPENSES", scope_br(b.id), p.code), pk, sum_of(pk),
                                formula="gastos propios de la sucursal")
            for p in self.periods:
                pk = [k for k in unit_parts.get(scope_bu(unit.id), []) if k.endswith(f"|{p.code}")]
                self.g.calc(nk("EXPENSES_UNIT_LEVEL", scope_bu(unit.id), p.code), pk, sum_of(pk),
                            formula="gastos de la unidad no distribuidos a sucursales")

        for su in cfg.support_units:
            for p in self.periods:
                pk = [k for k in support_parts.get(scope_su(su.id), []) if k.endswith(f"|{p.code}")]
                self.g.calc(nk("EXPENSES", scope_su(su.id), p.code), pk, sum_of(pk),
                            formula="gastos del área de soporte")

        for p in self.periods:
            pk = [k for k in company_parts if k.endswith(f"|{p.code}")]
            self.g.calc(nk("EXPENSES_COMPANY_LEVEL", scope_co(), p.code), pk, sum_of(pk),
                        formula="gastos cargados directamente a empresa")

    @staticmethod
    def _proportional(amount_key: str, num_key: str, den_key: str):
        def _fn(v):
            amount = v.get(amount_key)
            num = v.get(num_key)
            den = v.get(den_key)
            if amount is None:
                return None
            if not den:
                return ZERO
            return amount * (num or ZERO) / den
        return _fn

    # ------------------------------------------------------------------
    # 4. CAPEX
    # ------------------------------------------------------------------
    def _build_capex(self) -> None:
        cfg = self.cfg
        loaded: dict[str, dict[Period, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for iv in self.inputs.of(Concept.CAPEX_AMOUNT):
            lp = Period.parse(iv.period)
            for p, v in self._expand(iv.value, lp, cfg.capex.frequency).items():
                loaded[iv.scope_key][p] += self._to_pres(
                    v, iv.currency or self.fx.presentation, p
                )
        scopes = {scope_br(b.id) for _, b in cfg.all_branches()}
        scopes |= {scope_bu(u.id) for u in cfg.business_units}
        scopes |= {scope_su(u.id) for u in cfg.support_units} | {scope_co()}
        scopes |= set(loaded.keys())
        for s in sorted(scopes):
            for p in self.periods:
                self.g.constant(nk("CAPEX_OWN", s, p.code), loaded[s].get(p, ZERO), kind="INPUT",
                                formula="CAPEX cargado")

    # ------------------------------------------------------------------
    # 5. Consolidación P&L
    # ------------------------------------------------------------------
    def _build_aggregates(self) -> None:
        cfg = self.cfg

        for unit in cfg.business_units:
            br_scopes = [scope_br(b.id) for b in cfg.unit_branches(unit.id)]
            for p in self.periods:
                for metric in ("SALES", "COGS", "GROSS_MARGIN", "PAYROLL", "HEADCOUNT", "COMMISSION"):
                    keys = [nk(metric, s, p.code) for s in br_scopes]
                    self.g.calc(nk(metric, scope_bu(unit.id), p.code), keys, sum_of(keys),
                                formula="suma de sucursales")
                own = [nk("EXPENSES", s, p.code) for s in br_scopes]
                unit_lvl = nk("EXPENSES_UNIT_LEVEL", scope_bu(unit.id), p.code)
                keys = own + [unit_lvl]
                self.g.calc(nk("EXPENSES", scope_bu(unit.id), p.code), keys, sum_of(keys),
                            formula="gastos de sucursales + gastos de unidad")
                capex_keys = [nk("CAPEX_OWN", s, p.code) for s in br_scopes] + [
                    nk("CAPEX_OWN", scope_bu(unit.id), p.code)
                ]
                self.g.calc(nk("CAPEX", scope_bu(unit.id), p.code), capex_keys, sum_of(capex_keys),
                            formula="CAPEX de la unidad y sus sucursales")

        # EBITDA por sucursal, unidad, soporte y empresa
        for p in self.periods:
            for unit in cfg.business_units:
                for b in cfg.unit_branches(unit.id):
                    self._ebitda(scope_br(b.id), p.code)
                    self.g.calc(nk("CAPEX", scope_br(b.id), p.code),
                                [nk("CAPEX_OWN", scope_br(b.id), p.code)],
                                sum_of([nk("CAPEX_OWN", scope_br(b.id), p.code)]))
                self._ebitda(scope_bu(unit.id), p.code)

            for su in cfg.support_units:
                self.g.calc(nk("CAPEX", scope_su(su.id), p.code),
                            [nk("CAPEX_OWN", scope_su(su.id), p.code)],
                            sum_of([nk("CAPEX_OWN", scope_su(su.id), p.code)]))

            # empresa
            bu_scopes = [scope_bu(u.id) for u in cfg.business_units]
            su_scopes = [scope_su(u.id) for u in cfg.support_units]
            for metric in ("SALES", "COGS", "GROSS_MARGIN", "COMMISSION"):
                keys = [nk(metric, s, p.code) for s in bu_scopes]
                self.g.calc(nk(metric, scope_co(), p.code), keys, sum_of(keys),
                            formula="suma de unidades de negocio")
            hkeys = [nk("HEADCOUNT", s, p.code) for s in bu_scopes + su_scopes]
            self.g.calc(nk("HEADCOUNT", scope_co(), p.code), hkeys, sum_of(hkeys),
                        formula="dotación total")
            pkeys = [nk("PAYROLL", s, p.code) for s in bu_scopes + su_scopes]
            self.g.calc(nk("PAYROLL", scope_co(), p.code), pkeys, sum_of(pkeys),
                        formula="nómina de unidades + soporte")
            ekeys = ([nk("EXPENSES", s, p.code) for s in bu_scopes + su_scopes]
                     + [nk("EXPENSES_COMPANY_LEVEL", scope_co(), p.code)])
            self.g.calc(nk("EXPENSES", scope_co(), p.code), ekeys, sum_of(ekeys),
                        formula="gastos de unidades + soporte + empresa")
            ckeys = [nk("CAPEX", s, p.code) for s in bu_scopes + su_scopes] + [
                nk("CAPEX_OWN", scope_co(), p.code)
            ]
            self.g.calc(nk("CAPEX", scope_co(), p.code), ckeys, sum_of(ckeys),
                        formula="CAPEX total")
            self._ebitda(scope_co(), p.code)

    def _ebitda(self, scope: str, period_code: str) -> None:
        gm = nk("GROSS_MARGIN", scope, period_code)
        ex = nk("EXPENSES", scope, period_code)
        pay = nk("PAYROLL", scope, period_code)
        deps = [gm, ex, pay]

        def _fn(v, gm=gm, ex=ex, pay=pay):
            base = v.get(gm)
            if base is None:
                base = ZERO
            return base - (v.get(ex) or ZERO) - (v.get(pay) or ZERO)

        self.g.calc(nk("EBITDA", scope, period_code), deps, _fn,
                    formula="margen bruto - gastos - nómina")

    # ------------------------------------------------------------------
    # 6. Asignación de gastos corporativos (presentacional, doc 02 §53)
    # ------------------------------------------------------------------
    def _build_allocations(self) -> None:
        cfg = self.cfg
        su_scopes = [scope_su(u.id) for u in cfg.support_units]

        for p in self.periods:
            pool_keys = [nk("EXPENSES_COMPANY_LEVEL", scope_co(), p.code)]
            pool_keys += [nk("EXPENSES", s, p.code) for s in su_scopes]
            pool_keys += [nk("PAYROLL", s, p.code) for s in su_scopes]
            pool = nk("CORPORATE_POOL", scope_co(), p.code)
            self.g.calc(pool, pool_keys, sum_of(pool_keys),
                        formula="gastos de empresa + soporte (gastos y nómina)")

            sfy_co = nk("SALES", scope_co(), FY)
            for unit in cfg.business_units:
                sfy_bu = nk("SALES", scope_bu(unit.id), FY)
                self.g.calc(
                    nk("ALLOCATED_EXPENSES", scope_bu(unit.id), p.code),
                    [pool, sfy_bu, sfy_co], self._proportional(pool, sfy_bu, sfy_co),
                    formula="pool corporativo x participación en ventas anuales",
                )
                self._after_allocation(scope_bu(unit.id), p.code)

                for b in cfg.unit_branches(unit.id):
                    sfy_br = nk("SALES", scope_br(b.id), FY)
                    corp_from_co = nk("ALLOC_FROM_COMPANY", scope_br(b.id), p.code)
                    self.g.calc(corp_from_co, [pool, sfy_br, sfy_co],
                                self._proportional(pool, sfy_br, sfy_co),
                                formula="pool corporativo de empresa")
                    unit_lvl = nk("EXPENSES_UNIT_LEVEL", scope_bu(unit.id), p.code)
                    corp_from_bu = nk("ALLOC_FROM_UNIT", scope_br(b.id), p.code)
                    self.g.calc(corp_from_bu, [unit_lvl, sfy_br, sfy_bu],
                                self._proportional(unit_lvl, sfy_br, sfy_bu),
                                formula="gastos corporativos de la unidad")
                    keys = [corp_from_co, corp_from_bu]
                    self.g.calc(nk("ALLOCATED_EXPENSES", scope_br(b.id), p.code), keys,
                                sum_of(keys), formula="asignación corporativa total")
                    self._after_allocation(scope_br(b.id), p.code)

            self.g.constant(nk("ALLOCATED_EXPENSES", scope_co(), p.code), ZERO, kind="CALCULATED",
                            formula="la empresa no recibe asignación")
            self._after_allocation(scope_co(), p.code)

    def _after_allocation(self, scope: str, period_code: str) -> None:
        eb = nk("EBITDA", scope, period_code)
        al = nk("ALLOCATED_EXPENSES", scope, period_code)
        self.g.calc(
            nk("RESULT_AFTER_ALLOCATION", scope, period_code), [eb, al],
            lambda v, e=eb, a=al: None if v.get(e) is None else v[e] - (v.get(a) or ZERO),
            formula="EBITDA - gastos corporativos asignados",
        )

    # ------------------------------------------------------------------
    # 7. Inventario por familia
    # ------------------------------------------------------------------
    def _stock_scopes(self) -> list[tuple[str, list[str]]]:
        """(scope de stock, sucursales que aportan COGS)."""
        cfg = self.cfg
        if cfg.inventory.level is InventoryLevel.COMPANY:
            return [(scope_co(), [b.id for _, b in cfg.all_branches()])]
        if cfg.inventory.level is InventoryLevel.BUSINESS_UNIT:
            return [(scope_bu(u.id), [b.id for b in cfg.unit_branches(u.id)]) for u in cfg.business_units]
        return [(scope_br(b.id), [b.id]) for _, b in cfg.all_branches()]

    def _build_inventory(self) -> None:
        cfg = self.cfg
        if not cfg.inventory.enabled:
            return
        inv_ccy = cfg.inventory.currency

        opening: dict[tuple[str, str], Decimal] = {}
        for iv in self.inputs.of(Concept.OPENING_STOCK):
            opening[(iv.scope_key, iv.family_id)] = d(iv.value)

        purchases: dict[tuple[str, str], dict[Period, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for iv in self.inputs.of(Concept.PURCHASES):
            lp = Period.parse(iv.period)
            for p, v in self._expand(iv.value, lp, cfg.inventory.frequency).items():
                purchases[(iv.scope_key, iv.family_id)][p] += v

        families_by_unit = {u.id: u.families for u in cfg.business_units}
        product_family = {
            (u.id, pr.id): pr.family_id for u in cfg.business_units for pr in u.products
        }

        for base_scope, branch_ids in self._stock_scopes():
            fams: dict[str, str] = {}
            for bid in branch_ids:
                unit = cfg.branch_owner(bid)
                for f in families_by_unit[unit.id]:
                    fams[f.id] = f.name
            for fam_id in sorted(fams):
                sscope = scope_stock(base_scope, fam_id)
                prev_close: Optional[str] = None
                for p in self.periods:
                    # COGS de la familia = suma de los productos que la integran
                    cogs_keys = []
                    for bid in branch_ids:
                        unit = cfg.branch_owner(bid)
                        for pr in unit.products:
                            if product_family[(unit.id, pr.id)] == fam_id:
                                cogs_keys.append(nk("COGS", scope_prod(bid, pr.id), p.code))
                    cogs_key = nk("COGS_FAMILY", sscope, p.code)
                    self.g.calc(cogs_key, cogs_keys, sum_of(cogs_keys),
                                formula="costo de venta consolidado de la familia")

                    open_key = nk("OPENING_STOCK", sscope, p.code)
                    if prev_close is None:
                        val = self._to_pres(
                            opening.get((base_scope, fam_id), ZERO), inv_ccy, p, FXConvention.CLOSING
                        )
                        self.g.constant(open_key, val, kind="INPUT",
                                        formula="stock inicial cargado", currency=inv_ccy)
                    else:
                        self.g.calc(open_key, [prev_close],
                                    lambda v, k=prev_close: v.get(k),
                                    formula="stock final del período anterior")

                    pur_key = nk("PURCHASES", sscope, p.code)
                    self.g.constant(
                        pur_key,
                        self._to_pres(purchases[(base_scope, fam_id)].get(p, ZERO), inv_ccy, p),
                        kind="INPUT", formula="compras cargadas", currency=inv_ccy,
                    )

                    close_key = nk("CLOSING_STOCK", sscope, p.code)
                    self.g.calc(
                        close_key, [open_key, pur_key, cogs_key],
                        lambda v, o=open_key, pu=pur_key, c=cogs_key: (
                            None if v.get(o) is None
                            else v[o] + (v.get(pu) or ZERO) - (v.get(c) or ZERO)
                        ),
                        formula="stock anterior + compras - costo de venta",
                    )
                    prev_close = close_key

            # agregación de familias al ámbito de stock
            for p in self.periods:
                for metric in ("OPENING_STOCK", "PURCHASES", "CLOSING_STOCK", "COGS_FAMILY"):
                    keys = [nk(metric, scope_stock(base_scope, f), p.code) for f in sorted(fams)]
                    self.g.calc(nk(metric, base_scope, p.code), keys, sum_of(keys),
                                formula="suma de familias")
                o = nk("OPENING_STOCK", base_scope, p.code)
                c = nk("CLOSING_STOCK", base_scope, p.code)
                self.g.calc(
                    nk("STOCK_AVG", base_scope, p.code), [o, c],
                    lambda v, o=o, c=c: (
                        None if v.get(o) is None or v.get(c) is None
                        else (v[o] + v[c]) / Decimal(2)
                    ),
                    formula="(stock inicial + stock final) / 2",
                )

        self._rollup_inventory()

    def _rollup_inventory(self) -> None:
        """Consolida el stock hacia arriba en la jerarquía, para que los ratios
        de inventario existan también en unidad y empresa."""
        cfg = self.cfg
        level = cfg.inventory.level
        metrics = ("OPENING_STOCK", "PURCHASES", "CLOSING_STOCK", "COGS_FAMILY", "STOCK_AVG")

        rollups: list[tuple[str, list[str]]] = []
        if level is InventoryLevel.BRANCH:
            for u in cfg.business_units:
                rollups.append((scope_bu(u.id), [scope_br(b.id) for b in cfg.unit_branches(u.id)]))
            rollups.append((scope_co(), [scope_bu(u.id) for u in cfg.business_units]))
        elif level is InventoryLevel.BUSINESS_UNIT:
            rollups.append((scope_co(), [scope_bu(u.id) for u in cfg.business_units]))

        for target, sources in rollups:
            for p in self.periods:
                for metric in metrics:
                    keys = [nk(metric, s, p.code) for s in sources
                            if self.g.has(nk(metric, s, p.code))]
                    if keys and not self.g.has(nk(metric, target, p.code)):
                        self.g.calc(nk(metric, target, p.code), keys, sum_of(keys),
                                    formula="consolidación de stock")

    # ------------------------------------------------------------------
    # 8. Balance
    # ------------------------------------------------------------------
    def _build_balance(self) -> None:
        cfg = self.cfg
        if not cfg.balance.enabled:
            return
        by_item_open: dict[str, Decimal] = {}
        by_item_proj: dict[str, Decimal] = {}
        for iv in self.inputs.of(Concept.BALANCE_OPENING):
            by_item_open[iv.balance_item_id] = d(iv.value)
        for iv in self.inputs.of(Concept.BALANCE_PROJECTED):
            by_item_proj[iv.balance_item_id] = d(iv.value)

        last = self.periods[-1]
        for tag, data in (("OPENING", by_item_open), ("FY", by_item_proj)):
            item_keys: dict[str, str] = {}
            for item in cfg.balance.items:
                key = nk("BALANCE_ITEM", f"BI:{item.id}", tag)
                if item.source is BalanceSource.CALCULATED:
                    continue  # el patrimonio se resuelve abajo
                val = self._to_pres(data.get(item.id, ZERO), cfg.balance.currency, last,
                                    FXConvention.CLOSING)
                self.g.constant(key, val, kind="INPUT", item=item.name,
                                section=item.section.value, currency=cfg.balance.currency)
                item_keys[item.id] = key

            def group(section: BalanceSection, only_current: Optional[bool] = None) -> list[str]:
                return [
                    item_keys[i.id] for i in cfg.balance.items
                    if i.id in item_keys and i.section is section
                    and (only_current is None or i.current == only_current)
                ]

            assets = group(BalanceSection.ASSET)
            liabs = group(BalanceSection.LIABILITY)
            eq_loaded = group(BalanceSection.EQUITY)
            self.g.calc(nk("EQUITY_LOADED", scope_co(), tag), eq_loaded, sum_of(eq_loaded),
                        formula="suma de los rubros de patrimonio cargados")
            ca = group(BalanceSection.ASSET, True)
            cl = group(BalanceSection.LIABILITY, True)

            self.g.calc(nk("ASSETS", scope_co(), tag), assets, sum_of(assets), formula="total activo")
            self.g.calc(nk("LIABILITIES", scope_co(), tag), liabs, sum_of(liabs), formula="total pasivo")
            self.g.calc(nk("CURRENT_ASSETS", scope_co(), tag), ca, sum_of(ca), formula="activo corriente")
            self.g.calc(nk("CURRENT_LIABILITIES", scope_co(), tag), cl, sum_of(cl),
                        formula="pasivo corriente")
            a_key = nk("ASSETS", scope_co(), tag)
            l_key = nk("LIABILITIES", scope_co(), tag)
            self.g.calc(
                nk("EQUITY", scope_co(), tag), [a_key, l_key],
                lambda v, a=a_key, l=l_key: (
                    None if v.get(a) is None or v.get(l) is None else v[a] - v[l]
                ),
                formula="patrimonio = activo - pasivo (calculado, no se carga)",
            )

    # ------------------------------------------------------------------
    # 9. Anualización
    # ------------------------------------------------------------------
    def _build_annual(self) -> None:
        cfg = self.cfg
        scopes = [scope_co()]
        scopes += [scope_bu(u.id) for u in cfg.business_units]
        scopes += [scope_br(b.id) for _, b in cfg.all_branches()]
        scopes += [scope_su(u.id) for u in cfg.support_units]
        stock_scopes: list[str] = []
        if cfg.inventory.enabled:
            for base, branch_ids in self._stock_scopes():
                stock_scopes.append(base)
                fam_ids = sorted({
                    f.id for bid in branch_ids for f in cfg.branch_owner(bid).families
                })
                stock_scopes += [scope_stock(base, f) for f in fam_ids]

        flows = ("SALES", "COGS", "GROSS_MARGIN", "EXPENSES", "EXPENSES_UNIT_LEVEL",
                 "EXPENSES_COMPANY_LEVEL", "CORPORATE_POOL", "PAYROLL", "PAYROLL_BASE", "EBITDA",
                 "ALLOCATED_EXPENSES", "RESULT_AFTER_ALLOCATION", "CAPEX", "CAPEX_OWN",
                 "COMMISSION", "PURCHASES", "COGS_FAMILY")
        for s in scopes + [x for x in stock_scopes if x not in scopes]:
            for metric in flows:
                keys = [nk(metric, s, p.code) for p in self.periods if self.g.has(nk(metric, s, p.code))]
                if keys:
                    self.g.calc(nk(metric, s, FY), keys, sum_of(keys), formula="acumulado del ejercicio")
            # dotación: final = último período; promedio = media de los meses
            hk = [nk("HEADCOUNT", s, p.code) for p in self.periods if self.g.has(nk("HEADCOUNT", s, p.code))]
            if hk:
                self.g.calc(nk("HEADCOUNT", s, FY), [hk[-1]], lambda v, k=hk[-1]: v.get(k),
                            formula="dotación final del ejercicio")
                self.g.calc(
                    nk("HEADCOUNT_AVG", s, FY), hk,
                    lambda v, keys=hk: (
                        None if all(v.get(k) is None for k in keys)
                        else sum((v.get(k) or ZERO) for k in keys) / Decimal(len(keys))
                    ),
                    formula="dotación promedio del ejercicio",
                )
            for p in self.periods:
                k = nk("HEADCOUNT", s, p.code)
                if self.g.has(k):
                    self.g.calc(nk("HEADCOUNT_AVG", s, p.code), [k], lambda v, k=k: v.get(k),
                                formula="dotación del período")

        seen: set[str] = set()
        for s in stock_scopes + scopes:
            if s in seen:
                continue
            seen.add(s)
            o_first = nk("OPENING_STOCK", s, self.periods[0].code)
            c_last = nk("CLOSING_STOCK", s, self.periods[-1].code)
            if self.g.has(o_first) and self.g.has(c_last):
                self.g.calc(nk("OPENING_STOCK", s, FY), [o_first], lambda v, k=o_first: v.get(k),
                            formula="stock al inicio del ejercicio")
                self.g.calc(nk("CLOSING_STOCK", s, FY), [c_last], lambda v, k=c_last: v.get(k),
                            formula="stock al cierre del ejercicio")
                self.g.calc(
                    nk("STOCK_AVG", s, FY), [o_first, c_last],
                    lambda v, o=o_first, c=c_last: (
                        None if v.get(o) is None or v.get(c) is None
                        else (v[o] + v[c]) / Decimal(2)
                    ),
                    formula="(stock inicial + stock final del ejercicio) / 2",
                )

    # ------------------------------------------------------------------
    # 10. Ratios
    # ------------------------------------------------------------------
    def _build_ratios(self) -> None:
        cfg = self.cfg
        level_scopes = {
            "COMPANY": [scope_co()],
            "BUSINESS_UNIT": [scope_bu(u.id) for u in cfg.business_units],
            "BRANCH": [scope_br(b.id) for _, b in cfg.all_branches()],
        }
        period_codes = self._all_period_codes() + [FY]

        for sel in cfg.ratios:
            ratio = RATIO_CATALOG.get(sel.ratio_code)
            if ratio is None:
                continue
            for level in ratio.levels:
                for scope in level_scopes.get(level, []):
                    for pc in period_codes:
                        if ratio.annual_only and pc != FY:
                            continue
                        metric_keys = {m: nk(m, scope, pc) for m in ratio.metrics}
                        # Balance sólo existe a nivel empresa y con tag FY/OPENING
                        deps = [k for k in metric_keys.values()]
                        days = self._days(pc)

                        def _fn(v, mk=metric_keys, r=ratio, days=days):
                            metrics = {name: v.get(key) for name, key in mk.items()}
                            return r.compute(metrics, days)

                        self.g.calc(nk(f"RATIO:{ratio.code}", scope, pc), deps, _fn,
                                    ratio=ratio.code, formula=ratio.formula_text,
                                    unit=ratio.unit.value, direction=ratio.direction.value)
