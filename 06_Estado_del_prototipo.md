**Estado del prototipo — Sistema de Presupuestación V1**

**Fecha:** agosto 2026
**Estado:** motor completo y ejecutable, en iteración funcional antes de persistir

**1. Qué existe hoy**

Un prototipo que corre de punta a punta: interfaz web, API REST y motor de cálculo, con 90 tests que verifican reglas concretas del spec. Cubre el criterio de aceptación global del doc 02 §64.

| Componente | Estado |
| --- | --- |
| Configuration Engine + wizard de 9 pasos (CFO/COO) | Completo |
| Dependency Graph, recálculo incremental, impact analysis, explicabilidad | Completo |
| Calculation Engine: ventas, costo, gastos, nómina, CAPEX, stock, balance, ratios | Completo |
| Catálogo de 23 ratios con objetivos y dependencias | Completo |
| Versionado inmutable con snapshot de configuración | Completo |
| Workflow, aprobación parcial, autorización por capacidades y alcance, auditoría | Completo |
| Escenarios como overlay sobre inputs | Completo |
| Importación masiva atómica con plantilla generada desde la configuración | Completo |
| Interfaz web y API REST | Completo |
| **Persistencia en base de datos** | **Pendiente — hoy el estado vive en memoria** |
| Identity y login con contraseña | Pendiente |

**2. Decisiones de diseño tomadas**

**2.1 El grafo de dependencias es real**

Cada número es un nodo MÉTRICA|ÁMBITO|PERÍODO con sus dependencias declaradas. La empresa demo genera unos 3.300 nodos. De ahí salen el recálculo incremental, el análisis de impacto previo a un cambio y la explicación de cualquier valor.

**2.2 Un valor calculado nunca se carga**

INPUT y CALCULATED son tipos distintos, con tablas distintas en el modelo de datos. No existe endpoint para escribir un calculado.

**2.3 La unidad de cuenta interna es la moneda de presentación**

Cada input se convierte al entrar al grafo: TC promedio del período para flujos, TC de cierre para stocks.

**2.4 Un faltante no es un cero**

Un ratio sin denominador se reporta como *no calculable*, nunca como 0%.

**2.5 La configuración manda**

De ella salen las tareas, los formularios, las planillas, las validaciones y la obligatoriedad de los datos. No hay lista rígida: se deriva del modelo.

**3. Interpretaciones que hubo que resolver**

Puntos donde el spec no cerraba solo y se tomó una decisión explícita:

| Tema | Decisión |
| --- | --- |
| Balance (§26 vs §34) | El total de patrimonio es calculado (Activo − Pasivo); los componentes se cargan y su suma debe coincidir. Si no, se rechaza la carga completa. |
| Trimestre con sucursal que abre a mitad | El valor va entero a los meses vigentes, no se reparte a los meses cerrados. |
| Driver de gastos corporativos | Ventas anuales, no mensuales: con estacionalidad el driver mensual da asignaciones erráticas. |
| Escenario sobre costos | Actúa sobre el supuesto de margen, porque el costo es un calculado. |
| Tipos de cambio | Se cargan TC estimado de inicio y de cierre; el sistema interpola día por día. La tabla guardada es diaria. |

**4. Correcciones aplicadas tras la primera revisión funcional**

1. Las sucursales se dan de alta a nivel empresa y se asignan a su unidad con selectores en ambos campos: el nombre existe una sola vez.

2. La modalidad de venta y la fórmula de margen pasaron a ser **del producto**, no de la unidad de negocio.

3. El producto "Otros" se controla **por familia**: cada familia necesita el suyo.

4. Nueva fórmula de margen **sin costo** (100%), para intangibles cuyo precio de venta no tiene costo asociado.

5. Un gasto puede imputarse a **varios destinos a la vez** — sucursales, centros de costo, unidades y empresa — con un importe por destino (0 donde no corresponde) o un total repartido por porcentajes.

**5. Decisiones funcionales todavía abiertas**

Conviene cerrarlas antes de persistir, porque cambiar el modelo con datos guardados cuesta bastante más:

- ¿La nómina se carga por área a nivel sucursal, o hace falta el detalle por puesto ya en V1?

- ¿El driver de asignación de gastos corporativos es siempre ventas, o el CFO debería poder elegirlo por gasto (dotación, metros cuadrados, partes iguales)?

- ¿Las comisiones son siempre un porcentaje de las ventas de la unidad, o existen escalas?

- ¿Alcanza con que el stock se administre por familia, o hace falta por producto?

- ¿El CAPEX necesita algún dato más además de categoría, ámbito, período e importe?

**6. Próximo paso acordado**

Seguir iterando sobre el modelo funcional hasta que esté robusto. Recién entonces:

- PostgreSQL con esquema ya escrito (schema_postgres.sql), repositorio write-through y arranque idempotente.

- Login con usuario y contraseña, reemplazando el selector de roles.

- Empresa demo que se carga sola sólo si la base está vacía.

**7. Cómo probarlo**

Está desplegado en Render. Para armar una empresa desde cero: entrar como CFO y usar **Presupuestos → Crear un presupuesto nuevo**. La empresa demo viene con la configuración cerrada a propósito, para mostrar el sistema ya en operación.

Advertencia: mientras no haya base de datos, cada redeploy o cada vez que el servicio se duerme por inactividad borra lo cargado y vuelve la demo original.
