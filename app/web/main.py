"""Interfaz web del sistema de presupuestación.

Doc 02 §57: cada usuario ve qué tiene que hacer, dónde, con qué datos, qué
errores tiene y qué sigue. No se lo obliga a conocer la estructura completa
del modelo. Por eso la navegación depende del rol: el CFO gobierna y aprueba,
los responsables sólo ven sus tareas.

Es una UI server-rendered sobre los mismos servicios que usa la API. No hay
lógica de negocio acá: ni un cálculo, ni una regla de validación.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from io import BytesIO

from pydantic import ValidationError
from flask import (
    Flask, flash, redirect, render_template, request, send_file, session, url_for,
)

from ..domain.config import ConfigStatus, Role
from ..domain.engine import FY, scope_br, scope_bu, scope_co, scope_su
from ..domain.graph import nk
from ..domain.inputs import Concept
from ..domain.money import FXTable
from ..domain.periods import Frequency
from ..domain.ratios import RATIO_CATALOG
from ..domain.validation import validate_configuration
from ..services import reporting
from . import wizard
from ..services.budget import (
    BudgetError, BudgetService, Scenario, ScenarioAdjustment, TaskStatus, VersionStatus,
)
from ..services.import_export import commit_import, parse_sales_import, sales_template
from ..services.scenarios import compare, run_scenario
from .forms import (
    add_capex, add_headcount_change, apply_form, build_form, remove_headcount_change,
    update_headcount_change,
)


def create_web_app(service: BudgetService, budget_id: str) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "presupuesto-demo-v1")

    # ------------------------------------------------------------ contexto
    def budget():
        u = service.users.get(session.get("user_id") or "")
        propio = getattr(u, "budget_id", None)
        bid = session.get("budget_id") or propio or budget_id
        if propio and bid != propio:
            bid = propio
        if bid not in service.budgets:
            bid = propio or budget_id
        return service.budget(bid)

    def version():
        """La versión elegida manda: si pertenece a otro presupuesto, se cambia también
        el presupuesto activo. Presupuesto y versión nunca quedan desalineados."""
        vid = session.get("version_id")
        propio = getattr(service.users.get(session.get("user_id") or ""), "budget_id", None)
        if vid:
            for bud in service.budgets.values():
                # Elegir una versión cambia también de presupuesto, salvo que
                # el usuario pertenezca a uno: de ahí no sale.
                if vid in bud.versions and not (propio and bud.id != propio):
                    session["budget_id"] = bud.id
                    return bud.versions[vid]
        # Si hay una versión vigente, es ahí donde trabaja la empresa; la
        # última creada puede ser un borrador que todavía nadie cerró.
        bud = budget()
        v = (bud.versions.get(bud.current_version_id or "")
             if "budget.configuration.edit" not in service.auth.capabilities(user())
             else None) or bud.latest
        session["version_id"] = v.id
        return v

    def user():
        uid = session.get("user_id")
        return service.users.get(uid) if uid else None
    @app.context_processor
    def inject():
        u = user()
        v = version() if u else None
        return {
            "current_user": u,
            "version": v,
            "budget": budget(),
            "budgets": list(service.budgets.values()),
            "Role": Role,
            "is_cfo": bool(u and Role.CFO in u.roles),
            # Quién puede qué sale de las capacidades, no de preguntar si es CFO.
            "caps": service.auth.capabilities(u) if u else set(),
            "TaskStatus": TaskStatus,
        }

    @app.template_filter("money")
    def money(v, dec=0):
        if v is None:
            return "—"
        return f"{Decimal(v):,.{dec}f}"

    @app.errorhandler(BudgetError)
    def handle(err: BudgetError):
        detail = ""
        if err.details.get("errors"):
            detail = " " + " · ".join(str(x) for x in err.details["errors"][:4])
        flash(f"{err.code}: {err}{detail}", "error")
        return redirect(request.referrer or url_for("panel"))

    @app.errorhandler(ValidationError)
    def handle_model(err: ValidationError):
        """Las reglas del dominio viven en el modelo: acá sólo se traducen a pantalla."""
        msgs = [e.get("msg", "").replace("Value error, ", "") for e in err.errors()[:3]]
        flash(" · ".join(m for m in msgs if m) or str(err), "error")
        return redirect(request.referrer or url_for("panel"))

    @app.errorhandler(ValueError)
    def handle_value(err: ValueError):
        flash(str(err), "error")
        return redirect(request.referrer or url_for("panel"))

    @app.errorhandler(ArithmeticError)
    def handle_number(err: ArithmeticError):
        """decimal.InvalidOperation no es ValueError: un importe vacío llegaba
        hasta el 500 sin que ningún handler lo viera."""
        flash("Ese importe no es un número.", "error")
        return redirect(request.referrer or url_for("panel"))

    #: Qué capacidad hace falta para entrar a cada pantalla. Sin esto el
    #: control vive en la plantilla y basta con escribir la URL a mano.
    REQUIERE = {
        "configurar": "budget.configuration.edit",
        "configurar_accion": "budget.configuration.edit",
        "configurar_borrar": "budget.configuration.edit",
        "cerrar_configuracion": "budget.configuration.close",
        "auditoria": "budget.review",
        "aprobar_version": "budget.version.approve",
        "nueva_version": "budget.version.create",
        "vigente": "budget.version.set_current",
        "nuevo": "budget.configuration.edit",
        "crear_presupuesto": "budget.configuration.edit",
        "crear_escenario": "budget.scenario.run",
    }

    @app.before_request
    def guard():
        allowed = {"login", "do_login", "static"}
        if request.endpoint in allowed:
            return None
        u = user()
        if u is None:
            return redirect(url_for("login"))
        capacidad = REQUIERE.get(request.endpoint or "")
        if capacidad and capacidad not in service.auth.capabilities(u):
            flash(f"UNAUTHORIZED: {u.name} no tiene acceso a esta pantalla.", "error")
            return redirect(url_for("panel"))
        return None

    # ------------------------------------------------------------ acceso
    @app.get("/login")
    def login():
        return render_template("login.html", users=list(service.users.values()))

    @app.post("/login")
    def do_login():
        session["user_id"] = request.form["user_id"]
        return redirect(url_for("panel"))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------ panel
    @app.get("/")
    def panel():
        u, v = user(), version()
        if v.configuration.status is not ConfigStatus.LOCKED:
            # Doc 01 §5: no se puede cargar hasta cerrar la configuración. Al
            # que no configura no se lo manda al wizard —no puede entrar— sino
            # a una pantalla que le dice qué está pasando.
            if "budget.configuration.edit" in service.auth.capabilities(u):
                return redirect(url_for("configurar"))
            return render_template("esperando.html")
        tasks = service.tasks_for(v, u)
        if Role.CFO not in u.roles:
            return render_template("tareas.html", tasks=tasks, mine=True)
        report = service.validate_version(v)
        values = v.calculate()
        return render_template(
            "panel.html", report=report, values=values, tasks=list(v.tasks.values()),
            checklist=reporting.configuration_checklist(v.configuration),
            kpis=[("Ventas", values.get(nk("SALES", scope_co(), FY))),
                  ("Margen bruto", values.get(nk("GROSS_MARGIN", scope_co(), FY))),
                  ("EBITDA", values.get(nk("EBITDA", scope_co(), FY))),
                  ("Dotación", values.get(nk("HEADCOUNT", scope_co(), FY)))],
            nk=nk, scope_co=scope_co, FY=FY)

    @app.get("/tareas")
    def tareas():
        return render_template("tareas.html", tasks=service.tasks_for(version(), user()),
                               mine=False)

    # ------------------------------------------------------------ carga
    def tarea(v, task_id, concept: str = ""):
        task = v.tasks.get(task_id)
        if task is None:
            raise BudgetError("NOT_FOUND", "Esa tarea no existe en esta versión.")
        if concept and task.concept != concept:
            raise BudgetError("NOT_FOUND", f"Esa tarea no es de {concept.lower()}.")
        u = user()
        propia = {task.loader_role, task.reviewer_role, task.approver_role} & u.roles
        if not (propia and u.has_scope(task.scope_key, v.configuration)):
            raise BudgetError("UNAUTHORIZED_SCOPE",
                              f"{u.name} no participa de la tarea {task.label}.")
        return task

    @app.get("/tareas/<task_id>")
    def carga(task_id):
        v = version()
        task = tarea(v, task_id)
        return render_template("carga.html", task=task, spec=build_form(v, task))

    @app.post("/tareas/<task_id>")
    def guardar(task_id):
        v = version()
        task = tarea(v, task_id)
        n = apply_form(service, session["user_id"], v, task, request.form.to_dict())
        flash(f"{n} valores guardados como borrador." if n
              else "No había nada para guardar: los valores ya estaban así.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/dotacion")
    def dotacion(task_id):
        v = version()
        add_headcount_change(service, session["user_id"], v,
                             tarea(v, task_id, "PAYROLL_HEADCOUNT"), request.form)
        flash("Solicitud registrada. Nómina tiene que ponerle el valor.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/dotacion/<movement_id>/borrar")
    def borrar_dotacion(task_id, movement_id):
        v = version()
        t = tarea(v, task_id, "PAYROLL_HEADCOUNT")
        remove_headcount_change(service, session["user_id"], v, movement_id, t.scope_key)
        flash("Solicitud eliminada.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/dotacion/<movement_id>")
    def editar_dotacion(task_id, movement_id):
        v = version()
        tarea(v, task_id, "PAYROLL_HEADCOUNT")
        update_headcount_change(service, session["user_id"], v, movement_id, request.form)
        flash("Solicitud corregida. El importe se reajustó en la misma proporción.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/capex")
    def capex(task_id):
        v = version()
        tarea(v, task_id, "CAPEX")
        add_capex(service, session["user_id"], v, request.form)
        flash("Inversión registrada.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/enviar")
    def enviar(task_id):
        v = version()
        tarea(v, task_id)
        service.submit_task(session["user_id"], v, task_id)
        flash("Enviado a revisión.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    @app.post("/tareas/<task_id>/aprobar")
    def aprobar(task_id):
        v = version()
        tarea(v, task_id)
        service.approve_task(session["user_id"], v, task_id)
        flash("Aprobado.", "ok")
        return redirect(request.referrer or url_for("panel"))

    @app.post("/tareas/<task_id>/rechazar")
    def rechazar(task_id):
        v = version()
        tarea(v, task_id)
        service.reject_task(session["user_id"], v, task_id, request.form.get("comment", ""))
        flash("Rechazado y devuelto al responsable.", "ok")
        return redirect(request.referrer or url_for("panel"))

    # ------------------------------------------------------------ planillas
    @app.get("/tareas/<task_id>/plantilla")
    def plantilla(task_id):
        v = version()
        task = tarea(v, task_id, "SALES")
        operation_id = task.scope_key.split(":", 1)[1]
        return send_file(BytesIO(sales_template(v, operation_id)), as_attachment=True,
                         download_name=f"ventas_{operation_id}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument."
                                  "spreadsheetml.sheet")

    @app.post("/tareas/<task_id>/importar")
    def importar(task_id):
        v = version()
        task = tarea(v, task_id, "SALES")
        operation_id = task.scope_key.split(":", 1)[1]
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Elegí un archivo.", "error")
            return redirect(url_for("carga", task_id=task_id))
        result, parsed = parse_sales_import(v, file.read(), operation_id, session["user_id"])
        if result.status == "REJECTED":
            return render_template("import_errores.html", task=task, result=result)
        commit_import(service, session["user_id"], v, parsed)
        flash(f"Planilla importada: {result.imported} valores.", "ok")
        return redirect(url_for("carga", task_id=task_id))

    # ------------------------------------------------------------ reportes
    def ambitos_visibles(v):
        """Cada uno ve el resultado de lo suyo. El gerente de una sucursal no
        tiene por qué ver el EBITDA consolidado de la empresa."""
        u = user()
        todos = reporting.scopes_of(v.configuration)
        if not u.scopes:
            return todos
        return [x for x in todos if u.has_scope(x[0], v.configuration)]

    @app.get("/reportes/pnl")
    def pnl():
        v = version()
        cfg = v.configuration
        values = v.calculate()
        scopes = ambitos_visibles(v)
        if not scopes:
            flash("No tenés ningún ámbito asignado para ver reportes.", "error")
            return redirect(url_for("panel"))
        scope = request.args.get("scope", scopes[0][0])
        if scope not in {x[0] for x in scopes}:
            raise BudgetError("UNAUTHORIZED_SCOPE", "Ese ámbito no está en tu alcance.")
        mode = request.args.get("mode", "annual")
        return render_template(
            "pnl.html", scopes=scopes, scope=scope, mode=mode, values=values,
            rows=reporting.pnl(cfg, values, scope),
            table=reporting.pnl_by_period(cfg, values, scope),
            headcount=reporting.headcount_summary(cfg, values, scope),
            label=dict((s, n) for s, n, _ in scopes).get(scope, scope))

    @app.get("/reportes/ratios")
    def ratios():
        v = version()
        values = v.calculate()
        scopes = ambitos_visibles(v)
        if not scopes:
            flash("No tenés ningún ámbito asignado para ver reportes.", "error")
            return redirect(url_for("panel"))
        scope = request.args.get("scope", scopes[0][0])
        if scope not in {x[0] for x in scopes}:
            raise BudgetError("UNAUTHORIZED_SCOPE", "Ese ámbito no está en tu alcance.")
        return render_template("ratios.html", scopes=scopes, scope=scope,
                               rows=reporting.ratio_report(v.configuration, values, scope),
                               label=dict((s, n) for s, n, _ in scopes).get(scope, scope))

    @app.get("/reportes/stock")
    def stock():
        v = version()
        return render_template("stock.html",
                               rows=reporting.inventory_report(v.configuration, v.calculate(),
                                                               v.graph))

    @app.get("/reportes/alertas")
    def alertas():
        v = version()
        return render_template("alertas.html", report=service.validate_version(v))

    @app.post("/reportes/alertas/<int:index>")
    def resolver_alerta(index):
        v = version()
        if not 0 <= index < len(v.alerts):
            raise BudgetError("NOT_FOUND", "Esa alerta ya no existe.")
        service.resolve_alert(session["user_id"], v, index, request.form.get("comment", ""),
                              accept=bool(request.form.get("accept")))
        flash("Alerta registrada.", "ok")
        return redirect(url_for("alertas"))

    @app.get("/reportes/auditoria")
    def auditoria():
        return render_template("auditoria.html",
                               events=list(reversed(service.audit.events))[:200])

    @app.get("/explicar")
    def explicar():
        v = version()
        key = request.args.get("key", nk("EBITDA", scope_co(), FY))
        tree = v.graph.explain(key, v.calculate(), int(request.args.get("depth", 2)))
        return render_template("explicar.html", key=key, tree=tree,
                               stats=v.graph.stats())

    # ------------------------------------------------------------ escenarios
    @app.get("/escenarios")
    def escenarios():
        v = version()
        rows = {}
        for sc in v.scenarios.values():
            if sc._values:
                rows[sc.id] = compare(v, sc)
        return render_template("escenarios.html", scenarios=v.scenarios, rows=rows,
                               units=v.configuration.business_units,
                               base=v.calculate())

    @app.post("/escenarios")
    def crear_escenario():
        v = version()
        adjustments = []
        for concept, variation, unit_id in zip(request.form.getlist("concept"),
                                               request.form.getlist("variation"),
                                               request.form.getlist("unit")):
            if not variation.strip():
                continue
            adjustments.append(ScenarioAdjustment(
                concept=concept, variation_type="PERCENTAGE",
                variation=Decimal(variation) / Decimal(100),
                business_unit_id=unit_id or None))
        if not adjustments:
            flash("Cargá al menos una variación.", "error")
            return redirect(url_for("escenarios"))
        sc = Scenario(id=f"SC-{len(v.scenarios) + 1}", name=request.form["name"],
                      version_id=v.id, adjustments=adjustments)
        v.scenarios[sc.id] = sc
        run_scenario(v, sc)
        flash(f"Escenario '{sc.name}' calculado. El presupuesto base no cambió.", "ok")
        return redirect(url_for("escenarios"))

    # ------------------------------------------------------------ presupuestos
    @app.get("/presupuestos")
    def presupuestos():
        return render_template("presupuestos.html", budgets=list(service.budgets.values()))

    @app.post("/presupuestos/seleccionar")
    def seleccionar_presupuesto():
        session["budget_id"] = request.form["budget_id"]
        session.pop("version_id", None)
        return redirect(url_for("panel"))

    @app.get("/nuevo")
    def nuevo():
        return render_template("nuevo.html")

    @app.post("/nuevo")
    def crear_presupuesto():
        cfg = wizard.new_configuration(request.form)
        fx = FXTable(cfg.presentation_currency, cfg.enabled_currencies)
        nb = service.create_budget(session["user_id"], request.form["name"].strip(), cfg, fx)
        session["budget_id"] = nb.id
        session["version_id"] = nb.latest.id
        flash("Presupuesto creado. Ahora armá el modelo: la configuración manda sobre "
              "todo lo que viene después.", "ok")
        return redirect(url_for("configurar", step="estructura"))

    # ------------------------------------------------------------ wizard
    @app.get("/configurar")
    @app.get("/configurar/<step>")
    def configurar(step="general"):
        v, u = version(), user()
        if step not in wizard.STEP_BY_KEY:
            step = "general"
        report = service.validate_version(v) if step == "cierre" else None
        return render_template(
            "configurar.html", step=wizard.STEP_BY_KEY[step], steps=wizard.STEPS,
            state=wizard.step_state(v), cfg=v.configuration, fx=wizard.fx_summary(v),
            editable=wizard.can_edit_step(u, wizard.STEP_BY_KEY[step])
                     and v.configuration.status.value != "LOCKED" and v.mutable,
            catalog=RATIO_CATALOG, report=report, users=list(service.users.values()),
            scope_options=wizard.scope_options(v.configuration),
            expense_targets=wizard.expense_target_options(v.configuration),
            workflow_concepts=wizard.WORKFLOW_CONCEPTS,
            findings=validate_configuration(v.configuration, v.fx),
            frequencies=[f.value for f in Frequency], roles=[r.value for r in Role])

    @app.post("/configurar/<step>/<action>")
    def configurar_accion(step, action):
        if step not in wizard.STEP_BY_KEY:
            raise BudgetError("NOT_FOUND", f"El paso {step} no existe.")
        v, u = version(), user()
        service.auth.check(u, "budget.configuration.edit")
        if not wizard.can_edit_step(u, wizard.STEP_BY_KEY[step]):
            raise BudgetError("UNAUTHORIZED",
                              f"Este paso lo define {wizard.STEP_BY_KEY[step].owner.value}.")
        handlers = {
            "general": wizard.update_general, "fx": wizard.set_fx_rate,
            "unidad": wizard.add_business_unit, "sucursal": wizard.add_branch,
            "operacion": wizard.add_operation,
            "soporte": wizard.add_support_unit, "centro": wizard.add_cost_center,
            "familia": wizard.add_family, "producto": wizard.add_product,
            "gasto": wizard.add_expense, "nomina": wizard.update_payroll,
            "aumento": wizard.add_increase_rule, "concepto": wizard.add_percentage_concept,
            "modulos": wizard.update_modules, "capex_categoria": wizard.add_capex_category,
            "rubro": wizard.add_balance_item, "ratios": wizard.update_ratios,
            "workflow": wizard.update_workflow,
        }
        if action in ("usuario", "borrar_usuario"):
            if step != "workflow":
                raise BudgetError("NOT_FOUND", "Los responsables se definen en el paso workflow.")
            wizard.assert_open(v)
            if action == "usuario":
                wizard.add_user(service, v, request.form, budget().id)
            else:
                wizard.remove_user(service, request.form["user_id"])
        elif action in handlers:
            handlers[action](v, request.form)
        elif action == "rubros_default":
            wizard.add_default_balance_items(v)
        elif action == "workflow_default":
            wizard.default_workflow(v)
        else:
            raise BudgetError("NOT_FOUND", action)
        service.audit.record(actor=session["user_id"], action="ConfigurationUpdated",
                             entity_type="CONFIGURATION", entity_id=action, version_id=v.id,
                             after=request.form.get("name") or action)
        flash("Guardado.", "ok")
        return redirect(url_for("configurar", step=step))

    @app.post("/configurar/<step>/borrar/<kind>/<path:entity_id>")
    def configurar_borrar(step, kind, entity_id):
        if step not in wizard.STEP_BY_KEY:
            raise BudgetError("NOT_FOUND", f"El paso {step} no existe.")
        v, u = version(), user()
        service.auth.check(u, "budget.configuration.edit")
        if not wizard.can_edit_step(u, wizard.STEP_BY_KEY[step]):
            raise BudgetError("UNAUTHORIZED",
                              f"Este paso lo define {wizard.STEP_BY_KEY[step].owner.value}.")
        wizard.remove(v, kind, entity_id)
        service.audit.record(actor=session["user_id"], action="ConfigurationDeleted",
                             entity_type=kind.upper(), entity_id=entity_id, version_id=v.id,
                             before=entity_id)
        flash("Eliminado.", "ok")
        return redirect(url_for("configurar", step=step))

    @app.post("/configurar/cerrar")
    def cerrar_configuracion():
        v = version()
        service.close_configuration(session["user_id"], v)
        flash("Configuración cerrada. Se generaron las tareas de carga y a partir de "
              "ahora la estructura está bloqueada.", "ok")
        return redirect(url_for("panel"))

    # ------------------------------------------------------------ configuración y versiones
    @app.get("/configuracion")
    def configuracion():
        v = version()
        return render_template("configuracion.html", cfg=v.configuration,
                               checklist=reporting.configuration_checklist(v.configuration))

    @app.get("/versiones")
    def versiones():
        return render_template("versiones.html", report=service.validate_version(version()))

    @app.post("/versiones/aprobar")
    def aprobar_version():
        v = version()
        service.approve_version(session["user_id"], v)
        flash(f"Versión V{v.number} aprobada. A partir de acá es inmutable.", "ok")
        return redirect(url_for("versiones"))

    @app.post("/versiones/vigente")
    def vigente():
        v = version()
        service.set_current(session["user_id"], budget(), v.id)
        flash(f"V{v.number} es la versión vigente.", "ok")
        return redirect(url_for("versiones"))

    @app.post("/versiones/nueva")
    def nueva_version():
        v = version()
        nueva = service.create_version(session["user_id"], budget(), v.id)
        session["version_id"] = nueva.id
        flash(f"Creada V{nueva.number} a partir de V{v.number}. La anterior queda intacta.", "ok")
        return redirect(url_for("versiones"))

    @app.post("/versiones/seleccionar")
    def seleccionar_version():
        session["version_id"] = request.form["version_id"]
        return redirect(request.referrer or url_for("panel"))

    return app


def main() -> None:
    from seed.demo import bootstrap
    service, budget, version = bootstrap()
    app = create_web_app(service, budget.id)
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
