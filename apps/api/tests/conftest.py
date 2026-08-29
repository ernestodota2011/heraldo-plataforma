"""Andamiaje de la suite: un Postgres REAL, migrado, con el rol de aplicacion.

# WHY: aqui no hay ni un `pytest.skip`. Un `skip` cuando falta la base pinta el
# CI de verde sin haber medido nada, que es exactamente el defecto que este
# producto existe para no repetir (`feedback_verde_no_dice_que_midio`). Si no hay
# Postgres, la suite se pone en ROJO y dice por que.
#
# WHY: la contrasena del rol de aplicacion se GENERA en cada corrida y muere con
# ella. En el repositorio no hay ninguna credencial; del entorno solo viene el
# DSN del rol migrador.
#
# WHY: la base se deja en un estado conocido ANTES de migrar. Una corrida
# anterior que dejo una tabla saboteada (asi se comprueba que el gate se cae de
# verdad) no puede contaminar la siguiente.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tenancy import Inquilino, crear_motor
from app.tenancy.rol import ROL_APLICACION
from app.tenancy.secrets import (
    VARIABLE_DE_ENTORNO_CLAVE,
    SecretoEnClaro,
    cifrar,
    clave_de_cifrado,
    genera_clave,
)

RAIZ = Path(__file__).resolve().parents[3]
INI_ALEMBIC = RAIZ / "apps" / "api" / "migrations" / "alembic.ini"

#: DSN del rol MIGRADOR (dueño de las tablas). Lo declara quien corre la suite.
VARIABLE_DSN_ADMIN = "HERALDO_DATABASE_URL_ADMIN"

# Identificadores fijos: cuando una sonda falla, el mensaje dice QUE inquilino
# vio lo que no debia, en vez de un uuid distinto en cada corrida.
AGENCIA_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
AGENCIA_B = UUID("bbbbbbbb-0000-4000-8000-000000000001")
CLIENTE_A1 = UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
CLIENTE_A2 = UUID("aaaaaaaa-0000-4000-8000-0000000000a2")
CLIENTE_B1 = UUID("bbbbbbbb-0000-4000-8000-0000000000b1")
HERALDO_A1 = UUID("aaaaaaaa-0000-4000-8000-00000000f0a1")
HERALDO_A2 = UUID("aaaaaaaa-0000-4000-8000-00000000f0a2")
HERALDO_B1 = UUID("bbbbbbbb-0000-4000-8000-00000000f0b1")

# Las filas sembradas en las cinco tablas de la revision 0003. Una por cliente,
# porque la bateria necesita distinguir «lo mio», «lo del vecino» y «lo ajeno»
# en CADA tipo de recurso, no solo en `heraldos`.
SECRETO_A1 = UUID("aaaaaaaa-0000-4000-8000-00000000c0a1")
SECRETO_A2 = UUID("aaaaaaaa-0000-4000-8000-00000000c0a2")
SECRETO_B1 = UUID("bbbbbbbb-0000-4000-8000-00000000c0b1")
APUNTE_A1 = UUID("aaaaaaaa-0000-4000-8000-00000000b0a1")
APUNTE_A2 = UUID("aaaaaaaa-0000-4000-8000-00000000b0a2")
APUNTE_B1 = UUID("bbbbbbbb-0000-4000-8000-00000000b0b1")
TRABAJO_A1 = UUID("aaaaaaaa-0000-4000-8000-00000000d0a1")
TRABAJO_A2 = UUID("aaaaaaaa-0000-4000-8000-00000000d0a2")
TRABAJO_B1 = UUID("bbbbbbbb-0000-4000-8000-00000000d0b1")
ARCHIVADO_A1 = UUID("aaaaaaaa-0000-4000-8000-00000000e0a1")
ARCHIVADO_A2 = UUID("aaaaaaaa-0000-4000-8000-00000000e0a2")
ARCHIVADO_B1 = UUID("bbbbbbbb-0000-4000-8000-00000000e0b1")
MENSAJE_A1 = UUID("aaaaaaaa-0000-4000-8000-0000000090a1")
MENSAJE_A2 = UUID("aaaaaaaa-0000-4000-8000-0000000090a2")
MENSAJE_B1 = UUID("bbbbbbbb-0000-4000-8000-0000000090b1")

#: El nombre del secreto sembrado y su valor EN CLARO. El valor esta aqui a
#: proposito y no es una credencial: es el testigo que la sonda de CE-06 busca en
#: las respuestas para comprobar que NO aparece. Nace y muere con la corrida.
NOMBRE_DEL_SECRETO_SEMBRADO = "credencial_del_canal"
VALOR_DEL_SECRETO_SEMBRADO = "testigo-de-la-sonda-de-secretos"

#: Los trabajos sembrados nacen `pendiente` con su disponibilidad MUY en el
#: futuro. Asi existen —la bateria necesita filas contra las que medir— pero
#: ningun `reclamar` los coge y ningun archivado los mueve: no contaminan a las
#: pruebas de la cola, que miden su propio escenario.
DISPONIBILIDAD_LEJANA = datetime(2999, 1, 1, tzinfo=UTC)


def pytest_asyncio_loop_factories(config, item):
    """En Windows la suite corre sobre el bucle SELECTOR, no el Proactor.

    # WHY: psycopg (v3) NO soporta el `ProactorEventLoop`, que es el bucle por
    # defecto de asyncio en Windows: aborta la conexion con `InterfaceError`. El
    # producto se despliega en Linux, asi que esto NO va en el codigo de la
    # aplicacion — seria un arreglo para una plataforma que no existe en
    # produccion. Va aqui, y su efecto es que la maquina de desarrollo mide LO
    # MISMO que el CI en vez de saltarse los tests asincronos: es lo contrario
    # de `feedback_verde_plataforma_no_importa`, donde el marcador por-SO
    # producia divergencia. Si esto dejara de funcionar, en Windows los tests
    # asincronos se caen en ROJO — no se saltan en verde.
    #
    # WHY: `pytest_asyncio_loop_factories` no aparece en la documentacion
    # publicada; se verifico contra el codigo del plugin INSTALADO — el hookspec
    # esta declarado en `pytest_asyncio/plugin.py` y NO existe en 1.3.0 (medido).
    # Por eso `pyproject.toml` exige `pytest-asyncio>=1.4`: pytest aborta la
    # coleccion entera ante un hook que ningun hookspec declara, asi que un piso
    # mas bajo no degrada nada — deja la suite sin arrancar (P-09).
    #
    # WHY: es el hook, no la fixture `event_loop_policy`. Esa esta DEPRECADA en
    # pytest-asyncio 1.4 y desaparece en una version futura: habria funcionado
    # hoy y roto el dia de la actualizacion, que es deuda con fecha.
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


#: La suite BORRA el esquema antes de migrar. Para que no pueda borrar algo que
#: no sea suyo, el nombre de la base tiene que declararse como de pruebas.
#: Es una ALLOWLIST por forma, y no tiene interruptor que la puentee: una perilla
#: para saltarse el guard ES el guard (`feedback_fail_open_traga_al_guard`).
#: Tiene que TERMINAR en `test`, no solo contenerlo: `test_produccion` contiene
#: la palabra y no es una base de pruebas.
NOMBRE_DE_BASE_DE_PRUEBAS = re.compile(r"^[a-z0-9_]*test$")


class SinPostgres(RuntimeError):
    """No hay base contra la que medir. Es un fallo, no un motivo para saltar."""


class BaseQueNoEsDePruebas(RuntimeError):
    """El DSN apunta a una base que no se declara de pruebas: no se toca."""


@dataclass(frozen=True, slots=True)
class Escenario:
    """Dos agencias, tres clientes, tres heraldos. El minimo para que una fuga
    entre agencias y una fuga entre clientes de LA MISMA agencia sean distintas."""

    dsn_app: str


@pytest.fixture(scope="session")
def dsn_admin() -> str:
    dsn = os.environ.get(VARIABLE_DSN_ADMIN)
    if not dsn:
        raise SinPostgres(
            f"falta {VARIABLE_DSN_ADMIN}. La suite de aislamiento mide contra un "
            "Postgres REAL; sin el no hay nada que medir y el verde seria mentira. "
            "Forma: postgresql+psycopg://USUARIO:CLAVE@HOST:PUERTO/BASE (ver "
            "apps/api/migrations/README)."
        )
    _exigir_base_de_pruebas(dsn)
    return dsn


def _exigir_base_de_pruebas(dsn: str) -> None:
    """El paso ANTES del DROP. Sin esto, un DSN equivocado borra lo que sea.

    # WHY: `escenario` empieza con un `DROP TABLE ... CASCADE` para dejar la base
    # en un estado conocido. Eso es correcto para una base de pruebas y es una
    # catastrofe para cualquier otra, y hasta aqui lo unico que separaba las dos
    # era que quien exporta la variable de entorno no se equivocara. La
    # comprobacion va contra el NOMBRE de la base, que es lo que un despiste
    # cambia (`...heraldo_test` -> `...heraldo`), y falla cerrado.
    """
    nombre = (make_url(dsn).database or "").lower()
    if not NOMBRE_DE_BASE_DE_PRUEBAS.match(nombre):
        raise BaseQueNoEsDePruebas(
            f"la base {nombre!r} no se declara de pruebas y esta suite BORRA el "
            "esquema antes de migrar. Su nombre debe TERMINAR en 'test' "
            "(p. ej. heraldo_test). No hay forma de saltarse esta comprobacion: "
            "renombra la base o apunta el DSN a otra"
        )


@pytest.fixture(scope="session")
def escenario(dsn_admin: str) -> Iterator[Escenario]:
    os.environ[VARIABLE_DSN_ADMIN] = dsn_admin
    motor_admin = create_engine(dsn_admin, future=True, isolation_level="AUTOCOMMIT")

    with motor_admin.connect() as conexion:
        # WHY: se borra el ESQUEMA entero, no una lista de tablas escrita a mano.
        # Medido: con la lista, una tabla que una migracion creara y que nadie
        # anadiera a mano ahi SOBREVIVIA a todas las corridas siguientes — el
        # universo del setup era mas estrecho que el de lo que la suite afirma, que
        # es el mismo defecto de P-10 y P-11 por tercera vez. Derivarlo del esquema
        # lo cierra: lo que la migracion cree, esto lo borra, sin que nadie tenga
        # que acordarse.
        conexion.execute(text("DROP SCHEMA public CASCADE"))
        conexion.execute(text("CREATE SCHEMA public"))

    # WHY (P-10 / P-11): aqui NO se borra el rol, aunque su estado sobreviva a
    # estas tablas. Un rol es objeto de CLUSTER y `DROP OWNED BY` solo alcanza la
    # base ACTUAL, asi que en un cluster con dos bases de Heraldo el borrado falla
    # con `DependentObjectsStillExist` y se lleva por delante la suite entera
    # (medido). La staleness que P-10 destapo se cierra donde de verdad estaba: en
    # la SENTENCIA, obligando a que la migracion nombre explicitamente cada
    # atributo que las pruebas afirman — lo hace cumplir
    # `test_la_migracion_declara_cada_atributo_que_se_afirma`. Un `ALTER ROLE` que
    # nombra el atributo lo fija, venga el rol de donde venga.

    command.upgrade(Config(str(INI_ALEMBIC)), "head")

    clave = secrets.token_urlsafe(24)
    # WHY (T-016): la clave de cifrado de secretos tambien SE GENERA en cada
    # corrida y muere con ella. En el repositorio no vive ninguna clave, ni
    # siquiera de juguete: una clave de juguete commiteada enseña que commitear
    # claves es normal. Se declara por entorno, que es como la lee el producto.
    os.environ[VARIABLE_DE_ENTORNO_CLAVE] = genera_clave()
    # WHY: el `try` empieza AQUI, antes de dar LOGIN al rol. Si la siembra falla
    # —o cualquier cosa entre medias—, el rol ya tiene contrasena valida y sin
    # este alcance la limpieza no llegaria a corerr. La regla: el bloque de
    # limpieza cubre desde el instante en que existe algo que limpiar.
    try:
        with motor_admin.connect() as conexion:
            # ALTER ROLE no admite parametros ligados; la clave es
            # token_urlsafe (alfanumerico, `-` y `_`): no hay nada que escapar.
            conexion.execute(
                text(f"ALTER ROLE {ROL_APLICACION} LOGIN PASSWORD '{clave}'")  # noqa: S608
            )
            sembrar_escenario(conexion)

        url_app = make_url(dsn_admin).set(username=ROL_APLICACION, password=clave)
        dsn_app = url_app.render_as_string(hide_password=False)
        os.environ["HERALDO_DATABASE_URL"] = dsn_app

        yield Escenario(dsn_app=dsn_app)
    finally:
        # WHY: sin el `finally`, una suite interrumpida (Ctrl-C, un fallo de
        # coleccion, un timeout del CI) dejaba el rol de aplicacion con LOGIN y
        # contrasena viva. La limpieza intenta TODOS los pasos aunque uno falle:
        # abandonar al primer error dejaria a medias justo lo que vino a cerrar.
        for paso in (
            lambda: _revocar_login(motor_admin),
            lambda: os.environ.pop("HERALDO_DATABASE_URL", None),
            lambda: os.environ.pop(VARIABLE_DE_ENTORNO_CLAVE, None),
            motor_admin.dispose,
        ):
            try:
                paso()
            except Exception as fallo:  # noqa: BLE001 - se reporta y se sigue
                print(f"aviso: fallo limpiando el escenario: {fallo!r}")


def _revocar_login(motor_admin) -> None:
    """Quita el LOGIN y ademas BORRA la contrasena.

    # WHY: `NOLOGIN` ya impide entrar, pero deja el hash guardado en el catalogo
    # del cluster hasta la proxima corrida. Una credencial que sobrevive a la
    # tarea que la necesitaba no tiene por que existir, aunque sea efimera y
    # aunque hoy nada la pueda usar: si manana alguien devuelve el LOGIN, la
    # contrasena vieja vuelve a valer.
    """
    with motor_admin.connect() as conexion:
        conexion.execute(text(f"ALTER ROLE {ROL_APLICACION} NOLOGIN PASSWORD NULL"))  # noqa: S608


def sembrar_escenario(conexion) -> None:
    """Siembra con el rol MIGRADOR (superusuario): salta RLS a proposito.

    Si sembrara con el rol de aplicacion, las politicas filtrarian la siembra y
    la prueba mediria un escenario que ella misma recorto.
    """
    conexion.execute(text("DELETE FROM agencias"))
    conexion.execute(
        text(
            "INSERT INTO agencias (agencia_id, nombre) "
            "VALUES (:a, 'Agencia A'), (:b, 'Agencia B')"
        ),
        {"a": AGENCIA_A, "b": AGENCIA_B},
    )
    conexion.execute(
        text(
            "INSERT INTO clientes (id, agencia_id, nombre) VALUES "
            "(:a1, :a, 'Cliente A1'), (:a2, :a, 'Cliente A2'), (:b1, :b, 'Cliente B1')"
        ),
        {"a": AGENCIA_A, "b": AGENCIA_B, "a1": CLIENTE_A1, "a2": CLIENTE_A2, "b1": CLIENTE_B1},
    )
    conexion.execute(
        text(
            "INSERT INTO heraldos (id, agencia_id, cliente_id, nombre) VALUES "
            "(:h1, :a, :a1, 'Heraldo A1'), (:h2, :a, :a2, 'Heraldo A2'), "
            "(:h3, :b, :b1, 'Heraldo B1')"
        ),
        {
            "a": AGENCIA_A,
            "b": AGENCIA_B,
            "a1": CLIENTE_A1,
            "a2": CLIENTE_A2,
            "b1": CLIENTE_B1,
            "h1": HERALDO_A1,
            "h2": HERALDO_A2,
            "h3": HERALDO_B1,
        },
    )
    _sembrar_la_base_y_la_cola(conexion)


#: Los tres inquilinos sembrados, en el mismo orden que usa toda la siembra:
#: «lo mio» (A1), «lo del vecino» (A2, misma agencia) y «lo ajeno» (B1, otra
#: agencia). Los dos EJES de la cascada (C-01) necesitan los tres.
_INQUILINOS_SEMBRADOS = (
    (AGENCIA_A, CLIENTE_A1, "A1"),
    (AGENCIA_A, CLIENTE_A2, "A2"),
    (AGENCIA_B, CLIENTE_B1, "B1"),
)

_SECRETO_DE = {"A1": SECRETO_A1, "A2": SECRETO_A2, "B1": SECRETO_B1}
_APUNTE_DE = {"A1": APUNTE_A1, "A2": APUNTE_A2, "B1": APUNTE_B1}
_TRABAJO_DE = {"A1": TRABAJO_A1, "A2": TRABAJO_A2, "B1": TRABAJO_B1}
_ARCHIVADO_DE = {"A1": ARCHIVADO_A1, "A2": ARCHIVADO_A2, "B1": ARCHIVADO_B1}
_MENSAJE_DE = {"A1": MENSAJE_A1, "A2": MENSAJE_A2, "B1": MENSAJE_B1}


def valor_sembrado_del_secreto(etiqueta: str) -> str:
    """El valor en claro del secreto de ese inquilino. Distinto por inquilino.

    # WHY (distinto y no el mismo): si los tres compartieran valor, una sonda que
    # descifrara el secreto del vecino y lo comparara con el propio saldria VERDE
    # sin haber medido nada.
    """
    return f"{VALOR_DEL_SECRETO_SEMBRADO}-{etiqueta}"


def _sembrar_la_base_y_la_cola(conexion) -> None:
    """Una fila por inquilino en cada tabla de la revision 0003.

    # WHY (una por inquilino y no una suelta): sin filas de las TRES relaciones,
    # media bateria de sondas mediria «no veo nada» sobre una tabla vacia — que es
    # el mismo verde-por-ausencia que L-02 aprovecho dos veces para colarse.
    #
    # # WHY (el secreto se siembra CIFRADO, con la clave de la corrida): sembrarlo
    # en claro haria que la sonda de CE-06 midiera un escenario que no existe. Aqui
    # la fila es exactamente lo que el producto habria escrito.
    """
    clave = clave_de_cifrado()
    for agencia_id, cliente_id, etiqueta in _INQUILINOS_SEMBRADOS:
        comun = {"a": agencia_id, "c": cliente_id}
        conexion.execute(
            text(
                "INSERT INTO secretos (id, agencia_id, cliente_id, nombre, cifrado) "
                "VALUES (:id, :a, :c, :nombre, :cifrado)"
            ),
            comun
            | {
                "id": _SECRETO_DE[etiqueta],
                "nombre": NOMBRE_DEL_SECRETO_SEMBRADO,
                "cifrado": cifrar(
                    clave,
                    agencia_id=agencia_id,
                    cliente_id=cliente_id,
                    nombre=NOMBRE_DEL_SECRETO_SEMBRADO,
                    valor=SecretoEnClaro(valor_sembrado_del_secreto(etiqueta)),
                ),
            },
        )
        conexion.execute(
            text(
                "INSERT INTO bitacora (id, agencia_id, cliente_id, actor, accion, recurso) "
                "VALUES (:id, :a, :c, 'siembra', 'alta', 'escenario')"
            ),
            comun | {"id": _APUNTE_DE[etiqueta]},
        )
        conexion.execute(
            text(
                "INSERT INTO trabajos (id, agencia_id, cliente_id, tipo, disponible_en) "
                "VALUES (:id, :a, :c, 'siembra', :lejos)"
            ),
            comun | {"id": _TRABAJO_DE[etiqueta], "lejos": DISPONIBILIDAD_LEJANA},
        )
        conexion.execute(
            text(
                "INSERT INTO trabajos_archivados (id, agencia_id, cliente_id, tipo, carga, "
                "       estado, intentos, maximo_intentos, creado_en, terminado_en) "
                "VALUES (:id, :a, :c, 'siembra', '{}'::jsonb, 'hecho', 1, 5, now(), now())"
            ),
            comun | {"id": _ARCHIVADO_DE[etiqueta]},
        )
        conexion.execute(
            text(
                "INSERT INTO mensajes_entrantes (id, agencia_id, cliente_id, canal, id_externo) "
                "VALUES (:id, :a, :c, 'siembra', :externo)"
            ),
            # WHY: el MISMO identificador externo para los tres. Es a proposito:
            # demuestra que la unicidad es POR INQUILINO — si fuera global, esta
            # siembra reventaria y la suite entera lo diria en el arranque.
            comun | {"id": _MENSAJE_DE[etiqueta], "externo": "mensaje-sembrado"},
        )


@pytest.fixture
async def motor(escenario: Escenario) -> AsyncIterator[AsyncEngine]:
    """El motor de la aplicacion CON EL POOL EN CONFIGURACION DE PRODUCCION.

    No es una conexion directa a proposito: el pool con inquilino por conexion ya
    mordio a la casa en Supabase (`feedback_supavisor_tenant`), y una prueba que
    esquiva el pool no mide el sistema que se despliega.
    """
    motor = crear_motor(escenario.dsn_app)
    try:
        yield motor
    finally:
        await motor.dispose()


@pytest.fixture
async def motor_de_una_sola_conexion(escenario: Escenario) -> AsyncIterator[AsyncEngine]:
    """El PEOR caso del pool: una unica conexion fisica para todos los inquilinos."""
    motor = crear_motor(escenario.dsn_app, tamano_pool=1)
    try:
        yield motor
    finally:
        await motor.dispose()


@pytest.fixture
def motor_admin(dsn_admin: str, escenario: Escenario):
    motor = create_engine(dsn_admin, future=True, isolation_level="AUTOCOMMIT")
    try:
        yield motor
    finally:
        motor.dispose()


@pytest.fixture(scope="session")
def motor_de_siembra(dsn_admin: str, escenario: Escenario):
    """Motor de ADMIN de vida larga, para devolver el escenario a su estado sembrado.

    # WHY: la bateria de T-013 ESCRIBE — hay sondas cuyo veredicto correcto es
    # `PERMITIDO`, y esas commitean. Sin devolver la base al estado sembrado antes
    # de cada sonda, la siguiente mediria un escenario que la anterior le dejo, y
    # el ORDEN de ejecucion pasaria a ser parte del resultado.
    #
    # WHY: es de sesion y no por-test. Con un motor nuevo por sonda, cada una
    # pagaria un `connect()` completo; con uno de sesion el pool reutiliza la
    # conexion.
    """
    motor = create_engine(dsn_admin, future=True, isolation_level="AUTOCOMMIT")
    try:
        yield motor
    finally:
        motor.dispose()


def resembrar(motor) -> None:
    """Devuelve el escenario a su estado inicial usando LA MISMA siembra.

    # WHY (P-12): la limpieza no enumera nada a mano. `sembrar_escenario` empieza
    # por `DELETE FROM agencias` y la cascada de claves foraneas se lleva por
    # delante clientes y heraldos; una tabla de inquilino futura, colgada de esa
    # misma cascada, se limpia sola. El universo de la limpieza se DERIVA del
    # modelo — que es la leccion que P-10, P-11 y P-12 son tres veces.
    """
    with motor.connect() as conexion:
        sembrar_escenario(conexion)


# --------------------------------------------------------------------------
# El cliente de Redis de la idempotencia (T-021)
# --------------------------------------------------------------------------
@pytest.fixture
async def redis(dsn_redis: str):
    """Un cliente de Redis LIMPIO de claves de idempotencia, antes y despues.

    # WHY (se borra por PREFIJO, no con `FLUSHDB`): el prefijo se importa del
    # modulo que lo define, asi que la limpieza DERIVA su universo del producto en
    # vez de enumerarlo. Y no se vacia la base entera porque un `FLUSHDB` sobre un
    # Redis compartido se lleva por delante lo que no es suyo — la misma leccion de
    # «el universo del setup» de P-10/P-11/P-12, aqui aplicada a Redis.
    #
    # # WHY (limpia ANTES y DESPUES): antes, para que el orden de ejecucion no sea
    # parte del resultado; despues, para no dejarle el escenario sucio a nadie.
    #
    # # WHY (se apoya en `dsn_redis`, que declara el carril de sesiones y limites):
    # los dos carriles llegaron a Redis a la vez y cada uno escribio su forma de
    # pedir el DSN. Al fusionarlos habia DOS `dsn_redis` y DOS `SinRedis` en este
    # archivo, y `git` no vio ningun conflicto porque estaban en sitios distintos:
    # Python se queda con la ultima definicion y nadie se entera. Aqui hay una sola.
    """
    from redis.asyncio import Redis

    from app.channels.idempotency import PREFIJO

    cliente = Redis.from_url(dsn_redis, decode_responses=True)

    async def limpiar() -> None:
        async for clave in cliente.scan_iter(match=f"{PREFIJO}:*", count=500):
            await cliente.delete(clave)

    await limpiar()
    try:
        yield cliente
    finally:
        try:
            await limpiar()
        finally:
            await cliente.aclose()


@pytest.fixture
def catalogo_de_tablas(motor_admin) -> dict[str, set[str]]:
    """Tablas ordinarias del esquema con sus columnas, LEIDAS DEL CATALOGO.

    # WHY (vive aqui y no en un modulo de pruebas): la usan cuatro archivos. Con
    # una copia por archivo habria cuatro redacciones de «cual es el universo de
    # tablas», y la quinta se escribiria un poco distinta — que es como un gate
    # acaba midiendo menos de lo que dice.
    #
    # # WHY (la lectura del catalogo se importa de donde ya vive): `_tablas` y
    # `_columnas` son de `test_rls_cobertura`, el archivo que define las clases. Se
    # importan dentro de la funcion porque conftest se carga antes que los modulos
    # de prueba y un import de modulo aqui ataria el orden de carga.
    """
    from test_rls_cobertura import _columnas, _tablas

    with motor_admin.connect() as conexion:
        tablas = _tablas(conexion)
        columnas = _columnas(conexion)
    return {tabla: columnas.get(tabla, set()) for tabla in tablas}


def sesion_de_agencia(agencia_id: UUID) -> Inquilino:
    return Inquilino.desde_usuario(agencia_id=agencia_id, cliente_id=None)


def sesion_de_cliente(agencia_id: UUID, cliente_id: UUID) -> Inquilino:
    return Inquilino.desde_usuario(agencia_id=agencia_id, cliente_id=cliente_id)


# ==========================================================================
# Redis REAL para T-015 (sesiones) y T-019 (limites) — D-05
# ==========================================================================
# WHY: aqui tampoco hay `pytest.skip` ni doble en memoria. La decision D-05 dice
# que el estado compartido vive en Redis PORQUE en memoria del proceso el limite
# se multiplica por el numero de workers y «revocada» significa «revocada en este
# worker». Un doble en memoria mediria exactamente el defecto que D-05 descarta,
# y saldria verde. Sin Redis, la suite se pone en ROJO y dice por que.
#
# WHY (aislamiento entre pruebas): Redis es estado COMPARTIDO, asi que dos
# pruebas que usen la misma clave se pisan y el orden de ejecucion pasa a ser
# parte del resultado (`feedback_no_paralelizar_compartido`). Cada prueba recibe
# su PROPIO prefijo, derivado de su nodeid mas un token de la corrida — de modo
# que ni dos pruebas, ni dos corridas simultaneas, ni dos maquinas contra el
# mismo Redis se estorban.
#
# WHY (la limpieza se DERIVA, no se enumera): se borra lo que casa con el prefijo
# de la prueba, recorriendo el propio Redis con SCAN. Una clave nueva que un
# modulo futuro invente bajo ese prefijo se limpia sola. Es la leccion de
# P-10/P-11/P-12 aplicada al otro almacen.
#
# WHY (por que aqui NO hay un guard de «base de pruebas» como el de Postgres): el
# de Postgres existe porque `escenario` empieza con `DROP SCHEMA CASCADE`, que es
# destructivo sobre TODA la base. Aqui no hay ninguna operacion destructiva fuera
# del prefijo derivado: el borrado solo alcanza claves que empiezan por
# `prueba:<token>:<ranura>`, y eso lo comprueba `_borrar_por_prefijo`.

#: DSN de Redis. Se declara por entorno, igual que el de Postgres.
VARIABLE_REDIS = "HERALDO_REDIS_URL"


class SinRedis(RuntimeError):
    """No hay Redis contra el que medir. Es un fallo, no un motivo para saltar."""


@dataclass(frozen=True, slots=True)
class BancoRedis:
    """Un Redis real con un prefijo propio, y una fabrica de clientes nuevos.

    `otro_cliente()` da una conexion INDEPENDIENTE sobre el mismo Redis: es como
    se representa un segundo proceso de la API sin levantar un segundo proceso.
    Dos clientes distintos que comparten estado es justo lo que D-05 promete y lo
    que la memoria del proceso no puede dar.
    """

    cliente: object
    prefijo: str
    _fabrica: object

    def otro_cliente(self):
        return self._fabrica()


def _ranura(nodeid: str) -> str:
    """Prefijo estable y legible por prueba, derivado de su identificador."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", nodeid).strip("_")[-80:]


@pytest.fixture(scope="session")
def dsn_redis() -> str:
    dsn = os.environ.get(VARIABLE_REDIS)
    if not dsn:
        raise SinRedis(
            f"falta {VARIABLE_REDIS}. Las sesiones (T-015) y los limites (T-019) "
            "viven en Redis por decision D-05, y se miden contra un Redis REAL; sin "
            "el no hay nada que medir y el verde seria mentira. Forma: "
            "redis://HOST:PUERTO/BASE"
        )
    return dsn


@pytest.fixture(scope="session")
def token_de_corrida() -> str:
    """Distingue esta corrida de cualquier otra contra el mismo Redis."""
    return secrets.token_hex(6)


async def _borrar_por_prefijo(cliente, prefijo: str) -> int:
    """Borra TODO lo que cuelgue del prefijo, derivandolo del propio Redis.

    Comprueba que cada clave empieza de verdad por el prefijo antes de borrarla:
    el `match` de SCAN ya lo garantiza, y aun asi se afirma, porque el dia que
    alguien toque el patron el error tiene que salir aqui y no en produccion.
    """
    patron = f"{prefijo}:*"
    borradas = 0
    async for clave in cliente.scan_iter(match=patron, count=200):
        texto = clave.decode("utf-8") if isinstance(clave, bytes) else clave
        assert texto.startswith(f"{prefijo}:"), (
            f"la limpieza iba a borrar {texto!r}, que no cuelga del prefijo "
            f"{prefijo!r}: el patron de SCAN dejo de acotar"
        )
        borradas += await cliente.unlink(clave)
    return borradas


@pytest.fixture
async def banco_redis(dsn_redis: str, token_de_corrida: str, request) -> AsyncIterator[BancoRedis]:
    from redis.asyncio import Redis

    abiertos: list = []

    def fabrica():
        cliente = Redis.from_url(dsn_redis)
        abiertos.append(cliente)
        return cliente

    principal = fabrica()
    try:
        await principal.ping()
    except Exception as fallo:  # noqa: BLE001 - se re-lanza como fallo explicito
        await principal.aclose()
        raise SinRedis(
            f"{VARIABLE_REDIS} apunta a {dsn_redis!r} y no responde al ping: {fallo!r}. "
            "La suite no se salta: se cae"
        ) from fallo

    prefijo = f"prueba:{token_de_corrida}:{_ranura(request.node.nodeid)}"
    await _borrar_por_prefijo(principal, prefijo)
    try:
        yield BancoRedis(cliente=principal, prefijo=prefijo, _fabrica=fabrica)
    finally:
        # WHY (hallazgo de Crisol, corregido en su version REAL): el `finally`
        # anidado ya garantizaba que el cierre se intentara aunque la limpieza
        # fallara — eso no era el defecto. El defecto es que el BUCLE abandona al
        # primer `aclose()` que levante, y los clientes que quedan detras se
        # filtran. Es la misma regla que el `escenario` de Postgres ya aplica unas
        # lineas mas arriba: la limpieza intenta TODOS los pasos aunque uno falle,
        # porque abandonar al primer error deja a medias justo lo que vino a cerrar.
        try:
            await _borrar_por_prefijo(principal, prefijo)
        finally:
            for cliente in abiertos:
                try:
                    await cliente.aclose()
                except Exception as fallo:  # noqa: BLE001 - se reporta y se sigue
                    print(f"aviso: fallo cerrando un cliente de Redis: {fallo!r}")
