"""API REST (doc 04).

La API expresa casos de uso, no CRUD sobre tablas. Los cálculos están
centralizados en el Calculation Engine: ningún endpoint calcula nada por su
cuenta, y ninguno permite escribir un valor calculado.

Se implementa sobre Flask por disponibilidad en el entorno; el contrato es el
del documento 04 y es portable a FastAPI sin tocar servicios ni dominio.

    PYTHONPATH=. python3 -m app.api.main
    curl -H 'X-User: u.cfo' localhost:8000/api/v1/budgets
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Optional

from flask import Flask, jsonify, request, send_file
from io import BytesIO

from ..domain.config import Configuration
from ..domain.engine import FY, scope_co
from ..domain.graph import nk
from ..domain.inputs import Concept, InputValue
from ..domain.ratios import RATIO_CATALOG
from ..services import reporting
from ..services.budget import (
    BudgetError, BudgetService, Scenario, ScenarioAdjustment, TaskStatus,
)
from ..services.import_export import (
    commit_import, parse_sales_import, sales_template,
)
from ..services.scenarios import compare, run_scenario

HTTP_STATUS = {
    "UNAUTHORIZED": 403,
    "UNAUTHORIZED_SCOPE": 403,
    "NOT_FOUND": 404,
    "CONFIGURATION_LOCKED": 409,
    "CONFIGURATION_NOT_CLOSED": 409,
    "VERSION_IMMUTABLE": 409,
    "VERSION_NOT_APPROVED": 409,
    "WORKFLOW_INVALID_TRANSITION": 409,
    "CONCURRENT_MODIFICATION": 409,
    "PENDING_APPROVALS": 409,
    "CALCULATED_VALUE_NOT_EDITABLE": 409,
    "BALANCE_NOT_BALANCED": 422,
    "INPUT_VALIDATION_FAILED": 422,
    "IMPORT_VALIDATION_FAILED": 422,
    "INCOMPLETE_CONFIGURATION": 422,
    "VERSION_NOT_READY": 422,
}


def _j(obj):
    """Serializa Decimals como string: nunca float en valores monetarios."""
    return json.loads(json.dumps(obj, default=lambda o: str(o) if isinstance(o, Decimal)
                                 else (o.isoformat() if hasattr(o, "isoformat") else str(o))))


def create_app(service: BudgetService) -> Flask:
    app = Flask(__name__)

    # ------------------------------------------------------------ contexto
    def actor() -> str:
        user = request.headers.get("X-User")
        if not user:
            raise BudgetError("UNAUTHORIZED", "Falta el encabezado X-User")
        service.user(user)
        return user

    def find_version(version_id: str):
        for b in service.budgets.values():
            if version_id in b.versions:
                return b, b.versions[version_id]
        raise BudgetError("NOT_FOUND", f"versión {version_id} inexistente")

    @app.errorhandler(BudgetError)
    def _handle(err: BudgetError):
        return jsonify({"error": {
            "code": err.code, "message": str(err), "details": _j(err.details),
            "correlation_id": request.headers.get("X-Correlation-ID"),
        }}), HTTP_STATUS.get(err.code, 400)

    # ------------------------------------------------------------ catálogos
    @app.get("/api/v1/ratio-catalog")
    def ratio_catalog():
        return jsonify([{
            "code": r.code, "name": r.name, "group": r.group.value,
            "formula": r.formula_text, "unit": r.unit.value,
            "direction": r.direction.value, "levels": list(r.levels),
            "requires": list(r.required_inputs), "notes": r.notes,
        } for r in RATIO_CATALOG.values()])

    # ------------------------------------------------------------ presupuesto
    @app.get("/api/v1/budgets")
    def list_budgets():
        actor()
        return jsonify([{
            "budget_id": b.id, "name": b.name,
            "current_version_id": b.current_version_id,
            "versions": [{"version_id": v.id, "number": v.number, "status": v.status.value}
                         for v in b.versions.values()],
        } for b in service.budgets.values()])

    @app.get("/api/v1/budgets/<budget_id>")
    def get_budget(budget_id):
        actor()
        b = service.budget(budget_id)
        v = b.latest
        report = service.validate_version(v)
        return jsonify(_j({
            "budget_id": b.id, "name": b.name, "current_version_id": b.current_version_id,
            "version": {"id": v.id, "number": v.number, "status": v.status.value,
                        "configuration_status": v.configuration.status.value},
            "fiscal_year": {"start": v.configuration.fiscal_year_start,
                            "end": v.configuration.fiscal_year_end,
                            "presentation_currency": v.configuration.presentation_currency},
            "progress": {
                "tasks_total": len(v.tasks),
                "tasks_approved": sum(1 for t in v.tasks.values()
                                      if t.status is TaskStatus.APPROVED),
            },
            "blocking_findings": [f.message for f in report["blocking"]][:20],
            "alerts": [a.message for a in report["alerts"]][:20],
        }))

    @app.get("/api/v1/budgets/<budget_id>/configuration")
    def get_configuration(budget_id):
        actor()
        v = service.budget(budget_id).latest
        return jsonify({"status": v.configuration.status.value,
                        "checklist": reporting.configuration_checklist(v.configuration),
                        "configuration": json.loads(v.configuration.model_dump_json())})

    @app.post("/api/v1/budgets/<budget_id>/configuration/close")
    def close_configuration(budget_id):
        v = service.budget(budget_id).latest
        service.close_configuration(actor(), v)
        return jsonify({"status": v.configuration.status.value,
                        "tasks_created": len(v.tasks)})

    # ------------------------------------------------------------ inputs
    @app.post("/api/v1/versions/<version_id>/sales")
    def load_sales(version_id):
        _, v = find_version(version_id)
        body = request.get_json(force=True)
        cfg = v.configuration
        # La venta se carga en la operación: la combinación unidad x sucursal.
        op = (cfg.operation(body["operation_id"]) if body.get("operation_id")
              else cfg.operation_for(body["business_unit_id"], body["branch_id"]))
        if op is None:
            raise BudgetError("INVALID_OPERATION",
                              "Esa unidad de negocio no opera en esa sucursal.")
        concept = Concept.SALES_QTY if "quantity" in body else Concept.SALES_AMOUNT
        iv = InputValue(
            concept=concept, period=body["period"],
            value=Decimal(str(body.get("quantity", body.get("amount", 0)))),
            currency=body.get("currency"), operation_id=op.id,
            business_unit_id=op.business_unit_id, branch_id=op.branch_id,
            product_id=body["product_id"],
        )
        service.submit_input(actor(), v, iv, "budget.sales.load")
        return jsonify({"status": "ACCEPTED", "identity": iv.identity()}), 201

    @app.post("/api/v1/versions/<version_id>/expenses")
    def load_expense(version_id):
        _, v = find_version(version_id)
        body = request.get_json(force=True)
        iv = InputValue(concept=Concept.EXPENSE_AMOUNT, period=body["period"],
                        value=Decimal(str(body["amount"])), currency=body.get("currency"),
                        expense_id=body["expense_id"])
        service.submit_input(actor(), v, iv, "budget.expense.load")
        return jsonify({"status": "ACCEPTED"}), 201

    @app.post("/api/v1/versions/<version_id>/inventory/closing-stock")
    def closing_stock_denied(version_id):
        raise BudgetError("CALCULATED_VALUE_NOT_EDITABLE",
                          "El stock final es calculado: stock anterior + compras - costo de venta.")

    # ------------------------------------------------------------ plantillas
    @app.get("/api/v1/versions/<version_id>/sales/input-template")
    def input_template(version_id):
        actor()
        _, v = find_version(version_id)
        operation_id = request.args["operation_id"]
        data = sales_template(v, operation_id)
        return send_file(BytesIO(data), download_name=f"ventas_{operation_id}.xlsx",
                         as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument."
                                  "spreadsheetml.sheet")

    @app.post("/api/v1/versions/<version_id>/imports")
    def create_import(version_id):
        user = actor()
        _, v = find_version(version_id)
        operation_id = request.args["operation_id"]
        file = request.files["file"]
        result, parsed = parse_sales_import(v, file.read(), operation_id, user)
        if result.status == "REJECTED":
            return jsonify({"error": {"code": "IMPORT_VALIDATION_FAILED",
                                      "message": "La planilla fue rechazada por completo. "
                                                 "No se incorporó ningún dato.",
                                      "details": result.as_dict()}}), 422
        commit_import(service, user, v, parsed)
        return jsonify(result.as_dict()), 201

    # ------------------------------------------------------------ workflow
    @app.get("/api/v1/versions/<version_id>/tasks")
    def list_tasks(version_id):
        user = service.user(actor())
        _, v = find_version(version_id)
        return jsonify([{
            "task_id": t.id, "concept": t.concept, "scope": t.scope_key,
            "label": t.label, "status": t.status.value,
        } for t in service.tasks_for(v, user)])

    @app.post("/api/v1/versions/<version_id>/tasks/<task_id>/submit")
    def submit_task(version_id, task_id):
        _, v = find_version(version_id)
        t = service.submit_task(actor(), v, task_id)
        return jsonify({"task_id": t.id, "status": t.status.value})

    @app.post("/api/v1/versions/<version_id>/tasks/<task_id>/approve")
    def approve_task(version_id, task_id):
        _, v = find_version(version_id)
        t = service.approve_task(actor(), v, task_id)
        return jsonify({"task_id": t.id, "status": t.status.value})

    @app.post("/api/v1/versions/<version_id>/tasks/<task_id>/reject")
    def reject_task(version_id, task_id):
        _, v = find_version(version_id)
        body = request.get_json(force=True) or {}
        t = service.reject_task(actor(), v, task_id, body.get("comment", ""))
        return jsonify({"task_id": t.id, "status": t.status.value})

    # ------------------------------------------------------------ versiones
    @app.post("/api/v1/versions/<version_id>/approve")
    def approve_version(version_id):
        _, v = find_version(version_id)
        service.approve_version(actor(), v)
        return jsonify({"version_id": v.id, "status": v.status.value})

    @app.post("/api/v1/versions/<version_id>/set-current")
    def set_current(version_id):
        b, _ = find_version(version_id)
        service.set_current(actor(), b, version_id)
        return jsonify({"budget_id": b.id, "current_version_id": b.current_version_id})

    @app.post("/api/v1/budgets/<budget_id>/versions")
    def create_version(budget_id):
        b = service.budget(budget_id)
        body = request.get_json(force=True)
        v = service.create_version(actor(), b, body["source_version_id"])
        return jsonify({"version_id": v.id, "number": v.number, "status": v.status.value}), 201

    @app.put("/api/v1/versions/<version_id>/fx-rates/<currency>/<rate_date>")
    def set_fx(version_id, currency, rate_date):
        _, v = find_version(version_id)
        body = request.get_json(force=True)
        out = service.change_fx_rate(actor(), v, currency, date.fromisoformat(rate_date),
                                     Decimal(str(body["rate_to_presentation"])))
        impact = v.impact_of([])   # el grafo se reconstruye; informamos el alcance
        return jsonify(_j({**out, "recalculated": impact}))

    # ------------------------------------------------------------ reportes
    @app.get("/api/v1/versions/<version_id>/reports/pnl")
    def report_pnl(version_id):
        actor()
        _, v = find_version(version_id)
        scope = request.args.get("scope", scope_co())
        period = request.args.get("period", FY)
        values = v.calculate()
        if request.args.get("by_period") == "true":
            return jsonify(_j(reporting.pnl_by_period(v.configuration, values, scope)))
        return jsonify(_j({
            "scope": scope, "period": period,
            "currency": v.configuration.presentation_currency,
            "lines": reporting.pnl(v.configuration, values, scope, period),
            "headcount": reporting.headcount_summary(v.configuration, values, scope),
        }))

    @app.get("/api/v1/versions/<version_id>/reports/ratios")
    def report_ratios(version_id):
        actor()
        _, v = find_version(version_id)
        scope = request.args.get("scope", scope_co())
        values = v.calculate()
        return jsonify(_j(reporting.ratio_report(v.configuration, values, scope,
                                                 request.args.get("period", FY))))

    @app.get("/api/v1/versions/<version_id>/reports/alerts")
    def report_alerts(version_id):
        actor()
        _, v = find_version(version_id)
        report = service.validate_version(v)
        return jsonify(_j({
            "blocking": [{"code": f.code, "message": f.message} for f in report["blocking"]],
            "alerts": [{"code": a.code, "message": a.message, "status": a.status.value}
                       for a in report["alerts"]],
            "assumptions": report["assumptions"],
            "pending_tasks": [t.label for t in report["pending_tasks"]],
        }))

    @app.get("/api/v1/versions/<version_id>/inventory/stock")
    def report_stock(version_id):
        actor()
        _, v = find_version(version_id)
        return jsonify(_j(reporting.inventory_report(v.configuration, v.calculate(), v.graph)))

    @app.post("/api/v1/versions/<version_id>/balance/submit")
    def submit_balance(version_id):
        actor()
        _, v = find_version(version_id)
        from ..domain.validation import validate_balance
        findings = validate_balance(v.configuration, v.calculate(), "OPENING")
        if findings:
            values = v.calculate()
            diff = (values.get(nk("EQUITY", scope_co(), "OPENING")) or Decimal(0)) - (
                values.get(nk("EQUITY_LOADED", scope_co(), "OPENING")) or Decimal(0))
            return jsonify({"status": "REJECTED", "error_code": "BALANCE_NOT_BALANCED",
                            "difference": str(diff)}), 422
        return jsonify({"status": "ACCEPTED"})

    # ------------------------------------------------------------ escenarios
    @app.post("/api/v1/versions/<version_id>/scenarios")
    def create_scenario(version_id):
        actor()
        _, v = find_version(version_id)
        body = request.get_json(force=True)
        sc = Scenario(id=f"SC-{len(v.scenarios) + 1}", name=body["name"], version_id=v.id,
                      adjustments=[ScenarioAdjustment(
                          concept=a["concept"], variation_type=a.get("variation_type", "PERCENTAGE"),
                          variation=Decimal(str(a["variation"])),
                          business_unit_id=a.get("business_unit_id"),
                          branch_id=a.get("branch_id"),
                          operation_id=a.get("operation_id"))
                          for a in body.get("adjustments", [])])
        v.scenarios[sc.id] = sc
        return jsonify({"scenario_id": sc.id, "name": sc.name}), 201

    @app.post("/api/v1/versions/<version_id>/scenarios/<scenario_id>/run")
    def run_scenario_ep(version_id, scenario_id):
        actor()
        _, v = find_version(version_id)
        sc = v.scenarios[scenario_id]
        run_scenario(v, sc)
        scope = request.args.get("scope", scope_co())
        return jsonify(_j({"scenario": sc.name, "scope": scope,
                           "comparison": compare(v, sc, scope)}))

    # ------------------------------------------------------------ trazabilidad
    @app.get("/api/v1/versions/<version_id>/explain")
    def explain(version_id):
        actor()
        _, v = find_version(version_id)
        key = request.args["key"]
        depth = int(request.args.get("depth", 2))
        return jsonify(_j(v.graph.explain(key, v.calculate(), depth)))

    @app.get("/api/v1/versions/<version_id>/impact")
    def impact(version_id):
        actor()
        _, v = find_version(version_id)
        return jsonify(_j(v.impact_of(request.args.getlist("key"))))

    @app.get("/api/v1/audit-events")
    def audit_events():
        actor()
        events = service.audit.filter(
            version_id=request.args.get("version"),
            entity_type=request.args.get("entity"),
            action=request.args.get("action"),
        )
        return jsonify([e.as_dict() for e in events[-int(request.args.get("limit", 100)):]])

    return app


def main() -> None:
    from seed.demo import bootstrap
    service, budget, version = bootstrap()
    app = create_app(service)
    print(f"Presupuesto demo: {budget.id}  versión: {version.id}")
    print("Ejemplo: curl -H 'X-User: u.cfo' "
          f"localhost:8000/api/v1/versions/{version.id}/reports/pnl")
    app.run(port=8000, debug=False)


if __name__ == "__main__":
    main()
