# Heraldo

**Plataforma multi-inquilino de agentes de IA en marca blanca**, de AetherLogik.

Un heraldo viste los colores de su señor y habla en su nombre, sin que nunca se
le confunda con él. Esa es, literalmente, la definición de la marca blanca: cada
cliente tiene su agente, su portal, su marca y su dominio — y la garantía de que
sus datos no se cruzan jamás con los de otro cliente, **porque el aislamiento lo
impone la base de datos y no la disciplina de quien programa**.

> **Estado: cimiento.** Aquí todavía no hay producto: ningún heraldo responde a
> nadie. Lo que existe es el andamio, el gate que lo verifica, el aislamiento
> entre inquilinos impuesto por la base, y la **superficie HTTP** con sus tres
> garantías —quién puede hablar con la API, si está viva y si puede atender, y
> que nada irreversible ocurra sin confirmación—. Se construyó en este orden a
> propósito.

## Por qué el gate va primero

La auditoría del referente del mercado midió doce defectos. El más grave no fue
un fallo de código: **tenía buenos tests y nada los corría**. Un repositorio con
tests que nadie ejecuta da la sensación de estar verificado sin estarlo.

Por eso el orden aquí está invertido respecto a la costumbre: **el gate de
verificación existe antes que la primera función**. Está en
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) y corre en cada propuesta
de cambio. La prueba de aislamiento entre inquilinos —la que funda el
producto— ya cuelga de este mismo gate, contra un **Postgres real** levantado
como servicio del trabajo: RLS, `FORCE`, `SET LOCAL` y el rechazo de un `INSERT`
ajeno son comportamiento del motor, y un simulacro mediría el simulacro.

> **Y lo impide de verdad.** La rama principal exige la comprobación
> `verificacion`, y la regla **incluye a quien administra** (`enforce_admins`):
> con el gate en rojo GitHub rechaza la fusión, también al dueño del
> repositorio. No es una promesa de este README — está medido por efecto:
> se intentó fusionar un cambio con el CI en rojo y GitHub lo rechazó, con su
> control en verde. Lo aplica
> [`deploy/proteger_rama.py`](deploy/proteger_rama.py), que **deriva** del
> propio `ci.yml` las comprobaciones a exigir y **relee** lo aplicado, porque un
> `200` dice que la petición se aceptó, no que el estado quedara como se pidió.

## Cómo está organizado

```
apps/api        recibe, verifica, encola y sirve el panel
apps/worker     consume la cola y genera
apps/web        panel de agencia + portal de cliente + widget
packages/egress el punto ÚNICO por donde sale todo: red y mensajes
packages/review la capa de revisión de lo entrante y lo saliente
docs/           documentación versionada con el código
```

### Una regla que explica media arquitectura

`packages/egress` es un paquete de **primer nivel**, no un módulo dentro de
`apps/api`. La razón es concreta: `api` y `worker` corren como servicios
separados. Si la salida viviera dentro de uno, el otro acabaría **copiándola**,
y esa copia sería un segundo camino de salida que se salta las comprobaciones
—destino, consentimiento, ventana, cupo, corte—. Con un solo punto, olvidarse de
una comprobación es imposible: no hay dónde olvidarla.

Los dos servicios lo **declaran como dependencia**, y hay una prueba que se pone
en rojo si un servicio nuevo se olvida de declararlo. La regla no depende de que
alguien la recuerde.

## Trabajar en el repositorio

Necesitas [uv](https://docs.astral.sh/uv/), Python 3.12, **un Postgres 16 al que
puedas conectarte como administrador** y **un Redis**. Ninguno de los dos tiene
doble: si falta cualquiera, la suite **se pone en rojo y dice por qué** — no se
salta. Un `skip` pintaría de verde una corrida que no midió nada, que es justo
el defecto que este producto existe para no repetir.

> Redis no es opcional porque la idempotencia (RF-12) tiene **dos** defensas —una
> clave en Redis y una restricción única en la base— y la sonda que importa es la
> que comprueba que, **con Redis vacío**, la que atrapa el duplicado es la base.
> Con un doble se mediría el doble.

Los dos DSN se declaran **por entorno**; en el repositorio no hay ninguna
credencial:

```bash
export HERALDO_DATABASE_URL_ADMIN=postgresql+psycopg://USUARIO:CLAVE@HOST:5432/heraldo_test
export HERALDO_REDIS_URL=redis://[:CLAVE@]HOST:6379/0

uv sync --all-packages --dev   # prepara el entorno del workspace completo
uv run ruff check .            # lint
uv run pytest                  # la suite (migra la base y siembra el escenario)
```

La clave de cifrado de secretos (`HERALDO_SECRETOS_CLAVE`) **no se declara para
correr la suite**: se genera una en cada corrida y muere con ella. En un
despliegue real sí se declara por entorno, y `app.tenancy.secrets.genera_clave()`
produce una con la forma correcta. En el repositorio no vive ninguna.

Es el DSN del rol **migrador**, dueño de las tablas — nunca el de la aplicación.
Esa separación no es higiene: es el punto 1 del mecanismo de aislamiento, porque
el dueño de una tabla ignora sus propias políticas salvo `FORCE`. El rol de
aplicación lo crea la migración, sin `LOGIN`, y la suite le pone una contraseña
distinta en cada corrida que muere con ella.

Los comandos son exactamente los que corre el CI. Si pasan aquí, pasan allá; si
fallan allá, fallan aquí. En **Windows** hay una diferencia y está resuelta en el
andamiaje, no en el código: psycopg no funciona sobre el bucle de eventos que
asyncio usa allí por defecto, así que la suite fuerza el bucle *selector* — el
efecto es que Windows corre **los mismos** tests, no menos (**P-07**).

## La superficie HTTP

La API se sirve con una **fábrica**, no con un objeto de módulo: construir la
aplicación al importar obligaría a tener el entorno completo sólo para poder
leer el archivo.

```bash
export HERALDO_ENTORNO=produccion
export HERALDO_ORIGENES_PERMITIDOS=https://panel.tudominio.com,https://portal.cliente.com
export HERALDO_DATABASE_URL=...        # el DSN del rol de APLICACIÓN

uv run uvicorn app.main:crear_aplicacion --factory
```

Las tres variables **se declaran; no se adivinan**. Si falta cualquiera, el
proceso no arranca y dice cuál falta. No hay valor por defecto para los
orígenes, ni siquiera en desarrollo: el valor cómodo de hoy es el `localhost`
que aparece en producción mañana.

### Quién puede hablar con la API (RF-30)

Los orígenes permitidos se enumeran uno a uno, y **el entorno forma parte del
criterio**:

| Origen declarado | `desarrollo` | fuera de desarrollo |
|---|---|---|
| `*` o `null` | rechazado | rechazado |
| `http://localhost:5173` | **admitido** | **rechazado** |
| una dirección privada (`192.168.…`) | admitido | rechazado |
| `http://` sin cifrar | admitido | rechazado |
| `https://panel.ejemplo.com/` (con barra) | rechazado | rechazado |

Nunca se usa una expresión regular de orígenes: es el defecto que se midió en el
producto de referencia, donde un patrón admitía el bucle local **en producción**.
Una lista se puede escribir bien o mal; un patrón se escribe «casi bien».

### Las dos sondas (RF-51)

| Ruta | Contesta | Quién la usa |
|---|---|---|
| `GET /salud/vivacidad` | «el proceso está en pie», y nada más | el orquestador, para decidir si **reinicia** |
| `GET /salud/disponibilidad` | «puedo recibir tráfico», dependencias incluidas | el balanceador, para decidir si te **manda peticiones** |

No son la misma sonda con dos nombres. Con la base caída, la de disponibilidad
devuelve `503` y la de vivacidad sigue devolviendo `200` — si también fallara,
el orquestador reiniciaría el proceso una y otra vez mientras la base sigue
caída, y el bucle de reinicio se lleva por delante el trabajo en curso.

### Nada irreversible sin confirmación (RNF-06)

Una operación destructiva sobre los datos de un cliente **no se ejecuta a la
primera**. La primera petición devuelve `409` con el inventario: qué se destruye,
cuánto, tabla por tabla, y una confirmación derivada de ese recuento. La segunda
petición lleva esa confirmación, y entonces sí se ejecuta.

```
DELETE /clientes/{id}                              → 409 + inventario + confirmación
DELETE /clientes/{id}?confirmacion=<la de arriba>  → 200, hecho
```

- **No existe un «sí» genérico.** `?confirmacion=si` no vale: el único valor
  válido se calcula a partir del inventario, así que no se puede escribir sin
  haberlo recibido.
- **Si el recuento cambió**, la confirmación caduca. Confirmaste 412 filas; no se
  destruyen 900 en tu nombre.
- **Si no se puede contar, no se confirma.** Nunca se destruye a ciegas.
- El universo de lo que se destruye **se deriva del catálogo**: una tabla nueva
  entra sola en el inventario el día que su migración la cree.

> **Hoy esta ruta no destruye nada en producción, y es a propósito.** Necesita
> dos cosas que todavía no existen: la identidad autenticada (T-015) y la
> bitácora de sólo inserción (T-017). Sin cualquiera de las dos responde `503`.
> Son cerrojos, no adornos: una operación irreversible sobre datos de un cliente
> no se ejecuta sin saber **quién** la pidió ni dejar **rastro** (RF-10).

## Reglas de la casa que aplican a este repositorio

- **BYOK siempre.** Cada cliente paga sus propios costos de modelo y de
  mensajería, con su cuenta y su método de pago. La plataforma nunca adelanta
  el costo de nadie ni pone su credencial por un cliente.
- **Cero credenciales versionadas.** Ningún `.env`, ninguna clave, ninguna
  cadena de conexión. Ni siquiera de ejemplo con aspecto de real.
- **Aislamiento total entre clientes**, y también entre la agencia y sus
  clientes: datos, credenciales, cuentas de plataforma y repositorios.
- **La documentación vive aquí**, versionada con el código que describe.
- **Todo problema no trivial deja su entrada** en
  [`docs/heraldo-problemas.md`](docs/heraldo-problemas.md), con su prevención
  cableada como prueba.

## Seguridad

¿Encontraste un fallo de seguridad? [`SECURITY.md`](SECURITY.md) te dice cómo
avisarnos en privado y qué puedes esperar de nosotros.

---

Producto de **AetherLogik**. Repositorio **público**: el trabajo anterior se
hizo en un repositorio privado que se conserva como archivo histórico, porque
reescribir su historia para publicarla habría destruido la evidencia de cada
casilla cerrada.
