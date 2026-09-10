# Inventario legal de Heraldo

> **Este documento lo GENERA `scripts/inventario_legal.py` desde el código.**
> No se edita a mano: la siguiente generación borraría el cambio, y
> `apps/api/tests/test_inventario_legal.py` se pone **roja** si lo que está
> comprometido aquí deja de ser lo que el código deriva hoy (RF-31).

Para regenerarlo: `uv run --no-sync python scripts/inventario_legal.py`.

## Qué es y qué no es

Son los **hechos** sobre los que se puede escribir el piso legal sin afirmar
nada que el sistema no haga. Aquí no hay ninguna calificación jurídica:
**quién es responsable, encargado o subencargado** lo decide **T-030·ter**,
con este inventario delante. El texto público lo escribe **T-030·quater**,
que lee la versión en JSON de este mismo documento.

Lo que todavía no existe se declara **pendiente**, nombrando la casilla que
lo trae. Esa ausencia está **medida contra el árbol del repositorio**: el día
que el artefacto exista, el generador se niega a seguir declarándola.

## 1. Datos

Todas las tablas del catálogo vivo, más los datos que no son tabla. El
*alcance* dice de quién son las filas: **de cliente** (aisladas por agencia y
por cliente), **de agencia** o **no-inquilino**.

| dato | forma | categoría | alcance | qué contiene | procedencia |
|---|---|---|---|---|---|
| agencias | tabla del catálogo | plataforma | de agencia | Identificador y nombre comercial de la agencia inquilina, con su fecha de alta. No contiene datos de ninguna persona. | apps/api/migrations/versions/0001_cimiento_del_inquilino.py:64 |
| alembic_version | tabla del catálogo | plataforma | no-inquilino | Catálogo de migraciones aplicadas: solo el identificador de la revisión. El rol de aplicación no tiene ningún privilegio sobre ella. | no la crea ninguna migración de este repositorio |
| bitacora | tabla del catálogo | bitácora | de cliente | Registro de solo inserción de quién hizo qué y cuándo sobre datos de un cliente (RF-10): rol e identificador opaco del actor, acción, recurso y un detalle estructurado. La aplicación no puede corregirla ni borrarla. | apps/api/migrations/versions/0003_la_base_y_la_cola.py:84 |
| clientes | tabla del catálogo | plataforma | de agencia | Registro del negocio cliente dentro de una agencia: nombre, sector declarado con su fecha de verificación, y la marca de alta de desarrollo. No contiene datos de ninguna persona. | apps/api/migrations/versions/0001_cimiento_del_inquilino.py:69 |
| heraldos | tabla del catálogo | plataforma | de cliente | Configuración de un agente de un cliente: nombre y fecha de alta. No contiene datos de ninguna persona. | apps/api/migrations/versions/0001_cimiento_del_inquilino.py:78 |
| mensajes_entrantes | tabla del catálogo | conversación | de cliente | Constancia de idempotencia de la entrada (RF-12): canal, identificador externo del mensaje tal como lo emite la plataforma de mensajería, el trabajo que produjo y el momento de recepción. No guarda el texto del mensaje. | apps/api/migrations/versions/0003_la_base_y_la_cola.py:191 |
| secretos | tabla del catálogo | credencial cifrada | de cliente | Credenciales del inquilino guardadas cifradas (columna binaria); no existe ninguna columna con el valor en claro y no se devuelven en claro por ninguna vía (RF-09). | apps/api/migrations/versions/0003_la_base_y_la_cola.py:62 |
| trabajos | tabla del catálogo | cola | de cliente | Trabajos pendientes o en curso del inquilino, con su estado, su número de intentos y el momento en que vuelven a estar disponibles. Su carga útil es la del trabajo encolado. | apps/api/migrations/versions/0003_la_base_y_la_cola.py:114 |
| trabajos_archivados | tabla del catálogo | cola | de cliente | Copia de los trabajos ya terminados, para consulta y purga por antigüedad. Se inserta, se consulta y se purga: no se puede reescribir. | apps/api/migrations/versions/0003_la_base_y_la_cola.py:152 |
| heraldo:idem:* | clave de Redis | derivado | de cliente | Marca de que un mensaje entrante ya se recibió. La clave lleva la agencia, el cliente, el canal y el identificador externo del mensaje; no guarda ningún contenido del mensaje. | apps/api/app/channels/idempotency.py:PREFIJO |
| heraldo:sesion:* | clave de Redis | plataforma | de agencia | Sesión abierta de una persona operadora: agencia, cliente y rol, más la huella del secreto de la sesión. El secreto no se guarda. | apps/api/app/tenancy/auth.py:PREFIJO_POR_DEFECTO |
| heraldo:limite:* | clave de Redis | derivado | de cliente | Contador de un límite por ventana. La clave lleva el nombre del límite y una huella irreversible del par (inquilino, dirección de red del remitente): la dirección no se guarda. | apps/api/app/tenancy/limits.py:PREFIJO_POR_DEFECTO |

## 2. Destinatarios

A quién sale un dato, para qué, dónde se trata y por qué mecanismo.

| destinatario | función | ubicación (región) | mecanismo | procedencia |
|---|---|---|---|---|
| proveedor de modelo: anthropic | validar contra el proveedor la credencial que registra el cliente, antes de guardarla (RF-18). Es una consulta a su catálogo de modelos: no envía ningún contenido del inquilino | pendiente (T-100·bis): su ficha de condiciones no está verificada ni fechada, y sin ficha no se registra ninguna credencial | petición HTTPS saliente por el punto único de salida (`egress.red.pedir`): solo `https` al 443, con el destino revalidado contra direcciones internas antes de conectar y en cada redirección | apps/api/app/agents/providers.py:LISTA_DECLARADA |
| proveedor de modelo: openai | validar contra el proveedor la credencial que registra el cliente, antes de guardarla (RF-18). Es una consulta a su catálogo de modelos: no envía ningún contenido del inquilino | pendiente (T-100·bis): su ficha de condiciones no está verificada ni fechada, y sin ficha no se registra ninguna credencial | petición HTTPS saliente por el punto único de salida (`egress.red.pedir`): solo `https` al 443, con el destino revalidado contra direcciones internas antes de conectar y en cada redirección | apps/api/app/agents/providers.py:LISTA_DECLARADA |
| plataforma de mensajería | entregar y recibir los mensajes del usuario final | pendiente (T-107/T-119) | pendiente (T-119): hoy `packages/egress/` no tiene ningún módulo de salida de mensajes, así que ningún mensaje sale de esta plataforma | packages/egress/ (medido: solo existe el punto de salida de red) |
| borde de la agencia (publicación y red de entrega) | servir las superficies públicas y proteger su entrada | fuera de este repositorio | no versionado aquí: es infraestructura de la agencia, descrita en la especificación §7. Este inventario no lo puede derivar y lo declara como límite en vez de afirmarlo | límite declarado del inventario (no derivable del repositorio) |
| respaldos | conservar copias de seguridad de la base | pendiente (T-213·bis) | pendiente (T-213·bis) | deploy/retencion_respaldos.toml no existe (comprobado en el árbol) |
| avisos internos del negocio cliente | avisar al negocio por el canal que él declare | pendiente (T-118) | pendiente (T-118) | apps/api/app/channels/destinos_internos.py no existe (comprobado en el árbol) |
| buzón de privacidad y de borrado | recibir y resolver las peticiones de las personas | pendiente (T-030) | pendiente (T-030) | apps/api/app/legal/buzon.py no existe (comprobado en el árbol) |
| puntos del código que llaman al punto único de salida | es la lista derivada de quién puede mandar algo fuera: `apps/api/app/agents/providers.py` | no aplica | petición HTTPS saliente por el punto único de salida (`egress.red.pedir`): solo `https` al 443, con el destino revalidado contra direcciones internas antes de conectar y en cada redirección | barrido de `apps/` y `packages/` (llamadas a `egress.red.pedir`) |

## 3. Cookies y almacenamiento, por superficie

**Medido**, no declarado: el generador construye la aplicación real y recorre
todas sus rutas registradas anotando cada cabecera `Set-Cookie` que emite.

| superficie | estado | cookies | almacenamiento del navegador | procedencia |
|---|---|---|---|---|
| interfaz de programación (HTTP) | construida | ninguna: no emite `Set-Cookie` en ninguna de sus rutas | no sirve ninguna página propia. Las páginas de documentación de interfaz, cuando la decisión de entorno las expone, cargan su guion de un CDN de terceros declarado en `superficie_publica`; su almacenamiento lo decide ese guion y no se puede medir desde el servidor | medido sobre 6 pares (método, ruta) de `app.main.crear_aplicacion` |
| panel de la agencia | no construida (T-101) | pendiente (T-101) | pendiente (T-101) | apps/web/ solo contiene su marcador de posición |
| portal del cliente | no construida (T-200) | pendiente (T-200) | pendiente (T-200) | apps/web/ solo contiene su marcador de posición |
| widget | no construida (T-400) | pendiente (T-400) | pendiente (T-400) | apps/web/ solo contiene su marcador de posición |

### 3.1 La medida, ruta a ruta

La aplicación se construye apuntando a una base que **no existe a propósito**,
y sin identidad cableada: así ninguna ruta medida puede tocar una fila de
verdad. Por eso el *código* de algunas filas es el de una dependencia caída o
el de una ruta que no atiende — lo que se está midiendo aquí no es la salud
del sistema, sino **qué cabeceras emite cada ruta**.

| método | ruta | código | Set-Cookie | procedencia |
|---|---|---|---|---|
| DELETE | /clientes/{cliente_id} | 503 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |
| GET | /docs | 200 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |
| GET | /openapi.json | 200 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |
| GET | /redoc | 200 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |
| GET | /salud/disponibilidad | 503 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |
| GET | /salud/vivacidad | 200 | ninguna | medido sobre `app.main.crear_aplicacion` por su interfaz ASGI |

## 4. Licencias de terceros

Los paquetes de la resolución bloqueada (`uv.lock`) con la licencia que
declaran sus metadatos instalados.

| paquete | versión | licencia | fuente del dato | procedencia |
|---|---|---|---|---|
| alembic | 1.19.1 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| annotated-doc | 0.0.5 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| annotated-types | 0.8.0 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| anyio | 4.14.2 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| certifi | 2026.7.22 | MPL-2.0 | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| cffi | 2.1.1 | MIT-0 | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| click | 8.5.0 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| colorama | 0.4.6 | BSD License | metadatos instalados (Classifier) | uv.lock:[[package]] + importlib.metadata |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| fastapi | 0.141.1 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| greenlet | 3.5.5 | MIT AND PSF-2.0 | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| h11 | 0.16.0 | MIT | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| heraldo | 0.0.0 | propia: ver `LICENSE` en la raíz del repositorio | miembro del espacio de trabajo | uv.lock:[[package]] (source editable) |
| heraldo-api | 0.0.0 | propia: ver `LICENSE` en la raíz del repositorio | miembro del espacio de trabajo | uv.lock:[[package]] (source editable) |
| heraldo-egress | 0.0.0 | propia: ver `LICENSE` en la raíz del repositorio | miembro del espacio de trabajo | uv.lock:[[package]] (source editable) |
| heraldo-worker | 0.0.0 | propia: ver `LICENSE` en la raíz del repositorio | miembro del espacio de trabajo | uv.lock:[[package]] (source editable) |
| httpcore | 1.0.9 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| httptools | 0.8.0 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| httpx | 0.28.1 | BSD-3-Clause | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| idna | 3.19 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| iniconfig | 2.3.0 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| mako | 1.4.1 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| markupsafe | 3.0.3 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pluggy | 1.6.0 | MIT | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| psycopg | 3.3.4 | LGPL-3.0-only | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| psycopg-binary | 3.3.4 | LGPL-3.0-only | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pycparser | 3.0 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pydantic | 2.13.4 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pydantic-core | 2.46.4 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pygments | 2.21.0 | BSD-2-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pytest | 9.1.1 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| pytest-asyncio | 1.4.0 | Apache-2.0 | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| python-dotenv | 1.2.3 | BSD-3-Clause | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| pyyaml | 6.0.3 | MIT | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| redis | 8.1.0 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| ruff | 0.16.4 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| sqlalchemy | 2.0.52 | MIT | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| starlette | 1.6.0 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| typing-extensions | 4.16.0 | PSF-2.0 | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| typing-inspection | 0.4.4 | MIT | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| tzdata | 2026.3 | Apache-2.0 | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| uvicorn | 0.52.4 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |
| uvloop | 0.22.1 | MIT License | declarada (License): no se instala en Windows (`sys_platform != 'win32'` en uv.lock) | scripts/inventario_legal.py:LICENCIAS_DE_PAQUETES_CONDICIONADOS |
| watchfiles | 1.2.0 | MIT | metadatos instalados (License) | uv.lock:[[package]] + importlib.metadata |
| websockets | 17.1 | BSD-3-Clause | metadatos instalados (License-Expression) | uv.lock:[[package]] + importlib.metadata |

## 5. Retención vigente, por categoría

El plazo que hoy aplica el código, leído de la constante que lo fija.

| categoría | plazo vigente | punto de inicio | procedencia |
|---|---|---|---|
| cola | 1 día | desde que el trabajo termina; después pasa al archivo | apps/worker/cola.py:RETENCION_EN_CALIENTE |
| cola | 30 días | desde que el trabajo se archiva; después se purga | apps/worker/cola.py:RETENCION_EN_ARCHIVO |
| derivado | 7 días | desde que se recibe el mensaje (marca de idempotencia en Redis) | apps/api/app/channels/idempotency.py:VIGENCIA |
| plataforma | 8 horas | desde que se abre la sesión. Es un techo absoluto: no se renueva por usarla | apps/api/app/tenancy/auth.py:TTL_SESION_SEGUNDOS |
| derivado | la ventana que declare cada límite en su punto de uso | desde la primera petición de la ventana. Hoy ningún módulo declara un límite: la clase existe y nadie la instancia todavía | apps/api/app/tenancy/limits.py:Limite.ventana_segundos |
| bitácora | fuera de la retención por antigüedad (RF-10) | no se borra por tiempo: es de solo inserción por diseño y la aplicación no la puede reescribir. El residuo seudónimo del borrado de una persona (RF-49·bis) todavía no está construido: pendiente (T-212·ter) | apps/api/app/tenancy/rol.py:PRIVILEGIOS_DE_APLICACION |
| identificación | pendiente (T-213·bis) | pendiente (T-213·bis) | apps/api/app/tenancy/retention.py no existe (comprobado en el árbol) |
| conversación | pendiente (T-213·bis) | pendiente (T-213·bis) | apps/api/app/tenancy/retention.py no existe (comprobado en el árbol) |
| consentimiento | pendiente (T-213·bis) | pendiente (T-213·bis) | apps/api/app/tenancy/retention.py no existe (comprobado en el árbol) |
| conocimiento del negocio | pendiente (T-213·bis) | pendiente (T-213·bis) | apps/api/app/tenancy/retention.py no existe (comprobado en el árbol) |
| uso | pendiente (T-213·bis) | pendiente (T-213·bis) | apps/api/app/tenancy/retention.py no existe (comprobado en el árbol) |
| respaldos | pendiente (T-213·bis) | pendiente (T-213·bis) | deploy/retencion_respaldos.toml no existe (comprobado en el árbol) |

## 6. Identidades de plataforma y datos de operadores

| identidad | estado | qué contiene | procedencia |
|---|---|---|---|
| usuarios_agencia | pendiente (T-101) | personal de la agencia que opera la plataforma; queda fuera de la vía de borrado del usuario final (política interna de la agencia) | catálogo vivo (pg_class): la tabla no existe |
| usuarios_cliente | pendiente (T-212·sexies) | personas del negocio cliente: usuarios del portal y la identidad de plataforma del administrador que conecta el canal | catálogo vivo (pg_class): la tabla no existe |
| actor de la bitácora | en el catálogo | el «quién» de la bitácora se registra como rol más identificador opaco, nunca nombre ni correo (RF-10) | apps/api/migrations/versions/0003_la_base_y_la_cola.py |

## Límites declarados de este inventario

- El borde de la agencia que publica las superficies no vive en este repositorio: no se deriva y se declara como límite.
- El almacenamiento del navegador (cookies propias, `localStorage`) solo se puede medir cuando exista una superficie de navegador; hoy no hay ninguna.
- Este inventario no dice quién es responsable, encargado ni subencargado de nada: eso lo decide T-030·ter con estos hechos delante.
- La licencia de un paquete que solo se instala en un sistema operativo se declara en el guion y se cruza contra los metadatos en el entorno que sí lo instala.
