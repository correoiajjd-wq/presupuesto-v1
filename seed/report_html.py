"""Reporte HTML autocontenido del presupuesto (read model de presentación)."""
from __future__ import annotations

from decimal import Decimal
from html import escape

from app.domain.engine import FY, scope_br, scope_bu, scope_co
from app.domain.graph import nk
from app.services import reporting

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--txt:#e7e9ee;--muted:#98a0b0;
--pos:#3fa66a;--neg:#c9584f;--accent:#5b8def;--warn:#c9a227;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:24px;margin:0 0 4px} h2{font-size:16px;margin:36px 0 12px;font-weight:600}
h3{font-size:13px;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--muted);margin:0 0 8px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:14px}
table{width:100%;border-collapse:collapse}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
thead th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr.total td{font-weight:700;border-top:1px solid var(--line);background:rgba(91,141,239,.07)}
tr.sub td{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .l{color:var(--muted);font-size:12px} .kpi .v{font-size:22px;font-weight:650;margin-top:2px}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:600}
.ok{background:rgba(63,166,106,.15);color:var(--pos)} .bad{background:rgba(201,88,79,.15);color:var(--neg)}
.na{background:rgba(152,160,176,.15);color:var(--muted)}
ul{margin:6px 0 0;padding-left:18px;color:var(--muted)} li{margin:3px 0}
.bar{height:9px;background:var(--accent);border-radius:2px;display:block}
.barneg{background:var(--neg)}
.small{font-size:12px;color:var(--muted)}
"""


def m(v, dec=0):
    if v is None:
        return '<span class="small">no calculable</span>'
    return f"{Decimal(v):,.{dec}f}"


def write_report(path, cfg, values, service, version, budget) -> None:
    report = service.validate_version(version)
    ccy = cfg.presentation_currency
    out: list[str] = []
    a = out.append

    a(f"<!doctype html><html lang='es'><head><meta charset='utf-8'>"
      f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
      f"<title>{escape(cfg.company_name)} — Presupuesto</title><style>{CSS}</style></head><body>"
      f"<div class='wrap'>")
    a(f"<h1>{escape(cfg.company_name)}</h1>"
      f"<p class='sub'>Presupuesto {cfg.fiscal_year_start:%d/%m/%Y} — {cfg.fiscal_year_end:%d/%m/%Y}"
      f" · versión V{version.number} ({version.status.value}) · moneda de presentación {ccy}"
      f" · configuración {cfg.status.value}</p>")

    # KPIs
    kpi = [("Ventas", values.get(nk("SALES", scope_co(), FY))),
           ("Margen bruto", values.get(nk("GROSS_MARGIN", scope_co(), FY))),
           ("EBITDA", values.get(nk("EBITDA", scope_co(), FY))),
           ("Dotación final", values.get(nk("HEADCOUNT", scope_co(), FY))),
           ("CAPEX", values.get(nk("CAPEX", scope_co(), FY)))]
    a("<div class='kpis'>")
    for label, v in kpi:
        a(f"<div class='kpi'><div class='l'>{label}</div><div class='v'>{m(v)}</div></div>")
    a("</div>")

    # P&L consolidado y por unidad
    a("<h2>Estado de resultados hasta EBITDA</h2><div class='panel'><table>")
    scopes = [(scope_co(), "Consolidado")] + [(scope_bu(u.id), u.name) for u in cfg.business_units]
    a("<thead><tr><th>Concepto</th>" + "".join(f"<th>{escape(n)}</th>" for _, n in scopes)
      + "</tr></thead><tbody>")
    for metric, label, sign in reporting.PNL_LINES:
        cls = "total" if metric in reporting.SUBTOTALS else ""
        cells = []
        for scope, _ in scopes:
            v = values.get(nk(metric, scope, FY))
            if v is not None and sign < 0:
                v = -Decimal(v)
            cells.append(f"<td>{m(v)}</td>")
        a(f"<tr class='{cls}'><td>{label}</td>{''.join(cells)}</tr>")
    a("</tbody></table></div>")

    # Por sucursal
    a("<h2>Resultado por sucursal</h2><div class='panel'><table>"
      "<thead><tr><th>Sucursal</th><th>Ventas</th><th>Margen bruto</th><th>Gastos</th>"
      "<th>Nómina</th><th>EBITDA</th><th>Corporativos</th><th>Resultado final</th>"
      "<th>Dotación</th></tr></thead><tbody>")
    for u, b in cfg.all_branches():
            s = scope_br(b.id)
            a("<tr>"
              f"<td>{escape(u.name)} / {escape(b.name)}</td>"
              f"<td>{m(values.get(nk('SALES', s, FY)))}</td>"
              f"<td>{m(values.get(nk('GROSS_MARGIN', s, FY)))}</td>"
              f"<td>{m(values.get(nk('EXPENSES', s, FY)))}</td>"
              f"<td>{m(values.get(nk('PAYROLL', s, FY)))}</td>"
              f"<td>{m(values.get(nk('EBITDA', s, FY)))}</td>"
              f"<td>{m(values.get(nk('ALLOCATED_EXPENSES', s, FY)))}</td>"
              f"<td>{m(values.get(nk('RESULT_AFTER_ALLOCATION', s, FY)))}</td>"
              f"<td>{m(values.get(nk('HEADCOUNT', s, FY)))}</td></tr>")
    a("</tbody></table>"
      "<p class='small'>Los gastos corporativos de empresa y soporte se muestran asignados por "
      "debajo del EBITDA propio de cada sucursal, proporcionalmente a sus ventas anuales.</p></div>")

    # EBITDA mensual
    a("<h2>EBITDA mensual — consolidado</h2><div class='panel'><table>"
      "<thead><tr><th>Período</th><th>Ventas</th><th>EBITDA</th><th>Margen</th>"
      "<th style='width:34%'></th></tr></thead><tbody>")
    monthly = [(p.code, values.get(nk("SALES", scope_co(), p.code)),
                values.get(nk("EBITDA", scope_co(), p.code))) for p in cfg.periods]
    peak = max((abs(Decimal(e)) for _, _, e in monthly if e is not None), default=Decimal(1)) or 1
    for code, sales, eb in monthly:
        pct = "" if not sales else f"{Decimal(eb) / Decimal(sales) * 100:.1f}%"
        w = 0 if eb is None else int(abs(Decimal(eb)) / peak * 100)
        neg = " barneg" if eb is not None and Decimal(eb) < 0 else ""
        a(f"<tr><td>{code}</td><td>{m(sales)}</td><td>{m(eb)}</td><td>{pct}</td>"
          f"<td><span class='bar{neg}' style='width:{w}%'></span></td></tr>")
    a("</tbody></table></div>")

    # Ratios
    a("<h2>Ratios y objetivos</h2><div class='panel'><table>"
      "<thead><tr><th>Ratio</th><th>Fórmula</th><th>Valor</th><th>Objetivo</th>"
      "<th>Estado</th></tr></thead><tbody>")
    for r in reporting.ratio_report(cfg, values, scope_co()):
        if r["objective_met"] is None:
            tag = "<span class='tag na'>sin objetivo</span>" if r["computable"] else \
                  "<span class='tag na'>no calculable</span>"
        else:
            tag = ("<span class='tag ok'>cumple</span>" if r["objective_met"]
                   else "<span class='tag bad'>incumplido</span>")
        obj = r["objective"]["display"] if r["objective"] else "—"
        a(f"<tr><td>{escape(r['name'])}</td><td class='small'>{escape(r['formula'])}</td>"
          f"<td>{escape(r['display'])}</td><td>{escape(obj)}</td><td>{tag}</td></tr>")
    a("</tbody></table></div>")

    # Stock
    inv = reporting.inventory_report(cfg, values, version.graph)
    if inv:
        a("<h2>Stock por familia</h2><div class='panel'><table>"
          "<thead><tr><th>Ámbito</th><th>Familia</th><th>Inicial</th><th>Compras</th>"
          "<th>Costo de venta</th><th>Final</th></tr></thead><tbody>")
        for row in inv:
            a(f"<tr><td>{escape(row['scope'])}</td><td>{escape(row['family'])}</td>"
              f"<td>{m(row['opening'])}</td><td>{m(row['purchases'])}</td>"
              f"<td>{m(row['cogs'])}</td><td>{m(row['closing'])}</td></tr>")
        a("</tbody></table><p class='small'>Stock final = stock anterior + compras − costo de "
          "venta. Nunca se carga manualmente.</p></div>")

    # Escenarios
    if version.scenarios:
        a("<h2>Escenarios</h2><div class='panel'><table>"
          "<thead><tr><th>Escenario</th><th>Ventas</th><th>Margen bruto</th><th>EBITDA</th>"
          "<th>Variación EBITDA</th></tr></thead><tbody>")
        base_eb = values.get(nk("EBITDA", scope_co(), FY))
        a(f"<tr class='total'><td>Base (V{version.number})</td>"
          f"<td>{m(values.get(nk('SALES', scope_co(), FY)))}</td>"
          f"<td>{m(values.get(nk('GROSS_MARGIN', scope_co(), FY)))}</td>"
          f"<td>{m(base_eb)}</td><td>—</td></tr>")
        for sc in version.scenarios.values():
            sv = sc._values or {}
            eb = sv.get(nk("EBITDA", scope_co(), FY))
            delta = "" if eb is None or base_eb is None else \
                f"{(Decimal(eb) - Decimal(base_eb)) / abs(Decimal(base_eb)) * 100:+.1f}%"
            a(f"<tr><td>{escape(sc.name)}</td>"
              f"<td>{m(sv.get(nk('SALES', scope_co(), FY)))}</td>"
              f"<td>{m(sv.get(nk('GROSS_MARGIN', scope_co(), FY)))}</td>"
              f"<td>{m(eb)}</td><td>{delta}</td></tr>")
        a("</tbody></table><p class='small'>Los escenarios modifican inputs y el motor recalcula "
          "las consecuencias. El presupuesto base no se altera.</p></div>")

    # Alertas y supuestos
    a("<h2>Alertas, supuestos y faltantes</h2><div class='panel'>")
    a(f"<h3>Validaciones bloqueantes ({len(report['blocking'])})</h3>")
    if report["blocking"]:
        a("<ul>" + "".join(f"<li>{escape(f.message)}</li>" for f in report["blocking"][:15]) + "</ul>")
    else:
        a("<p class='small'>Ninguna. La versión puede aprobarse.</p>")
    a(f"<h3>Alertas informativas ({len(report['alerts'])})</h3><ul>")
    for al in report["alerts"]:
        a(f"<li>{escape(al.message)}</li>")
    a("</ul><h3>Supuestos utilizados</h3><ul>")
    for s in report["assumptions"]:
        a(f"<li>{escape(s)}</li>")
    a("</ul></div>")

    # Checklist
    a("<h2>Estado de la configuración</h2><div class='panel'><table>"
      "<thead><tr><th>Módulo</th><th>Estado</th><th>Detalle</th></tr></thead><tbody>")
    for row in reporting.configuration_checklist(cfg):
        tag = ("<span class='tag ok'>Completo</span>" if row["status"] == "Completo"
               else "<span class='tag na'>No configurado</span>")
        a(f"<tr><td>{escape(row['module'])}</td><td>{tag}</td>"
          f"<td class='small'>{escape(str(row['detail']))}</td></tr>")
    a("</tbody></table></div>")

    a(f"<p class='small'>Grafo de cálculo: {version.graph.stats()['nodes']} nodos, "
      f"{version.graph.stats()['edges']} dependencias · "
      f"{len(service.audit.events)} eventos de auditoría registrados.</p>")
    a("</div></body></html>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
