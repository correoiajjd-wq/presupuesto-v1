"""El proceso completo, de una empresa vacía a la versión vigente.

Es la prueba que verifica que el sistema sirve para lo que existe: no que cada
pieza funcione por separado, sino que ocho personas con roles distintos puedan
recorrer el circuito entero sin que nadie quede trabado.
"""
from __future__ import annotations

import unittest

from app.domain.graph import nk
from app.web.forms import build_form
from app.web.main import create_web_app
from seed.demo import bootstrap


class CircuitoCompletoCase(unittest.TestCase):
    def setUp(self):
        self.service, demo, _ = bootstrap()
        self.app = create_web_app(self.service, demo.id)

    def cliente(self, user_id):
        c = self.app.test_client()
        c.post("/login", data={"user_id": user_id})
        return c

    def post(self, cliente, url, **data):
        r = cliente.post(url, data=data, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        cuerpo = r.data.decode()
        self.assertNotIn('<div class="flash error">', cuerpo,
                         f"{url} devolvió un error: {cuerpo[cuerpo.find('flash error'):][:200]}")
        return r

    def test_de_empresa_vacia_a_version_vigente(self):
        cfo = self.cliente("u.cfo")
        self.post(cfo, "/nuevo", name="Circuito", company_name="Circuito SA",
                  fiscal_year_start="2027-01-01", fiscal_year_end="2027-12-31",
                  presentation_currency="USD", currencies="USD")
        budget = next(b for b in self.service.budgets.values() if b.name == "Circuito")
        version = budget.latest
        cfg = version.configuration

        # --- el CFO arma la estructura -------------------------------------
        for nombre in ("Centro", "Norte"):
            self.post(cfo, "/configurar/estructura/sucursal", name=nombre)
        self.post(cfo, "/configurar/estructura/unidad", name="Retail")
        unidad = cfg.business_units[0]
        for branch in cfg.branches:
            self.post(cfo, "/configurar/estructura/operacion",
                      business_unit_id=unidad.id, branch_id=branch.id,
                      cost_center_name=f"Retail {branch.name}",
                      responsible_role="UNIT_MANAGER")
        self.post(cfo, "/configurar/estructura/soporte", name="Administración",
                  cost_center_name="Contabilidad", responsible_role="ADMIN_AREA")

        # --- el COO arma el catálogo, que es lo suyo ------------------------
        coo = self.cliente("u.coo")
        coo.post("/versiones/seleccionar", data={"version_id": version.id})
        self.post(coo, "/configurar/productos/familia",
                  business_unit_id=unidad.id, name="Alimentos")
        familia = unidad.families[0].id
        for code, name, is_other in (("A001", "Harina", ""), ("XX", "Otros", "1")):
            datos = {"business_unit_id": unidad.id, "code": code, "name": name,
                     "family_id": familia, "sales_mode": "UNIT_BASED",
                     "margin_formula": "PERCENTAGE_OF_SALES", "price": "20",
                     "currency": "USD", "margin": "25", "sales_frequency": "MONTHLY"}
            if is_other:
                datos["is_other"] = "1"
            self.post(coo, "/configurar/productos/producto", **datos)

        # --- el resto de la configuración ----------------------------------
        self.post(cfo, "/configurar/gastos/gasto", name="Alquiler",
                  target=[f"COST_CENTER:{o.cost_center.id}" for o in cfg.operations],
                  currency="USD", frequency="MONTHLY")
        self.post(cfo, "/configurar/gastos/gasto", name="Licencias",
                  target=["COMPANY:"], currency="USD", frequency="ANNUAL")
        self.post(cfo, "/configurar/nomina/nomina", currency="USD")
        self.post(cfo, "/configurar/nomina/concepto", concept="Cargas", percentage="18")
        self.post(cfo, "/configurar/ratios/ratios", ratio=["GROSS_MARGIN_PCT"])
        self.post(cfo, "/configurar/workflow/workflow_default")
        for nombre, rol, alcance in (
                ("Marcos Centro", "UNIT_MANAGER", [f"BR:{cfg.branches[0].id}"]),
                ("Nadia Norte", "UNIT_MANAGER", [f"BR:{cfg.branches[1].id}"]),
                ("Elena Admin", "ADMIN_AREA", []),
                ("Sofia Nomina", "PAYROLL_AREA", []),
                ("Diana Coo", "COO", [])):
            self.post(cfo, "/configurar/workflow/usuario",
                      name=nombre, role=[rol], scope=alcance)
        self.post(cfo, "/configurar/cerrar")
        self.assertEqual(cfg.status.value, "LOCKED")
        self.assertTrue(version.tasks)

        equipo = {uid: self.cliente(uid)
                  for uid in ("u.marcos", "u.nadia", "u.elena", "u.sofia", "u.diana")}

        # nadie queda encerrado al entrar
        for uid, cliente in equipo.items():
            cuerpo = cliente.get("/", follow_redirects=True).data.decode()
            self.assertNotIn("todavía se está armando", cuerpo, uid)

        def responsable(task):
            return next(c for uid, c in equipo.items()
                        if task.loader_role in self.service.users[uid].roles
                        and self.service.users[uid].has_scope(task.scope_key, cfg))

        # --- cada uno carga lo suyo ----------------------------------------
        periodos = [p.code for p in cfg.periods]
        for task in [t for t in version.tasks.values() if t.concept == "SALES"]:
            self.post(responsable(task), f"/tareas/{task.id}",
                      **{f"S~{p.id}~{per}": "100" for p in unidad.products for per in periodos})
        for task in [t for t in version.tasks.values() if t.concept == "PAYROLL_HEADCOUNT"]:
            self.post(responsable(task), f"/tareas/{task.id}/dotacion",
                      change_type="HIRED", quantity="2", effective_date="2027-06-01",
                      comment="dos vendedores")

        salarios = next(t for t in version.tasks.values() if t.concept == "PAYROLL_SALARY")
        datos = {}
        for cc, _kind, _label in cfg.cost_centers():
            datos[f"IH~{cc.id}"] = "3"
            datos[f"IN~{cc.id}"] = "6000"
        for iv in version.inputs.values:
            if iv.movement_id and iv.concept.value == "HEADCOUNT_CHANGE":
                datos[f"MV~{iv.movement_id}"] = "4000"
        self.post(responsable(salarios), f"/tareas/{salarios.id}", **datos)

        for task in [t for t in version.tasks.values() if t.concept == "EXPENSES"]:
            self.post(responsable(task), f"/tareas/{task.id}",
                      **{f.name: "500" for row in build_form(version, task).rows
                         for f in row.fields})

        # --- revisión y aprobación -----------------------------------------
        for task in list(version.tasks.values()):
            self.post(responsable(task), f"/tareas/{task.id}/enviar")
            self.assertEqual(task.status.value, "IN_REVIEW", task.label)
        for task in list(version.tasks.values()):
            self.post(cfo, f"/tareas/{task.id}/aprobar")
            self.assertEqual(task.status.value, "APPROVED", task.label)

        self.post(cfo, "/versiones/aprobar")
        self.post(cfo, "/versiones/vigente")
        self.assertEqual(version.status.value, "APPROVED")
        self.assertEqual(budget.current_version_id, version.id)
        self.assertGreater(version.calculate()[nk("SALES", "CO", "FY")], 0)


if __name__ == "__main__":
    unittest.main()
