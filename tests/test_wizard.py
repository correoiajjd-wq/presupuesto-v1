"""El primer paso real: el CFO y el COO arman el modelo desde cero.

Verifica el wizard de configuración de punta a punta — crear la empresa,
definir estructura, catálogo, gastos, nómina, módulos, ratios y workflow —
y que al cerrarlo el sistema quede listo para que la gente cargue.
"""
from __future__ import annotations

import unittest

from app.domain.config import ConfigStatus
from app.services.budget import BudgetError
from app.web.main import create_web_app
from seed.demo import D, bootstrap

CFO = {"user_id": "u.cfo"}


class WizardCase(unittest.TestCase):
    def setUp(self):
        self.service, self.budget, _ = bootstrap()
        self.client = create_web_app(self.service, self.budget.id).test_client()
        self.client.post("/login", data=CFO)

    def crear(self, **over):
        data = {"name": "Presupuesto 2028", "company_name": "Mi Empresa SA",
                "fiscal_year_start": "2028-01-01", "fiscal_year_end": "2028-12-31",
                "presentation_currency": "USD", "currencies": "USD, UYU"}
        data.update(over)
        r = self.client.post("/nuevo", data=data, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        return self.service.budgets[
            [b for b in self.service.budgets if self.service.budgets[b].name == data["name"]][0]
        ].latest

    def paso(self, step, action, data, expect=200):
        r = self.client.post(f"/configurar/{step}/{action}", data=data, follow_redirects=True)
        self.assertEqual(r.status_code, expect)
        return r

    def unidad(self, name="Retail", **extra):
        self.paso("estructura", "unidad", {"name": name, **extra})

    def sucursal(self, v, name, unit_id, **extra):
        """Alta en el catálogo de la empresa y después asignación, con selector."""
        self.paso("estructura", "sucursal", {"name": name, **extra})
        branch = next(b for b in v.configuration.branches if b.name == name)
        self.paso("estructura", "asignar",
                  {"branch_id": branch.id, "business_unit_id": unit_id})
        return branch

    def producto(self, unit_id, code, name, family_id, expect=200, **extra):
        data = {"business_unit_id": unit_id, "code": code, "name": name,
                "family_id": family_id, "sales_mode": "UNIT_BASED",
                "margin_formula": "PERCENTAGE_OF_SALES", "price": "10",
                "currency": "USD", "margin": "25", "sales_frequency": "MONTHLY"}
        data.update(extra)
        return self.paso("productos", "producto", data, expect)

    # ------------------------------------------------------------------
    def test_armar_una_empresa_completa_y_cerrar(self):
        v = self.crear()
        self.assertEqual(v.configuration.status, ConfigStatus.DRAFT)

        # ---- tipos de cambio: el ejercicio necesita TC de cada día
        self.paso("general", "fx", {"currency": "UYU", "start_rate": "40", "end_rate": "44"})
        rate_ini = v.fx.rate_on("UYU", v.configuration.fiscal_year_start)
        rate_fin = v.fx.rate_on("UYU", v.configuration.fiscal_year_end)
        self.assertAlmostEqual(float(1 / rate_ini), 40, places=4)
        self.assertAlmostEqual(float(1 / rate_fin), 44, places=4)
        self.assertGreater(rate_ini, rate_fin)   # el peso se deprecia -> vale menos USD

        # ---- estructura
        self.unidad("Retail")
        unit = v.configuration.business_units[0]
        self.sucursal(v, "Casa central", unit.id)
        self.sucursal(v, "Sucursal Este", unit.id, effective_from="2028-07-01")
        self.paso("estructura", "soporte", {"name": "Administración"})
        su = v.configuration.support_units[0]
        self.paso("estructura", "centro", {"support_unit_id": su.id, "name": "Contabilidad"})
        sucursales = v.configuration.unit_branches(unit.id)
        self.assertEqual(len(sucursales), 2)
        self.assertEqual(sucursales[1].effective_from.month, 7)

        # ---- catálogo (lo define el COO)
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "Bebidas"})
        fam = unit.families[0]
        self.producto(unit.id, "B001", "Gaseosa", fam.id, margin="35")
        self.producto(unit.id, "XX", "Otros", fam.id, margin="20", is_other="1")
        self.assertEqual(len(unit.products), 2)
        self.assertEqual(unit.products[0].margin, __import__("decimal").Decimal("0.35"))

        # ---- gastos
        self.paso("gastos", "gasto", {
            "name": "Alquiler", "target": [f"BRANCH:{sucursales[0].id}"],
            "currency": "USD", "frequency": "MONTHLY", "responsible_role": "ADMIN_AREA"})
        self.paso("gastos", "gasto", {
            "name": "Licencias", "target": ["COMPANY:"], "currency": "USD",
            "frequency": "ANNUAL", "responsible_role": "ADMIN_AREA"})
        self.assertEqual(len(v.configuration.expenses), 2)

        # ---- nómina
        self.paso("nomina", "area", {"name": "Ventas", "base_salary": "2000", "currency": "USD"})
        self.paso("nomina", "aumento", {"effective_date": "2028-04-01", "percentage": "6"})
        self.paso("nomina", "concepto", {"concept": "Cargas sociales", "percentage": "15"})
        self.assertEqual(float(v.configuration.payroll.charges_factor), 1.15)

        # ---- módulos
        self.paso("modulos", "modulos", {
            "capex_enabled": "1", "capex_frequency": "MONTHLY",
            "inventory_enabled": "1", "inventory_level": "BRANCH",
            "inventory_frequency": "QUARTERLY", "inventory_currency": "USD",
            "purchases_enabled": "1", "balance_enabled": "1", "balance_currency": "USD"})
        self.paso("modulos", "capex_categoria", {"name": "Equipamiento"})
        self.paso("modulos", "rubros_default", {})
        self.assertTrue(v.configuration.inventory.enabled)
        self.assertEqual(len(v.configuration.balance.items), 8)

        # ---- ratios con objetivo
        self.paso("ratios", "ratios", {
            "ratio": ["GROSS_MARGIN_PCT", "EBITDA_MARGIN_PCT", "STOCK_DAYS"],
            "objective_EBITDA_MARGIN_PCT": "12", "objective_type_EBITDA_MARGIN_PCT": "MINIMUM",
            "objective_STOCK_DAYS": "60", "objective_type_STOCK_DAYS": "MAXIMUM"})
        codes = [r.ratio_code for r in v.configuration.ratios]
        self.assertIn("STOCK_DAYS", codes)
        objetivo = next(r for r in v.configuration.ratios if r.ratio_code == "EBITDA_MARGIN_PCT")
        self.assertEqual(float(objetivo.objective.value), 0.12)   # 12 -> 0.12

        # ---- workflow
        self.paso("workflow", "workflow_default", {})
        self.assertEqual(len(v.configuration.workflow.steps), 6)

        # ---- cierre
        r = self.client.post("/configurar/cerrar", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(v.configuration.status, ConfigStatus.LOCKED)

        # el cierre genera las tareas de carga a partir de la configuración
        labels = [t.label for t in v.tasks.values()]
        self.assertTrue(any("Casa central" in x for x in labels))
        self.assertTrue(any("Sucursal Este" in x for x in labels))
        self.assertTrue(any(t.concept == "OPENING_STOCK" for t in v.tasks.values()))
        self.assertTrue(any(t.concept == "BALANCE" for t in v.tasks.values()))

        # y el modelo ya sabe qué datos va a exigir
        required = v.configuration.required_concepts()
        self.assertIn("OPENING_STOCK", required)
        self.assertIn("CAPEX", required)

    # ------------------------------------------------------------------
    def test_no_se_puede_cerrar_sin_tipo_de_cambio(self):
        """Doc 02 §30: el TC se carga para cada día del ejercicio."""
        v = self.crear()
        self.unidad("Retail")
        unit = v.configuration.business_units[0]
        self.sucursal(v, "Central", unit.id)
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "General"})
        self.producto(unit.id, "XX", "Otros", unit.families[0].id, is_other="1")
        r = self.client.post("/configurar/cerrar", follow_redirects=True)
        self.assertIn(b"MISSING_FX_RATE", r.data)
        self.assertEqual(v.configuration.status, ConfigStatus.DRAFT)

    def test_producto_sin_familia_se_rechaza(self):
        v = self.crear()
        self.unidad("Retail")
        unit = v.configuration.business_units[0]
        r = self.producto(unit.id, "A", "A", "")
        self.assertIn(b"INVALID_FAMILY", r.data)
        self.assertEqual(unit.products, [])

    def test_otros_es_por_familia_no_por_unidad(self):
        """El 'Otros' se controla por familia: cada una necesita el suyo."""
        v = self.crear()
        self.unidad("Retail")
        unit = v.configuration.business_units[0]
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "Bebidas"})
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "Comidas"})
        bebidas, comidas = unit.families
        self.producto(unit.id, "XX1", "Otros bebidas", bebidas.id, is_other="1")
        # la segunda familia tiene que poder tener su propio "Otros"
        self.producto(unit.id, "XX2", "Otros comidas", comidas.id, is_other="1")
        self.assertEqual(len(unit.products), 2)
        self.assertEqual(unit.missing_other_products(), [])
        # pero dos en la misma familia, no
        r = self.producto(unit.id, "XX3", "Otro más", bebidas.id, is_other="1")
        self.assertIn(b"ya tiene su producto", r.data)
        self.assertEqual(len(unit.products), 2)

    def test_distribucion_de_gasto_debe_sumar_cien(self):
        v = self.crear()
        for name in ("A", "B"):
            self.unidad(name)
        a, b = v.configuration.business_units
        base = {"name": "Seguros", "currency": "USD", "frequency": "ANNUAL",
                "allocation_mode": "PERCENTAGE",
                "target": [f"BUSINESS_UNIT:{a.id}", f"BUSINESS_UNIT:{b.id}"]}
        r = self.paso("gastos", "gasto", {
            **base, f"pct_BUSINESS_UNIT:{a.id}": "60", f"pct_BUSINESS_UNIT:{b.id}": "30"})
        self.assertIn(b"INVALID_ALLOCATION", r.data)
        self.assertEqual(v.configuration.expenses, [])
        self.paso("gastos", "gasto", {
            **base, f"pct_BUSINESS_UNIT:{a.id}": "60", f"pct_BUSINESS_UNIT:{b.id}": "40"})
        self.assertEqual(len(v.configuration.expenses), 1)

    def test_el_coo_no_puede_tocar_los_gastos(self):
        """Doc 01 §6: cada elemento de la configuración tiene su responsable."""
        v = self.crear()
        self.unidad("Retail")
        coo = self.client
        coo.post("/login", data={"user_id": "u.coo"})
        r = coo.post("/configurar/gastos/gasto", data={
            "name": "X", "target": ["COMPANY:"], "currency": "USD", "frequency": "MONTHLY"},
            follow_redirects=True)
        self.assertIn(b"UNAUTHORIZED", r.data)
        self.assertEqual(v.configuration.expenses, [])
        # pero sí puede definir el catálogo, que es lo suyo
        unit = v.configuration.business_units[0]
        r = coo.post("/configurar/productos/familia",
                     data={"business_unit_id": unit.id, "name": "Bebidas"},
                     follow_redirects=True)
        self.assertEqual(len(unit.families), 1)

    def test_configuracion_cerrada_no_se_modifica(self):
        """Doc 01 §7 / doc 04 §10: 409 CONFIGURATION_LOCKED."""
        service, budget, version = bootstrap()      # ya viene con la configuración cerrada
        client = create_web_app(service, budget.id).test_client()
        client.post("/login", data=CFO)
        r = client.post("/configurar/estructura/unidad", data={"name": "Nueva"},
                        follow_redirects=True)
        self.assertIn(b"CONFIGURATION_LOCKED", r.data)
        self.assertEqual(len(version.configuration.business_units), 2)

    def test_no_se_carga_hasta_cerrar_la_configuracion(self):
        """Doc 01 §5: el panel de carga no existe antes del cierre."""
        self.crear()
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/configurar", r.headers["Location"])


    # ------------------------------------------------------------------
    def test_asignar_responsables_y_cargar(self):
        """Doc 02 §56: cada uno ve y carga sólo lo suyo.

        Recorre el caso completo de una empresa nueva: el CFO arma el modelo,
        asigna a las personas, cierra, y cada responsable carga lo que le toca.
        """
        from app.domain.graph import nk

        v = self.crear(currencies="USD")
        self.unidad("Mayorista")
        unit = v.configuration.business_units[0]
        central = self.sucursal(v, "Central", unit.id)
        norte = self.sucursal(v, "Norte", unit.id)
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "Alimentos"})
        fam = unit.families[0].id
        self.producto(unit.id, "A001", "Harina", fam, price="20", margin="25")
        self.producto(unit.id, "XX", "Otros", fam, price="10", margin="18", is_other="1")
        # el alquiler existe en las dos sucursales: en Norte se cargará 0
        self.paso("gastos", "gasto", {
            "name": "Alquiler", "target": [f"BRANCH:{central.id}", f"BRANCH:{norte.id}"],
            "currency": "USD", "frequency": "MONTHLY", "responsible_role": "ADMIN_AREA"})
        self.paso("nomina", "area", {"name": "Ventas", "base_salary": "2200", "currency": "USD"})
        self.paso("nomina", "concepto", {"concept": "Cargas", "percentage": "18"})
        self.paso("ratios", "ratios", {"ratio": ["GROSS_MARGIN_PCT", "EBITDA_MARGIN_PCT"]})
        self.paso("workflow", "workflow_default", {})

        self.paso("workflow", "usuario", {
            "name": "Marcos Gerente", "role": ["UNIT_MANAGER", "PAYROLL_AREA"],
            "scope": [f"BR:{central.id}"]})
        self.paso("workflow", "usuario", {"name": "Elena Admin", "role": ["ADMIN_AREA"],
                                          "scope": []})
        self.assertIn("u.marcos", self.service.users)
        self.assertEqual(self.service.users["u.marcos"].scopes, {f"BR:{central.id}"})

        self.client.post("/configurar/cerrar", follow_redirects=True)
        self.assertEqual(v.configuration.status, ConfigStatus.LOCKED)

        # el gerente sólo ve las tareas de su sucursal
        gerente = create_web_app(self.service, self.budget.id).test_client()
        gerente.post("/login", data={"user_id": "u.marcos"})
        gerente.post("/versiones/seleccionar", data={"version_id": v.id})
        propias = [t for t in v.tasks.values()
                   if t.scope_key == f"BR:{central.id}"]
        self.assertEqual(len(propias), 2)          # ventas y dotación de Central

        t_ventas = next(t for t in propias if t.concept == "SALES")
        gerente.post(f"/tareas/{t_ventas.id}",
                     data={f"S~{unit.products[0].id}~{v.configuration.periods[0].code}": "1000",
                           f"S~{unit.products[1].id}~{v.configuration.periods[0].code}": "200"},
                     follow_redirects=True)
        t_dot = next(t for t in propias if t.concept == "PAYROLL_HEADCOUNT")
        gerente.post(f"/tareas/{t_dot.id}",
                     data={f"H~{v.configuration.payroll.areas[0].id}": "4"},
                     follow_redirects=True)

        # y no puede tocar la sucursal Norte
        t_norte = next(t for t in v.tasks.values()
                       if t.scope_key == f"BR:{norte.id}" and t.concept == "SALES")
        r = gerente.post(f"/tareas/{t_norte.id}",
                         data={f"S~{unit.products[0].id}~{v.configuration.periods[0].code}": "500"},
                         follow_redirects=True)
        self.assertIn(b"UNAUTHORIZED_SCOPE", r.data)

        admin = create_web_app(self.service, self.budget.id).test_client()
        admin.post("/login", data={"user_id": "u.elena"})
        admin.post("/versiones/seleccionar", data={"version_id": v.id})
        t_gastos = next(t for t in v.tasks.values() if t.concept == "EXPENSES")
        exp = v.configuration.expenses[0].id
        p0 = v.configuration.periods[0].code
        admin.post(f"/tareas/{t_gastos.id}",
                   data={f"E~{exp}~BR:{central.id}~{p0}": "3000",
                         f"E~{exp}~BR:{norte.id}~{p0}": "0"},
                   follow_redirects=True)

        # el motor ya calcula sobre el modelo que se armó a mano
        vals = v.calculate()
        self.assertEqual(vals[nk("SALES", "CO", p0)], D(22000))          # 1000x20 + 200x10
        self.assertEqual(vals[nk("COGS", "CO", p0)], D(16640))           # 20000x0.75 + 2000x0.82
        self.assertEqual(vals[nk("PAYROLL", "CO", p0)], D(10384))        # 4 x 2200 x 1.18
        self.assertEqual(vals[nk("EXPENSES", "CO", p0)], D(3000))
        self.assertEqual(vals[nk("EBITDA", "CO", p0)],
                         D(22000) - D(16640) - D(3000) - D(10384))

    def test_un_gerente_no_ve_el_wizard(self):
        gerente = create_web_app(self.service, self.budget.id).test_client()
        gerente.post("/login", data={"user_id": "u.br01"})
        r = gerente.post("/configurar/estructura/unidad", data={"name": "X"},
                         follow_redirects=True)
        self.assertIn(b"UNAUTHORIZED", r.data)


if __name__ == "__main__":
    unittest.main()
