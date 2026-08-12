# Ponerlo online — sin usar la terminal

Son dos pasos: subir el código a GitHub y conectarlo a Render. Unos 5 minutos.
No hace falta instalar nada ni tocar la consola.

---

## 1. Subir el código a GitHub

1. Descomprimí `presupuesto-v1.zip`. Vas a ver los archivos sueltos: `app`, `seed`,
   `tests`, `README.md`, `render.yaml`, `requirements.txt`, `wsgi.py`, etc.
   **No hay una carpeta que los envuelva, y eso es a propósito.**
2. Entrá a **[github.com/new](https://github.com/new)**.
3. Nombre: `presupuesto-v1`. Dejalo **público** (Render lee repos privados también,
   pero público es un paso menos). No marques ninguna casilla de "Add a README".
   → **Create repository**.
4. En la pantalla que aparece, hacé clic en **"uploading an existing file"**.
5. Arrastrá **todos los archivos y carpetas** que descomprimiste en el paso 1.
   Ojo acá: arrastrá el *contenido*, no una carpeta que los contenga —
   `render.yaml` tiene que quedar en la raíz del repositorio.
6. Abajo, **Commit changes**.

Verificá antes de seguir: en la página principal del repo tenés que ver `render.yaml`
y `wsgi.py` en el listado. Si están dentro de una carpeta, borrá el repo y repetí
el paso 5 arrastrando el contenido.

---

## 2. Deploy en Render

1. Entrá a **[render.com](https://render.com)** y elegí **Get Started / Sign up with GitHub**.
   El plan gratuito no pide tarjeta.
2. **New → Blueprint** (no "Web Service": el Blueprint es el que lee `render.yaml`
   y completa la configuración solo).
3. Conectá tu cuenta de GitHub si te lo pide y elegí el repositorio `presupuesto-v1`.
4. Render muestra el servicio `presupuesto-v1` que encontró en el archivo.
   → **Apply / Create**.
5. Esperá el build. La primera vez tarda 2 a 4 minutos.

Cuando termine, arriba tenés la URL: algo como
`https://presupuesto-v1.onrender.com`. Entrás, elegís un rol y ya está.

### Si preferís hacerlo a mano

Si en vez de Blueprint usás **New → Web Service**, completá:

| Campo | Valor |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn wsgi:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT` |
| Instance type | Free |

El `--workers 1` no es opcional: el presupuesto vive en memoria y con varios
workers cada uno tendría su propia copia.

---

## Qué esperar del plan gratuito

- **El servicio se duerme** tras unos 15 minutos sin uso. La primera visita después
  de eso tarda ~40 segundos en responder. No está roto, está despertando.
- **Al despertarse arranca de cero**: vuelve la empresa demo original y se pierde
  lo que hayas cargado. Es esperable — el prototipo guarda todo en memoria, no en
  una base de datos. Conectar PostgreSQL es el primer punto de la lista de
  pendientes del README.
- Cualquiera con la URL puede entrar: no hay autenticación real, se elige el rol
  de una lista. No subas datos reales de la empresa.

---

## Por dónde empezar cuando lo abras

Entrá primero como **Laura (CFO)** y mirá el panel: progreso, checklist de
configuración, qué falta para aprobar. Después salí y entrá como **Martín
(gerente Montevideo)** para ver lo mismo desde el otro lado: sólo sus tareas,
sólo su sucursal.

El README tiene la lista de cosas que vale la pena intentar romper.
