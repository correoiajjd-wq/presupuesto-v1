# Sistema de Presupuestación V1 — prototipo ejecutable del núcleo

Implementación funcionando de la parte que decide si el sistema sirve o no:
**configuración → inputs → grafo de dependencias → cálculo → validación → workflow → versión**.

No es el producto terminado. Es el motor, con la cadena completa andando de punta a punta
sobre una empresa demo, y 133 tests que verifican reglas concretas del spec.

```
PYTHONPATH=. python3 wsgi.py                           # interfaz web en :8000
PYTHONPATH=. python3 run_demo.py --html reporte.html   # recorrido end-to-end por consola
PYTHONPATH=. python3 -m unittest discover -s tests     # 133 tests
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
declaradas. La demo genera ~5.400 nodos y ~11.000 aristas. De ahí salen tres cosas que
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

Cada test cita la regla que verifica: `tests/test_rules.py` (dominio y servicios),
`tests/test_wizard.py` (el wizard y los formularios, simulando un navegador) y
`tests/test_api_persistence.py` (API y persistencia).
Las que más costó dejar bien:

- **Vigencia parcial + frecuencia.** Una sucursal que abre en junio y un producto de carga
  trimestral: el trimestre abril-junio va **entero a junio**, no un tercio a cada mes.
  El caso obvio (repartir y después poner cero en abril y mayo) pierde dos tercios del valor.
- **El permiso sale de la estructura, no de una tabla de roles.** El CFO designa un perfil
  responsable al crear cada centro de costo, y eso habilita a ese perfil a cargar los valores
  de ese centro —sus gastos y sus solicitudes de dotación— y sólo de ese. Designar responsables
  no requiere tocar código.
- **Las áreas piden, Nómina valoriza.** Nómina carga la foto inicial de cada centro de costo
  —cuánta gente hay y cuánto suma por mes— y después **cada solicitud de un área (alta, baja o
  ajuste) le llega para que le ponga su nominal**. Mientras quede una sin valorizar, la versión
  no se aprueba; y si un área agrega o cambia una solicitud después, la tarea de Nómina vuelve
  a revisión sola. Es la única dependencia entre tareas del sistema, y está declarada en una
  tabla, no escondida en un `if`.
- **La cantidad autorizada reajusta el importe sola.** Nómina valoriza una persona de la
  solicitud, así que si en la revisión se autorizan 6 de las 10 que se pidieron, el costo se
  recalcula con una regla de tres sin volver a pedirle nada a Nómina. Y el que autoriza los
  objetivos de venta de una operación ve, en la misma pantalla, la gente que se pidió para
  ella: aprobar lo uno es aprobar lo otro, y si no corresponde rechaza con el motivo y vuelve
  al que lo pidió.
- **Los aumentos siguen la fecha de cada movimiento.** Cada solicitud es una cohorte con fecha
  propia, así que quien entra en junio no cobra el aumento de marzo pero sí el de agosto. El
  alta paga desde su mes; la baja paga el mes en que ocurre, se descuenta desde el siguiente y
  arrastra los aumentos que esa persona ya había recibido — si no, el descuento quedaría corto
  contra una masa que sí creció. El **ajuste** cubre ascensos y cambios de jornada: mueve plata
  sin mover personas, y admite importe negativo.
- **Cada centro de costo tiene su responsable.** El perfil se elige al crear el centro, en la
  estructura, y de ahí sale quién carga sus gastos: la tarea de ese centro de costo es suya y
  no aparece en la tarea general de gastos.
- **La operación es la unidad mínima.** Una unidad de negocio puede operar en varias
  sucursales y una sucursal puede alojar varias unidades: la relación es n a n. Cada
  combinación es una **operación** con su propio centro de costo, y es ahí donde se cargan
  ventas y dotación y donde se imputan los gastos propios. Unidades y sucursales son dos
  agrupaciones distintas de las mismas operaciones, así que sus resultados **no se suman
  entre sí**: sumar unidad + sucursal contaría dos veces la misma operación.
- **Gastos corporativos.** Se asignan a las operaciones proporcionalmente a las ventas
  **anuales** (no mensuales: con ventas estacionales el driver mensual da asignaciones
  erráticas). La asignación es presentacional: se muestra debajo del EBITDA propio, y
  `Σ(resultado después de asignación de cada operación) = EBITDA de la empresa`. Hay un test
  que verifica esa identidad. A una unidad se le asigna lo corporativo y lo de las sucursales
  donde opera; a una sucursal, lo corporativo y lo de las unidades que operan ahí.
- **Un gasto, varios destinos.** Internet existe en todas las sucursales y en administración;
  el alquiler sólo en las sucursales que no son propias. Un gasto tiene una lista de destinos
  y dos modos de carga: un importe por cada destino (donde no corresponde se carga 0) o un
  total que se reparte con porcentajes fijos.
- **La comisión es del producto.** Dentro de una misma operación unos productos comisionan,
  otros comisionan distinto y otros no. La comisión de una operación es la suma de las ventas
  de cada producto por su propia tasa, y va al costo de nómina.
- **La fórmula de margen incluye "sin costo".** Para intangibles, donde el precio de venta es
  todo margen: el costo es 0 y el margen 100%. El motor trabaja con la proporción de costo de
  cada producto, así que las tres fórmulas conviven en la misma unidad.
- **El producto "Otros" es por familia, no por unidad.** Cada familia necesita su propio cajón
  para lo que no está en el catálogo; dos "Otros" en la misma familia se rechazan.
- **El código de producto es único en toda la empresa.** No alcanza con que no se repita dentro
  de la familia o de la unidad: es lo que se escribe en la planilla de carga, y ahí no hay nada
  que lo desambigüe.
- **Balance.** El total de patrimonio es calculado (`Activo − Pasivo`); los componentes
  (capital, resultados acumulados) se cargan, y si no coinciden con ese total el balance no
  cierra y **se rechaza la carga completa**. Es la única lectura de los §26 y §34 que no se
  contradice a sí misma.
- **Aprobación parcial.** Modificar ventas de Montevideo devuelve a revisión sólo esa tarea;
  la de Centro sigue aprobada.
- **Importación atómica.** Un error en una fila rechaza la planilla entera e informa
  fila / columna / valor / error / corrección esperada. El commit revierte todo si algo falla.
- **Cero vs vacío.** `0` es válido, vacío es error.
- **Los formularios son idempotentes.** Reenviar cualquier formulario del wizard tal como
  viene no cambia nada. Hay un test que renderiza cada paso, extrae los valores por defecto
  igual que haría un navegador y los reenvía: la configuración tiene que quedar idéntica.
  Es la garantía contra los valores por defecto que hacen algo que el usuario no pidió.
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
no baja a operación en V1.

---

## Empresa demo

ACME Distribución S.A., ejercicio 2027, presentación USD:

- **Repuestos** (venta por unidades) operando en Montevideo y en **Salto, que abre en junio**
- **Servicios** (venta por monto, en UYU, con comisión por producto) operando en Centro
  **y también en Montevideo**: la misma sucursal aloja dos unidades, cada una con su
  centro de costo. La relación n a n queda ejercitada en las dos direcciones.
- Administración central como área de soporte con su centro de costo
- Los cinco tipos de gasto: propio de sucursal, de unidad distribuido a sus operaciones,
  de centro de costo, distribuido 60/40 por porcentaje, y corporativo de empresa
- Nómina con foto inicial por centro de costo, dos aumentos y cargas del 17%, más dos altas,
  una baja y un ascenso pedidos por las áreas y valorizados por Nómina
- Stock por familia a nivel operación, CAPEX, balance inicial y proyectado

Resultado: ventas 10.915.200, EBITDA 2.593.059 (23,8%), 0 validaciones bloqueantes.
El reporte HTML (`reporte.html`) muestra todo eso.

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
2. **Estructura** — sucursales y unidades de negocio se dan de alta por separado, cada una
   una sola vez; después se crea cada **operación** eligiendo unidad y sucursal de sendos
   selectores y dándole su **centro de costo**, que es donde se van a registrar sus gastos.
   El nombre del centro de costo es único en toda la empresa —operaciones y áreas de soporte
   comparten el mismo espacio de nombres—, así que no hace falta un código aparte. Un área de
   soporte se da de alta junto con su centro de costo, en el mismo formulario.
   Una unidad puede operar en varias sucursales y una sucursal alojar varias unidades. Una
   sucursal donde no opera nadie, o una unidad que no opera en ningún lado, no genera cargas
   y no deja cerrar.
3. **Productos y familias** — lo define el **COO**. La modalidad de venta, la fórmula de
   margen y la comisión son **de cada producto**, no de la unidad: la misma unidad puede
   vender mercadería por unidades y servicios por monto, y cada producto comisiona distinto.
   Cada familia necesita su propio producto "Otros".
4. **Gastos** — qué existe, a qué **destinos** se imputa (varios a la vez), con qué
   frecuencia y moneda.
5. **Nómina** — moneda, reglas de aumento y conceptos porcentuales. La pantalla lista los
   centros de costo que salen de la estructura: son los que Nómina va a tener que valorizar, y
   no se definen acá. El nominal es mensual y se anualiza.
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
| **Mis tareas** | Un gerente sólo ve sus tareas. El alcance sobre una sucursal le alcanza a todas las operaciones que viven ahí; si intenta entrar a otra, el sistema lo frena. |
| **Carga** | El formulario se genera desde la configuración: sólo tus productos, tus períodos, tu moneda. El precio y el margen se muestran pero no se editan. Guardás borrador o enviás a revisión. |
| **Carga masiva** | Descargás la planilla Excel de tu operación, la llenás, la subís. Si tiene un error, se rechaza entera y te muestra fila, columna, valor, error y corrección esperada. |
| **P&L** | Consolidado, por unidad, por sucursal y por operación, anual o mes a mes, con los corporativos separados debajo del EBITDA. |
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
- Entrá como **Martín (gerente Montevideo)** y fijate que ve las dos operaciones de su
  sucursal —Repuestos y Servicios— y ninguna de Salto.
- En **Ventas**, dejá una celda vacía y guardá: pasa. Ahora bajá la planilla Excel, dejá una
  celda vacía y subila: la rechaza entera, porque en carga masiva vacío es error.
- Cargá un producto trimestral en un período que no sea cabecera de trimestre: `INVALID_FREQUENCY`,
  y te dice cuál es el período correcto.
- Entrá como gerente, pedí un alta y fijate que aparece como "pendiente en Nómina" y que el
  cierre se bloquea. Entrá como Nómina, ponele el número, y mirá cómo el costo laboral sube
  sólo desde el mes del alta.
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
tests/          test_rules.py · test_wizard.py · test_api_persistence.py
schema_postgres.sql
run_demo.py
```

Dependencias: `pydantic>=2`, `flask`, `openpyxl`, `gunicorn`. Nada más.
