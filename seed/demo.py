"""Empresa demo: recorre el criterio de aceptación global de la V1 (doc 02 §64).

ACME Distribución S.A. — ejercicio 2027
  - 2 unidades de negocio con modalidades de venta distintas (unidades y monto)
  - 3 sucursales, una de las cuales abre en junio
  - 1 unidad de soporte con centro de costo
  - gastos propios, de unidad distribuidos a sucursal, distribuidos por %,
    corporativos de empresa y de soporte
  - nómina con dotación inicial, altas, bajas, aumentos y comisiones
  - CAPEX, stock por familia, compras, balance inicial y proyectado
  - ratios con objetivos
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.config import (
    AllocationMode, BalanceConfig, BalanceItem, BalanceSection, Branch,
    BusinessUnit, CapexCategory, CapexConfig, Configuration, CostCenter, ExpenseDefinition,
    ExpenseTarget, ExpenseTargetType, InventoryConfig, InventoryLevel, MarginFormula, Objective,
    ObjectiveType, PayrollArea, PayrollConfig, PayrollPercentageConcept, Product, ProductFamily,
    RatioSelection, Role, SalaryIncreaseRule, SalesMode, SupportUnit, WorkflowConfig, WorkflowStep,
)
from app.domain.inputs import ChangeType, Concept, InputSet, InputValue
from app.domain.money import FXTable
from app.domain.periods import Frequency, Period
from app.services.budget import AuthorizationProvider, BudgetService, User

FY_START = date(2027, 1, 1)
FY_END = date(2027, 12, 31)


def D(x) -> Decimal:
    return Decimal(str(x))


# ==========================================================================
# Configuración
# ==========================================================================
def build_configuration() -> Configuration:
    repuestos = BusinessUnit(
        id="BU-01", name="Repuestos",
        families=[
            ProductFamily(id="FAM-REP", name="Repuestos mecánicos"),
            ProductFamily(id="FAM-ACC", name="Accesorios"),
        ],
        products=[
            Product(id="P-001", code="P001", name="Filtros", family_id="FAM-REP",
                    sales_mode=SalesMode.UNIT_BASED,
                    margin_formula=MarginFormula.PERCENTAGE_OF_SALES,
                    price=D(100), currency="USD", margin=D("0.30"),
                    sales_frequency=Frequency.MONTHLY),
            Product(id="P-002", code="P002", name="Frenos", family_id="FAM-REP",
                    sales_mode=SalesMode.UNIT_BASED,
                    margin_formula=MarginFormula.MARKUP_ON_COST,
                    price=D(250), currency="USD", margin=D("0.35"),
                    sales_frequency=Frequency.QUARTERLY),
            Product(id="P-003", code="XXREP", name="Otros repuestos", family_id="FAM-REP",
                    sales_mode=SalesMode.UNIT_BASED, price=D(80), currency="USD",
                    margin=D("0.25"), sales_frequency=Frequency.MONTHLY, is_other=True),
            Product(id="P-099", code="XXACC", name="Otros accesorios", family_id="FAM-ACC",
                    sales_mode=SalesMode.UNIT_BASED, price=D(50), currency="USD",
                    margin=D("0.20"), sales_frequency=Frequency.MONTHLY, is_other=True),
        ],
    )
    servicios = BusinessUnit(
        id="BU-02", name="Servicios",
        families=[ProductFamily(id="FAM-SVC", name="Servicios")],
        products=[
            Product(id="P-101", code="S001", name="Mantenimiento", family_id="FAM-SVC",
                    sales_mode=SalesMode.AMOUNT_BASED,
                    margin_formula=MarginFormula.PERCENTAGE_OF_SALES,
                    currency="UYU", margin=D("0.40"), sales_frequency=Frequency.MONTHLY,
                    commission_rate=D("0.02")),
            # Intangible: el precio de venta es todo margen, no tiene costo asociado.
            Product(id="P-102", code="S002", name="Consultoría", family_id="FAM-SVC",
                    sales_mode=SalesMode.AMOUNT_BASED, margin_formula=MarginFormula.NO_COST,
                    currency="UYU", margin=D(1), sales_frequency=Frequency.MONTHLY,
                    commission_rate=D("0.05")),   # la consultoría comisiona más
            Product(id="P-199", code="XXSVC", name="Otros servicios", family_id="FAM-SVC",
                    sales_mode=SalesMode.AMOUNT_BASED, currency="UYU",
                    margin=D("0.35"), sales_frequency=Frequency.MONTHLY, is_other=True),
        ],
    )
    branches = [
        Branch(id="BR-01", name="Montevideo", business_unit_id="BU-01"),
        Branch(id="BR-02", name="Salto", business_unit_id="BU-01",
               effective_from=date(2027, 6, 1)),
        Branch(id="BR-03", name="Centro", business_unit_id="BU-02"),
    ]
    soporte = SupportUnit(id="SU-01", name="Administración central",
                          cost_centers=[CostCenter(id="CC-01", name="Administración")])

    def target(kind: str, tid=None, pct=None, split=False) -> ExpenseTarget:
        return ExpenseTarget(target_type=ExpenseTargetType(kind), target_id=tid,
                             percentage=pct, distribute_to_branches=split)

    expenses = [
        # Alquiler: Montevideo y las oficinas son alquiladas; Salto es propia y
        # se carga 0. Un mismo concepto con varios destinos y montos distintos.
        ExpenseDefinition(id="EXP-01", name="Alquiler", currency="USD",
                          frequency=Frequency.MONTHLY,
                          targets=[target("BRANCH", "BR-01"), target("BRANCH", "BR-02"),
                                   target("COST_CENTER", "CC-01")]),
        # Internet: existe en todas las sucursales y en administración.
        ExpenseDefinition(id="EXP-02", name="Internet y comunicaciones", currency="USD",
                          frequency=Frequency.MONTHLY,
                          targets=[target("BRANCH", "BR-01"), target("BRANCH", "BR-02"),
                                   target("BRANCH", "BR-03"), target("COST_CENTER", "CC-01")]),
        # Marketing de la unidad, que se muestra repartido entre sus sucursales.
        ExpenseDefinition(id="EXP-03", name="Marketing Repuestos", currency="USD",
                          frequency=Frequency.QUARTERLY,
                          targets=[target("BUSINESS_UNIT", "BU-01", split=True)]),
        # Seguros: un único total que se reparte por porcentajes fijos.
        ExpenseDefinition(id="EXP-04", name="Seguros", currency="USD",
                          frequency=Frequency.ANNUAL,
                          allocation_mode=AllocationMode.PERCENTAGE,
                          targets=[target("BUSINESS_UNIT", "BU-01", pct=D("0.60")),
                                   target("BUSINESS_UNIT", "BU-02", pct=D("0.40"))]),
        ExpenseDefinition(id="EXP-05", name="Licencias corporativas", currency="USD",
                          frequency=Frequency.MONTHLY, targets=[target("COMPANY")]),
        ExpenseDefinition(id="EXP-06", name="Servicios administrativos", currency="UYU",
                          frequency=Frequency.MONTHLY,
                          targets=[target("COST_CENTER", "CC-01")]),
    ]

    payroll = PayrollConfig(
        areas=[
            PayrollArea(id="AR-VEN", name="Ventas", base_salary=D(2500), currency="USD"),
            PayrollArea(id="AR-TAL", name="Taller", base_salary=D(1800), currency="USD"),
            PayrollArea(id="AR-ADM", name="Administración", base_salary=D(80000), currency="UYU"),
        ],
        increase_rules=[
            SalaryIncreaseRule(effective_date=date(2027, 3, 1), percentage=D("0.05")),
            SalaryIncreaseRule(effective_date=date(2027, 8, 1), percentage=D("0.04")),
        ],
        percentage_concepts=[
            PayrollPercentageConcept(concept="Cargas sociales", percentage=D("0.12")),
            PayrollPercentageConcept(concept="Beneficios", percentage=D("0.05")),
        ],
    )

    balance = BalanceConfig(
        enabled=True, currency="USD",
        items=[
            BalanceItem(id="BI-CASH", name="Caja y bancos", section=BalanceSection.ASSET),
            BalanceItem(id="BI-AR", name="Créditos por ventas", section=BalanceSection.ASSET),
            BalanceItem(id="BI-STK", name="Bienes de cambio", section=BalanceSection.ASSET),
            BalanceItem(id="BI-PPE", name="Bienes de uso", section=BalanceSection.ASSET,
                        current=False),
            BalanceItem(id="BI-AP", name="Deudas comerciales", section=BalanceSection.LIABILITY),
            BalanceItem(id="BI-DEBT", name="Deuda financiera", section=BalanceSection.LIABILITY,
                        current=False),
            BalanceItem(id="BI-CAP", name="Capital", section=BalanceSection.EQUITY),
            BalanceItem(id="BI-RET", name="Resultados acumulados", section=BalanceSection.EQUITY),
        ],
    )

    ratios = [
        RatioSelection(ratio_code="GROSS_MARGIN_PCT"),
        RatioSelection(ratio_code="EBITDA_MARGIN_PCT",
                       objective=Objective(type=ObjectiveType.MINIMUM, value=D("0.15"))),
        RatioSelection(ratio_code="OPEX_PCT"),
        RatioSelection(ratio_code="PAYROLL_PCT",
                       objective=Objective(type=ObjectiveType.MAXIMUM, value=D("0.20"))),
        RatioSelection(ratio_code="CORPORATE_ALLOCATION_PCT"),
        RatioSelection(ratio_code="RESULT_AFTER_ALLOCATION_PCT"),
        RatioSelection(ratio_code="SALES_PER_HEAD"),
        RatioSelection(ratio_code="PAYROLL_COST_PER_HEAD"),
        RatioSelection(ratio_code="STOCK_TURNOVER"),
        RatioSelection(ratio_code="STOCK_DAYS",
                       objective=Objective(type=ObjectiveType.MAXIMUM, value=D(90))),
        RatioSelection(ratio_code="CAPEX_TO_SALES"),
        RatioSelection(ratio_code="CURRENT_RATIO",
                       objective=Objective(type=ObjectiveType.MINIMUM, value=D("1.2"))),
        RatioSelection(ratio_code="DEBT_TO_EQUITY"),
        RatioSelection(ratio_code="EQUITY_RATIO"),
    ]

    workflow = WorkflowConfig(steps=[
        WorkflowStep(concept="SALES", loader_role=Role.UNIT_MANAGER,
                     reviewer_role=Role.COO, approver_role=Role.CFO),
        WorkflowStep(concept="EXPENSES", loader_role=Role.ADMIN_AREA,
                     reviewer_role=Role.CFO, approver_role=Role.CFO),
        WorkflowStep(concept="PAYROLL_HEADCOUNT", loader_role=Role.PAYROLL_AREA,
                     reviewer_role=Role.CFO, approver_role=Role.CFO),
        WorkflowStep(concept="CAPEX", loader_role=Role.ADMIN_AREA,
                     reviewer_role=Role.CFO, approver_role=Role.CFO),
        WorkflowStep(concept="OPENING_STOCK", loader_role=Role.ADMIN_AREA,
                     reviewer_role=Role.CFO, approver_role=Role.CFO),
        WorkflowStep(concept="BALANCE", loader_role=Role.FINANCE_AREA,
                     reviewer_role=Role.CFO, approver_role=Role.CFO),
    ])

    return Configuration(
        company_name="ACME Distribución S.A.",
        fiscal_year_start=FY_START, fiscal_year_end=FY_END,
        presentation_currency="USD", enabled_currencies=["USD", "UYU", "ARS"],
        business_units=[repuestos, servicios], branches=branches,
        support_units=[soporte],
        expenses=expenses, payroll=payroll,
        capex=CapexConfig(enabled=True, frequency=Frequency.MONTHLY,
                          categories=[CapexCategory(id="CAT-01", name="Maquinaria"),
                                      CapexCategory(id="CAT-02", name="Tecnología")]),
        inventory=InventoryConfig(enabled=True, level=InventoryLevel.BRANCH,
                                  frequency=Frequency.QUARTERLY, currency="USD",
                                  purchases_enabled=True),
        balance=balance, ratios=ratios, workflow=workflow,
    )


def build_fx() -> FXTable:
    fx = FXTable("USD", ["USD", "UYU", "ARS"])
    fx.add_flat("UYU", FY_START, FY_END, D("0.025"))   # 1 UYU = 0.025 USD (TC 40)
    fx.add_flat("ARS", FY_START, FY_END, D("0.001"))
    return fx


# ==========================================================================
# Inputs
# ==========================================================================
def _expense(scope: str, expense_id: str, period: str, value: Decimal,
             currency: str) -> InputValue:
    """Un gasto se carga contra un destino concreto."""
    iv = InputValue(concept=Concept.EXPENSE_AMOUNT, period=period, value=value,
                    currency=currency, expense_id=expense_id)
    if scope.startswith("BR:"):
        iv.branch_id = scope.split(":", 1)[1]
    elif scope.startswith("BU:"):
        iv.business_unit_id = scope.split(":", 1)[1]
    elif scope.startswith("CC:"):
        iv.cost_center_id = scope.split(":", 1)[1]
    return iv


def build_inputs(cfg: Configuration) -> InputSet:
    s = InputSet()
    fy = cfg.fiscal_year
    months = fy.periods

    # ---- Ventas BU-01 (por unidades) ----
    monthly_qty = {
        ("BR-01", "P-001"): 2500, ("BR-01", "P-003"): 300, ("BR-01", "P-099"): 600,
        ("BR-02", "P-001"): 900, ("BR-02", "P-003"): 120, ("BR-02", "P-099"): 250,
    }
    quarterly_qty = {("BR-01", "P-002"): 400, ("BR-02", "P-002"): 150}
    for branch_id in ("BR-01", "BR-02"):
        branch = cfg.branch(branch_id)
        for p in months:
            if not cfg.is_active(branch, p):
                continue
            for (b, prod), q in monthly_qty.items():
                if b != branch_id:
                    continue
                s.add(InputValue(concept=Concept.SALES_QTY, period=p.code, value=D(q),
                                 business_unit_id="BU-01", branch_id=branch_id, product_id=prod))
        for head, bucket in fy.iter_buckets(Frequency.QUARTERLY):
            if not any(cfg.is_active(branch, p) for p in bucket):
                continue
            for (b, prod), q in quarterly_qty.items():
                if b != branch_id:
                    continue
                s.add(InputValue(concept=Concept.SALES_QTY, period=head.code, value=D(q),
                                 business_unit_id="BU-01", branch_id=branch_id, product_id=prod))

    # ---- Ventas BU-02 (por monto, en UYU) ----
    amounts = {"P-101": 12_000_000, "P-102": 1_500_000, "P-199": 1_200_000}
    for p in months:
        for prod, amount in amounts.items():
            s.add(InputValue(concept=Concept.SALES_AMOUNT, period=p.code, value=D(amount),
                             currency="UYU", business_unit_id="BU-02", branch_id="BR-03",
                             product_id=prod))

    # ---- Gastos: un importe por destino; 0 donde no corresponde ----
    alquiler = {"BR:BR-01": 8_000, "BR:BR-02": 0, "CC:CC-01": 2_500}   # Salto es propia
    internet = {"BR:BR-01": 600, "BR:BR-02": 350, "BR:BR-03": 500, "CC:CC-01": 400}
    for p in months:
        for scope, amount in alquiler.items():
            s.add(_expense(scope, "EXP-01", p.code, D(amount), "USD"))
        for scope, amount in internet.items():
            s.add(_expense(scope, "EXP-02", p.code, D(amount), "USD"))
        s.add(_expense("CO", "EXP-05", p.code, D(3_500), "USD"))
        s.add(_expense("CC:CC-01", "EXP-06", p.code, D(900_000), "UYU"))
    for head, _ in fy.iter_buckets(Frequency.QUARTERLY):
        s.add(_expense("BU:BU-01", "EXP-03", head.code, D(30_000), "USD"))
    s.add(_expense("CO", "EXP-04", months[0].code, D(48_000), "USD"))   # total, se reparte 60/40

    # ---- Nómina ----
    s.add(InputValue(concept=Concept.INITIAL_HEADCOUNT, value=D(5),
                     branch_id="BR-01", business_unit_id="BU-01", area_id="AR-VEN"))
    s.add(InputValue(concept=Concept.INITIAL_HEADCOUNT, value=D(3),
                     branch_id="BR-01", business_unit_id="BU-01", area_id="AR-TAL"))
    s.add(InputValue(concept=Concept.INITIAL_HEADCOUNT, value=D(4),
                     branch_id="BR-03", business_unit_id="BU-02", area_id="AR-VEN"))
    s.add(InputValue(concept=Concept.INITIAL_HEADCOUNT, value=D(3),
                     support_unit_id="SU-01", area_id="AR-ADM"))
    s.add(InputValue(concept=Concept.HEADCOUNT_CHANGE, value=D(2), change_type=ChangeType.HIRED,
                     effective_date=date(2027, 2, 1), branch_id="BR-01",
                     business_unit_id="BU-01", area_id="AR-VEN"))
    s.add(InputValue(concept=Concept.HEADCOUNT_CHANGE, value=D(2), change_type=ChangeType.HIRED,
                     effective_date=date(2027, 6, 1), branch_id="BR-02",
                     business_unit_id="BU-01", area_id="AR-VEN"))
    s.add(InputValue(concept=Concept.HEADCOUNT_CHANGE, value=D(1),
                     change_type=ChangeType.TERMINATED, effective_date=date(2027, 9, 1),
                     branch_id="BR-01", business_unit_id="BU-01", area_id="AR-TAL"))

    # ---- CAPEX ----
    s.add(InputValue(concept=Concept.CAPEX_AMOUNT, period="2027-06", value=D(250_000),
                     currency="USD", business_unit_id="BU-01", capex_category_id="CAT-01"))
    s.add(InputValue(concept=Concept.CAPEX_AMOUNT, period="2027-03", value=D(60_000),
                     currency="USD", support_unit_id="SU-01", capex_category_id="CAT-02"))

    # ---- Stock y compras (nivel sucursal, moneda USD) ----
    opening = {("BR-01", "FAM-REP"): 900_000, ("BR-01", "FAM-ACC"): 100_000,
               ("BR-02", "FAM-REP"): 250_000, ("BR-02", "FAM-ACC"): 40_000,
               ("BR-03", "FAM-SVC"): 60_000}
    for (branch_id, fam_id), amount in opening.items():
        s.add(InputValue(concept=Concept.OPENING_STOCK, value=D(amount), currency="USD",
                         branch_id=branch_id, family_id=fam_id))
    purchases = {("BR-01", "FAM-REP"): 620_000, ("BR-01", "FAM-ACC"): 74_000,
                 ("BR-02", "FAM-REP"): 145_000, ("BR-02", "FAM-ACC"): 20_000,
                 ("BR-03", "FAM-SVC"): 620_000}
    for head, _ in fy.iter_buckets(Frequency.QUARTERLY):
        for (branch_id, fam_id), amount in purchases.items():
            s.add(InputValue(concept=Concept.PURCHASES, period=head.code, value=D(amount),
                             currency="USD", branch_id=branch_id, family_id=fam_id))

    # ---- Balance ----
    opening_balance = {"BI-CASH": 300_000, "BI-AR": 500_000, "BI-STK": 1_350_000,
                       "BI-PPE": 2_000_000, "BI-AP": 700_000, "BI-DEBT": 1_100_000,
                       "BI-CAP": 1_500_000, "BI-RET": 850_000}
    projected_balance = {"BI-CASH": 420_000, "BI-AR": 560_000, "BI-STK": 1_500_000,
                         "BI-PPE": 2_250_000, "BI-AP": 780_000, "BI-DEBT": 950_000,
                         "BI-CAP": 1_500_000, "BI-RET": 1_500_000}
    for item_id, amount in opening_balance.items():
        s.add(InputValue(concept=Concept.BALANCE_OPENING, value=D(amount), currency="USD",
                         balance_item_id=item_id))
    for item_id, amount in projected_balance.items():
        s.add(InputValue(concept=Concept.BALANCE_PROJECTED, value=D(amount), currency="USD",
                         balance_item_id=item_id))
    return s


# ==========================================================================
# Armado completo
# ==========================================================================
def users() -> list[User]:
    return [
        User(id="u.cfo", name="Laura (CFO)", roles={Role.CFO}),
        User(id="u.coo", name="Diego (COO)", roles={Role.COO, Role.REVIEWER}),
        User(id="u.admin", name="Ana (Administración)", roles={Role.ADMIN_AREA}),
        User(id="u.payroll", name="Sofía (Nómina)", roles={Role.PAYROLL_AREA}),
        User(id="u.br01", name="Martín (Gerente Montevideo)", roles={Role.UNIT_MANAGER},
             scopes={"BR:BR-01"}),
        User(id="u.br02", name="Paula (Gerente Salto)", roles={Role.UNIT_MANAGER},
             scopes={"BR:BR-02"}),
        User(id="u.br03", name="Julián (Gerente Centro)", roles={Role.UNIT_MANAGER},
             scopes={"BR:BR-03"}),
        User(id="u.fin", name="Rodrigo (Finanzas)", roles={Role.FINANCE_AREA}),
    ]


def bootstrap(load_inputs: bool = True, close_config: bool = True):
    """Devuelve (service, budget, version) con la demo lista para calcular."""
    service = BudgetService(AuthorizationProvider())
    for u in users():
        service.register_user(u)
    cfg = build_configuration()
    budget = service.create_budget("u.cfo", "Presupuesto 2027", cfg, build_fx())
    version = budget.latest
    if close_config:
        service.close_configuration("u.cfo", version)
    if load_inputs:
        version.inputs = build_inputs(version.configuration)
        version.invalidate()
    return service, budget, version
