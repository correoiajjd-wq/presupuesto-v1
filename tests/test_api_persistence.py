"""Tests de la API REST y de la persistencia."""
from __future__ import annotations

import unittest
from decimal import Decimal

from app.api.main import create_app
from app.domain.engine import FY, scope_co
from app.domain.graph import nk
from app.services.budget import TaskStatus
from app.services.repository import Repository
from seed.demo import D, bootstrap

CFO = {"X-User": "u.cfo"}


class TestApi(unittest.TestCase):
    def setUp(self):
        self.service, self.budget, self.version = bootstrap()
        self.client = create_app(self.service).test_client()

    def test_sin_usuario_no_hay_acceso(self):
        r = self.client.get(f"/api/v1/budgets/{self.budget.id}")
        self.assertEqual(r.status_code, 403)

    def test_pnl(self):
        r = self.client.get(f"/api/v1/versions/{self.version.id}/reports/pnl", headers=CFO)
        self.assertEqual(r.status_code, 200)
        lines = {l["metric"]: Decimal(l["value"]) for l in r.get_json()["lines"]}
        self.assertEqual(lines["EBITDA"],
                         lines["GROSS_MARGIN"] - lines["EXPENSES"] - lines["PAYROLL"])

    def test_no_se_puede_cargar_un_calculado(self):
        """Doc 04 §23: no existe POST /inventory/closing-stock."""
        r = self.client.post(f"/api/v1/versions/{self.version.id}/inventory/closing-stock",
                             headers=CFO)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "CALCULATED_VALUE_NOT_EDITABLE")

    def test_scope_devuelve_403(self):
        r = self.client.post(f"/api/v1/versions/{self.version.id}/sales",
                             headers={"X-User": "u.br01"},
                             json={"business_unit_id": "BU-01", "branch_id": "BR-02",
                                   "product_id": "P-001", "period": "2027-07", "quantity": 10})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"]["code"], "UNAUTHORIZED_SCOPE")

    def test_frecuencia_invalida_devuelve_422(self):
        r = self.client.post(f"/api/v1/versions/{self.version.id}/sales", headers={"X-User": "u.br01"},
                             json={"business_unit_id": "BU-01", "branch_id": "BR-01",
                                   "product_id": "P-002", "period": "2027-02", "quantity": 10})
        self.assertEqual(r.status_code, 422)
        self.assertIn("2027-01", r.get_json()["error"]["message"])

    def test_version_aprobada_devuelve_409(self):
        for t in self.version.tasks.values():
            t.status = TaskStatus.APPROVED
        self.client.post(f"/api/v1/versions/{self.version.id}/approve", headers=CFO)
        r = self.client.put(
            f"/api/v1/versions/{self.version.id}/fx-rates/UYU/2027-01-15",
            headers=CFO, json={"rate_to_presentation": "0.026"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "VERSION_IMMUTABLE")

    def test_escenario_end_to_end(self):
        r = self.client.post(f"/api/v1/versions/{self.version.id}/scenarios", headers=CFO,
                             json={"name": "Optimista",
                                   "adjustments": [{"concept": "SALES", "variation": "0.10"}]})
        sid = r.get_json()["scenario_id"]
        r = self.client.post(f"/api/v1/versions/{self.version.id}/scenarios/{sid}/run", headers=CFO)
        rows = {x["metric"]: x for x in r.get_json()["comparison"]}
        self.assertGreater(Decimal(rows["EBITDA"]["scenario"]), Decimal(rows["EBITDA"]["base"]))

    def test_catalogo_de_ratios_publico(self):
        r = self.client.get("/api/v1/ratio-catalog")
        self.assertGreaterEqual(len(r.get_json()), 20)

    def test_auditoria_expuesta(self):
        self.client.put(f"/api/v1/versions/{self.version.id}/fx-rates/UYU/2027-01-15",
                        headers=CFO, json={"rate_to_presentation": "0.026"})
        r = self.client.get("/api/v1/audit-events", headers=CFO,
                            query_string={"entity": "FX_RATE"})
        self.assertTrue(r.get_json())
        self.assertEqual(r.get_json()[-1]["action"], "CHANGE_FX_RATE")


class TestPersistence(unittest.TestCase):
    def test_round_trip_conserva_el_calculo(self):
        service, budget, version = bootstrap()
        expected = version.calculate()[nk("EBITDA", scope_co(), FY)]
        repo = Repository(":memory:")
        repo.save_budget(budget, service.audit.events)
        reloaded = repo.load_budget(budget.id)
        rv = reloaded.versions[version.id]
        self.assertEqual(len(rv.inputs.values), len(version.inputs.values))
        self.assertEqual(rv.configuration.status, version.configuration.status)
        self.assertEqual(rv.calculate()[nk("EBITDA", scope_co(), FY)], expected)

    def test_snapshot_de_configuracion_es_de_la_version(self):
        """Doc 03 §45: una versión histórica no depende de la configuración actual."""
        service, budget, version = bootstrap()
        v2 = service.create_version("u.cfo", budget, version.id)
        v2.configuration.business_units[0].products[0].margin = D("0.50")
        v2.invalidate()
        self.assertNotEqual(
            v2.calculate()[nk("COGS", scope_co(), FY)],
            version.calculate()[nk("COGS", scope_co(), FY)],
        )

    def test_auditoria_es_append_only(self):
        service, budget, version = bootstrap()
        repo = Repository(":memory:")
        repo.save_budget(budget, service.audit.events)
        n = len(repo.audit_events())
        repo.save_budget(budget, service.audit.events)
        self.assertEqual(len(repo.audit_events()), 2 * n)


if __name__ == "__main__":
    unittest.main()
