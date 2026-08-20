"""El primer paso real: el CFO y el COO arman el modelo desde cero.

Verifica el wizard de configuración de punta a punta — crear la empresa,
definir estructura, catálogo, gastos, nómina, módulos, ratios y workflow —
y que al cerrarlo el sistema quede listo para que la gente cargue.
"""
from __future__ import annotations

import unittest
from html.parser import HTMLParser

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

    def sucursal(self, v, name, unit_id=None, **extra):
        """Alta de la sucursal y, si se indica unidad, de la operación con su CC."""
        self.paso("estructura", "sucursal", {"name": name, **extra})
        branch = next(b for b in v.configuration.branches if b.name == name)
        if unit_id:
            self.operacion(v, unit_id, branch.id)
        return branch

    def operacion(self, v, unit_id, branch_id, expect=200, **extra):
        """La combinación unidad x sucursal, que siempre trae su centro de costo."""
        data = {"business_unit_id": unit_id, "branch_id": branch_id,
                "cost_center_name": f"Centro de costo {len(v.configuration.operations) + 1}"}
        data.update(extra)
        return self.paso("estructura", "operacion", data, expect)

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
        self.paso("estructura", "soporte", {"name": "Administración",
                                            "cost_center_name": "Administración central"})
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
        self.paso("nomina", "area", {"name": "Ventas"})
        self.paso("nomina", "aumento", {"effective_date": "2028-04-01", "percentage": "6"})
        self.paso("nomina", "concepto", {"concept": "Cargas sociales", "percentage": "15"})
        self.assertEqual(float(v.configuration.payroll.charges_factor), 1.15)

        # ---- módulos
        self.paso("modulos", "modulos", {
            "capex_enabled": "1", "capex_frequency": "MONTHLY",
            "inventory_enabled": "1", "inventory_level": "OPERATION",
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
        self.assertEqual(len(v.configuration.workflow.steps), 7)

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
        self.paso("nomina", "area", {"name": "Ventas"})
        self.paso("nomina", "concepto", {"concept": "Cargas", "percentage": "18"})
        self.paso("ratios", "ratios", {"ratio": ["GROSS_MARGIN_PCT", "EBITDA_MARGIN_PCT"]})
        self.paso("workflow", "workflow_default", {})

        self.paso("workflow", "usuario", {
            "name": "Marcos Gerente", "role": ["UNIT_MANAGER"],
            "scope": [f"BR:{central.id}"]})
        self.paso("workflow", "usuario", {"name": "Elena Admin", "role": ["ADMIN_AREA"],
                                          "scope": []})
        self.paso("workflow", "usuario", {"name": "Nadia Nomina", "role": ["PAYROLL_AREA"],
                                          "scope": []})
        self.assertIn("u.marcos", self.service.users)
        self.assertEqual(self.service.users["u.marcos"].scopes, {f"BR:{central.id}"})

        self.client.post("/configurar/cerrar", follow_redirects=True)
        self.assertEqual(v.configuration.status, ConfigStatus.LOCKED)

        # el gerente sólo ve las tareas de su sucursal
        gerente = create_web_app(self.service, self.budget.id).test_client()
        gerente.post("/login", data={"user_id": "u.marcos"})
        gerente.post("/versiones/seleccionar", data={"version_id": v.id})
        op_central = v.configuration.branch_operations(central.id)[0]
        propias = [t for t in v.tasks.values() if t.scope_key == f"OP:{op_central.id}"]
        self.assertEqual(len(propias), 2)          # ventas y dotación de Central
        # el alcance sobre la sucursal alcanza a la operación que vive ahí
        self.assertTrue(self.service.users["u.marcos"].has_scope(
            f"OP:{op_central.id}", v.configuration))

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
        op_norte = v.configuration.branch_operations(norte.id)[0]
        t_norte = next(t for t in v.tasks.values()
                       if t.scope_key == f"OP:{op_norte.id}" and t.concept == "SALES")
        r = gerente.post(f"/tareas/{t_norte.id}",
                         data={f"S~{unit.products[0].id}~{v.configuration.periods[0].code}": "500"},
                         follow_redirects=True)
        self.assertIn(b"UNAUTHORIZED_SCOPE", r.data)

        # los gastos de cada sucursal son su propia tarea
        admin = create_web_app(self.service, self.budget.id).test_client()
        admin.post("/login", data={"user_id": "u.elena"})
        admin.post("/versiones/seleccionar", data={"version_id": v.id})
        exp = v.configuration.expenses[0].id
        p0 = v.configuration.periods[0].code
        for sucursal, importe in ((central, "3000"), (norte, "0")):
            t_gastos = next(t for t in v.tasks.values()
                            if t.concept == "EXPENSES" and t.scope_key == f"BR:{sucursal.id}")
            admin.post(f"/tareas/{t_gastos.id}",
                       data={f"E~{exp}~BR:{sucursal.id}~{p0}": importe},
                       follow_redirects=True)

        # Nómina pone el valor de cada centro de costo
        nomina = create_web_app(self.service, self.budget.id).test_client()
        nomina.post("/login", data={"user_id": "u.nadia"})
        nomina.post("/versiones/seleccionar", data={"version_id": v.id})
        t_sal = next(t for t in v.tasks.values() if t.concept == "PAYROLL_SALARY")
        centros = [cc for cc, _k, _l in v.configuration.cost_centers()]
        self.assertEqual(len(centros), 2)          # una operación por sucursal
        nomina.post(f"/tareas/{t_sal.id}",
                    data={f"N~{cc.id}~{p0}": "8800" if i == 0 else "0"
                          for i, cc in enumerate(centros)},
                    follow_redirects=True)

        # el motor ya calcula sobre el modelo que se armó a mano
        vals = v.calculate()
        self.assertEqual(vals[nk("SALES", "CO", p0)], D(22000))          # 1000x20 + 200x10
        self.assertEqual(vals[nk("COGS", "CO", p0)], D(16640))           # 20000x0.75 + 2000x0.82
        self.assertEqual(vals[nk("PAYROLL", "CO", p0)], D(10384))        # 8800 x 1.18
        self.assertEqual(vals[nk("EXPENSES", "CO", p0)], D(3000))
        self.assertEqual(vals[nk("EBITDA", "CO", p0)],
                         D(22000) - D(16640) - D(3000) - D(10384))
        # y la dotación que informó el gerente sigue estando, sin calcular plata
        self.assertEqual(vals[nk("HEADCOUNT", "CO", p0)], D(4))

    def test_un_gerente_no_ve_el_wizard(self):
        gerente = create_web_app(self.service, self.budget.id).test_client()
        gerente.post("/login", data={"user_id": "u.br01"})
        r = gerente.post("/configurar/estructura/unidad", data={"name": "X"},
                         follow_redirects=True)
        self.assertIn(b"UNAUTHORIZED", r.data)


    # ------------------------------------------------------------------
    def test_relacion_n_a_n_entre_unidades_y_sucursales(self):
        """Una unidad opera en varias sucursales y una sucursal aloja varias
        unidades. Cada combinación es una operación con su propio centro de costo."""
        v = self.crear()
        self.unidad("Retail")
        self.unidad("Mayorista")
        cfg = v.configuration
        retail, mayorista = cfg.business_units
        for nombre in ("Centro", "Norte", "Sur"):
            self.paso("estructura", "sucursal", {"name": nombre})
        centro, norte, sur = cfg.branches

        # Retail opera en las tres
        for b in (centro, norte, sur):
            self.operacion(v, retail.id, b.id)
        self.assertEqual(len(cfg.unit_branches(retail.id)), 3)

        # y Mayorista también opera en Centro: la misma sucursal, dos unidades
        self.operacion(v, mayorista.id, centro.id)
        self.assertEqual({u.id for u in cfg.branch_units(centro.id)},
                         {retail.id, mayorista.id})
        self.assertEqual(len(cfg.operations), 4)

        # cada operación tiene su centro de costo, y ninguno se repite
        centros = [o.cost_center.name for o in cfg.operations]
        self.assertEqual(len(set(centros)), 4)

        # la misma combinación no se puede crear dos veces
        r = self.operacion(v, retail.id, centro.id)
        self.assertIn(b"DUPLICATE_OPERATION", r.data)
        self.assertEqual(len(cfg.operations), 4)

        # y borrar la operación no borra ni la unidad ni la sucursal
        op = cfg.operation_for(mayorista.id, centro.id)
        self.client.post(
            f"/configurar/estructura/borrar/operation/{op.id}", follow_redirects=True)
        self.assertEqual(len(cfg.operations), 3)
        self.assertEqual(len(cfg.branches), 3)
        self.assertEqual(len(cfg.business_units), 2)
        # Mayorista queda sin operar en ningún lado: eso no deja cerrar
        self.assertTrue(any("UNIT_WITHOUT_OPERATION" in e for e in cfg.validate_structure()))

    def test_cada_operacion_necesita_su_centro_de_costo(self):
        v = self.crear()
        self.unidad("Retail")
        self.paso("estructura", "sucursal", {"name": "Centro"})
        cfg = v.configuration
        r = self.paso("estructura", "operacion",
                      {"business_unit_id": cfg.business_units[0].id,
                       "branch_id": cfg.branches[0].id, "cost_center_name": ""})
        self.assertIn(b"MISSING_COST_CENTER", r.data)
        self.assertEqual(cfg.operations, [])

    def test_el_centro_de_costo_es_unico_en_toda_la_empresa(self):
        """El nombre es la identificación del centro de costo: no se repite ni
        entre operaciones, ni entre áreas de soporte, ni cruzado."""
        v = self.crear()
        self.unidad("Retail")
        cfg = v.configuration
        for nombre in ("Centro", "Norte"):
            self.paso("estructura", "sucursal", {"name": nombre})
        self.operacion(v, cfg.business_units[0].id, cfg.branches[0].id,
                       cost_center_name="Retail Centro")

        # otra operación no puede repetirlo
        r = self.operacion(v, cfg.business_units[0].id, cfg.branches[1].id,
                           cost_center_name="retail centro")
        self.assertIn(b"DUPLICATE_COST_CENTER_NAME", r.data)
        self.assertEqual(len(cfg.operations), 1)

        # y un área de soporte tampoco, aunque sea otro tipo de dueño
        r = self.paso("estructura", "soporte",
                      {"name": "Administración", "cost_center_name": "Retail Centro"})
        self.assertIn(b"DUPLICATE_COST_CENTER_NAME", r.data)
        self.assertEqual(cfg.support_units, [])

    def test_el_area_de_soporte_nace_con_su_centro_de_costo(self):
        """No existe el estado intermedio de un área sin centro de costo."""
        v = self.crear()
        cfg = v.configuration
        r = self.paso("estructura", "soporte", {"name": "Administración"})
        self.assertIn(b"MISSING_COST_CENTER", r.data)
        self.assertEqual(cfg.support_units, [])

        self.paso("estructura", "soporte",
                  {"name": "Administración", "cost_center_name": "Contabilidad"})
        su = cfg.support_units[0]
        self.assertEqual([c.name for c in su.cost_centers], ["Contabilidad"])

        # después se le pueden agregar más
        self.paso("estructura", "centro", {"support_unit_id": su.id, "name": "Sistemas"})
        self.assertEqual([c.name for c in su.cost_centers], ["Contabilidad", "Sistemas"])

    def test_el_codigo_de_producto_es_unico_en_toda_la_empresa(self):
        """El código es lo que se escribe en la planilla de carga, donde no hay
        unidad ni familia que lo desambigüe."""
        v = self.crear()
        self.unidad("Retail")
        self.unidad("Mayorista")
        retail, mayorista = v.configuration.business_units
        for u in (retail, mayorista):
            self.paso("productos", "familia", {"business_unit_id": u.id, "name": "General"})
        self.paso("productos", "familia", {"business_unit_id": retail.id, "name": "Otra"})
        self.producto(retail.id, "A001", "Harina", retail.families[0].id)

        # el mismo código en otra familia de la misma unidad
        r = self.producto(retail.id, "A001", "Otra cosa", retail.families[1].id)
        self.assertIn(b"DUPLICATE_PRODUCT_CODE", r.data)
        # y en otra unidad de negocio
        r = self.producto(mayorista.id, "a001", "Harina mayorista", mayorista.families[0].id)
        self.assertIn(b"DUPLICATE_PRODUCT_CODE", r.data)
        self.assertEqual(len(retail.products) + len(mayorista.products), 1)

    def test_no_se_repite_el_nombre_de_una_sucursal(self):
        v = self.crear()
        self.paso("estructura", "sucursal", {"name": "Centro"})
        r = self.paso("estructura", "sucursal", {"name": "centro"})
        self.assertIn(b"DUPLICATE_BRANCH_NAME", r.data)
        self.assertEqual(len(v.configuration.branches), 1)

    def test_comision_por_producto(self):
        """La comisión es de cada producto, no de la unidad."""
        v = self.crear()
        self.unidad("Servicios")
        unit = v.configuration.business_units[0]
        self.sucursal(v, "Centro", unit.id)
        self.paso("productos", "familia", {"business_unit_id": unit.id, "name": "General"})
        fam = unit.families[0].id
        self.producto(unit.id, "S001", "Mantenimiento", fam, commission_rate="2")
        self.producto(unit.id, "XX", "Otros", fam, is_other="1")
        con, sin = unit.products
        self.assertEqual(float(con.commission_rate), 0.02)
        self.assertIsNone(sin.commission_rate)


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
# Lo que manda el navegador, no lo que manda el test
# ==========================================================================
class BrowserForm:
    """Los valores por defecto de un formulario, como los enviaría un navegador.

    Los tests que postean datos explícitos no ven una clase entera de errores:
    la que aparece cuando el usuario envía el formulario sin tocar un campo y
    el valor por defecto de ese campo hace algo destructivo.

    Los campos que se repiten (checkboxes con el mismo nombre, selects
    múltiples) se envían como lista, igual que hace un navegador.
    """

    def __init__(self, action: str):
        self.action = action
        self.data: dict[str, object] = {}

    def add(self, name: str, value: str, multiple: bool) -> None:
        if multiple:
            self.data.setdefault(name, [])
            if isinstance(self.data[name], list):
                self.data[name].append(value)
        else:
            self.data[name] = value


class FormScraper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[BrowserForm] = []
        self._current: BrowserForm | None = None
        self._select: str | None = None
        self._select_multiple = False
        self._select_options: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._current = BrowserForm(a.get("action", ""))
            self.forms.append(self._current)
        elif self._current is None:
            return
        elif tag == "input" and a.get("name"):
            tipo = a.get("type")
            if tipo in (None, "hidden", "text", "number", "date"):
                self._current.add(a["name"], a.get("value", ""), False)
            elif tipo == "checkbox" and "checked" in a:
                self._current.add(a["name"], a.get("value", "on"), True)
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self._select_multiple = "multiple" in a
            self._select_options = []
        elif tag == "option" and self._select:
            self._select_options.append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None
        elif tag == "select" and self._select and self._current is not None:
            marcadas = [v for v, sel in self._select_options if sel]
            if self._select_multiple:
                for v in marcadas:
                    self._current.add(self._select, v, True)
            else:
                # un navegador manda la opción marcada; si no hay, la primera
                elegida = marcadas[0] if marcadas else (
                    self._select_options[0][0] if self._select_options else "")
                self._current.add(self._select, elegida, False)
            self._select = None


def forms_in(html: str, action_contains: str) -> list[BrowserForm]:
    s = FormScraper()
    s.feed(html)
    return [f for f in s.forms if action_contains in f.action]


class BrowserDefaultsCase(unittest.TestCase):
    """Reenviar un formulario sin tocar nada no puede romper lo que ya está."""

    def setUp(self):
        self.service, self.budget, _ = bootstrap()
        self.client = create_web_app(self.service, self.budget.id).test_client()
        self.client.post("/login", data=CFO)
        self.client.post("/nuevo", data={
            "name": "Browser", "company_name": "Browser SA",
            "fiscal_year_start": "2028-01-01", "fiscal_year_end": "2028-12-31",
            "presentation_currency": "USD", "currencies": "USD"}, follow_redirects=True)
        self.v = next(b for b in self.service.budgets.values()
                      if b.name == "Browser").latest

    def post(self, action, data):
        return self.client.post(f"/configurar/estructura/{action}", data=data,
                                follow_redirects=True)

    def operacion(self, unit_id, branch_id, nombre_cc):
        return self.post("operacion", {"business_unit_id": unit_id, "branch_id": branch_id,
                                       "cost_center_name": nombre_cc})

    def test_crear_una_operacion_no_toca_las_anteriores(self):
        """El caso que se rompió con la asignación: dar de alta la segunda
        combinación no puede deshacer la primera."""
        self.post("unidad", {"name": "Retail"})
        unit = self.v.configuration.business_units[0]
        self.post("sucursal", {"name": "Centro"})
        self.post("sucursal", {"name": "Norte"})
        centro, norte = self.v.configuration.branches
        self.operacion(unit.id, centro.id, "Retail Centro")
        self.operacion(unit.id, norte.id, "Retail Norte")
        pares = lambda: [(o.business_unit_id, o.branch_id) for o in self.v.configuration.operations]
        self.assertEqual(pares(), [(unit.id, centro.id), (unit.id, norte.id)])

        # y ahora lo que hace un navegador: reenviar el formulario tal como está
        html = self.client.get("/configurar/estructura").data.decode()
        for f in forms_in(html, "/operacion"):
            self.client.post(f.action, data=f.data, follow_redirects=True)
        self.assertEqual(pares(), [(unit.id, centro.id), (unit.id, norte.id)])

    def test_el_formulario_de_operacion_vacio_no_crea_nada(self):
        """Viene con los selectores en la primera opción y el código vacío:
        reenviarlo sin escribir nada tiene que fallar, no crear una operación."""
        self.post("unidad", {"name": "Retail"})
        self.post("sucursal", {"name": "Centro"})
        html = self.client.get("/configurar/estructura").data.decode()
        formularios = forms_in(html, "/operacion")
        self.assertEqual(len(formularios), 1)
        r = self.client.post(formularios[0].action, data=formularios[0].data,
                             follow_redirects=True)
        self.assertIn(b"MISSING_COST_CENTER", r.data)
        self.assertEqual(self.v.configuration.operations, [])

    def test_los_selectores_traen_todas_las_opciones(self):
        """Se elige de un selector para no equivocarse escribiendo el nombre."""
        for nombre in ("Retail", "Mayorista"):
            self.post("unidad", {"name": nombre})
        for nombre in ("Centro", "Norte"):
            self.post("sucursal", {"name": nombre})
        html = self.client.get("/configurar/estructura").data.decode()
        for u in self.v.configuration.business_units:
            self.assertIn(f'value="{u.id}"', html)
        for b in self.v.configuration.branches:
            self.assertIn(f'value="{b.id}"', html)

    def test_borrar_una_sucursal_se_lleva_sus_operaciones(self):
        self.post("unidad", {"name": "Retail"})
        unit = self.v.configuration.business_units[0]
        self.post("sucursal", {"name": "Centro"})
        centro = self.v.configuration.branches[0]
        self.operacion(unit.id, centro.id, "Retail Centro")
        self.assertEqual(len(self.v.configuration.operations), 1)
        self.client.post(f"/configurar/estructura/borrar/branch/{centro.id}",
                         follow_redirects=True)
        self.assertEqual(self.v.configuration.operations, [])
        self.assertEqual(len(self.v.configuration.business_units), 1)


class IdempotenciaCase(unittest.TestCase):
    """Reenviar cualquier formulario del wizard, tal como viene, no cambia nada.

    Es la garantía general contra el error que se coló en la asignación de
    sucursales: un valor por defecto que hace algo que el usuario no pidió.
    """

    def setUp(self):
        self.service, self.budget, self.version = bootstrap(close_config=False)
        self.client = create_web_app(self.service, self.budget.id).test_client()
        self.client.post("/login", data=CFO)

    def _foto(self):
        cfg = self.version.configuration
        return {
            "unidades": [u.id for u in cfg.business_units],
            "sucursales": [b.id for b in cfg.branches],
            "operaciones": [(o.id, o.business_unit_id, o.branch_id, o.cost_center.name)
                            for o in cfg.operations],
            "soporte": [(s.id, [(c.id, c.name) for c in s.cost_centers])
                        for s in cfg.support_units],
            "familias": [f.id for u in cfg.business_units for f in u.families],
            "productos": [(p.id, str(p.margin), str(p.commission_rate))
                          for u in cfg.business_units for p in u.products],
            "gastos": [(e.id, [t.scope_key for t in e.targets]) for e in cfg.expenses],
            "nomina": [a.id for a in cfg.payroll.areas],
            "nomina_carga": (cfg.payroll.currency, cfg.payroll.frequency.value),
            "aumentos": [str(r.effective_date) for r in cfg.payroll.increase_rules],
            "ratios": [(r.ratio_code, str(r.objective.value) if r.objective else None)
                       for r in cfg.ratios],
            "workflow": [(s.concept, s.loader_role.value, s.approver_role.value)
                         for s in cfg.workflow.steps],
            "modulos": (cfg.capex.enabled, cfg.inventory.enabled, cfg.balance.enabled,
                        cfg.inventory.level.value, [i.id for i in cfg.balance.items]),
        }

    def test_reenviar_todos_los_formularios_no_cambia_la_configuracion(self):
        antes = self._foto()
        for step in ("general", "estructura", "productos", "gastos", "nomina",
                     "modulos", "ratios", "workflow"):
            html = self.client.get(f"/configurar/{step}").data.decode()
            for f in forms_in(html, "/configurar/"):
                if "borrar" in f.action or "default" in f.action:
                    continue          # esos sí son acciones deliberadas
                self.client.post(f.action, data=f.data, follow_redirects=True)
        self.assertEqual(self._foto(), antes)

    def test_los_formularios_de_alta_vacios_no_crean_nada(self):
        cfg = self.version.configuration
        cuantas = len(cfg.branches)
        r = self.client.post("/configurar/estructura/sucursal", data={"name": "  "},
                             follow_redirects=True)
        self.assertIn(b"MISSING_NAME", r.data)
        self.assertEqual(len(cfg.branches), cuantas)

        cuantas = len(cfg.business_units)
        self.client.post("/configurar/estructura/unidad", data={"name": ""},
                         follow_redirects=True)
        self.assertEqual(len(cfg.business_units), cuantas)
