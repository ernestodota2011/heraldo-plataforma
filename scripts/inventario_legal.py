"""T-030-bis (RNF-09, RF-31) — los inventarios que alimentan el piso legal, DERIVADOS del codigo.

Este guion NO redacta el piso legal. Produce los HECHOS sobre los que el piso
legal se puede escribir sin mentir: que datos existen, a quien salen, que emite
cada superficie servida, con que licencias de terceros corre, cuanto se guarda
cada cosa y que identidades de plataforma trata. Todo sale de leer el codigo, el
catalogo vivo de la base y el bloqueo de dependencias — nunca de la
especificacion, que es intencion y no sistema.

# WHY (P-51): la v1 del piso legal se REDACTO desde el diseno y afirmo como
# hechos ocho cosas que nada sostenia — «no se cruza jamas», «no hay mas
# destinatarios», «sin identificarte». La revision legal externa las conto una a
# una. La prevencion no es revisar mejor: es que el texto publico se GENERE desde
# inventarios derivados, y que algo se ponga rojo el dia que el codigo y el
# documento dejen de coincidir. Eso lo hace `apps/api/tests/test_inventario_legal.py`.

Lo que este guion NO decide, dicho en voz alta:

- **Quien es responsable, encargado o subencargado.** Ni de un destinatario, ni
  de una operacion. Eso es **T-030-ter** (la matriz de tratamientos y roles), y
  su gate de cierre es precisamente que toda operacion y todo destinatario de
  ESTE inventario tenga fila alli. Aqui no hay ninguna calificacion juridica.
- **Que promete el texto publico.** Eso es **T-030-quater**, que lee el JSON de
  aqui y escribe la v2 con su tabla promesa -> evidencia.
- **Nada que no este en el codigo.** Lo que todavia no existe se declara
  `pendiente (T-xxx)` nombrando la casilla que lo trae, y esa ausencia se
  COMPRUEBA contra el arbol: el dia que el artefacto aparezca, este guion se
  niega a seguir declarandolo pendiente.

Granularidad maxima publicable (I-10-09, RNF-08): **categoria - funcion -
region**. Nunca un equipo, una direccion de red, un contenedor ni un nombre de
servidor. La salida pasa por `scripts/publicable.py` ANTES de escribirse: un
inventario que no pase el gate no se escribe.

Uso:
    uv run --no-sync python scripts/inventario_legal.py              # escribe
    uv run --no-sync python scripts/inventario_legal.py --verificar  # comprueba

Codigos de salida, uno por clase de fallo (no se mezclan: cada uno pide una
receta distinta de quien lee el CI):

- **0** — todo en orden.
- **1** — lo comprometido en `docs/legal/` DIVERGE de lo derivado: se regenera.
- **2** — NO SE PUDO DERIVAR el inventario (una tabla sin categoria, un
  destinatario nuevo, el catalogo de otra rama, la salida no publicable). No se
  arregla regenerando: hay que mirar lo que dice el mensaje.

Necesita el DSN del rol migrador en `HERALDO_DATABASE_URL_ADMIN` y el esquema
migrado: el bloque de datos se deriva del CATALOGO VIVO, igual que
`test_rls_cobertura` y `app/tenancy/confirmacion.py`.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import os
import re
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from app.channels import idempotency
from app.main import Entorno, crear_aplicacion
from app.superficie_publica import ExposicionDeDocumentacion
from app.tenancy import auth, crear_motor, limits
from app.tenancy.politicas import COLUMNA_AGENCIA, COLUMNA_CLIENTE
from worker import cola

RAIZ = Path(__file__).resolve().parents[1]
SALIDA_POR_DEFECTO = RAIZ / "docs" / "legal"
NOMBRE_MD = "inventario.md"
NOMBRE_JSON = "inventario.json"

#: DSN del rol migrador. El mismo que declaran `conftest.py` y las migraciones.
VARIABLE_DSN_ADMIN = "HERALDO_DATABASE_URL_ADMIN"

#: Un extremo donde no escucha nadie, para construir la aplicacion sin base. Es
#: el mismo patron de `test_cabeceras.py`: la superficie se MIDE, no se consulta.
DSN_SIN_DESTINO = "postgresql+psycopg://heraldo_app@127.0.0.1:1/no_existe"

#: Con que se rellena un parametro de ruta al medirla. Es un valor imposible a
#: proposito: ninguna ruta medida puede tocar una fila de verdad.
PARAMETRO_DE_MEDIDA = "00000000-0000-4000-8000-000000000000"


# ==========================================================================
# Errores. Todos fallan CERRADO: un inventario a medias es peor que ninguno
# ==========================================================================
class InventarioIncompleto(RuntimeError):
    """Raiz de todo lo que impide derivar un inventario honesto."""


class SinCatalogo(InventarioIncompleto):
    """El esquema no tiene tablas: no hay catalogo del que derivar nada."""


class CatalogoDeOtraRevision(InventarioIncompleto):
    """La base no esta en la revision de ESTE repositorio: describiria otro esquema."""


class TablaSinCategoria(InventarioIncompleto):
    """Una tabla del catalogo sin categoria de dato declarada."""


class CategoriaDesconocida(InventarioIncompleto):
    """Una categoria que no esta en la lista: no vale inventarse una."""


class TablaConMediaClave(InventarioIncompleto):
    """`cliente_id` sin `agencia_id`: no es una clase, es un defecto (T-011)."""


class ArtefactoQueYaExiste(InventarioIncompleto):
    """Lo que se declaraba pendiente ya existe: hay que DERIVARLO, no declararlo."""


class SalidaNoDeclarada(InventarioIncompleto):
    """Aparecio un modulo de salida nuevo: el inventario no nombra ese destinatario."""


class SuperficieNoMedible(InventarioIncompleto):
    """Una ruta servida lanzo en vez de responder: no se puede decir que emite."""


class PrefijoSinInventariar(InventarioIncompleto):
    """Produccion declara una familia de claves que ninguna fila del inventario nombra."""


class DeclaracionDePrefijoMuerta(InventarioIncompleto):
    """Se declara inventariado un prefijo que ya no existe: tapa al siguiente que falte."""


class EntornoNoCorrespondeAlBloqueo(InventarioIncompleto):
    """Falta en el entorno un paquete que `uv.lock` declara: el inventario saldria corto."""


class LicenciaCondicionadaSinDeclarar(InventarioIncompleto):
    """Un paquete que solo se instala en un sistema operativo y sin licencia declarada."""


class DeclaracionDeLicenciaCaduca(InventarioIncompleto):
    """La licencia declarada ya no coincide con la que dicen los metadatos."""


class InventarioNoPublicable(InventarioIncompleto):
    """La salida no pasa el gate de publicabilidad: no se escribe."""


# ==========================================================================
# Las categorias de dato — ALLOWLIST
# ==========================================================================
#: Las categorias que existen. Redactado por allowlist: una tabla con una
#: categoria fuera de esta lista se cae (`feedback_denylist_por_allowlist`).
CATEGORIAS: tuple[str, ...] = (
    "identificación",
    "conversación",
    "consentimiento",
    "uso",
    "credencial cifrada",
    "bitácora",
    "cola",
    "conocimiento del negocio",
    "derivado",
    "plataforma",
)

#: Categoria de cada tabla del catalogo, con QUE CONTIENE escrito. Una tabla que
#: no este aqui pone el inventario en ROJO: es la unica forma de que una tabla
#: nueva no entre en silencio en un documento publico. El contenido se describe
#: por sus COLUMNAS reales, no por lo que se espera que guarde algun dia.
CATEGORIA_POR_TABLA: dict[str, tuple[str, str]] = {
    "agencias": (
        "plataforma",
        "Identificador y nombre comercial de la agencia inquilina, con su fecha de "
        "alta. No contiene datos de ninguna persona.",
    ),
    "clientes": (
        "plataforma",
        "Registro del negocio cliente dentro de una agencia: nombre, sector declarado "
        "con su fecha de verificación, y la marca de alta de desarrollo. No contiene "
        "datos de ninguna persona.",
    ),
    "heraldos": (
        "plataforma",
        "Configuración de un agente de un cliente: nombre y fecha de alta. No contiene "
        "datos de ninguna persona.",
    ),
    "secretos": (
        "credencial cifrada",
        "Credenciales del inquilino guardadas cifradas (columna binaria); no existe "
        "ninguna columna con el valor en claro y no se devuelven en claro por ninguna "
        "vía (RF-09).",
    ),
    "bitacora": (
        "bitácora",
        "Registro de solo inserción de quién hizo qué y cuándo sobre datos de un "
        "cliente (RF-10): rol e identificador opaco del actor, acción, recurso y un "
        "detalle estructurado. La aplicación no puede corregirla ni borrarla.",
    ),
    "trabajos": (
        "cola",
        "Trabajos pendientes o en curso del inquilino, con su estado, su número de "
        "intentos y el momento en que vuelven a estar disponibles. Su carga útil es la "
        "del trabajo encolado.",
    ),
    "trabajos_archivados": (
        "cola",
        "Copia de los trabajos ya terminados, para consulta y purga por antigüedad. "
        "Se inserta, se consulta y se purga: no se puede reescribir.",
    ),
    "mensajes_entrantes": (
        "conversación",
        "Constancia de idempotencia de la entrada (RF-12): canal, identificador "
        "externo del mensaje tal como lo emite la plataforma de mensajería, el trabajo "
        "que produjo y el momento de recepción. No guarda el texto del mensaje.",
    ),
    "alembic_version": (
        "plataforma",
        "Catálogo de migraciones aplicadas: solo el identificador de la revisión. El "
        "rol de aplicación no tiene ningún privilegio sobre ella.",
    ),
}

CLASE_CLIENTE = "de cliente"
CLASE_AGENCIA = "de agencia"
CLASE_NO_INQUILINO = "no-inquilino"
CLASE_MEDIA_CLAVE = "media-clave"


def alcance_de(columnas: set[str]) -> str:
    """De qué clase es la tabla, derivado de SUS COLUMNAS.

    # WHY (hay una segunda lectura de esto, y es deliberado): la clase la define
    # `test_rls_cobertura.clase_de`, que es una prueba, y un guion no importa
    # pruebas. Las dos leen las MISMAS dos constantes de `app.tenancy.politicas`,
    # y `test_inventario_legal` cruza las dos lecturas tabla a tabla: si algun
    # dia dejaran de coincidir, se pone rojo. Una segunda redaccion sin ese cruce
    # seria drift esperando.
    """
    tiene_agencia = COLUMNA_AGENCIA in columnas
    tiene_cliente = COLUMNA_CLIENTE in columnas
    if tiene_agencia and tiene_cliente:
        return CLASE_CLIENTE
    if tiene_agencia:
        return CLASE_AGENCIA
    if tiene_cliente:
        return CLASE_MEDIA_CLAVE
    return CLASE_NO_INQUILINO


# ==========================================================================
# Lo que todavia no existe — ausencias MEDIDAS, no frases
# ==========================================================================
#: Artefacto que traera cada pieza que hoy falta, y la casilla que lo construye.
#: Mientras el artefacto no exista, el inventario dice `pendiente (casilla)`. El
#: dia que exista, `exigir_que_sigan_pendientes` se cae: la pieza hay que
#: DERIVARLA de verdad, no seguir declarandola. Es lo contrario de una nota que
#: envejece en silencio.
ARTEFACTOS_PENDIENTES: dict[str, tuple[str, str]] = {
    "retencion_por_defecto": ("apps/api/app/tenancy/retention.py", "T-213·bis"),
    "retencion_de_respaldos": ("deploy/retencion_respaldos.toml", "T-213·bis"),
    "avisos_internos": ("apps/api/app/channels/destinos_internos.py", "T-118"),
    "buzon_de_privacidad": ("apps/api/app/legal/buzon.py", "T-030"),
}

#: Modulos que HOY viven en el punto unico de salida. El paquete es de primer
#: nivel a proposito (README): si aparece uno nuevo —la salida de mensajes, por
#: ejemplo— hay un destinatario nuevo de datos y este guion se niega a producir
#: un inventario que no lo nombre.
MODULOS_DE_SALIDA_CONOCIDOS: frozenset[str] = frozenset({"__init__", "red"})

#: Lo unico que hoy vive en `apps/web/`. Cualquier otra cosa es una superficie de
#: navegador empezando a existir, y una superficie que existe se MIDE.
ARTEFACTOS_DE_WEB_CONOCIDOS: frozenset[str] = frozenset({"README.md"})

#: Superficies de navegador que el producto tendra, con la casilla que las
#: construye. Su estado NO se declara: se deriva de `artefactos_de_navegador`.
SUPERFICIES_DE_NAVEGADOR: tuple[tuple[str, str], ...] = (
    ("panel de la agencia", "T-101"),
    ("portal del cliente", "T-200"),
    ("widget", "T-400"),
)


def exigir_que_sigan_pendientes(raiz: Path = RAIZ) -> None:
    """Si lo que se declara pendiente ya existe, este guion se niega a mentir."""
    for clave, (ruta, casilla) in sorted(ARTEFACTOS_PENDIENTES.items()):
        if (raiz / ruta).exists():
            raise ArtefactoQueYaExiste(
                f"{ruta} ya existe, asi que {clave} dejo de estar pendiente de "
                f"{casilla}: el inventario tiene que DERIVAR esa pieza de ese archivo "
                "en vez de seguir declarandola pendiente. Cambia este guion; no borres "
                "la entrada sin sustituirla por una derivacion"
            )


def artefactos_de_navegador(raiz: Path = RAIZ) -> list[str]:
    """Lo que hay en `apps/web/` y no es su marcador de posicion."""
    web = raiz / "apps" / "web"
    if not web.is_dir():
        return []
    return sorted(
        hijo.name for hijo in web.iterdir() if hijo.name not in ARTEFACTOS_DE_WEB_CONOCIDOS
    )


def modulos_de_salida(raiz: Path = RAIZ) -> list[str]:
    """Los modulos del punto unico de salida, leidos del arbol — TODO el arbol.

    # WHY (`rglob` y no `glob`): mirando solo el primer nivel, un subpaquete
    # —`packages/egress/mensajes/…`, que es justo la forma que tendria la salida
    # de mensajes de T-119— no se veia, y el inventario habria seguido diciendo
    # que no hay mas caminos de salida. El universo de la medida se deriva
    # entero, o mide menos sin fallar (P-52).
    """
    paquete = raiz / "packages" / "egress"
    if not paquete.is_dir():
        return []
    return sorted(
        archivo.relative_to(paquete).with_suffix("").as_posix()
        for archivo in paquete.rglob("*.py")
        if "__pycache__" not in archivo.parts
    )


#: Como se llama la funcion del punto unico de salida.
_NOMBRE_DEL_PUNTO_DE_SALIDA = "pedir"

#: Donde vive el codigo que CORRE en produccion. Es el universo del barrido de
#: llamantes.
#:
#: # WHY (las pruebas quedan fuera, y es una decision, no un descuido): una
#: bateria que ejercita el guard de salida menciona el punto de salida a
#: proposito, y un inventario que las contara diria que la suite es un
#: destinatario de datos — ademas de cambiar cada vez que alguien escribe una
#: prueba. Lo que se inventaria es lo que se despliega.
ARBOL_DE_PRODUCCION: tuple[str, ...] = ("apps/api/app", "apps/worker", "packages")


def _ruta_punteada(nodo: ast.expr) -> str:
    """`egress.red` a partir del arbol de `egress.red.pedir(...)`."""
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        base = _ruta_punteada(nodo.value)
        return f"{base}.{nodo.attr}" if base else ""
    return ""


def llamantes_del_punto_de_salida(raiz: Path = RAIZ) -> list[str]:
    """Los modulos de PRODUCCION que LLAMAN al punto unico de salida.

    # WHY: la lista de destinatarios no se mantiene a mano. Todo lo que sale de
    # aqui a la red pasa por `egress.red.pedir` (regla del README, medida por
    # `test_egreso_red`), asi que quien llame a esa funcion esta mandando datos
    # fuera. Un llamador nuevo cambia el inventario y pone en rojo la prueba de
    # divergencia — que es como se entera el piso legal de que hay un
    # destinatario mas.
    #
    # # WHY (AST y no una expresion regular): con texto, `from egress.red import
    # pedir as salir` daba un falso NEGATIVO —una salida real que el inventario
    # no nombra— y la palabra `pedir(` dentro de un comentario daba un falso
    # positivo. El falso negativo es el caro, y `test_egreso_red` no lo tapa: ese
    # guard mira quien IMPORTA un cliente de red, y un alias de `pedir` no
    # importa ninguno. Aqui se resuelve el import —alias incluido— y se buscan
    # las LLAMADAS a ese nombre.
    """
    encontrados: list[str] = []
    for carpeta in ARBOL_DE_PRODUCCION:
        base = raiz / carpeta
        if not base.is_dir():
            continue
        for archivo in sorted(base.rglob("*.py")):
            if "__pycache__" in archivo.parts:
                continue
            relativa = archivo.relative_to(raiz).as_posix()
            if relativa.startswith("packages/egress/"):
                continue  # es el propio punto de salida
            arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=relativa)

            directos: set[str] = set()  # `pedir(...)`, con su alias
            modulos: set[str] = set()  # `red.pedir(...)` / `egress.red.pedir(...)`
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.ImportFrom):
                    # WHY: un import RELATIVO (`from ..egress.red import pedir`,
                    # `from .red import pedir`) es una salida igual de real. Se
                    # reconoce por el ultimo segmento, que en este arbol solo lo
                    # usa el punto de salida: si algun dia hubiera otro modulo
                    # `red`, esto lo contaria de mas — y sobre-declarar un
                    # destinatario es el lado seguro del error en un documento
                    # que existe para no omitir ninguno.
                    relativo_al_punto = (
                        nodo.level > 0 and (nodo.module or "").split(".")[-1] == "red"
                    )
                    if nodo.module == "egress.red" or relativo_al_punto:
                        directos |= {
                            alias.asname or alias.name
                            for alias in nodo.names
                            if alias.name == _NOMBRE_DEL_PUNTO_DE_SALIDA
                        }
                    elif nodo.module == "egress":
                        modulos |= {
                            alias.asname or alias.name
                            for alias in nodo.names
                            if alias.name == "red"
                        }
                elif isinstance(nodo, ast.Import):
                    modulos |= {
                        alias.asname or alias.name
                        for alias in nodo.names
                        if alias.name == "egress.red"
                    }
            if not directos and not modulos:
                continue

            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.Call):
                    continue
                funcion = nodo.func
                llama = isinstance(funcion, ast.Name) and funcion.id in directos
                if not llama and isinstance(funcion, ast.Attribute):
                    llama = (
                        funcion.attr == _NOMBRE_DEL_PUNTO_DE_SALIDA
                        and _ruta_punteada(funcion.value) in modulos
                    )
                if llama:
                    encontrados.append(relativa)
                    break
    return sorted(encontrados)


#: Por que nombre se reconoce, en este arbol, una constante que declara el
#: PREFIJO de una familia de claves. Las cuatro que hay hoy lo usan.
_NOMBRE_DE_PREFIJO = "PREFIJO"

#: Cada constante de prefijo de PRODUCCION, con la fila del inventario que la
#: cubre. Es el cruce que impide que una familia de claves entre en silencio.
#:
#: # WHY (P-52, tercera vez): la primera version enumeraba a mano las TRES
#: familias de Redis que la casilla nombra, y con eso el bloque de datos afirmaba
#: cubrir «los derivados» habiendo mirado solo donde ya sabia que mirar. Al
#: derivar el universo de verdad aparecio una CUARTA constante de prefijo en
#: produccion que ninguna fila mencionaba. No fallaba: media menos, y salia
#: verde. El universo de una medida se DERIVA, no se supone.
PREFIJOS_INVENTARIADOS: dict[str, str] = {
    "apps/api/app/channels/idempotency.py:PREFIJO": (
        "la marca de idempotencia en Redis, que el bloque de datos inventaria como "
        "clave propia"
    ),
    "apps/api/app/tenancy/auth.py:PREFIJO_POR_DEFECTO": (
        "la sesion de una persona operadora en Redis, que el bloque de datos "
        "inventaria como clave propia"
    ),
    "apps/api/app/tenancy/limits.py:PREFIJO_POR_DEFECTO": (
        "el contador de limite en Redis, que el bloque de datos inventaria como "
        "clave propia"
    ),
    "apps/api/app/agents/providers.py:PREFIJO_DEL_SECRETO": (
        "no es un almacen aparte: nombra las FILAS de la tabla `secretos`, que el "
        "bloque de datos ya inventaria como credencial cifrada. Se declara aqui para "
        "que conste que se miro y se decidio, no que se paso por alto"
    ),
}


def constantes_de_prefijo(raiz: Path = RAIZ) -> list[str]:
    """`archivo:SIMBOLO` de cada constante de prefijo del arbol de PRODUCCION.

    Se lee con el analizador de sintaxis y no con una expresion regular: lo que
    interesa es una asignacion de MODULO, no la palabra suelta dentro de un
    comentario o de una cadena.
    """
    encontradas: list[str] = []
    for carpeta in ARBOL_DE_PRODUCCION:
        base = raiz / carpeta
        if not base.is_dir():
            continue
        for archivo in sorted(base.rglob("*.py")):
            if "__pycache__" in archivo.parts:
                continue
            relativa = archivo.relative_to(raiz).as_posix()
            arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=relativa)
            # WHY (`feedback_analisis_incompleto_falla_caro`): se recorre el arbol
            # ENTERO, no solo el nivel de modulo. Una constante de prefijo declarada
            # dentro de una clase es igual de real, y el recorrido estrecho la habria
            # dejado fuera SIN fallar — un falso OK en un documento publico cuesta mas
            # que una falsa alarma, que como mucho obliga a declarar una linea de mas.
            for nodo in ast.walk(arbol):
                destinos: list[ast.expr] = []
                if isinstance(nodo, ast.Assign):
                    destinos = list(nodo.targets)
                elif isinstance(nodo, ast.AnnAssign):
                    destinos = [nodo.target]
                for destino in destinos:
                    if isinstance(destino, ast.Name) and destino.id.startswith(
                        _NOMBRE_DE_PREFIJO
                    ):
                        encontradas.append(f"{relativa}:{destino.id}")
    return sorted(encontradas)


def exigir_que_todo_prefijo_este_inventariado(raiz: Path = RAIZ) -> None:
    """Ninguna familia de claves de produccion se queda fuera del inventario.

    Falla en las DOS direcciones: un prefijo sin fila, y una declaracion que
    apunta a una constante que ya no existe — porque una declaracion caducada
    tapa a la siguiente que falte.
    """
    encontradas = set(constantes_de_prefijo(raiz))
    declaradas = set(PREFIJOS_INVENTARIADOS)

    sin_fila = sorted(encontradas - declaradas)
    if sin_fila:
        raise PrefijoSinInventariar(
            "produccion declara familias de claves que ninguna fila del inventario "
            f"nombra: {sin_fila}. Un documento publico que dice que datos existen no "
            "puede saltarse una: declara en PREFIJOS_INVENTARIADOS que fila la cubre, "
            "y si no la cubre ninguna, anadela al bloque de datos"
        )

    muertas = sorted(declaradas - encontradas)
    if muertas:
        raise DeclaracionDePrefijoMuerta(
            f"PREFIJOS_INVENTARIADOS declara prefijos que ya no existen: {muertas}. Una "
            "declaracion caducada tapa a la siguiente que falte: quitala"
        )


# ==========================================================================
# Bloque 1 — DATOS: el catalogo vivo, y los derivados que no son tabla
# ==========================================================================
def tablas_y_columnas(conexion) -> dict[str, set[str]]:
    """Tablas ordinarias del esquema con sus columnas, leidas de `pg_catalog`.

    # WHY (`pg_catalog` y NO `information_schema`, P-16): las vistas de
    # `information_schema` estan FILTRADAS POR PRIVILEGIO. Un inventario que se
    # encoge en silencio cuando alguien revoca un permiso es exactamente un
    # documento publico que miente por omision.
    """
    filas = conexion.execute(
        text(
            """
            SELECT c.relname AS tabla, a.attname AS columna
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attribute a ON a.attrelid = c.oid
                                     AND a.attnum > 0 AND NOT a.attisdropped
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname, a.attname
            """
        )
    ).all()
    mapa: dict[str, set[str]] = {}
    for fila in filas:
        columnas = mapa.setdefault(fila.tabla, set())
        if fila.columna is not None:
            columnas.add(fila.columna)
    return mapa


def revision_de_este_repositorio(raiz: Path = RAIZ) -> str:
    """La revision `head` de las migraciones de ESTE arbol, segun el propio Alembic."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = raiz / "apps" / "api" / "migrations" / "alembic.ini"
    return ScriptDirectory.from_config(Config(str(ini))).get_current_head() or ""


def exigir_catalogo_de_esta_revision(conexion, raiz: Path = RAIZ) -> None:
    """El catalogo tiene que ser el que producen las migraciones de este arbol.

    # WHY (medido el 2026-09-09, y no es teorico): el banco de pruebas es UNO y
    # lo comparten varios worktrees. Al generar el inventario contra un banco que
    # otra rama habia migrado, el catalogo traia una tabla que en esta rama no
    # existe — y sin esta comprobacion el fallo aparecia como «tabla sin
    # categoria», que manda a declarar una tabla ajena en un documento publico en
    # vez de a mirar de que esquema se estaba hablando. Un inventario derivado de
    # OTRO esquema es exactamente el documento que miente. Aqui se falla cerrado
    # y se dice cual es la diferencia.
    """
    esperada = revision_de_este_repositorio(raiz)
    if not esperada:
        # WHY: `get_current_head()` devuelve `None` si no encuentra ninguna
        # revision. Compararlo contra la del banco producia el mensaje «las
        # migraciones de este arbol terminan en ''», que manda a mirar el banco
        # cuando el problema esta AQUI: el arbol no tiene migraciones legibles.
        raise SinCatalogo(
            "este arbol no declara ninguna revision `head` de Alembic: no hay con que "
            "comparar el catalogo del banco. Revisa apps/api/migrations/"
        )
    aplicada = conexion.execute(
        text(
            "SELECT version_num FROM alembic_version "
            "WHERE to_regclass('public.alembic_version') IS NOT NULL"
        )
    ).scalar_one_or_none()
    if aplicada is None:
        raise SinCatalogo(
            "la base no tiene tabla de revisiones: no se ha migrado nunca. El "
            "inventario se deriva del catalogo que producen ESTAS migraciones"
        )
    if aplicada != esperada:
        raise CatalogoDeOtraRevision(
            f"la base esta en la revision {aplicada!r} y las migraciones de este arbol "
            f"terminan en {esperada!r}: el inventario describiria un esquema que no es "
            "el de esta rama. Migra este arbol contra una base suya antes de generar"
        )


_CREA_TABLA = re.compile(r"CREATE TABLE\s+([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE)


def donde_nace_cada_tabla(raiz: Path = RAIZ) -> dict[str, str]:
    """`archivo:linea` de la migracion que crea cada tabla. Procedencia de verdad."""
    origen: dict[str, str] = {}
    carpeta = raiz / "apps" / "api" / "migrations" / "versions"
    for archivo in sorted(carpeta.glob("*.py")):
        relativa = archivo.relative_to(raiz).as_posix()
        for numero, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
            encontrada = _CREA_TABLA.search(linea)
            if encontrada is not None:
                origen.setdefault(encontrada.group(1), f"{relativa}:{numero}")
    return origen


def derivar_datos(conexion, raiz: Path = RAIZ) -> list[dict[str, str]]:
    """Las tablas del catalogo vivo, cada una con su categoria declarada."""
    catalogo = tablas_y_columnas(conexion)
    if not catalogo:
        raise SinCatalogo(
            "el esquema `public` no tiene ninguna tabla: no hay catalogo del que "
            "derivar el inventario. Migra antes con `alembic upgrade head`"
        )
    nacimiento = donde_nace_cada_tabla(raiz)
    filas: list[dict[str, str]] = []
    for tabla in sorted(catalogo):
        columnas = catalogo[tabla]
        alcance = alcance_de(columnas)
        if alcance == CLASE_MEDIA_CLAVE:
            raise TablaConMediaClave(
                f"{tabla} lleva {COLUMNA_CLIENTE} y no {COLUMNA_AGENCIA}: no se puede "
                "decir de que inquilino son sus filas, asi que tampoco se puede "
                "inventariar de quien son sus datos"
            )
        declarada = CATEGORIA_POR_TABLA.get(tabla)
        if declarada is None:
            raise TablaSinCategoria(
                f"la tabla {tabla} existe en el catalogo y no tiene categoria de dato "
                "declarada en CATEGORIA_POR_TABLA. Una tabla nueva no entra en un "
                "documento publico en silencio: declara que categoria es y que contiene"
            )
        categoria, contenido = declarada
        if categoria not in CATEGORIAS:
            raise CategoriaDesconocida(
                f"{tabla} declara la categoria {categoria!r}, que no esta en CATEGORIAS "
                f"({list(CATEGORIAS)})"
            )
        filas.append(
            {
                "nombre": tabla,
                "forma": "tabla del catálogo",
                "categoria": categoria,
                "alcance": alcance,
                "contenido": contenido,
                "procedencia": nacimiento.get(
                    tabla, "no la crea ninguna migración de este repositorio"
                ),
            }
        )
    filas.extend(_datos_derivados())
    return filas


#: Marca que se pone en la parte VARIABLE de una clave para poder cortarla. En
#: minusculas porque el nombre de un limite es una allowlist POR FORMA.
_SONDA_DE_CLAVE = "sondadeinventario"


class _RedisQueNoHabla:
    """Un Redis que no habla. `Limitador` registra su guion al construirse, y de el
    aqui solo se quiere el constructor de CLAVES, nunca su comportamiento."""

    def register_script(self, guion):  # noqa: ARG002 - la firma es del cliente real
        return None


def _patron_de_clave(construida: str, sonda: str) -> str:
    """`heraldo:sesion:<sonda>` -> `heraldo:sesion:*`.

    # WHY: el PREFIJO ya se leia de su constante, pero el segmento siguiente
    # —`idem`, `sesion`, `limite`— estaba escrito a mano aqui. Renombrarlo en su
    # modulo dejaba este inventario publicando un patron de clave que ya no
    # existe: una afirmacion falsa sobre que datos hay, que es justo lo que esta
    # casilla existe para impedir. Ahora el patron sale de LLAMAR al constructor
    # de claves de verdad y cortar por la sonda — ningun segmento se escribe dos
    # veces, y si el constructor deja de usar el valor que se le pasa, se cae.
    """
    cabeza, separador, _ = construida.partition(sonda)
    if not separador:
        raise InventarioIncompleto(
            f"la clave {construida!r} no contiene la sonda que se le paso: el "
            "constructor de claves dejo de usar ese valor, y el patron publicado "
            "seria una suposicion en vez de una derivacion"
        )
    return cabeza + "*"


def patrones_de_clave() -> dict[str, str]:
    """El patron de cada familia de claves, DERIVADO de quien las construye."""
    from uuid import UUID

    from app.tenancy.inquilino import Alcance, Inquilino

    sonda = UUID(PARAMETRO_DE_MEDIDA)
    inquilino = Inquilino(agencia_id=sonda, cliente_id=sonda, alcance=Alcance.CLIENTE)
    return {
        "idempotencia": _patron_de_clave(
            idempotency.clave_de(
                inquilino, canal=_SONDA_DE_CLAVE, id_externo=_SONDA_DE_CLAVE
            ),
            str(sonda),
        ),
        "sesion": _patron_de_clave(
            auth.AlmacenDeSesiones(None).clave(_SONDA_DE_CLAVE), _SONDA_DE_CLAVE
        ),
        "limite": _patron_de_clave(
            limits.LimitadorCompartido(_RedisQueNoHabla()).clave(
                limits.Limite(nombre=_SONDA_DE_CLAVE, cuota=1, ventana_segundos=1),
                inquilino,
                limits.Direccion.desde_texto("203.0.113.7"),
            ),
            _SONDA_DE_CLAVE,
        ),
    }


def _datos_derivados() -> list[dict[str, str]]:
    """Lo que no es tabla y aun asi son datos: las claves de Redis, DERIVADAS.

    Ni el prefijo ni el resto del patron se escriben aqui: se obtienen llamando a
    los constructores de claves reales. Si alguien cambia una constante o renombra
    un segmento, el inventario cambia con el codigo.
    """
    patrones = patrones_de_clave()
    return [
        {
            "nombre": patrones["idempotencia"],
            "forma": "clave de Redis",
            "categoria": "derivado",
            "alcance": CLASE_CLIENTE,
            "contenido": (
                "Marca de que un mensaje entrante ya se recibió. La clave lleva la "
                "agencia, el cliente, el canal y el identificador externo del mensaje; "
                "no guarda ningún contenido del mensaje."
            ),
            "procedencia": "apps/api/app/channels/idempotency.py:clave_de",
        },
        {
            "nombre": patrones["sesion"],
            "forma": "clave de Redis",
            "categoria": "plataforma",
            "alcance": CLASE_AGENCIA,
            "contenido": (
                "Sesión abierta de una persona operadora: agencia, cliente y rol, más "
                "la huella del secreto de la sesión. El secreto no se guarda."
            ),
            "procedencia": "apps/api/app/tenancy/auth.py:AlmacenDeSesiones.clave",
        },
        {
            "nombre": patrones["limite"],
            "forma": "clave de Redis",
            "categoria": "derivado",
            "alcance": CLASE_CLIENTE,
            "contenido": (
                "Contador de un límite por ventana. La clave lleva el nombre del "
                "límite y una huella irreversible del par (inquilino, dirección de red "
                "del remitente): la dirección no se guarda."
            ),
            "procedencia": "apps/api/app/tenancy/limits.py:LimitadorCompartido.clave",
        },
    ]


# ==========================================================================
# Bloque 2 — DESTINATARIOS: a quien sale un dato, y por donde
# ==========================================================================
#: Como sale a la red todo lo que sale, en una sola frase publicable. No nombra
#: ningun equipo: nombra el MECANISMO, que es lo que se puede publicar.
MECANISMO_DE_SALIDA = (
    "petición HTTPS saliente por el punto único de salida (`egress.red.pedir`): solo "
    "`https` al 443, con el destino revalidado contra direcciones internas antes de "
    "conectar y en cada redirección"
)


def derivar_destinatarios(raiz: Path = RAIZ, lista_de_proveedores=None) -> list[dict[str, str]]:
    """A quien salen datos hoy, derivado del codigo; y lo que falta, con su casilla."""
    if lista_de_proveedores is None:
        from app.agents.providers import LISTA_DECLARADA

        lista_de_proveedores = LISTA_DECLARADA

    nuevos = sorted(set(modulos_de_salida(raiz)) - MODULOS_DE_SALIDA_CONOCIDOS)
    if nuevos:
        raise SalidaNoDeclarada(
            f"`packages/egress/` tiene modulos que este inventario no nombra: {nuevos}. "
            "Un camino de salida nuevo es un destinatario nuevo de datos: declaralo "
            "aqui antes de que el piso legal siga diciendo que no hay mas"
        )

    llamantes = llamantes_del_punto_de_salida(raiz)
    filas: list[dict[str, str]] = []

    for nombre in sorted(lista_de_proveedores):
        proveedor = lista_de_proveedores[nombre]
        ficha = proveedor.condiciones
        # WHY (P-51 otra vez, en su forma exacta): una región publicada a secas es
        # una afirmación sin respaldo. `FichaDeCondiciones` trae, junto a la
        # ubicación, QUIÉN la verificó (`fuente`) y CUÁNDO (`verificada_en`): las
        # tres viajan juntas o la fila diría «se trata en tal región» sin nada
        # detrás. Hoy ningún proveedor tiene ficha, así que esta rama nace antes
        # que su dato — y por eso se mide con una ficha inyectada.
        if ficha is not None:
            ubicacion = ficha.ubicacion
            procedencia = (
                "apps/api/app/agents/providers.py:LISTA_DECLARADA — ficha verificada el "
                f"{ficha.verificada_en.isoformat()} en {ficha.fuente}"
            )
        else:
            ubicacion = (
                "pendiente (T-100·bis): su ficha de condiciones no está verificada ni "
                "fechada, y sin ficha no se registra ninguna credencial"
            )
            procedencia = (
                "apps/api/app/agents/providers.py:LISTA_DECLARADA (sin ficha verificada: "
                "no se afirma ninguna región)"
            )
        filas.append(
            {
                "destinatario": f"proveedor de modelo: {nombre}",
                "funcion": (
                    "validar contra el proveedor la credencial que registra el cliente, "
                    "antes de guardarla (RF-18). Es una consulta a su catálogo de "
                    "modelos: no envía ningún contenido del inquilino"
                ),
                "ubicacion": ubicacion,
                "mecanismo": MECANISMO_DE_SALIDA,
                "procedencia": procedencia,
            }
        )

    filas.append(
        {
            "destinatario": "plataforma de mensajería",
            "funcion": "entregar y recibir los mensajes del usuario final",
            "ubicacion": "pendiente (T-107/T-119)",
            "mecanismo": (
                "pendiente (T-119): hoy `packages/egress/` no tiene ningún módulo de "
                "salida de mensajes, así que ningún mensaje sale de esta plataforma"
            ),
            "procedencia": "packages/egress/ (medido: solo existe el punto de salida de red)",
        }
    )
    filas.append(
        {
            "destinatario": "borde de la agencia (publicación y red de entrega)",
            "funcion": "servir las superficies públicas y proteger su entrada",
            "ubicacion": "fuera de este repositorio",
            "mecanismo": (
                "no versionado aquí: es infraestructura de la agencia, descrita en la "
                "especificación §7. Este inventario no lo puede derivar y lo declara "
                "como límite en vez de afirmarlo"
            ),
            "procedencia": "límite declarado del inventario (no derivable del repositorio)",
        }
    )
    for clave, etiqueta, funcion in (
        ("retencion_de_respaldos", "respaldos", "conservar copias de seguridad de la base"),
        (
            "avisos_internos",
            "avisos internos del negocio cliente",
            "avisar al negocio por el canal que él declare",
        ),
        (
            "buzon_de_privacidad",
            "buzón de privacidad y de borrado",
            "recibir y resolver las peticiones de las personas",
        ),
    ):
        ruta, casilla = ARTEFACTOS_PENDIENTES[clave]
        filas.append(
            {
                "destinatario": etiqueta,
                "funcion": funcion,
                "ubicacion": f"pendiente ({casilla})",
                "mecanismo": f"pendiente ({casilla})",
                "procedencia": f"{ruta} no existe (comprobado en el árbol)",
            }
        )

    filas.append(
        {
            "destinatario": "puntos del código que llaman al punto único de salida",
            "funcion": (
                "es la lista derivada de quién puede mandar algo fuera: "
                + (", ".join(f"`{ruta}`" for ruta in llamantes) if llamantes else "ninguno")
            ),
            "ubicacion": "no aplica",
            "mecanismo": MECANISMO_DE_SALIDA,
            "procedencia": "barrido de `apps/` y `packages/` (llamadas a `egress.red.pedir`)",
        }
    )
    return filas


# ==========================================================================
# Bloque 3 — COOKIES Y ALMACENAMIENTO por superficie, MEDIDOS
# ==========================================================================
def aplicacion_de_medida():
    """La aplicacion REAL, construida para medirla; nunca para servir.

    # WHY (documentacion PUBLICA): es el conjunto MAS AMPLIO de rutas que la
    # fabrica puede montar. Medir el conjunto mas estrecho dejaria fuera de la
    # medida justo lo que se sirve cuando alguien decide exponer el mapa.
    """
    return crear_aplicacion(
        entorno=Entorno.DESARROLLO,
        origenes=(),
        motor=crear_motor(DSN_SIN_DESTINO),
        exposicion_de_documentacion=ExposicionDeDocumentacion.PUBLICA,
        tiempo_limite_de_salud=0.5,
    )


_PARAMETRO = re.compile(r"\{[^}]+\}")


def rellenar_parametros(ruta: str) -> str:
    """Un parametro de ruta se rellena con un valor imposible, nunca con uno real."""
    return _PARAMETRO.sub(PARAMETRO_DE_MEDIDA, ruta)


async def _pedir_por_asgi(aplicacion, metodo: str, camino: str) -> tuple[int, list[str]]:
    """Llama a la aplicacion por su interfaz ASGI y devuelve (codigo, cookies emitidas).

    # WHY (un llamador propio y NINGUN cliente HTTP): este guion escribe un
    # documento publico y no tiene por que tener a mano nada capaz de salir a la
    # red — ni siquiera por accidente. Lo que mide es exactamente lo que la
    # aplicacion emite, con sus middlewares puestos. Que la medida sea correcta
    # no se cree: `test_inventario_legal` recorre la misma aplicacion con `httpx`
    # y exige que las dos lecturas coincidan (P-44: hace falta un tercero).
    """
    estado: dict[str, Any] = {"codigo": 0, "cookies": []}

    async def recibir() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def enviar(mensaje: dict[str, Any]) -> None:
        if mensaje["type"] != "http.response.start":
            return
        estado["codigo"] = mensaje["status"]
        estado["cookies"] = [
            valor.decode("latin-1").split("=", 1)[0].strip()
            for clave, valor in mensaje.get("headers", [])
            if clave.decode("latin-1").lower() == "set-cookie"
        ]

    ambito = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": metodo,
        "scheme": "http",
        "path": camino,
        "raw_path": camino.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"heraldo.invalid")],
        "client": ("127.0.0.1", 0),
        "server": ("heraldo.invalid", 80),
    }
    # WHY: si la aplicacion LANZA en vez de responder, esta medida no sabe que
    # emite esa ruta — y el documento no puede decir «ninguna emite cookies»
    # habiendola contado. Se falla CERRADO nombrando la ruta: una superficie que
    # no se pudo medir es una superficie sin inventariar, no una superficie
    # limpia. (Antes la excepcion se llevaba por delante toda la derivacion con
    # una traza cruda, que ademas enterraba de que ruta se trataba.)
    try:
        await aplicacion(ambito, recibir, enviar)
    except Exception as fallo:  # noqa: BLE001 - se re-lanza con su contexto
        raise SuperficieNoMedible(
            f"{metodo} {camino} no respondio: lanzo {type(fallo).__name__}: {fallo}. "
            "Una ruta que no se puede medir no se puede inventariar como si no "
            "emitiera nada"
        ) from fallo
    return int(estado["codigo"]), list(estado["cookies"])


def rutas_planas(objeto: Any, prefijo: str = "") -> list[tuple[str, set[str]]]:
    """Aplana el arbol de rutas: un router INCLUIDO tambien es superficie servida.

    # WHY (medido, no supuesto): `aplicacion.routes` NO es una lista plana. Un
    # `include_router` deja un envoltorio cuyo `path` es `None` y que guarda el
    # router de verdad dentro (`original_router`), con su prefijo aparte. La
    # primera version de este recorrido miraba solo el primer nivel y por eso
    # midio 4 rutas de 6: las DOS sondas de salud —que son las unicas que
    # cualquiera puede pedir sin credencial— se quedaron fuera y el inventario
    # decia «no emite cookies» sin haberlas mirado. El universo de la medida era
    # mas estrecho que lo que la medida afirmaba, y salia igual de verde.
    # `test_inventario_legal` lo fija con un control que las nombra.
    """
    original = getattr(objeto, "original_router", None)
    if original is not None:
        contexto = getattr(objeto, "include_context", None)
        propio = getattr(contexto, "prefix", "") or ""
        planas: list[tuple[str, set[str]]] = []
        for hijo in getattr(original, "routes", []):
            planas.extend(rutas_planas(hijo, prefijo + propio))
        return planas

    camino = getattr(objeto, "path", None)
    metodos = getattr(objeto, "methods", None)
    if camino is not None and metodos:
        return [(prefijo + camino, {str(metodo) for metodo in metodos})]

    hijos = getattr(objeto, "routes", None)
    if hijos:
        planas = []
        for hijo in hijos:
            planas.extend(rutas_planas(hijo, prefijo + (camino or "")))
        return planas
    return []


async def medir_rutas_async(aplicacion) -> list[dict[str, Any]]:
    """Recorre TODAS las rutas registradas y anota lo que cada una emite.

    # WHY (hay una version asincrona y una sincrona, y no es duplicacion): la
    # aplicacion es ASGI, asi que medirla es asincrono. El guion corre en un
    # programa sincrono y usa `medir_rutas`, que abre UN bucle para todas las
    # rutas. Pero `asyncio.run` revienta si ya hay un bucle corriendo, y la
    # prueba que cruza esta medida con `httpx` es asincrona: sin esta puerta,
    # el segundo testigo no podria ni llamar a la medida que audita.
    """
    pares: set[tuple[str, str]] = set()
    for camino, metodos in rutas_planas(aplicacion):
        for metodo in metodos:
            if metodo.upper() == "HEAD":
                continue  # es la misma respuesta que GET, sin cuerpo
            pares.add((metodo.upper(), camino))

    filas: list[dict[str, Any]] = []
    for metodo, camino in sorted(pares, key=lambda par: (par[1], par[0])):
        codigo, cookies = await _pedir_por_asgi(
            aplicacion, metodo, rellenar_parametros(camino)
        )
        filas.append(
            {
                "metodo": metodo,
                "ruta": camino,
                "codigo": codigo,
                "cookies": cookies,
                "procedencia": "medido sobre `app.main.crear_aplicacion` por su interfaz ASGI",
            }
        )
    return filas


def medir_rutas(aplicacion) -> list[dict[str, Any]]:
    """La misma medida, para quien no tiene un bucle de eventos corriendo."""
    return asyncio.run(medir_rutas_async(aplicacion))


def derivar_superficies(
    raiz: Path = RAIZ, fabrica_de_aplicacion=None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Las superficies del producto, con lo que emite cada una. Medido o pendiente."""
    sobrantes = artefactos_de_navegador(raiz)
    if sobrantes:
        raise ArtefactoQueYaExiste(
            f"`apps/web/` tiene artefactos que este inventario no mide: {sobrantes}. "
            "Una superficie de navegador que empieza a existir hay que MEDIRLA: que "
            "`Set-Cookie` emite y que guarda en el navegador"
        )

    fabrica = fabrica_de_aplicacion or aplicacion_de_medida
    rutas = medir_rutas(fabrica())
    con_cookie = sorted({nombre for fila in rutas for nombre in fila["cookies"]})

    superficies: list[dict[str, Any]] = [
        {
            "superficie": "interfaz de programación (HTTP)",
            "estado": "construida",
            "cookies": (
                ", ".join(f"`{nombre}`" for nombre in con_cookie)
                if con_cookie
                else "ninguna: no emite `Set-Cookie` en ninguna de sus rutas"
            ),
            "almacenamiento": (
                "no sirve ninguna página propia. Las páginas de documentación de "
                "interfaz, cuando la decisión de entorno las expone, cargan su guion de "
                "un CDN de terceros declarado en `superficie_publica`; su almacenamiento "
                "lo decide ese guion y no se puede medir desde el servidor"
            ),
            "procedencia": (
                f"medido sobre {len(rutas)} pares (método, ruta) de "
                "`app.main.crear_aplicacion`"
            ),
        }
    ]
    for nombre, casilla in SUPERFICIES_DE_NAVEGADOR:
        superficies.append(
            {
                "superficie": nombre,
                "estado": f"no construida ({casilla})",
                "cookies": f"pendiente ({casilla})",
                "almacenamiento": f"pendiente ({casilla})",
                "procedencia": "apps/web/ solo contiene su marcador de posición",
            }
        )
    return superficies, rutas


# ==========================================================================
# Bloque 4 — LICENCIAS de terceros
# ==========================================================================
#: Variables de marcador que cambian con el SISTEMA OPERATIVO. `python_version`
#: NO esta aqui a proposito: la fija `.python-version`, asi que no hace que el
#: inventario dependa de donde se genere.
EJES_DE_SISTEMA: tuple[str, ...] = ("sys_platform", "platform_system", "os_name")

#: Licencia de los paquetes que solo se instalan en UN sistema operativo.
#:
#: # WHY: sin esto el inventario cambiaria segun donde se genere — en Windows no
#: hay metadatos de `uvloop` que leer, y en Linux no los hay de `colorama` ni de
#: `tzdata` — y el gate de divergencia se pondria rojo por el sistema operativo
#: en vez de por un cambio real (`feedback_verde_plataforma_no_importa` al
#: reves). No es una lista de buena fe: en el entorno que SI tiene el paquete,
#: `derivar_licencias` cruza esta declaracion contra los metadatos y se cae si ya
#: no coincide. Cada entorno audita lo que puede leer.
LICENCIAS_DE_PAQUETES_CONDICIONADOS: dict[str, tuple[str, str, str]] = {
    "colorama": (
        "Classifier",
        "BSD License",
        "solo se instala en Windows (`sys_platform == 'win32'` en uv.lock)",
    ),
    "tzdata": (
        "License",
        "Apache-2.0",
        "solo se instala en Windows (`sys_platform == 'win32'` en uv.lock)",
    ),
    "uvloop": (
        "License",
        "MIT License",
        "no se instala en Windows (`sys_platform != 'win32'` en uv.lock)",
    ),
}


def _leer_bloqueo(raiz: Path) -> dict[str, Any]:
    return tomllib.loads((raiz / "uv.lock").read_text(encoding="utf-8"))


def _aristas_de_entrada(bloqueo: dict[str, Any]) -> dict[str, list[str | None]]:
    """Por cada paquete, los marcadores de las dependencias que lo traen."""
    entradas: dict[str, list[str | None]] = {}
    for paquete in bloqueo.get("package", []):
        grupos: list[list[dict[str, Any]]] = [paquete.get("dependencies") or []]
        for opcionales in (paquete.get("optional-dependencies") or {}).values():
            grupos.append(opcionales)
        for desarrollo in (paquete.get("dev-dependencies") or {}).values():
            grupos.append(desarrollo)
        for grupo in grupos:
            for dependencia in grupo:
                entradas.setdefault(dependencia["name"], []).append(dependencia.get("marker"))
    return entradas


def condicionados_por_sistema(bloqueo: dict[str, Any]) -> set[str]:
    """Paquetes que TODAS sus entradas condicionan al sistema operativo."""
    entradas = _aristas_de_entrada(bloqueo)
    condicionados: set[str] = set()
    for nombre, marcadores in entradas.items():
        if not marcadores:
            continue
        if all(
            marcador is not None and any(eje in marcador for eje in EJES_DE_SISTEMA)
            for marcador in marcadores
        ):
            condicionados.add(nombre)
    return condicionados


def licencia_instalada(nombre: str) -> dict[str, str] | None:
    """Licencia y version que declaran los metadatos instalados, o `None` si no esta.

    Orden: `License-Expression` (PEP 639) -> `License` -> clasificadores. El campo
    `License` se descarta cuando trae el texto entero de la licencia en vez de su
    nombre, que es lo que hacen algunos paquetes viejos.
    """
    try:
        dist = distribution(nombre)
    except PackageNotFoundError:
        return None
    metadatos = dist.metadata
    for campo in ("License-Expression", "License"):
        valor = metadatos.get(campo)
        if valor:
            limpio = valor.strip()
            if limpio and "\n" not in limpio and len(limpio) <= 64:
                return {"campo": campo, "valor": limpio, "version": dist.version}
    clasificadores = sorted(
        clasificador.split(" :: ")[-1]
        for clasificador in (metadatos.get_all("Classifier") or [])
        if clasificador.startswith("License ::")
    )
    if clasificadores:
        return {
            "campo": "Classifier",
            "valor": " | ".join(clasificadores),
            "version": dist.version,
        }
    return {"campo": "-", "valor": "no declarada", "version": dist.version}


def derivar_licencias(raiz: Path = RAIZ) -> list[dict[str, str]]:
    """Los paquetes del bloqueo con su licencia: de los metadatos instalados, y de la
    declaracion para los que `uv.lock` condiciona al sistema operativo (esos no se leen
    de los metadatos ni donde estan, o el documento cambiaria segun donde se genere).
    """
    bloqueo = _leer_bloqueo(raiz)
    condicionados = condicionados_por_sistema(bloqueo)
    filas: list[dict[str, str]] = []

    for paquete in sorted(bloqueo.get("package", []), key=lambda p: p["name"]):
        nombre = paquete["name"]
        version = paquete.get("version", "")
        fuente = paquete.get("source") or {}
        if "editable" in fuente or "virtual" in fuente:
            filas.append(
                {
                    "paquete": nombre,
                    "version": version,
                    "licencia": "propia: ver `LICENSE` en la raíz del repositorio",
                    "fuente": "miembro del espacio de trabajo",
                    "procedencia": "uv.lock:[[package]] (source editable)",
                }
            )
            continue

        leida = licencia_instalada(nombre)
        declarada = LICENCIAS_DE_PAQUETES_CONDICIONADOS.get(nombre)
        condicionado = nombre in condicionados

        # WHY (medido el 2026-09-09, `feedback_verde_plataforma_no_importa`): la
        # fila de un paquete condicionado al sistema operativo se redacta SIEMPRE
        # desde la declaracion, lo instale este sistema o no. Antes salia de los
        # metadatos cuando estaban y de la declaracion cuando no, asi que el MISMO
        # arbol producia documentos distintos en Windows y en Linux: `--verificar`
        # en el CI se habria puesto rojo contra un inventario generado en Windows
        # por tres filas que nadie toco. La declaracion ya existia, pero solo
        # evitaba la CAIDA; la DIVERGENCIA seguia viva — media solucion. Donde los
        # metadatos se pueden leer, AUDITAN la declaracion en vez de redactar la
        # fila: cada entorno comprueba lo que puede.
        if condicionado:
            if declarada is None:
                raise LicenciaCondicionadaSinDeclarar(
                    f"{nombre} solo se instala en algunos sistemas operativos: sin una "
                    "entrada en LICENCIAS_DE_PAQUETES_CONDICIONADOS el inventario diria "
                    "cosas distintas segun donde se genere"
                )
            campo, valor, motivo = declarada
            if leida is not None and (campo, valor) != (leida["campo"], leida["valor"]):
                raise DeclaracionDeLicenciaCaduca(
                    f"la licencia declarada de {nombre} ya no coincide con la que dicen "
                    f"sus metadatos en este entorno: declarada {campo}={valor!r}, leida "
                    f"{leida['campo']}={leida['valor']!r}"
                )
            filas.append(
                {
                    "paquete": nombre,
                    "version": version,
                    "licencia": valor,
                    "fuente": f"declarada ({campo}): {motivo}",
                    "procedencia": (
                        "scripts/inventario_legal.py:LICENCIAS_DE_PAQUETES_CONDICIONADOS"
                    ),
                }
            )
            continue

        if declarada is not None:
            raise DeclaracionDeLicenciaCaduca(
                f"{nombre} tiene entrada en LICENCIAS_DE_PAQUETES_CONDICIONADOS y "
                "`uv.lock` NO lo condiciona a ningun sistema: esa declaracion no la lee "
                "nadie, y una declaracion muerta tapa a la siguiente que haga falta"
            )
        if leida is None:
            raise EntornoNoCorrespondeAlBloqueo(
                f"`uv.lock` declara {nombre} y este entorno no lo tiene instalado: "
                "el inventario de licencias saldria corto y en verde. Sincroniza "
                "con `uv sync --locked --all-packages --dev`"
            )
        filas.append(
            {
                "paquete": nombre,
                "version": version,
                "licencia": leida["valor"],
                "fuente": f"metadatos instalados ({leida['campo']})",
                "procedencia": "uv.lock:[[package]] + importlib.metadata",
            }
        )
    return filas


# ==========================================================================
# Bloque 5 — RETENCION vigente por categoria
# ==========================================================================
def _plazo(duracion: timedelta) -> str:
    segundos = int(duracion.total_seconds())
    if segundos % 86400 == 0:
        dias = segundos // 86400
        return f"{dias} día" if dias == 1 else f"{dias} días"
    if segundos % 3600 == 0:
        horas = segundos // 3600
        return f"{horas} hora" if horas == 1 else f"{horas} horas"
    return f"{segundos} segundos"


#: Categorias cuyo plazo por defecto todavia no lo fija ningun modulo.
CATEGORIAS_SIN_PLAZO: tuple[str, ...] = (
    "identificación",
    "conversación",
    "consentimiento",
    "conocimiento del negocio",
    "uso",
)


def derivar_retencion() -> list[dict[str, str]]:
    """El plazo VIGENTE de cada categoria, leido de la constante que lo fija.

    # WHY (no recibe la raiz): lo unico que aqui depende del arbol es si los
    # artefactos que fijarian los plazos que faltan existen ya, y eso lo mide
    # `exigir_que_sigan_pendientes` una sola vez, al empezar a derivar. Medirlo
    # otra vez seria una segunda redaccion de la misma pregunta.
    """
    ruta_retencion, casilla_retencion = ARTEFACTOS_PENDIENTES["retencion_por_defecto"]
    ruta_respaldos, casilla_respaldos = ARTEFACTOS_PENDIENTES["retencion_de_respaldos"]

    filas: list[dict[str, str]] = [
        {
            "categoria": "cola",
            "plazo": _plazo(cola.RETENCION_EN_CALIENTE),
            "inicio": "desde que el trabajo termina; después pasa al archivo",
            "procedencia": "apps/worker/cola.py:RETENCION_EN_CALIENTE",
        },
        {
            "categoria": "cola",
            "plazo": _plazo(cola.RETENCION_EN_ARCHIVO),
            "inicio": "desde que el trabajo se archiva; después se purga",
            "procedencia": "apps/worker/cola.py:RETENCION_EN_ARCHIVO",
        },
        {
            "categoria": "derivado",
            "plazo": _plazo(idempotency.VIGENCIA),
            "inicio": "desde que se recibe el mensaje (marca de idempotencia en Redis)",
            "procedencia": "apps/api/app/channels/idempotency.py:VIGENCIA",
        },
        {
            "categoria": "plataforma",
            "plazo": _plazo(timedelta(seconds=auth.TTL_SESION_SEGUNDOS)),
            "inicio": (
                "desde que se abre la sesión. Es un techo absoluto: no se renueva por "
                "usarla"
            ),
            "procedencia": "apps/api/app/tenancy/auth.py:TTL_SESION_SEGUNDOS",
        },
        {
            "categoria": "derivado",
            "plazo": "la ventana que declare cada límite en su punto de uso",
            "inicio": (
                "desde la primera petición de la ventana. Hoy ningún módulo declara un "
                "límite: la clase existe y nadie la instancia todavía"
            ),
            "procedencia": "apps/api/app/tenancy/limits.py:Limite.ventana_segundos",
        },
        {
            "categoria": "bitácora",
            "plazo": "fuera de la retención por antigüedad (RF-10)",
            "inicio": (
                "no se borra por tiempo: es de solo inserción por diseño y la aplicación "
                "no la puede reescribir. El residuo seudónimo del borrado de una persona "
                "(RF-49·bis) todavía no está construido: pendiente (T-212·ter)"
            ),
            "procedencia": "apps/api/app/tenancy/rol.py:PRIVILEGIOS_DE_APLICACION",
        },
    ]
    for categoria in CATEGORIAS_SIN_PLAZO:
        filas.append(
            {
                "categoria": categoria,
                "plazo": f"pendiente ({casilla_retencion})",
                "inicio": f"pendiente ({casilla_retencion})",
                "procedencia": f"{ruta_retencion} no existe (comprobado en el árbol)",
            }
        )
    filas.append(
        {
            "categoria": "respaldos",
            "plazo": f"pendiente ({casilla_respaldos})",
            "inicio": f"pendiente ({casilla_respaldos})",
            "procedencia": f"{ruta_respaldos} no existe (comprobado en el árbol)",
        }
    )
    return filas


# ==========================================================================
# Bloque 6 — IDENTIDADES de plataforma y datos de operadores
# ==========================================================================
#: Donde viviran las personas cuando existan, y quien las construye. El estado NO
#: se declara: se deriva de si la tabla esta en el catalogo.
TABLAS_DE_PERSONAS: tuple[tuple[str, str, str], ...] = (
    (
        "usuarios_agencia",
        "T-101",
        "personal de la agencia que opera la plataforma; queda fuera de la vía de "
        "borrado del usuario final (política interna de la agencia)",
    ),
    (
        "usuarios_cliente",
        "T-212·sexies",
        "personas del negocio cliente: usuarios del portal y la identidad de "
        "plataforma del administrador que conecta el canal",
    ),
)


def derivar_identidades(conexion) -> list[dict[str, str]]:
    """Que identidades de personas trata hoy la plataforma. Hoy: ninguna en tabla."""
    catalogo = tablas_y_columnas(conexion)
    filas: list[dict[str, str]] = []
    for tabla, casilla, descripcion in TABLAS_DE_PERSONAS:
        existe = tabla in catalogo
        filas.append(
            {
                "identidad": tabla,
                "estado": "en el catálogo" if existe else f"pendiente ({casilla})",
                "contenido": (
                    descripcion + ". La tabla ya existe: hay que declarar su categoría "
                    "de dato y describir sus columnas"
                    if existe
                    else descripcion
                ),
                "procedencia": (
                    "catálogo vivo (pg_class)"
                    if existe
                    else "catálogo vivo (pg_class): la tabla no existe"
                ),
            }
        )
    filas.append(
        {
            "identidad": "actor de la bitácora",
            "estado": "en el catálogo",
            "contenido": (
                "el «quién» de la bitácora se registra como rol más identificador "
                "opaco, nunca nombre ni correo (RF-10)"
            ),
            "procedencia": "apps/api/migrations/versions/0003_la_base_y_la_cola.py",
        }
    )
    return filas


# ==========================================================================
# El inventario, sus dos redacciones y su comparacion
# ==========================================================================
@dataclass
class Inventario:
    datos: list[dict[str, Any]]
    destinatarios: list[dict[str, Any]]
    superficies: list[dict[str, Any]]
    rutas: list[dict[str, Any]]
    licencias: list[dict[str, Any]]
    retencion: list[dict[str, Any]]
    identidades: list[dict[str, Any]]
    limites: list[str] = field(default_factory=list)


#: Lo que este inventario NO puede derivar, escrito como parte del documento. Un
#: limite que no se publica es una omision que parece completitud.
LIMITES_DEL_INVENTARIO: tuple[str, ...] = (
    "El borde de la agencia que publica las superficies no vive en este repositorio: "
    "no se deriva y se declara como límite.",
    "El almacenamiento del navegador (cookies propias, `localStorage`) solo se puede "
    "medir cuando exista una superficie de navegador; hoy no hay ninguna.",
    "Este inventario no dice quién es responsable, encargado ni subencargado de nada: "
    "eso lo decide T-030·ter con estos hechos delante.",
    "La licencia de un paquete que solo se instala en un sistema operativo se declara "
    "en el guion y se cruza contra los metadatos en el entorno que sí lo instala.",
    "Los datos que no son tabla se descubren por las constantes de prefijo del código, "
    "y el generador se niega a escribir si alguna no tiene fila. Ese descubrimiento "
    "reconoce las constantes por su nombre: una familia de claves que no lo siguiera "
    "quedaría fuera, y por eso el nombre es convención del repositorio y no criterio "
    "de quien la escribe.",
)


def derivar(
    conexion,
    *,
    raiz: Path = RAIZ,
    lista_de_proveedores=None,
    fabrica_de_aplicacion=None,
) -> Inventario:
    """Todo el inventario, derivado. Si algo no se puede derivar, se cae aqui."""
    exigir_que_sigan_pendientes(raiz)
    exigir_que_todo_prefijo_este_inventariado(raiz)
    exigir_catalogo_de_esta_revision(conexion, raiz)
    superficies, rutas = derivar_superficies(raiz, fabrica_de_aplicacion)
    return Inventario(
        datos=derivar_datos(conexion, raiz),
        destinatarios=derivar_destinatarios(raiz, lista_de_proveedores),
        superficies=superficies,
        rutas=rutas,
        licencias=derivar_licencias(raiz),
        retencion=derivar_retencion(),
        identidades=derivar_identidades(conexion),
        limites=list(LIMITES_DEL_INVENTARIO),
    )


def _celda(valor: Any) -> str:
    texto = ", ".join(valor) if isinstance(valor, list) else str(valor)
    return texto.replace("|", "\\|").replace("\n", " ")


def _tabla(cabeceras: tuple[str, ...], claves: tuple[str, ...], filas: list[dict]) -> list[str]:
    lineas = ["| " + " | ".join(cabeceras) + " |", "|" + "---|" * len(cabeceras)]
    for fila in filas:
        lineas.append("| " + " | ".join(_celda(fila.get(clave, "")) for clave in claves) + " |")
    return [*lineas, ""]


def render_markdown(inventario: Inventario) -> str:
    """El inventario para personas.

    No lleva fecha de generacion: una marca de tiempo lo haria divergir en cada
    corrida y el gate acabaria apagado por ruido.
    """
    lineas: list[str] = [
        "# Inventario legal de Heraldo",
        "",
        "> **Este documento lo GENERA `scripts/inventario_legal.py` desde el código.**",
        "> No se edita a mano: la siguiente generación borraría el cambio, y",
        "> `apps/api/tests/test_inventario_legal.py` se pone **roja** si lo que está",
        "> comprometido aquí deja de ser lo que el código deriva hoy (RF-31).",
        "",
        "Para regenerarlo: `uv run --no-sync python scripts/inventario_legal.py`.",
        "",
        "## Qué es y qué no es",
        "",
        "Son los **hechos** sobre los que se puede escribir el piso legal sin afirmar",
        "nada que el sistema no haga. Aquí no hay ninguna calificación jurídica:",
        "**quién es responsable, encargado o subencargado** lo decide **T-030·ter**,",
        "con este inventario delante. El texto público lo escribe **T-030·quater**,",
        "que lee la versión en JSON de este mismo documento.",
        "",
        "Lo que todavía no existe se declara **pendiente**, nombrando la casilla que",
        "lo trae. Esa ausencia está **medida contra el árbol del repositorio**: el día",
        "que el artefacto exista, el generador se niega a seguir declarándola.",
        "",
        "## 1. Datos",
        "",
        "Todas las tablas del catálogo vivo, más los datos que no son tabla. El",
        "*alcance* dice de quién son las filas: **de cliente** (aisladas por agencia y",
        "por cliente), **de agencia** o **no-inquilino**.",
        "",
    ]
    lineas += _tabla(
        ("dato", "forma", "categoría", "alcance", "qué contiene", "procedencia"),
        ("nombre", "forma", "categoria", "alcance", "contenido", "procedencia"),
        inventario.datos,
    )
    lineas += [
        "## 2. Destinatarios",
        "",
        "A quién sale un dato, para qué, dónde se trata y por qué mecanismo.",
        "",
    ]
    lineas += _tabla(
        ("destinatario", "función", "ubicación (región)", "mecanismo", "procedencia"),
        ("destinatario", "funcion", "ubicacion", "mecanismo", "procedencia"),
        inventario.destinatarios,
    )
    lineas += [
        "## 3. Cookies y almacenamiento, por superficie",
        "",
        "**Medido**, no declarado: el generador construye la aplicación real y recorre",
        "todas sus rutas registradas anotando cada cabecera `Set-Cookie` que emite.",
        "",
    ]
    lineas += _tabla(
        ("superficie", "estado", "cookies", "almacenamiento del navegador", "procedencia"),
        ("superficie", "estado", "cookies", "almacenamiento", "procedencia"),
        inventario.superficies,
    )
    lineas += [
        "### 3.1 La medida, ruta a ruta",
        "",
        "La aplicación se construye apuntando a una base que **no existe a propósito**,",
        "y sin identidad cableada: así ninguna ruta medida puede tocar una fila de",
        "verdad. Por eso el *código* de algunas filas es el de una dependencia caída o",
        "el de una ruta que no atiende — lo que se está midiendo aquí no es la salud",
        "del sistema, sino **qué cabeceras emite cada ruta**.",
        "",
    ]
    lineas += _tabla(
        ("método", "ruta", "código", "Set-Cookie", "procedencia"),
        ("metodo", "ruta", "codigo", "cookies", "procedencia"),
        [
            {**fila, "cookies": ", ".join(fila["cookies"]) if fila["cookies"] else "ninguna"}
            for fila in inventario.rutas
        ],
    )
    lineas += [
        "## 4. Licencias de terceros",
        "",
        "Los paquetes de la resolución bloqueada (`uv.lock`) con la licencia que",
        "declaran sus metadatos instalados.",
        "",
    ]
    lineas += _tabla(
        ("paquete", "versión", "licencia", "fuente del dato", "procedencia"),
        ("paquete", "version", "licencia", "fuente", "procedencia"),
        inventario.licencias,
    )
    lineas += [
        "## 5. Retención vigente, por categoría",
        "",
        "El plazo que hoy aplica el código, leído de la constante que lo fija.",
        "",
    ]
    lineas += _tabla(
        ("categoría", "plazo vigente", "punto de inicio", "procedencia"),
        ("categoria", "plazo", "inicio", "procedencia"),
        inventario.retencion,
    )
    lineas += ["## 6. Identidades de plataforma y datos de operadores", ""]
    lineas += _tabla(
        ("identidad", "estado", "qué contiene", "procedencia"),
        ("identidad", "estado", "contenido", "procedencia"),
        inventario.identidades,
    )
    lineas += ["## Límites declarados de este inventario", ""]
    lineas += [f"- {limite}" for limite in inventario.limites]
    lineas.append("")
    return "\n".join(lineas)


def render_json(inventario: Inventario) -> str:
    """El inventario para T-030-quater. Ordenado y sin marcas de tiempo."""
    documento = {
        "version_del_formato": 1,
        "generado_por": "scripts/inventario_legal.py",
        "no_decide": [
            "quién es responsable, encargado o subencargado (T-030·ter)",
            "qué promete el texto público (T-030·quater)",
        ],
        "bloques": {
            "datos": inventario.datos,
            "destinatarios": inventario.destinatarios,
            "superficies": inventario.superficies,
            "licencias": inventario.licencias,
            "retencion": inventario.retencion,
            "identidades": inventario.identidades,
        },
        "medicion_de_rutas": inventario.rutas,
        "limites": inventario.limites,
    }
    return json.dumps(documento, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def gate_de_publicabilidad():
    """Carga `scripts/publicable.py` como modulo, sin tocar `sys.path`."""
    guion = Path(__file__).resolve().parent / "publicable.py"
    especificacion = importlib.util.spec_from_file_location("publicable", guion)
    if especificacion is None or especificacion.loader is None:
        raise InventarioNoPublicable(f"no se pudo cargar el gate de publicabilidad: {guion}")
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def _redacciones(inventario: Inventario) -> dict[str, str]:
    return {NOMBRE_MD: render_markdown(inventario), NOMBRE_JSON: render_json(inventario)}


def escribir(inventario: Inventario, salida: Path = SALIDA_POR_DEFECTO) -> list[Path]:
    """Escribe las dos redacciones. Antes las pasa por el gate: si no pasa, no escribe."""
    gate = gate_de_publicabilidad()
    redacciones = _redacciones(inventario)
    faltas: list[str] = []
    for nombre, texto in redacciones.items():
        faltas.extend(gate.revisar_texto(texto, f"docs/legal/{nombre}"))
    if faltas:
        raise InventarioNoPublicable(
            "el inventario derivado NO pasa el gate de publicabilidad y por eso no se "
            "escribe:\n  " + "\n  ".join(faltas)
        )
    salida.mkdir(parents=True, exist_ok=True)
    escritos: list[Path] = []
    for nombre, texto in redacciones.items():
        destino = salida / nombre
        destino.write_text(texto, encoding="utf-8", newline="\n")
        escritos.append(destino)
    return escritos


def divergencias(inventario: Inventario, salida: Path = SALIDA_POR_DEFECTO) -> list[str]:
    """Que diferencia hay entre lo comprometido y lo que el codigo deriva hoy."""
    faltas: list[str] = []
    for nombre, derivado in _redacciones(inventario).items():
        destino = salida / nombre
        if not destino.is_file():
            faltas.append(f"{destino}: no existe; el inventario nunca se genero")
            continue
        comprometido = destino.read_text(encoding="utf-8")
        if comprometido != derivado:
            faltas.append(
                f"{destino}: lo comprometido no es lo que el codigo deriva hoy "
                f"({len(comprometido)} caracteres comprometidos, {len(derivado)} derivados)"
            )
    return faltas


@contextmanager
def _conexion_de_admin():
    """Conexion al catalogo vivo que ademas SUELTA el motor al salir.

    # WHY: la version anterior devolvia `create_engine(...).connect()`, asi que el
    # `with` cerraba la conexion y el motor —con su pool— se quedaba sin dueno
    # hasta que muriera el proceso. En un guion corto no se nota, y por eso
    # sobrevive: es la clase de fuga que solo duele cuando alguien reutiliza la
    # funcion desde un proceso que no termina.
    """
    dsn = os.environ.get(VARIABLE_DSN_ADMIN)
    if not dsn:
        raise SinCatalogo(
            f"falta {VARIABLE_DSN_ADMIN}: el bloque de datos se deriva del CATALOGO "
            "VIVO, y sin base no hay catalogo. No se inventa uno"
        )
    motor = create_engine(dsn, future=True, isolation_level="AUTOCOMMIT")
    try:
        with motor.connect() as conexion:
            yield conexion
    finally:
        motor.dispose()


def main(argumentos: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description="Inventario legal derivado del codigo")
    analizador.add_argument(
        "--verificar",
        action="store_true",
        help="no escribe: sale 1 si lo comprometido diverge de lo derivado",
    )
    analizador.add_argument(
        "--salida", default=str(SALIDA_POR_DEFECTO), help="directorio de las dos redacciones"
    )
    opciones = analizador.parse_args(argumentos)
    salida = Path(opciones.salida)

    # WHY (dos fallos distintos, dos codigos distintos): «el documento diverge»
    # se arregla regenerando, y «no se pudo derivar el inventario» es
    # estructural — una tabla sin categoria, un destinatario nuevo, el catalogo
    # de otra rama. Con un solo codigo de salida, quien lee el CI ve el mismo
    # rojo para las dos y prueba la receta equivocada; y una traza cruda entierra
    # el mensaje, que es justo la parte escrita para ser leida.
    try:
        with _conexion_de_admin() as conexion:
            inventario = derivar(conexion)

        if opciones.verificar:
            faltas = divergencias(inventario, salida=salida)
            if faltas:
                print("INVENTARIO LEGAL DIVERGENTE:", file=sys.stderr)
                for falta in faltas:
                    print(f"  {falta}", file=sys.stderr)
                print(
                    "\nLo que el piso legal publica tiene que ser lo que el codigo hace "
                    "(RF-31). Regenera con "
                    "`uv run --no-sync python scripts/inventario_legal.py`",
                    file=sys.stderr,
                )
                return 1
            print(
                f"inventario legal: {len(inventario.datos)} datos, "
                f"{len(inventario.destinatarios)} destinatarios, "
                f"{len(inventario.superficies)} superficies, "
                f"{len(inventario.licencias)} paquetes — sin divergencias"
            )
            return 0

        escritos = escribir(inventario, salida=salida)
        for destino in escritos:
            print(f"escrito {destino}")
        return 0
    except InventarioIncompleto as fallo:
        print(f"INVENTARIO LEGAL NO DERIVABLE: {fallo}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
