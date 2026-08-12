"""Recorrido end-to-end de la V1 sobre la empresa demo.

    PYTHONPATH=. python3 run_demo.py [--html reporte.html]

Ejecuta el criterio de aceptación global del doc 02 §64 e imprime lo que el
sistema produce en cada etapa.
"""
from __future__ import annotations

import argparse
from decimal import Decimal

from app.domain.engine import FY, scope_bu, scope_co
from app.domain.graph import nk
from app.services import reporting
from app.services.budget import BudgetError, Scenario, ScenarioAdjustment, TaskStatus
from app.services.repository import Repository
from app.services.scenarios import compare, run_scenario
from seed.demo import D, bootstrap

LINE = "─" * 78


def money(v) -> str:
    if v is None:
        return "no calculable"
    return f"{Decimal(v):>16,.0f}"


def section(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def print_pnl(cfg, values, scope, label):
    print(f"\n{label}")
    for row in reporting.pnl(cfg, values, scope):
        prefix = "  " if not row["subtotal"] else "  "
        name = row["label"] if not row["subtotal"] else row["label"].upper()
        print(f"{prefix}{name:<38}{money(row['display'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="genera un reporte HTML en la ruta indicada")
    args = ap.parse_args()

    service, budget, version = bootstrap()
    cfg = version.configuration
    values = version.calculate()

    section("1. CONFIGURACIÓN — checklist del CFO (doc 02 §58)")
    for row in reporting.configuration_checklist(cfg):
        print(f"  {row['module']:<24}{row['status']:<16}{row['detail']}")
    print(f"\n  Estado de la configuración: {cfg.status.value}")
    print(f"  Grafo de cálculo: {version.graph.stats()}")

    section("2. TAREAS GENERADAS POR LA CONFIGURACIÓN (doc 02 §59)")
    for t in list(version.tasks.values())[:8]:
        print(f"  {t.label:<52}{t.status.value}")
    print(f"  ... {len(version.tasks)} tareas en total")

    section("3. P&L HASTA EBITDA (doc 02 §31)")
    print_pnl(cfg, values, scope_co(), f"CONSOLIDADO — {cfg.company_name} (USD)")
    for u in cfg.business_units:
        print_pnl(cfg, values, scope_bu(u.id), f"UNIDAD — {u.name}")
    for u in cfg.business_units:
        for b in u.branches:
            print_pnl(cfg, values, f"BR:{b.id}", f"SUCURSAL — {u.name} / {b.name}")

    section("4. DOTACIÓN (doc 02 §54)")
    for u in cfg.business_units:
        for b in u.branches:
            h = reporting.headcount_summary(cfg, values, f"BR:{b.id}")
            print(f"  {u.name} / {b.name:<22} inicial {h['initial']:>4}  final {h['final']:>4}  "
                  f"variación {h['net_change']:>+4}  costo {money(h['payroll_cost'])}")

    section("5. STOCK POR FAMILIA (doc 02 §27)")
    for row in reporting.inventory_report(cfg, values, version.graph):
        print(f"  {row['scope']:<12}{row['family']:<10} inicial{money(row['opening'])}"
              f"  compras{money(row['purchases'])}  costo{money(row['cogs'])}"
              f"  final{money(row['closing'])}")

    section("6. RATIOS Y OBJETIVOS (doc 02 §36/§38)")
    for r in reporting.ratio_report(cfg, values, scope_co()):
        obj = r["objective"]["display"] if r["objective"] else "—"
        estado = "" if r["objective_met"] is None else (" OK" if r["objective_met"]
                                                        else " INCUMPLIDO")
        print(f"  {r['name']:<44}{r['display']:>14}   objetivo {obj:>9}{estado}")

    section("7. ALERTAS, SUPUESTOS Y FALTANTES (doc 02 §41)")
    report = service.validate_version(version)
    print(f"  Validaciones bloqueantes: {len(report['blocking'])}")
    for f in report["blocking"][:5]:
        print(f"    · {f.message}")
    print(f"  Alertas informativas: {len(report['alerts'])}")
    for a in report["alerts"]:
        print(f"    · {a.message}")
    print("  Supuestos utilizados:")
    for s in report["assumptions"]:
        print(f"    · {s}")

    section("8. ESCENARIOS (doc 02 §39) — input → variación → cálculo")
    escenarios = [
        ("Pesimista", [ScenarioAdjustment("SALES", "PERCENTAGE", D("-0.10")),
                       ScenarioAdjustment("COST", "PERCENTAGE", D("0.05"))]),
        ("Expansión", [ScenarioAdjustment("SALES", "PERCENTAGE", D("0.15"), business_unit_id="BU-01"),
                       ScenarioAdjustment("EXPENSES", "PERCENTAGE", D("0.08"))]),
    ]
    for name, adjustments in escenarios:
        sc = Scenario(id=name, name=name, version_id=version.id, adjustments=adjustments)
        version.scenarios[name] = sc
        run_scenario(version, sc)
        print(f"\n  {name}")
        for row in compare(version, sc):
            delta = row["delta"]
            pct = "" if row["delta_pct"] is None else f"  ({Decimal(row['delta_pct']) * 100:+.1f}%)"
            print(f"    {row['metric']:<24}{money(row['base'])} → {money(row['scenario'])}"
                  f"   {money(delta)}{pct}")
    print("\n  El presupuesto base no cambió:",
          money(version.calculate()[nk('EBITDA', scope_co(), FY)]))

    section("9. GOBIERNO: aprobación, inmutabilidad y nueva versión")
    for t in version.tasks.values():
        t.status = TaskStatus.APPROVED
    service.approve_version("u.cfo", version)
    service.set_current("u.cfo", budget, version.id)
    print(f"  V{version.number} aprobada y definida como vigente.")
    try:
        service.change_fx_rate("u.cfo", version, "UYU", version.configuration.fiscal_year_start,
                               D("0.026"))
    except BudgetError as exc:
        print(f"  Intento de modificar el TC de la versión aprobada → {exc.code}: {exc}")
    v2 = service.create_version("u.cfo", budget, version.id)
    print(f"  Creada V{v2.number} a partir de V{version.number}: "
          f"{len(v2.inputs.values)} inputs clonados, la V1 queda intacta.")

    section("10. AUDITORÍA (doc 02 §55)")
    for ev in service.audit.events[-6:]:
        print(f"  {ev.at:%Y-%m-%d %H:%M}  {ev.actor:<8}{ev.action:<22}"
              f"{ev.entity_type:<14}{(ev.before or '')[:12]:<14}→ {(ev.after or '')[:24]}")

    section("11. PERSISTENCIA")
    repo = Repository(":memory:")
    repo.save_budget(budget, service.audit.events)
    reloaded = repo.load_budget(budget.id)
    rv = reloaded.versions[version.id]
    same = rv.calculate()[nk("EBITDA", scope_co(), FY)] == values[nk("EBITDA", scope_co(), FY)]
    print(f"  Guardado y recargado desde la base: {len(reloaded.versions)} versiones, "
          f"{len(rv.inputs.values)} inputs.")
    print(f"  EBITDA recalculado desde el snapshot persistido coincide: {same}")
    print(f"  Eventos de auditoría almacenados: {len(repo.audit_events())}")

    if args.html:
        from seed.report_html import write_report
        write_report(args.html, cfg, values, service, version, budget)
        print(f"\n  Reporte HTML generado: {args.html}")


if __name__ == "__main__":
    main()
