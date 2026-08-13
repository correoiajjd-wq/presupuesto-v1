# Sistema de Presupuestación V1 — prototipo ejecutable del núcleo

Implementación funcionando de la parte que decide si el sistema sirve o no:
**configuración → inputs → grafo de dependencias → cálculo → validación → workflow → versión**.

No es el producto terminado. Es el motor, con la cadena completa andando de punta a punta
sobre una empresa demo, y 84 tests que verifican reglas concretas del spec.

```
PYTHONPATH=. python3 wsgi.py                           # interfaz web en :8000
PYTHONPATH=. python3 run_demo.py --html reporte.html   # recorrido end-to-end por consola
PYTHONPATH=. python3 -m unittest discover -s tests     # 84 tests
```

---

## Qué está implementado

| Componente (doc 03) | Estado | Dónde |
|---|---|---|
| Configuration Engine + state machine | ✔ | `app/domain/config.py` |
| Dependency Graph + orden topológico + impact analysis | ✔ | `app/domain/graph.py` |
| Calculation Engine (ventas, costo, gastos, nómina, CAPEX, stock, balance, ratios) | ✔ | `app/domain/engine.py` |
| Calculation Registry / catálogo de ratios | ✔ | `app/domain/ratios.py` |
| Currency / FX Engine con moneda puente | ✔ | `app/domain/money.py` |
| Validation Engine + Alert Engine | ✔ | `app/domain/validation.py` |
| Versioning + inmutabilidad + snapshot de configuración | ✔ | `app/services/budget.py` |
| Workflow + Approval + aprobación parcial | ✔ | `app/services/budget.py` |
| Authorization Provider (capabilities + scope) | ✔ | `app/services/budget.py` |
| Audit Engine | ✔ | `app/services/budget.py` |
| Scenario Engine (overlay sobre inputs) | ✔ | `app/services/scenarios.py` |
| Import/Export Engine (plantilla dinámica, commit atómico) | ✔ | `app/services/import_export.py` |
| Reporting / read models | ✔ | `app/services/reporting.py` |
| API REST | ✔ | `app/api/main.py` |
| Interfaz web (server-rendered) | ✔ | `app/web/` |
| Wizard de configuración (CFO / COO) | ✔ | `app/web/wizard.py` |
| Persistencia | parcial | `app/services/repository.py` + `schema_postgres.sql` |
| Identity, notificaciones, job engine, cache | ✖ | pendiente |

**Fuera de V1 por decisión del spec:** depreciación, intereses, impuestos, cash flow,
ratios personalizados, alta de productos durante el ejercicio.

---

## Las cinco decisiones que definen el diseño

**1. El grafo de dependencias es real, no un diagrama.**
Cada número del presupuesto es un nodo `MÉTRICA|ÁMBITO|PERÍODO` con sus dependencias
declaradas. La demo genera ~3.200 nodos y ~6.400 aristas. De ahí salen tres cosas que
de otro modo hay que programar a mano una y otra vez:

- *Recálculo incremental*: cambia un input → clausura hacia adelante → sólo se recalcula
  lo afectado, en orden topológico. Hay un test que verifica que el incremental da
  exactamente lo mismo que el completo.
- *Impact analysis*: `GET /versions/{id}/impact?key=SALES|BR:BR-01|2027-03` responde
  cuántos valores, ratios y alertas cambia una modificación, antes de hacerla.
- *Explicabilidad*: `GET /versions/{id}/explain?key=EBITDA|CO|2027-01` devuelve el árbol
  de dependencias con valores y fórmula. El CFO puede ver de dónde salió un número.

**2. Un valor calculado nunca se carga.**
`INPUT` y `CALCULATED` son tipos de nodo distintos, tablas distintas en el modelo
PostgreSQL, y no existe endpoint para escribir un calculado: `POST /inventory/closing-stock`
devuelve `409 CALCULATED_VALUE_NOT_EDITABLE`. Esto es lo que permite que el motor de
dependencias tenga sentido: si un valor puede venir de dos lados, el grafo miente.

**3. La unidad de cuenta interna es la moneda de presentación.**
Cada input se convierte al entrar al grafo. Los flujos usan TC promedio del período,
los stocks TC de cierre — es una distinción financiera real, no un detalle. La moneda de
presentación es la moneda puente: con USD como presentación, ARS→UYU sale sin cargar
ese par. Cargar stock y compras en monedas distintas se rechaza en validación, como pide
el doc 02 §28.

**4. La configuración manda sobre todo lo demás.**
De la configuración salen: qué inputs se piden, qué tareas existen, qué columnas tiene la
planilla de cada usuario, qué se valida, qué ratios se calculan y qué dependencias arrastran.
No hay lista rígida de datos obligatorios: `required_concepts()` la deriva del modelo.
Si el CFO no configura Stock, el sistema no lo pide; si elige el ratio de días de stock,
aparecen Stock inicial, costo de venta y compras como dependencias.

**5. Un faltante no es un cero.**
Un ratio sin denominador devuelve `None` y se reporta como *no calculable*, nunca como 0%.
Los reportes explicitan supuestos, faltantes y conceptos no configurados.

---

## Reglas del spec que quedaron verificadas por test

Cada test cita el parágrafo que verifica (`tests/test_rules.py`, `tests/test_api_persistence.py`).
Las que más costó dejar bien:

- **Vigencia parcial + frecuencia.** Una sucursal que abre en junio y un producto de carga
  trimestral: el trimestre abril-junio va **entero a junio**, no un tercio a cada mes.
  El caso obvio (repartir y después poner cero en abril y mayo) pierde dos tercios del valor.
- **Aumentos salariales por fecha de ingreso.** Quien entra en febrero cobra el aumento de
  marzo y el de agosto; quien entra en abril, sólo el de agosto. Se modela por cohortes de
  ingreso, y las bajas consumen las cohortes más antiguas primero.
- **Gastos corporativos.** Se asignan a unidades y sucursales proporcionalmente a las ventas
  **anuales** (no mensuales: con ventas estacionales el driver mensual da asignaciones
  erráticas). La asignación es presentacional: se muestra debajo del EBITDA propio, y
  `Σ(resultado después de asignación de cada unidad) = EBITDA de la empresa`. Hay un test
  que verifica esa identidad.
- **Balance.** El total de patrimonio es calculado (`Activo − Pasivo`); los componentes
  (capital, resultados acumulados) se cargan, y si no coinciden con ese total el balance no
  cierra y **se rechaza la carga completa**. Es la única lectura de los §26 y §34 que no se
  contradice a sí misma.
- **Aprobación parcial.** Modificar ventas de Montevideo devuelve a revisión sólo esa tarea;
  la de Centro sigue aprobada.
- **Importación atómica.** Un error en una fila rechaza la planilla entera e informa
  fila / columna / valor / error / corrección esperada. El commit revierte todo si algo falla.
- **Cero vs vacío.** `0` es válido, vacío es error.
- **Inmutabilidad.** Cambiar el TC de una versión aprobada devuelve `409 VERSION_IMMUTABLE`
  en la API — y un trigger lo impide también en la base (`schema_postgres.sql`).

---

## Catálogo de ratios V1 (el punto que quedaba abierto en el doc 01 §47)

23 ratios, cada uno con fórmula, métricas, dependencias, unidad, dirección y niveles
donde aplica. `GET /api/v1/ratio-catalog` lo devuelve completo.

| Grupo | Ratios |
|---|---|
| Rentabilidad | Margen bruto %, EBITDA %, Costo/ventas %, Gastos/ventas %, Nómina/ventas %, Estructura operativa/ventas %, Nómina/margen bruto % |
| Estructura | Gastos corporativos asignados/ventas %, Resultado después de asignación % |
| Productividad | Ventas por persona, Margen bruto por persona, EBITDA por persona, Costo laboral por persona |
| Inventario | Rotación de stock, Días de stock, Stock final/ventas, Cobertura de compras |
| Inversión | CAPEX/ventas %, CAPEX/EBITDA |
| Balance | Liquidez corriente, Capital de trabajo, Pasivo/patrimonio, Solvencia patrimonial % |

Los objetivos admiten `MINIMUM`, `MAXIMUM`, `RANGE` y `EXACT` (el doc preveía sólo
mínimo en V1; el tipo ya está soportado sin costo). Un objetivo incumplido genera alerta
y **no** bloquea la aprobación.

Los ratios de inventario se anualizan por días del período, para que un mes sea comparable
con el ejercicio. Los de balance existen sólo a nivel empresa y sólo anuales: el balance
no baja a sucursal en V1.

---

## Empresa demo

ACME Distribución S.A., ejercicio 2027, presentación USD:

- **Repuestos** (venta por unidades) con Montevideo y **Salto, que abre en junio**
- **Servicios** (venta por monto, en UYU, con comisión del 2% sobre ventas)
- Administración central como unidad de soporte con centro de costo
- Los cinco tipos de gasto: propio de sucursal, de unidad distribuido a sucursales,
  de centro de costo, distribuido 60/40 por porcentaje, y corporativo de empresa
- Nómina con dotación inicial, altas, una baja, dos aumentos y cargas del 17%
- Stock por familia a nivel sucursal, CAPEX, balance inicial y proyectado

Resultado: ventas 8.550.000, EBITDA 1.611.884 (18,9%), 0 validaciones bloqueantes,
5 alertas informativas. El reporte HTML (`reporte.html`) muestra todo eso.

---

---

## Probarlo sin instalar nada

El repositorio está listo para deploy en Render (plan gratuito, sin tarjeta):

1. Entrá a **render.com** y logueate con GitHub.
2. **New → Web Service** → elegí este repositorio.
3. Render lee `render.yaml` y completa todo solo. Confirmá.

En un par de minutos tenés una URL pública. Entrás, elegís con qué rol querés
navegar y probás el sistema completo.

También hay `Dockerfile` si preferís correrlo en cualquier otro lado, y para
levantarlo local alcanza con:

```bash
pip install -r requirements.txt
PYTHONPATH=. python3 wsgi.py        # web en / y API en /api
```

### Armar una empresa desde cero

Es lo primero que pasa en la vida real, y está implementado: **Presupuestos → Crear un
presupuesto nuevo** (como CFO). El wizard tiene nueve pasos, se puede hacer en varias
sesiones y entre varias personas, y cada paso dice de quién es:

1. **Datos generales** — empresa, ejercicio (cualquier fecha de inicio y fin), monedas
   habilitadas y tipos de cambio. El TC se guarda día por día; para no cargar 365 valores
   se pide el estimado de inicio y el de cierre, y el sistema interpola.
2. **Estructura** — unidades de negocio, sucursales, unidades de soporte y centros de costo,
   cada uno con su fecha de apertura y cierre dentro del ejercicio.
3. **Productos y familias** — lo define el **COO**: catálogo por unidad, modalidad de venta
   (por unidades o por monto), fórmula de margen, precio, margen y frecuencia de carga.
4. **Gastos** — qué existe, dónde se imputa, con qué frecuencia y moneda, y si se reparte.
5. **Nómina** — áreas con su sueldo base, reglas de aumento y conceptos porcentuales.
6. **CAPEX, Stock y Balance** — módulos opcionales. Lo que no se configura, no se pide.
7. **Ratios y objetivos** — se eligen del catálogo de 23; cada uno arrastra sus dependencias.
8. **Workflow y responsables** — quién carga, quién revisa y quién aprueba cada concepto, y
   qué persona ocupa cada rol con qué alcance.
9. **Validación y cierre** — muestra qué falta y, cuando no queda nada bloqueante, cierra.

Al cerrar pasan tres cosas: se generan las tareas de carga y cada responsable ve las suyas,
el sistema deriva qué datos son obligatorios (sale del modelo, no de una lista fija), y la
estructura queda bloqueada — cambiarla exige una versión nueva.

El panel de carga directamente no existe antes del cierre: entrar a `/` con la configuración
en borrador redirige al wizard.

### Qué se puede hacer en la interfaz

| Pantalla | Qué probar |
|---|---|
| **Ingreso** | Elegís rol: CFO, COO, gerente de sucursal, administración, nómina, finanzas. Cada uno ve algo distinto. |
| **Configurar** | El wizard de nueve pasos. Los pasos del COO no los puede editar otro rol, y viceversa. |
| **Panel del CFO** | Progreso de carga y aprobación, checklist de configuración, qué falta para poder aprobar, alertas. |
| **Mis tareas** | Un gerente sólo ve sus tareas y sus sucursales. Si intenta entrar a otra, el sistema lo frena. |
| **Carga** | El formulario se genera desde la configuración: sólo tus productos, tus períodos, tu moneda. El precio y el margen se muestran pero no se editan. Guardás borrador o enviás a revisión. |
| **Carga masiva** | Descargás la planilla Excel de tu sucursal, la llenás, la subís. Si tiene un error, se rechaza entera y te muestra fila, columna, valor, error y corrección esperada. |
| **P&L** | Consolidado, por unidad y por sucursal, anual o mes a mes, con los corporativos separados debajo del EBITDA. |
| **Ratios** | Los 23 del catálogo contra sus objetivos, por ámbito. |
| **Escenarios** | Cargás variaciones sobre inputs y ves el impacto contra la base. La base no cambia. |
| **Explicar** | De dónde sale un número: el árbol de dependencias con valores y fórmulas. |
| **Versiones** | Aprobás la versión y después probás cambiar cualquier cosa: `VERSION_IMMUTABLE`. Creás la V2 y la V1 queda intacta. |
| **Auditoría** | Quién, cuándo, qué cambió, antes y después. |

### Cosas que vale la pena intentar romper

- Armá una empresa desde cero con el wizard, asignale un gerente a una sola sucursal, cerrá
  la configuración y entrá con ese usuario: sólo ve sus tareas, y si intenta cargar otra
  sucursal el sistema responde `UNAUTHORIZED_SCOPE`.
- Intentá cerrar la configuración con una moneda habilitada sin tipo de cambio: no te deja.
- Cargá dos productos "Otros" en la misma unidad, o una distribución de gasto que sume 90%.
- Entrá como **Martín (gerente Montevideo)** y fijate que no existe la sucursal Salto para él.
- En **Ventas**, dejá una celda vacía y guardá: pasa. Ahora bajá la planilla Excel, dejá una
  celda vacía y subila: la rechaza entera, porque en carga masiva vacío es error.
- Cargá un producto trimestral en un período que no sea cabecera de trimestre: `INVALID_FREQUENCY`,
  y te dice cuál es el período correcto.
- Como **CFO**, aprobá la versión en *Versiones* y después volvé a *Tareas* e intentá cargar algo.
- En **Balance**, cambiá un rubro del activo y mirá *Alertas*: `BALANCE_NOT_BALANCED` con la
  diferencia exacta, bloqueando la aprobación.
- En **Escenarios**, no vas a encontrar EBITDA entre las variables: sólo se simula sobre inputs.

### Advertencia sobre el estado

El prototipo mantiene el presupuesto **en memoria**, no en una base. Si el servicio se reinicia
—en el plan gratuito de Render se duerme tras un rato sin uso— vuelve a arrancar con la empresa
demo original y se pierde lo que hayas cargado. Para uso real hay que conectar PostgreSQL, que es
el punto 1 de la lista de abajo.

---

## Lo que falta para que esto sea un producto

Honestamente, en orden:

1. **Persistencia real.** El repositorio actual usa sqlite3 de la stdlib porque el entorno
   donde se construyó no tiene acceso a PyPI (no hay SQLAlchemy ni FastAPI disponibles).
   El esquema PostgreSQL está escrito y es el destino: `schema_postgres.sql`, con
   `calculated_value` separada de `input_value`, `audit_event` append-only y triggers de
   inmutabilidad. Todo el acceso a datos está en un módulo: migrar es reemplazarlo.
2. **Identity + autenticación.** Hoy el actor viene en un header. El `AuthorizationProvider`
   ya está separado y es reemplazable, que era la decisión arquitectónica importante.
3. **Persistir el grafo calculado.** Hoy se reconstruye en memoria en cada request (~3.200
   nodos, milisegundos, sirve para esta escala). Con 20 unidades y 5 años de histórico hay
   que persistir `calculated_value` y recalcular incrementalmente contra la base.
4. **Job engine** para importaciones grandes y escenarios pesados (`202 Accepted` + polling),
   idempotencia por `Idempotency-Key`, y optimistic locking con `If-Match`. El esquema ya
   tiene las columnas.
5. **UI.** No hay. El reporte HTML es un read model de demostración, no la aplicación.
6. **Decisiones funcionales todavía abiertas** — vale la pena cerrarlas antes de escalar
   el desarrollo:
   - ¿La nómina se carga por área a nivel sucursal, como está acá, o hace falta el detalle
     por puesto ya en V1?
   - ¿El driver de asignación de gastos corporativos es siempre ventas, o el CFO debería
     poder elegirlo por gasto (headcount, m², partes iguales)?
   - ¿Las comisiones son siempre % de ventas de la unidad, o hay escalas?
   - ¿Un escenario puede tocar el margen? Acá se permite como "escenario sobre el supuesto
     de margen", porque *Costos +5%* no se puede aplicar a un valor calculado de otra forma.

---

## Estructura

```
app/domain/     periods · money · config · inputs · graph · engine · ratios · validation
app/services/   budget (versión, workflow, aprobación, auditoría, autorización)
                scenarios · import_export · reporting · repository
app/api/        main.py (REST)
app/web/        main.py · forms.py · templates/  (interfaz)
seed/           demo.py (empresa completa) · report_html.py
tests/          test_rules.py · test_api_persistence.py
schema_postgres.sql
run_demo.py
```

Dependencias: `pydantic>=2`, `flask`, `openpyxl`, `gunicorn`. Nada más.
