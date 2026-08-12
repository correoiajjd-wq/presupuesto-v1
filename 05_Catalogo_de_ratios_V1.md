**Catálogo de ratios V1 — Sistema de Presupuestación**

**Versión:** 1.0
**Estado:** catálogo cerrado — resuelve el punto pendiente del Documento técnico §47
**Fecha:** agosto 2026

**1. Propósito**

El Documento técnico dejaba un único bloque abierto antes de pasar a desarrollo: el catálogo definitivo de ratios V1 y las dependencias exactas de cada uno. Este documento lo cierra.

El catálogo contiene **23 ratios** agrupados en 6 familias. El CFO **selecciona** de esta lista; no escribe fórmulas. La creación de ratios personalizados queda para V2, tal como estaba previsto.

**2. Reglas de funcionamiento**

**2.1 Cada ratio declara sus dependencias**

Un ratio no es sólo una fórmula: declara qué métricas calculadas consume y qué **inputs arrastra**. Seleccionarlo activa automáticamente esos requerimientos de carga.

Ejemplo:

Días de stock

   ↓ exige

Stock inicial + Costo de venta

   ↓ exige

Ventas (para el costo) + Compras (si el modelo las usa)

Si el CFO elige un ratio de inventario sin haber configurado Stock, el sistema lo informa como dependencia pendiente y **bloquea el cierre de configuración** hasta resolverlo.

**2.2 Un faltante no es un cero**

Si falta un dato o el denominador es cero, el ratio devuelve **no calculable** y así se reporta. Nunca se muestra 0%. Esta regla es la que evita que un presupuesto incompleto se lea como un presupuesto malo.

**2.3 Niveles**

Cada ratio declara en qué niveles tiene sentido: empresa, unidad de negocio, sucursal. Los ratios de balance existen **sólo a nivel empresa y sólo anuales**, porque en V1 el balance no baja a unidad ni a sucursal.

**2.4 Anualización**

Los ratios de inventario se anualizan por los días del período, de modo que el valor de un mes sea comparable con el del ejercicio completo. Los demás ratios se calculan tanto por período como acumulados.

**2.5 Dirección**

Cada ratio declara si un valor más alto es mejor o peor. Esto permite que la interfaz muestre la señal correcta sin que el usuario tenga que interpretarla.

**3. Objetivos**

El CFO puede asignar opcionalmente un objetivo a cada ratio. Tipos soportados:

| **Tipo** | **Significado** |
| --- | --- |
| MINIMUM | El valor debe ser mayor o igual al objetivo |
| MAXIMUM | El valor debe ser menor o igual al objetivo |
| RANGE | El valor debe estar entre un mínimo y un máximo |
| EXACT | El valor debe ser igual al objetivo |

El Documento técnico preveía sólo objetivos de mínimo para V1 y los demás tipos para V2. Se incorporaron los cuatro desde V1 porque el costo de implementación es nulo una vez que existe la estructura del objetivo.

Comportamiento:

- Sin objetivo → el ratio se calcula y se reporta.

- Con objetivo cumplido → se reporta el cumplimiento.

- Con objetivo incumplido → se genera una **alerta informativa que no bloquea** el workflow.

- Sin datos suficientes → alerta de ratio no calculable.

**4. Catálogo**

### Rentabilidad

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `GROSS_MARGIN_PCT` | Margen bruto % | MARGEN_BRUTO / VENTAS | Porcentaje | Más es mejor | Empresa, Unidad, Sucursal | Ventas |
| `EBITDA_MARGIN_PCT` | EBITDA % | EBITDA / VENTAS | Porcentaje | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos, Dotación |
| `COGS_PCT` | Costo de ventas sobre ventas % | COSTO / VENTAS | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas |
| `EXPENSES_PCT` | Gastos sobre ventas % | GASTOS / VENTAS | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos |
| `PAYROLL_PCT` | Nómina sobre ventas % | NOMINA / VENTAS | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Dotación |
| `OPEX_PCT` | Estructura operativa sobre ventas % | (GASTOS + NOMINA) / VENTAS | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos, Dotación |
| `PAYROLL_TO_GROSS_MARGIN` | Nómina sobre margen bruto % | NOMINA / MARGEN_BRUTO | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Dotación |

> **Nómina sobre margen bruto %.** Cuánto del margen bruto se consume en estructura de personal.

### Estructura y asignación

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `CORPORATE_ALLOCATION_PCT` | Gastos corporativos asignados sobre ventas % | GASTOS_CORPORATIVOS_ASIGNADOS / VENTAS | Porcentaje | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos |
| `RESULT_AFTER_ALLOCATION_PCT` | Resultado después de asignación % | (EBITDA - GASTOS_CORPORATIVOS_ASIGNADOS) / VENTAS | Porcentaje | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos, Dotación |

> **Gastos corporativos asignados sobre ventas %.** Doc 02 §53: separa el resultado propio del impacto corporativo.

### Productividad

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `SALES_PER_HEAD` | Ventas por persona | VENTAS / DOTACION_PROMEDIO | Moneda | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Dotación |
| `GROSS_MARGIN_PER_HEAD` | Margen bruto por persona | MARGEN_BRUTO / DOTACION_PROMEDIO | Moneda | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Dotación |
| `EBITDA_PER_HEAD` | EBITDA por persona | EBITDA / DOTACION_PROMEDIO | Moneda | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos, Dotación |
| `PAYROLL_COST_PER_HEAD` | Costo laboral por persona | NOMINA / DOTACION_PROMEDIO | Moneda | Menos es mejor | Empresa, Unidad, Sucursal | Dotación |

### Inventario

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `STOCK_TURNOVER` | Rotación de stock | COSTO_DE_VENTA_ANUALIZADO / STOCK_PROMEDIO | Veces | Más es mejor | Empresa, Unidad, Sucursal | Ventas, Stock inicial |
| `STOCK_DAYS` | Días de stock | STOCK_PROMEDIO / COSTO_DE_VENTA * DIAS_DEL_PERIODO | Días | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Stock inicial |
| `STOCK_TO_SALES` | Stock final sobre ventas | STOCK_FINAL / VENTAS | Índice | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Stock inicial |
| `PURCHASE_TO_COGS` | Cobertura de compras | COMPRAS / COSTO_DE_VENTA | Índice | Neutro | Empresa, Unidad, Sucursal | Ventas, Stock inicial, Compras |

> **Rotación de stock.** Anualizado por días del período para que sea comparable mes a mes.

> **Cobertura de compras.** >1 acumula stock, <1 lo consume.

### Inversión

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `CAPEX_TO_SALES` | CAPEX sobre ventas % | CAPEX / VENTAS | Porcentaje | Neutro | Empresa, Unidad, Sucursal | Ventas, CAPEX |
| `CAPEX_TO_EBITDA` | CAPEX sobre EBITDA | CAPEX / EBITDA | Índice | Menos es mejor | Empresa, Unidad, Sucursal | Ventas, Gastos, Dotación, CAPEX |

> **CAPEX sobre EBITDA.** Sin depreciación en V1, mide esfuerzo de inversión contra generación operativa.

### Balance

| Código | Ratio | Fórmula | Unidad | Dirección | Niveles | Inputs que exige |
|---|---|---|---|---|---|---|
| `CURRENT_RATIO` | Liquidez corriente | ACTIVO_CORRIENTE / PASIVO_CORRIENTE | Índice | Más es mejor | Empresa (anual) | Balance |
| `WORKING_CAPITAL` | Capital de trabajo | ACTIVO_CORRIENTE - PASIVO_CORRIENTE | Moneda | Más es mejor | Empresa (anual) | Balance |
| `DEBT_TO_EQUITY` | Pasivo sobre patrimonio | PASIVO_TOTAL / PATRIMONIO | Índice | Menos es mejor | Empresa (anual) | Balance |
| `EQUITY_RATIO` | Solvencia patrimonial % | PATRIMONIO / ACTIVO_TOTAL | Porcentaje | Más es mejor | Empresa (anual) | Balance |

**5. Métricas de las que se alimenta el catálogo**

Todas provienen del Calculation Engine. Ninguna se carga manualmente.

| **Métrica** | **Origen** |
| --- | --- |
| VENTAS | Cantidad x precio, o monto cargado, según la modalidad de la unidad |
| COSTO | Ventas y fórmula de margen configurada |
| MARGEN_BRUTO | Ventas - costo |
| GASTOS | Gastos propios del ámbito, ya distribuidos |
| NOMINA | Dotación x sueldo con aumentos x (1 + cargas) + comisiones |
| EBITDA | Margen bruto - gastos - nómina |
| GASTOS_CORPORATIVOS_ASIGNADOS | Pool corporativo repartido según ventas anuales |
| DOTACION_PROMEDIO | Promedio de la dotación vigente en los períodos considerados |
| STOCK_PROMEDIO | (Stock inicial + stock final) / 2 |
| STOCK_FINAL | Stock anterior + compras - costo de venta |
| COMPRAS | Compras cargadas por familia |
| CAPEX | Inversiones cargadas |
| ACTIVO / PASIVO / PATRIMONIO | Balance configurado; el patrimonio total es calculado |

**6. Ratios evaluados y descartados para V1**

Se dejaron fuera deliberadamente, no por olvido:

| **Ratio** | **Motivo** |
| --- | --- |
| Margen neto, ROE, ROA | Requieren resultado después de impuestos; V1 llega hasta EBITDA |
| Cobertura de intereses | Requiere gastos financieros, previstos para V2 |
| Deuda neta / EBITDA | Requiere caja y deuda financiera detallada, previstas para V2 |
| Ciclo de conversión de efectivo | Requiere días de cobro y de pago, que dependen del módulo de cash flow |
| Punto de equilibrio | Requiere separar costos fijos de variables, que V1 no modela |
| ROIC | Requiere capital invertido y resultado operativo después de impuestos |

Todos ellos son incorporables en V2 agregando métricas al motor, sin modificar la estructura del catálogo ni el mecanismo de dependencias.

**7. Extensión en V2**

La estructura de un ratio (código, nombre, fórmula, métricas, dependencias, unidad, dirección, niveles) ya contempla lo necesario para que en V2 el CFO pueda definir ratios propios: alcanza con permitir que la fórmula y las dependencias se declaren desde configuración en lugar de venir del catálogo. El resto del motor no cambia.

**8. Estado**

El catálogo está implementado y verificado en el prototipo del motor de cálculo. Cada ratio se calcula, se anualiza, se compara contra su objetivo y genera alerta cuando corresponde. Se expone completo en el endpoint GET /api/v1/ratio-catalog.
