"""Aislamiento entre inquilinos, medido POR EFECTO contra un Postgres real.

Este archivo tiene DOS capas, escritas en dos tareas distintas y a proposito:

1. **T-012** — el subconjunto que aquella tarea necesitaba verificar por efecto
   antes de cerrarse (K-02: la escritura cruzada estaba resuelta por
   DOCUMENTACION, no por medicion). Son las sondas escritas a mano de la primera
   mitad del archivo.
2. **T-013 / T-014·quinquies** — LA BATERIA: la matriz completa de pares de
   alcance sobre todos los tipos de recurso, en los dos ejes y las tres
   direcciones de escritura; la sonda DERIVADA de *sesion-de-cliente -> tabla de
   nivel agencia*; y el ENDPOINT-TRAMPA escrito a proposito sin filtro (CE-02).
   Empieza en la seccion «T-013 — LA BATERIA».

# WHY: todas las sondas corren con el pool en CONFIGURACION DE PRODUCCION, no
# con conexion directa. El pool con inquilino por conexion ya mordio a la casa en
# Supabase (`feedback_supavisor_tenant`); una prueba que esquiva el pool no mide
# el sistema que se despliega (mitigacion de R-01).
#
# # WHY: la bateria ESCRIBE, y algunas de sus escrituras deben salir PERMITIDAS —
# esas commitean. Por eso este modulo devuelve el escenario a su estado sembrado
# antes de cada sonda (`escenario_intacto`) y otra vez al terminar el modulo. La
# limpieza NO enumera tablas: reusa la MISMA siembra, y la cascada de claves
# foraneas se encarga del resto (P-12).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import pytest
from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError

from app.tenancy import Alcance, AlcanceInvalido, Inquilino, sesion_de_inquilino
from app.tenancy.inquilino import CENTINELA_SIN_CLIENTE
from app.tenancy.politicas import COLUMNA_AGENCIA, valida_identificador
from conftest import (
    AGENCIA_A,
    AGENCIA_B,
    APUNTE_A1,
    APUNTE_A2,
    APUNTE_B1,
    ARCHIVADO_A1,
    ARCHIVADO_A2,
    ARCHIVADO_B1,
    CLIENTE_A1,
    CLIENTE_A2,
    CLIENTE_B1,
    HERALDO_A1,
    HERALDO_A2,
    HERALDO_B1,
    MENSAJE_A1,
    MENSAJE_A2,
    MENSAJE_B1,
    SECRETO_A1,
    SECRETO_A2,
    SECRETO_B1,
    TRABAJO_A1,
    TRABAJO_A2,
    TRABAJO_B1,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
)

# WHY: la clasificacion de una tabla en sus tres clases se importa de donde ya
# vive (`test_rls_cobertura.py`, T-014·bis) en vez de reescribirse aqui. Dos
# redacciones de la misma regla divergen; una, no.
from test_rls_cobertura import (
    CLASE_AGENCIA,
    CLASE_CLIENTE,
    CLASE_NO_INQUILINO,
    _politicas,
    clase_de,
)

CONTAR_HERALDOS = text("SELECT count(*) FROM heraldos")
CONTAR_CLIENTES = text("SELECT count(*) FROM clientes")
CONTAR_AGENCIAS = text("SELECT count(*) FROM agencias")


# --------------------------------------------------------------------------
# Andamiaje: el escenario vuelve a su sitio, y el catalogo se DERIVA
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Cada sonda arranca del MISMO escenario sembrado.

    # WHY: sin esto, el ORDEN de ejecucion pasaria a formar parte del resultado —
    # una sonda cuyo veredicto correcto es PERMITIDO deja la base cambiada para la
    # siguiente. Y la limpieza reusa `sembrar_escenario`, no una lista de tablas
    # escrita a mano: es la leccion de P-10, P-11 y P-12, que fueron la misma tres
    # veces (el universo del *setup* mas estrecho que el de lo que la suite afirma).
    """
    resembrar(motor_de_siembra)


@pytest.fixture(scope="module", autouse=True)
def _escenario_devuelto_al_salir(motor_de_siembra):
    """Y al salir del modulo tambien: lo que escribe, lo devuelve."""
    yield
    resembrar(motor_de_siembra)


@pytest.fixture
def tablas_de_nivel_agencia(motor_admin, catalogo_de_tablas) -> dict[str, dict]:
    """Las de la clase *de agencia*, con su politica. Derivadas, no enumeradas."""
    with motor_admin.connect() as conexion:
        politicas = _politicas(conexion)
    return {
        tabla: {"columnas": columnas, "politicas": politicas.get(tabla, [])}
        for tabla, columnas in catalogo_de_tablas.items()
        if clase_de(columnas) == CLASE_AGENCIA
    }


def _contar(tabla: str) -> TextClause:
    valida_identificador(tabla)
    # S608: `tabla` sale del catalogo de Postgres y pasa por `valida_identificador()`.
    # Aqui no llega entrada de usuario.
    return text(f"SELECT count(*) FROM {tabla}")  # noqa: S608


def _contar_propias(tabla: str, columna: str) -> TextClause:
    valida_identificador(tabla)
    valida_identificador(columna)
    return text(  # noqa: S608
        f"SELECT count(*) FROM {tabla} "  # noqa: S608
        f"WHERE {COLUMNA_AGENCIA} = :agencia AND {columna} = :cliente"
    )


def _contar_de_mi_agencia(tabla: str) -> TextClause:
    valida_identificador(tabla)
    return text(f"SELECT count(*) FROM {tabla} WHERE {COLUMNA_AGENCIA} = :agencia")  # noqa: S608


def _contar_ajenas(tabla: str) -> TextClause:
    """Filas que no pertenecen a ninguna de las dos agencias sembradas."""
    valida_identificador(tabla)
    return text(  # noqa: S608
        f"SELECT count(*) FROM {tabla} WHERE {COLUMNA_AGENCIA} NOT IN (:a, :b)"  # noqa: S608
    )


# --------------------------------------------------------------------------
# El par (alcance, cliente_id) no se puede pedir incoherente
# --------------------------------------------------------------------------
def test_el_alcance_se_deriva_de_la_fila_del_usuario() -> None:
    """No hay parametro `alcance`: se DERIVA (plan §3.1 punto 5)."""
    operador = Inquilino.desde_usuario(agencia_id=AGENCIA_A, cliente_id=None)
    assert operador.alcance is Alcance.AGENCIA
    assert operador.cliente_id == CENTINELA_SIN_CLIENTE

    portal = Inquilino.desde_usuario(agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1)
    assert portal.alcance is Alcance.CLIENTE


def test_el_centinela_es_el_uuid_nulo_y_no_una_cadena_vacia() -> None:
    """L-19: una cadena vacia no convierte a uuid y fallaria por otra razon."""
    assert str(CENTINELA_SIN_CLIENTE) == "00000000-0000-0000-0000-000000000000"
    with pytest.raises(AlcanceInvalido):
        Inquilino(agencia_id=AGENCIA_A, cliente_id=CLIENTE_A1, alcance=Alcance.AGENCIA)
    with pytest.raises(AlcanceInvalido):
        Inquilino(
            agencia_id=AGENCIA_A, cliente_id=CENTINELA_SIN_CLIENTE, alcance=Alcance.CLIENTE
        )


# --------------------------------------------------------------------------
# Lectura (RF-01 / RF-02)
# --------------------------------------------------------------------------
async def test_una_sesion_de_cliente_solo_ve_sus_heraldos(motor) -> None:
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        filas = (await conexion.execute(text("SELECT id, cliente_id FROM heraldos"))).all()
    assert [f.cliente_id for f in filas] == [CLIENTE_A1], (
        "el portal del cliente A1 alcanza heraldos que no son suyos: la fuga "
        "cliente-a-cliente DENTRO de la misma agencia es el eje que C-01 abrio"
    )


async def test_una_sesion_de_cliente_no_ve_a_otra_agencia(motor) -> None:
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_B, CLIENTE_B1)) as conexion:
        cuantos = (await conexion.execute(CONTAR_HERALDOS)).scalar_one()
    assert cuantos == 1


async def test_una_sesion_de_agencia_ve_a_todos_sus_clientes(motor) -> None:
    """Si esto sale 0, el panel del operador esta roto y lo habriamos dado por bueno.

    K-04: RF-01 no es un absoluto. Alcance agencia -> los clientes DE ESA agencia.
    """
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        clientes = (await conexion.execute(CONTAR_CLIENTES)).scalar_one()
        heraldos = (await conexion.execute(CONTAR_HERALDOS)).scalar_one()
    assert clientes == 2, "el operador de la agencia A no ve sus dos clientes"
    assert heraldos == 2, "el operador de la agencia A no ve los heraldos de sus clientes"


async def test_una_sesion_de_agencia_no_ve_a_otra_agencia(motor) -> None:
    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        ajenos = (
            await conexion.execute(
                text("SELECT count(*) FROM clientes WHERE agencia_id = :b"), {"b": AGENCIA_B}
            )
        ).scalar_one()
    assert ajenos == 0


async def test_el_portal_de_cliente_no_ve_la_tabla_de_agencias(motor) -> None:
    """L-02, por efecto: la clase *de agencia* SIN columna propia es invisible.

    Es tambien lo que sostiene la marca blanca (RF-59): el portal no puede ni
    nombrar a la agencia que hay detras.
    """
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        agencias = (await conexion.execute(CONTAR_AGENCIAS)).scalar_one()
    assert agencias == 0


async def test_el_portal_de_cliente_solo_ve_su_fila_en_la_tabla_de_clientes(motor) -> None:
    """La clase *de agencia* CON columna propia expone una fila: la suya."""
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        filas = (await conexion.execute(text("SELECT id FROM clientes"))).all()
    assert [f.id for f in filas] == [CLIENTE_A1], (
        "el portal de A1 ve filas de la tabla de clientes que no son la suya "
        "(o no ve la suya): es exactamente el agujero de L-02"
    )


# --------------------------------------------------------------------------
# Escritura (RF-02-bis) — la mitad que todo el aparato anterior no medía
# --------------------------------------------------------------------------
async def test_insertar_con_el_identificador_de_otra_agencia_es_rechazado(motor) -> None:
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(
            motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
        ) as conexion:
            await conexion.execute(
                text(
                    "INSERT INTO heraldos (agencia_id, cliente_id, nombre) "
                    "VALUES (:a, :c, 'intruso')"
                ),
                {"a": AGENCIA_B, "c": CLIENTE_B1},
            )
    assert "row-level security" in str(capturado.value).lower()


async def test_insertar_para_otro_cliente_de_mi_agencia_es_rechazado(motor) -> None:
    """El segundo eje: no basta con aislar agencias (C-01)."""
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(
            motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
        ) as conexion:
            await conexion.execute(
                text(
                    "INSERT INTO heraldos (agencia_id, cliente_id, nombre) "
                    "VALUES (:a, :c, 'intruso')"
                ),
                {"a": AGENCIA_A, "c": CLIENTE_A2},
            )
    assert "row-level security" in str(capturado.value).lower()


async def test_reasignar_una_fila_a_otro_inquilino_es_rechazado(motor) -> None:
    """La fila es mia; moverla de dueño tambien es escritura cruzada."""
    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(
            motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
        ) as conexion:
            await conexion.execute(
                text("UPDATE heraldos SET cliente_id = :otro WHERE id = :mio"),
                {"otro": CLIENTE_A2, "mio": HERALDO_A1},
            )
    assert "row-level security" in str(capturado.value).lower()


async def test_insertar_lo_mio_si_esta_permitido(motor) -> None:
    """Control de las sondas anteriores: si todo fuera rechazado, no hay producto."""
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        insertado = (
            await conexion.execute(
                text(
                    "INSERT INTO heraldos (agencia_id, cliente_id, nombre) "
                    "VALUES (:a, :c, 'propio') RETURNING id"
                ),
                {"a": AGENCIA_A, "c": CLIENTE_A1},
            )
        ).scalar_one()
        assert insertado is not None
        # No se deja el escenario sucio para las sondas que vengan detras.
        await conexion.execute(text("DELETE FROM heraldos WHERE id = :id"), {"id": insertado})


# --------------------------------------------------------------------------
# Fail-closed (RF-03) y el pool
# --------------------------------------------------------------------------
async def test_sin_inquilino_declarado_la_consulta_aborta(motor) -> None:
    """RF-03: rechazar la operacion, NO devolver cero filas.

    Se abre la transaccion saltandose la dependencia a proposito — es el camino
    que un modulo despistado tomaria. Cero filas silenciosas se confunden con
    «no hay datos»; un error, no.
    """
    with pytest.raises(DBAPIError) as capturado:
        async with motor.begin() as conexion:
            await conexion.execute(CONTAR_HERALDOS)
    mensaje = str(capturado.value).lower()
    motivos = ("unrecognized configuration parameter", "invalid input syntax for type uuid")
    assert any(motivo in mensaje for motivo in motivos), (
        "la consulta sin inquilino declarado fallo por una razon inesperada, y la "
        f"razon importa: {mensaje}"
    )


async def test_el_inquilino_no_sobrevive_a_la_transaccion(motor_de_una_sola_conexion) -> None:
    """El peor caso del pool: la MISMA conexion fisica atiende a dos inquilinos.

    Es el `LOCAL` de `SET LOCAL` lo que se esta midiendo aqui. Con un `SET` de
    sesion, la segunda peticion heredaria el inquilino de la primera.
    """
    motor = motor_de_una_sola_conexion
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        assert (await conexion.execute(CONTAR_HERALDOS)).scalar_one() == 1

    # Misma conexion fisica, inquilino distinto: no puede ver lo del anterior.
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_B, CLIENTE_B1)) as conexion:
        filas = (await conexion.execute(text("SELECT cliente_id FROM heraldos"))).all()
    assert [f.cliente_id for f in filas] == [CLIENTE_B1]

    # Y sin declarar nada, la misma conexion vuelve a abortar.
    with pytest.raises(DBAPIError):
        async with motor.begin() as conexion:
            await conexion.execute(CONTAR_HERALDOS)


# ==========================================================================
# T-013 — LA BATERIA. Redactada POR PARES DE ALCANCE, y con la rejilla
# declarada para que «completa» sea una comprobacion y no una opinion.
# ==========================================================================
#
# # WHY: un recuento suelto («hay 20 sondas») no dice nada sobre lo que quedo
# fuera. Aqui el universo se declara: TRES ejes ortogonales cuyo producto es la
# rejilla, y un meta-test que exige que CADA celda este o bien medida o bien
# declarada NO APLICABLE con un motivo escrito. Anadir una tabla, un alcance o
# una direccion sin sus sondas pone el CI en ROJO: olvidarlo no compila.
#
# # WHY (los dos EJES de la cascada, C-01): la relacion `vecino` es el eje
# cliente<->cliente DENTRO de la misma agencia y la relacion `ajeno` es el eje
# agencia<->agencia. Una bateria que solo tuviera `ajeno` saldria verde con la
# fuga entre clientes viva dentro — que es exactamente lo que le medimos al
# referente.
#
# # WHY (las dos DIRECCIONES, RF-02-bis): `lectura` no es la mitad del problema,
# es un tercio. `insercion` crea una fila ajena, `reasignacion` empuja una fila
# propia hacia otro inquilino y `usurpacion` tira de una fila ajena hacia el
# propio. Todo el aparato original tenia forma de lectura.
#
# # WHY (el par PERMITIDO, K-04): las celdas cuyo veredicto es `PERMITIDO` no son
# relleno — son el control. Un aislamiento que no deja trabajar a nadie no es un
# logro: si `sesion-de-agencia -> sus propios clientes` saliera 0, el panel del
# operador estaria roto y esta bateria lo habria firmado.

#: Identidades que NO estan sembradas. Fijas, no `uuid4()`: cuando una sonda
#: falla, el mensaje dice el mismo uuid en todas las maquinas.
AGENCIA_FANTASMA = UUID("cccccccc-0000-4000-8000-00000000000c")
CLIENTE_NUEVO = UUID("aaaaaaaa-0000-4000-8000-0000000000a9")


class Veredicto(StrEnum):
    """Lo que la base hace, no lo que quisieramos que hiciera.

    Son CUATRO y no dos a proposito. `RECHAZADO` e `INVISIBLE` son resultados
    distintos y confundirlos es el defecto que RF-03 existe para evitar: una
    escritura que ABORTA no es lo mismo que una que no encuentra la fila.

    # WHY (`SIN_PRIVILEGIO`, anadido con la revision 0003): a partir de RF-10 hay
    # tablas sobre las que la aplicacion NO tiene `UPDATE` ni `DELETE`. Ahi la
    # escritura ni siquiera llega a la politica de fila: el motor la corta antes,
    # por PRIVILEGIO. Los dos fallos comparten el codigo `42501` de Postgres y
    # significan cosas distintas — «la politica no te deja tocar ESA fila» frente a
    # «no puedes tocar NINGUNA fila de esta tabla»— asi que se distinguen por el
    # mensaje y se declaran aparte. Fundirlos dejaria pasar el peor caso: una tabla
    # de solo insercion cuyo `UPDATE` fallara por RLS —y no por permiso— seguiria
    # siendo reescribible para el operador de la agencia, y la bateria lo firmaria.
    """

    PERMITIDO = "permitido"
    RECHAZADO = "rechazado por row-level security"
    INVISIBLE = "sin efecto: la fila no es alcanzable"
    SIN_PRIVILEGIO = "rechazado antes de la politica: el rol no tiene ese verbo"


#: Los alcances de la sesion que consulta. No hay un tercero.
ALCANCES = ("agencia", "cliente")

#: La relacion entre la sesion y el inquilino DUENO del dato.
#:   mio    -> el propio inquilino de la sesion
#:   vecino -> otro cliente de LA MISMA agencia   (eje cliente<->cliente)
#:   ajeno  -> otra agencia y su cliente          (eje agencia<->agencia)
RELACIONES = ("mio", "vecino", "ajeno")

#: Que se intenta hacer.
#:   lectura       -> ver la fila de `relacion`
#:   insercion     -> crear una fila que pertenezca a `relacion`
#:   actualizacion -> tocar un campo NO identitario de la fila de `relacion`
#:   reasignacion  -> empujar MI fila hacia `relacion`
#:   usurpacion    -> tirar de la fila de `relacion` hacia mi
DIRECCIONES = ("lectura", "insercion", "actualizacion", "reasignacion", "usurpacion")

#: (agencia, cliente) duenos de cada relacion, desde el punto de vista de A / A1.
DUENOS: dict[str, tuple[UUID, UUID]] = {
    "mio": (AGENCIA_A, CLIENTE_A1),
    "vecino": (AGENCIA_A, CLIENTE_A2),
    "ajeno": (AGENCIA_B, CLIENTE_B1),
}
CLIENTE_DE: dict[str, UUID] = {"mio": CLIENTE_A1, "vecino": CLIENTE_A2, "ajeno": CLIENTE_B1}
AGENCIA_DE: dict[str, UUID] = {"mio": AGENCIA_A, "vecino": AGENCIA_A, "ajeno": AGENCIA_B}


@dataclass(frozen=True, slots=True)
class Caso:
    """Una celda de la rejilla, con su veredicto DECLARADO y su motivo."""

    alcance: str
    relacion: str
    direccion: str
    veredicto: Veredicto
    porque: str

    @property
    def celda(self) -> tuple[str, str, str]:
        return (self.alcance, self.relacion, self.direccion)


def _c(alcance: str, relacion: str, direccion: str, veredicto: Veredicto, porque: str) -> Caso:
    return Caso(alcance, relacion, direccion, veredicto, porque)


PERMITIDO = Veredicto.PERMITIDO
RECHAZADO = Veredicto.RECHAZADO
INVISIBLE = Veredicto.INVISIBLE
SIN_PRIVILEGIO = Veredicto.SIN_PRIVILEGIO


# --------------------------------------------------------------------------
# La matriz CANONICA de la clase DE CLIENTE — una redaccion, no una por tabla
# --------------------------------------------------------------------------
#
# # WHY (derivada y no copiada, C-06 llevado hasta el final): la revision 0003
# anadio CINCO tablas de la clase *de cliente*. Escribir a mano las 26 celdas de
# cada una habria producido seis redacciones de la MISMA regla —la de `heraldos`
# incluida— y seis redacciones divergen: la sexta se escribe con prisa, se le
# cuela un `INVISIBLE` donde iba un `RECHAZADO`, y como el veredicto declarado es
# el que la sonda compara, la tabla quedaria «medida» contra una expectativa
# equivocada. Aqui la matriz de una tabla de esta clase se GENERA, y
# `test_toda_tabla_de_clase_cliente_usa_la_matriz_canonica` exige que TODA tabla
# de la clase que exista en el esquema use exactamente esta. Una tabla nueva con
# una matriz mas floja no compila.
#
# # WHY (`puede_actualizar`): la unica diferencia legitima entre dos tablas de
# esta clase es que verbos tiene concedidos el rol de aplicacion sobre ellas. Sin
# `UPDATE`, las tres direcciones de escritura no-insercion no fallan por la
# politica: fallan ANTES, por privilegio. Eso es RF-10 visto desde la bateria.


def casos_de_clase_cliente(*, puede_actualizar: bool) -> tuple[Caso, ...]:
    """Las 26 celdas de una tabla con `agencia_id` Y `cliente_id`."""

    def tocar(veredicto: Veredicto, porque: str) -> tuple[Veredicto, str]:
        """El veredicto de una direccion de escritura que exige `UPDATE`."""
        if puede_actualizar:
            return veredicto, porque
        return SIN_PRIVILEGIO, (
            "esta tabla no concede UPDATE al rol de aplicacion (RF-10): el motor "
            "corta la escritura por PRIVILEGIO antes de llegar a la politica de fila"
        )

    return (
        # --- alcance cliente (portal de A1) ---
        _c("cliente", "mio", "lectura", PERMITIDO, "su fila: si sale 0 el portal esta roto"),
        _c("cliente", "vecino", "lectura", INVISIBLE, "EJE CLIENTE<->CLIENTE, misma agencia"),
        _c("cliente", "ajeno", "lectura", INVISIBLE, "EJE AGENCIA<->AGENCIA"),
        _c("cliente", "mio", "insercion", PERMITIDO, "control: crear lo propio debe funcionar"),
        _c("cliente", "vecino", "insercion", RECHAZADO,
           "crear una fila a nombre de otro cliente mio"),
        _c("cliente", "ajeno", "insercion", RECHAZADO,
           "crear una fila a nombre de otra agencia"),
        _c("cliente", "mio", "actualizacion",
           *tocar(PERMITIDO, "control: editar lo propio debe funcionar")),
        _c("cliente", "vecino", "actualizacion",
           *tocar(INVISIBLE, "editar la fila del vecino no alcanza nada")),
        _c("cliente", "ajeno", "actualizacion",
           *tocar(INVISIBLE, "editar la fila ajena no alcanza nada")),
        _c("cliente", "vecino", "reasignacion",
           *tocar(RECHAZADO, "regalarle MI fila al vecino (RF-02-bis)")),
        _c("cliente", "ajeno", "reasignacion",
           *tocar(RECHAZADO, "sacar MI fila a otra agencia (RF-02-bis)")),
        _c("cliente", "vecino", "usurpacion",
           *tocar(INVISIBLE, "robarle la fila al vecino: ni la ve")),
        _c("cliente", "ajeno", "usurpacion",
           *tocar(INVISIBLE, "robarle la fila a otra agencia: ni la ve")),
        # --- alcance agencia (operador de A) ---
        _c("agencia", "mio", "lectura", PERMITIDO, "el operador ve la fila de su cliente A1"),
        _c("agencia", "vecino", "lectura", PERMITIDO,
           "K-04: y TAMBIEN la de A2, o el panel esta roto"),
        _c("agencia", "ajeno", "lectura", INVISIBLE, "pero jamas la de otra agencia"),
        _c("agencia", "mio", "insercion", PERMITIDO, "el operador da de alta para su cliente A1"),
        _c("agencia", "vecino", "insercion", PERMITIDO, "y para A2: los dos son suyos"),
        _c("agencia", "ajeno", "insercion", RECHAZADO,
           "pero no para un cliente de otra agencia"),
        _c("agencia", "mio", "actualizacion",
           *tocar(PERMITIDO, "el operador edita lo de su cliente A1")),
        _c("agencia", "vecino", "actualizacion", *tocar(PERMITIDO, "y lo de A2")),
        _c("agencia", "ajeno", "actualizacion",
           *tocar(INVISIBLE, "y nada de la otra agencia")),
        _c("agencia", "vecino", "reasignacion",
           *tocar(PERMITIDO, "mover una fila ENTRE SUS clientes es su trabajo")),
        _c("agencia", "ajeno", "reasignacion", *tocar(RECHAZADO, "sacarla de la agencia, no")),
        _c("agencia", "vecino", "usurpacion",
           *tocar(PERMITIDO, "traerla de A2 a A1: la misma operacion, al reves")),
        _c("agencia", "ajeno", "usurpacion",
           *tocar(INVISIBLE, "traerse la de otra agencia: ni la ve")),
    )


#: Las celdas de la rejilla que NO son expresables sobre una tabla de la clase
#: *de cliente*, con su motivo ESCRITO. Es la misma para todas ellas: la razon no
#: depende de la tabla, depende de la forma de la operacion.
NO_APLICA_DE_CLASE_CLIENTE: dict[tuple[str, str], str] = {
    ("mio", "reasignacion"): (
        "empujar mi fila hacia mi mismo no cambia de dueno a nadie: no es una "
        "reasignacion, es una operacion vacia que no mide ningun aislamiento"
    ),
    ("mio", "usurpacion"): (
        "tirar de mi propia fila hacia mi mismo no cambia de dueno a nadie: no "
        "es una usurpacion, y su equivalente util ya se mide en `actualizacion`"
    ),
}

CASOS_HERALDOS: tuple[Caso, ...] = casos_de_clase_cliente(puede_actualizar=True)


# --------------------------------------------------------------------------
# Matriz de `clientes` — clase DE AGENCIA **con** columna propia (`id`)
# --------------------------------------------------------------------------
CASOS_CLIENTES: tuple[Caso, ...] = (
    # --- alcance cliente (portal de A1) ---
    _c("cliente", "mio", "lectura", PERMITIDO, "el portal ve SU ficha, y solo la suya"),
    _c("cliente", "vecino", "lectura", INVISIBLE, "RF-01-bis / L-02: el agujero que abrio K-03"),
    _c("cliente", "ajeno", "lectura", INVISIBLE, "ni de lejos la de otra agencia"),
    _c("cliente", "mio", "insercion", RECHAZADO,
       "un portal no da de alta clientes, ni en su agencia"),
    _c("cliente", "ajeno", "insercion", RECHAZADO, "y mucho menos en otra"),
    _c("cliente", "mio", "actualizacion", PERMITIDO, "control: el portal edita SU ficha"),
    _c("cliente", "vecino", "actualizacion", INVISIBLE, "y no la del vecino"),
    _c("cliente", "ajeno", "actualizacion", INVISIBLE, "y no la de otra agencia"),
    _c("cliente", "ajeno", "reasignacion", RECHAZADO, "llevarse SU ficha a otra agencia"),
    _c("cliente", "ajeno", "usurpacion", INVISIBLE, "traerse la ficha de un cliente ajeno"),
    # --- alcance agencia (operador de A) ---
    _c("agencia", "mio", "lectura", PERMITIDO, "el operador ve a A1"),
    _c("agencia", "vecino", "lectura", PERMITIDO, "K-04: y a A2. Es la cartera de su agencia"),
    _c("agencia", "ajeno", "lectura", INVISIBLE, "y a ningun cliente de otra agencia"),
    _c("agencia", "mio", "insercion", PERMITIDO,
       "dar de alta un cliente en SU agencia es su trabajo"),
    _c("agencia", "ajeno", "insercion", RECHAZADO, "darlo de alta en otra agencia, no"),
    _c("agencia", "mio", "actualizacion", PERMITIDO, "control: edita la ficha de su cliente"),
    _c("agencia", "vecino", "actualizacion", PERMITIDO, "y la del otro cliente suyo"),
    _c("agencia", "ajeno", "actualizacion", INVISIBLE, "y ninguna de otra agencia"),
    _c("agencia", "ajeno", "reasignacion", RECHAZADO, "regalarle un cliente suyo a otra agencia"),
    _c("agencia", "ajeno", "usurpacion", INVISIBLE, "quedarse con el cliente de otra agencia"),
)

# --------------------------------------------------------------------------
# Matriz de `agencias` — clase DE AGENCIA **sin** columna propia
# --------------------------------------------------------------------------
CASOS_AGENCIAS: tuple[Caso, ...] = (
    # --- alcance cliente (portal de A1) ---
    _c("cliente", "mio", "lectura", INVISIBLE,
       "MARCA BLANCA (RF-59): no puede ni nombrar a su agencia"),
    _c("cliente", "ajeno", "lectura", INVISIBLE, "y menos a otra"),
    _c("cliente", "ajeno", "insercion", RECHAZADO, "un portal no crea agencias"),
    _c("cliente", "mio", "actualizacion", INVISIBLE, "ni toca la fila de su propia agencia"),
    _c("cliente", "ajeno", "actualizacion", INVISIBLE, "ni la de ninguna otra"),
    _c("cliente", "ajeno", "reasignacion", INVISIBLE, "la fila de su agencia no le es alcanzable"),
    _c("cliente", "ajeno", "usurpacion", INVISIBLE, "la fila de otra agencia tampoco"),
    # --- alcance agencia (operador de A) ---
    _c("agencia", "mio", "lectura", PERMITIDO, "el operador SI ve la ficha de su agencia"),
    _c("agencia", "ajeno", "lectura", INVISIBLE, "pero no la de otra"),
    _c("agencia", "ajeno", "insercion", RECHAZADO, "crear una agencia es acto de la PLATAFORMA"),
    _c("agencia", "mio", "actualizacion", PERMITIDO, "control: edita el nombre de SU agencia"),
    _c("agencia", "ajeno", "actualizacion", INVISIBLE, "y no el de otra"),
    _c("agencia", "ajeno", "reasignacion", RECHAZADO,
       "cambiarle el identificador a su propia agencia"),
    _c("agencia", "ajeno", "usurpacion", INVISIBLE, "tocarle el identificador a otra agencia"),
)

# --------------------------------------------------------------------------
# El registro de recursos de la clase DE CLIENTE
# --------------------------------------------------------------------------
#
# # WHY: cada tabla de esta clase aporta lo UNICO que la distingue —sus cuatro
# sentencias, la fila sembrada de cada relacion y si tiene `UPDATE` concedido—.
# La matriz sale de `casos_de_clase_cliente`, que es una sola. Anadir una tabla de
# inquilino en una migracion futura y no anadirla aqui pone el CI en rojo por dos
# sitios distintos: `test_ninguna_tabla_del_catalogo_queda_fuera_de_la_bateria` y
# `test_toda_tabla_de_clase_cliente_usa_la_matriz_canonica`.


@dataclass(frozen=True, slots=True)
class RecursoDeCliente:
    """Lo que hay que saber de una tabla de la clase *de cliente* para sondearla."""

    tabla: str
    #: La fila sembrada de cada relacion: `mio`, `vecino`, `ajeno`.
    fila_de: dict[str, UUID]
    #: ¿Tiene el rol de aplicacion el verbo `UPDATE` sobre ella?
    puede_actualizar: bool
    #: Un identificador que no existe, para las inserciones de sonda.
    fila_nueva: UUID


RECURSOS_DE_CLIENTE: tuple[RecursoDeCliente, ...] = (
    RecursoDeCliente(
        "heraldos",
        {"mio": HERALDO_A1, "vecino": HERALDO_A2, "ajeno": HERALDO_B1},
        puede_actualizar=True,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-00000000f0f0"),
    ),
    RecursoDeCliente(
        "secretos",
        {"mio": SECRETO_A1, "vecino": SECRETO_A2, "ajeno": SECRETO_B1},
        puede_actualizar=True,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-00000000c0c0"),
    ),
    RecursoDeCliente(
        "bitacora",
        {"mio": APUNTE_A1, "vecino": APUNTE_A2, "ajeno": APUNTE_B1},
        # RF-10: solo insercion. Lo dice el GRANT de la revision 0003, no un comentario.
        puede_actualizar=False,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-00000000b0b0"),
    ),
    RecursoDeCliente(
        "trabajos",
        {"mio": TRABAJO_A1, "vecino": TRABAJO_A2, "ajeno": TRABAJO_B1},
        puede_actualizar=True,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-00000000d0d0"),
    ),
    RecursoDeCliente(
        "trabajos_archivados",
        {"mio": ARCHIVADO_A1, "vecino": ARCHIVADO_A2, "ajeno": ARCHIVADO_B1},
        # Un archivo que se puede reescribir no es un archivo: sin `UPDATE`.
        puede_actualizar=False,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-00000000e0e0"),
    ),
    RecursoDeCliente(
        "mensajes_entrantes",
        {"mio": MENSAJE_A1, "vecino": MENSAJE_A2, "ajeno": MENSAJE_B1},
        # RF-12: reescribir esta tabla es reescribir que mensajes llegaron.
        puede_actualizar=False,
        fila_nueva=UUID("aaaaaaaa-0000-4000-8000-000000009090"),
    ),
)

POR_TABLA: dict[str, RecursoDeCliente] = {r.tabla: r for r in RECURSOS_DE_CLIENTE}

MATRICES: dict[str, tuple[Caso, ...]] = {
    **{
        recurso.tabla: casos_de_clase_cliente(puede_actualizar=recurso.puede_actualizar)
        for recurso in RECURSOS_DE_CLIENTE
    },
    "clientes": CASOS_CLIENTES,
    "agencias": CASOS_AGENCIAS,
}

#: Celdas de la rejilla que NO son expresables sobre esa tabla, con su motivo
#: ESCRITO. La clave es `(relacion, direccion)` y vale para los dos alcances.
#: Redactado por ALLOWLIST: una celda sin sonda y sin motivo pone el CI en rojo.
NO_APLICA: dict[str, dict[tuple[str, str], str]] = {
    # Las de la clase *de cliente* comparten motivo: no depende de la tabla, sino
    # de la forma de la operacion (empujar una fila hacia uno mismo no la mueve).
    **{recurso.tabla: NO_APLICA_DE_CLASE_CLIENTE for recurso in RECURSOS_DE_CLIENTE},
    "clientes": {
        ("vecino", "insercion"): (
            "una fila nueva de esta tabla pertenece a una AGENCIA, no a un cliente: "
            "`vecino` y `mio` producirian exactamente la misma fila, en la misma "
            "agencia. El caso ya lo mide `mio` y duplicarlo solo inflaria el recuento"
        ),
        ("mio", "reasignacion"): (
            "reasignar es cambiar de AGENCIA la fila; hacia `mio` la deja donde ya "
            "estaba. Es una operacion vacia y no mide ningun aislamiento"
        ),
        ("vecino", "reasignacion"): (
            "el vecino vive en MI MISMA agencia, asi que mover la fila hacia el la "
            "deja en la misma agencia: no cruza ninguna frontera y no mide nada"
        ),
        ("mio", "usurpacion"): (
            "tirar de mi propia fila hacia mi propia agencia no cambia de dueno a "
            "nadie: es una operacion vacia"
        ),
        ("vecino", "usurpacion"): (
            "la fila del vecino ya esta en MI MISMA agencia, que es la unica "
            "dimension de dueno que tiene esta tabla: no hay nada que usurpar"
        ),
    },
    "agencias": {
        ("vecino", "lectura"): (
            "esta tabla no tiene dimension de cliente: no existe una fila que "
            "pertenezca a `otro cliente de mi agencia`. La relacion no es expresable"
        ),
        ("vecino", "insercion"): (
            "esta tabla no tiene dimension de cliente: una fila nueva pertenece a "
            "una agencia, nunca a un cliente. La relacion no es expresable"
        ),
        ("vecino", "actualizacion"): (
            "esta tabla no tiene dimension de cliente: no existe una fila que "
            "pertenezca a `otro cliente de mi agencia`. La relacion no es expresable"
        ),
        ("vecino", "reasignacion"): (
            "esta tabla no tiene dimension de cliente: no hay a donde empujar la "
            "fila dentro de la misma agencia. La relacion no es expresable"
        ),
        ("vecino", "usurpacion"): (
            "esta tabla no tiene dimension de cliente: no existe una fila de un "
            "vecino que usurpar. La relacion no es expresable"
        ),
        ("mio", "insercion"): (
            "la unica fila que la politica dejaria insertar es la de MI PROPIA "
            "agencia, y esa ya existe: el error seria una violacion de clave "
            "primaria y no una medida de aislamiento. El gobierno de escritura de "
            "esta tabla se mide con `ajeno`, que si llega a la clausula WITH CHECK"
        ),
        ("mio", "reasignacion"): (
            "empujar la fila de mi agencia hacia mi propia agencia la deja donde "
            "estaba: operacion vacia, no mide ningun aislamiento"
        ),
        ("mio", "usurpacion"): (
            "tirar de la fila de mi agencia hacia mi propia agencia la deja donde "
            "estaba: operacion vacia, no mide ningun aislamiento"
        ),
    },
}

#: Tablas del catalogo que NO tienen matriz propia, con su motivo ESCRITO.
SIN_MATRIZ_PROPIA: dict[str, str] = {
    "alembic_version": (
        "Catalogo de migraciones de Alembic: no contiene ningun dato de inquilino y "
        "el rol de aplicacion no tiene NINGUN privilegio sobre ella, cosa que "
        "comprueba `test_el_rol_de_aplicacion_no_alcanza_las_tablas_exentas` en "
        "`test_rls_cobertura.py`. Una sonda de acceso cruzado contra una tabla que "
        "la aplicacion no puede ni abrir mediria el privilegio, no el aislamiento"
    ),
}


# --------------------------------------------------------------------------
# Las sentencias. Literales, una por (tabla, operacion): sin f-strings, para
# que ninguna sonda pueda convertirse en una superficie de interpolacion.
# --------------------------------------------------------------------------
_SQL = {
    ("heraldos", "lectura"): text("SELECT count(*) FROM heraldos WHERE id = :objetivo"),
    ("heraldos", "insercion"): text(
        "INSERT INTO heraldos (agencia_id, cliente_id, nombre) VALUES (:a, :c, 'sonda')"
    ),
    ("heraldos", "actualizacion"): text(
        "UPDATE heraldos SET nombre = 'sonda' WHERE id = :objetivo"
    ),
    ("heraldos", "movimiento"): text(
        "UPDATE heraldos SET agencia_id = :a, cliente_id = :c WHERE id = :objetivo"
    ),
    ("clientes", "lectura"): text("SELECT count(*) FROM clientes WHERE id = :objetivo"),
    ("clientes", "insercion"): text(
        "INSERT INTO clientes (id, agencia_id, nombre) VALUES (:nuevo, :a, 'sonda')"
    ),
    ("clientes", "actualizacion"): text(
        "UPDATE clientes SET nombre = 'sonda' WHERE id = :objetivo"
    ),
    ("clientes", "movimiento"): text("UPDATE clientes SET agencia_id = :a WHERE id = :objetivo"),
    ("agencias", "lectura"): text("SELECT count(*) FROM agencias WHERE agencia_id = :objetivo"),
    ("agencias", "insercion"): text(
        "INSERT INTO agencias (agencia_id, nombre) VALUES (:nuevo, 'sonda')"
    ),
    ("agencias", "actualizacion"): text(
        "UPDATE agencias SET nombre = 'sonda' WHERE agencia_id = :objetivo"
    ),
    ("agencias", "movimiento"): text(
        "UPDATE agencias SET agencia_id = :nuevo WHERE agencia_id = :objetivo"
    ),
    # --- las cinco tablas de la revision 0003 ---
    # WHY: el identificador de la fila nueva viaja SIEMPRE como `:nuevo`, tambien
    # donde la columna tiene valor por defecto. Un juego de parametros uniforme es
    # lo que permite que `_sentencia_de_cliente` sea UNA funcion y no seis.
    ("secretos", "lectura"): text("SELECT count(*) FROM secretos WHERE id = :objetivo"),
    ("secretos", "insercion"): text(
        "INSERT INTO secretos (id, agencia_id, cliente_id, nombre, cifrado) "
        "VALUES (:nuevo, :a, :c, 'sonda', decode('00', 'hex'))"
    ),
    ("secretos", "actualizacion"): text(
        "UPDATE secretos SET nombre = 'sonda' WHERE id = :objetivo"
    ),
    # WHY (el `nombre` cambia tambien, y solo aqui): `secretos` lleva una clave
    # unica `(agencia_id, cliente_id, nombre)`. Empujar la fila de A1 hacia A2 sin
    # tocar el nombre choca con el secreto que A2 ya tiene con ESE nombre, y la
    # sonda mediria la unicidad en vez del aislamiento — el `INSERT`/`UPDATE`
    # fallaria por una razon que no tiene nada que ver con la politica. Cambiando
    # el nombre, la unica cosa que puede parar el movimiento es la politica de RLS,
    # que es lo que esta celda existe para medir.
    ("secretos", "movimiento"): text(
        "UPDATE secretos SET agencia_id = :a, cliente_id = :c, nombre = 'sonda-movida' "
        "WHERE id = :objetivo"
    ),
    ("bitacora", "lectura"): text("SELECT count(*) FROM bitacora WHERE id = :objetivo"),
    ("bitacora", "insercion"): text(
        "INSERT INTO bitacora (id, agencia_id, cliente_id, actor, accion, recurso) "
        "VALUES (:nuevo, :a, :c, 'sonda', 'sonda', 'sonda')"
    ),
    ("bitacora", "actualizacion"): text(
        "UPDATE bitacora SET accion = 'sonda' WHERE id = :objetivo"
    ),
    ("bitacora", "movimiento"): text(
        "UPDATE bitacora SET agencia_id = :a, cliente_id = :c WHERE id = :objetivo"
    ),
    ("trabajos", "lectura"): text("SELECT count(*) FROM trabajos WHERE id = :objetivo"),
    ("trabajos", "insercion"): text(
        "INSERT INTO trabajos (id, agencia_id, cliente_id, tipo) VALUES (:nuevo, :a, :c, 'sonda')"
    ),
    ("trabajos", "actualizacion"): text("UPDATE trabajos SET tipo = 'sonda' WHERE id = :objetivo"),
    ("trabajos", "movimiento"): text(
        "UPDATE trabajos SET agencia_id = :a, cliente_id = :c WHERE id = :objetivo"
    ),
    ("trabajos_archivados", "lectura"): text(
        "SELECT count(*) FROM trabajos_archivados WHERE id = :objetivo"
    ),
    ("trabajos_archivados", "insercion"): text(
        "INSERT INTO trabajos_archivados (id, agencia_id, cliente_id, tipo, carga, estado, "
        "       intentos, maximo_intentos, creado_en, terminado_en) "
        "VALUES (:nuevo, :a, :c, 'sonda', '{}'::jsonb, 'hecho', 1, 5, now(), now())"
    ),
    ("trabajos_archivados", "actualizacion"): text(
        "UPDATE trabajos_archivados SET tipo = 'sonda' WHERE id = :objetivo"
    ),
    ("trabajos_archivados", "movimiento"): text(
        "UPDATE trabajos_archivados SET agencia_id = :a, cliente_id = :c WHERE id = :objetivo"
    ),
    ("mensajes_entrantes", "lectura"): text(
        "SELECT count(*) FROM mensajes_entrantes WHERE id = :objetivo"
    ),
    ("mensajes_entrantes", "insercion"): text(
        "INSERT INTO mensajes_entrantes (id, agencia_id, cliente_id, canal, id_externo) "
        "VALUES (:nuevo, :a, :c, 'sonda', 'sonda')"
    ),
    ("mensajes_entrantes", "actualizacion"): text(
        "UPDATE mensajes_entrantes SET canal = 'sonda' WHERE id = :objetivo"
    ),
    ("mensajes_entrantes", "movimiento"): text(
        "UPDATE mensajes_entrantes SET agencia_id = :a, cliente_id = :c WHERE id = :objetivo"
    ),
}


def _sentencia_de_cliente(recurso: RecursoDeCliente, caso: Caso):
    """La sentencia de una celda sobre CUALQUIER tabla de la clase *de cliente*.

    # WHY: es la generalizacion exacta de lo que `heraldos` hacia a mano. Los dos
    # ejes de la cascada y las tres direcciones de escritura se expresan igual en
    # todas: lo unico que cambia entre tablas es el texto de la sentencia y que
    # fila esta sembrada, y las dos cosas viven en `RECURSOS_DE_CLIENTE`.
    """
    tabla = recurso.tabla
    agencia, cliente = DUENOS[caso.relacion]
    parametros = {"nuevo": recurso.fila_nueva}
    if caso.direccion in ("lectura", "actualizacion"):
        return _SQL[(tabla, caso.direccion)], parametros | {
            "objetivo": recurso.fila_de[caso.relacion]
        }
    if caso.direccion == "insercion":
        return _SQL[(tabla, "insercion")], parametros | {"a": agencia, "c": cliente}
    if caso.direccion == "reasignacion":
        # MI fila (la de A1) empujada hacia el dueno de `relacion`.
        return _SQL[(tabla, "movimiento")], parametros | {
            "a": agencia,
            "c": cliente,
            "objetivo": recurso.fila_de["mio"],
        }
    # usurpacion: la fila de `relacion`, tirada hacia MI (A / A1).
    return _SQL[(tabla, "movimiento")], parametros | {
        "a": AGENCIA_A,
        "c": CLIENTE_A1,
        "objetivo": recurso.fila_de[caso.relacion],
    }


def _sentencia(tabla: str, caso: Caso):
    """Traduce una celda de la rejilla a la sentencia que la ejerce."""
    agencia, cliente = DUENOS[caso.relacion]
    recurso = POR_TABLA.get(tabla)
    if recurso is not None:
        return _sentencia_de_cliente(recurso, caso)
    if tabla == "clientes":
        if caso.direccion in ("lectura", "actualizacion"):
            return _SQL[(tabla, caso.direccion)], {"objetivo": CLIENTE_DE[caso.relacion]}
        if caso.direccion == "insercion":
            return (
                _SQL[(tabla, "insercion")],
                {"nuevo": CLIENTE_NUEVO, "a": AGENCIA_DE[caso.relacion]},
            )
        if caso.direccion == "reasignacion":
            # MI ficha, empujada a la agencia de `relacion`.
            return (
                _SQL[(tabla, "movimiento")],
                {"a": AGENCIA_DE[caso.relacion], "objetivo": CLIENTE_A1},
            )
        # usurpacion: la ficha de `relacion`, tirada hacia MI agencia.
        return _SQL[(tabla, "movimiento")], {"a": AGENCIA_A, "objetivo": CLIENTE_DE[caso.relacion]}
    if caso.direccion in ("lectura", "actualizacion"):
        return _SQL[(tabla, caso.direccion)], {"objetivo": AGENCIA_DE[caso.relacion]}
    if caso.direccion == "insercion":
        # Una agencia que no es la mia: es lo unico que llega a la clausula
        # WITH CHECK sin chocar antes con la clave primaria.
        return _SQL[(tabla, "insercion")], {"nuevo": AGENCIA_FANTASMA}
    if caso.direccion == "reasignacion":
        # Cambiarle el identificador a MI PROPIA agencia = sacarla de mi alcance.
        return _SQL[(tabla, "movimiento")], {"nuevo": AGENCIA_FANTASMA, "objetivo": AGENCIA_A}
    # usurpacion: tocarle el identificador a la fila de OTRA agencia.
    return _SQL[(tabla, "movimiento")], {"nuevo": AGENCIA_FANTASMA, "objetivo": AGENCIA_B}


def _inquilino_de(alcance: str) -> Inquilino:
    if alcance == "agencia":
        return sesion_de_agencia(AGENCIA_A)
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


async def _medir(motor, caso: Caso, tabla: str) -> Veredicto:
    """Corre la celda y devuelve LO QUE PASO, no lo que se esperaba.

    Comparar el veredicto observado con el declarado es lo que hace que cada
    sonda tenga una respuesta a la pregunta «que resultado la pondria en rojo».
    """
    sentencia, parametros = _sentencia(tabla, caso)
    try:
        async with sesion_de_inquilino(motor, _inquilino_de(caso.alcance)) as conexion:
            resultado = await conexion.execute(sentencia, parametros)
            if caso.direccion == "lectura":
                return PERMITIDO if resultado.scalar_one() else INVISIBLE
            afectadas = resultado.rowcount
    except DBAPIError as fallo:
        # WHY: los dos fallos comparten el SQLSTATE `42501` de Postgres y significan
        # cosas distintas. Se distinguen por el mensaje —que es lo unico que los
        # separa— y NO se funden: «la politica no te deja tocar esa fila» y «no
        # tienes ese verbo sobre esta tabla» son dos mecanismos, y una bateria que
        # los confundiera daria por bueno el mecanismo equivocado (RF-10).
        mensaje = str(fallo).lower()
        if "row-level security" in mensaje:
            return RECHAZADO
        if "permission denied" in mensaje:
            return SIN_PRIVILEGIO
        raise
    return PERMITIDO if afectadas else INVISIBLE


def _ids(casos: tuple[Caso, ...], tabla: str) -> list[str]:
    return [f"{tabla}-{c.alcance}-{c.relacion}-{c.direccion}" for c in casos]


def _reproche(tabla: str, caso: Caso, observado: Veredicto) -> str:
    return (
        f"\n  celda    : {tabla} · sesion de {caso.alcance} -> dato {caso.relacion} · "
        f"{caso.direccion}"
        f"\n  esperado : {caso.veredicto.value}"
        f"\n  observado: {observado.value}"
        f"\n  por que  : {caso.porque}"
    )


#: (tabla, caso) de TODAS las tablas de la clase *de cliente*. Aplanado aqui para
#: que `pytest` genere una sonda por celda con su identificador legible.
_CELDAS_DE_CLIENTE = [
    (recurso.tabla, caso)
    for recurso in RECURSOS_DE_CLIENTE
    for caso in MATRICES[recurso.tabla]
]


@pytest.mark.parametrize(
    ("tabla", "caso"),
    _CELDAS_DE_CLIENTE,
    ids=[f"{tabla}-{c.alcance}-{c.relacion}-{c.direccion}" for tabla, c in _CELDAS_DE_CLIENTE],
)
async def test_matriz_de_clase_cliente(motor, tabla: str, caso: Caso) -> None:
    """Clase DE CLIENTE: los dos ejes y las tres direcciones, sobre CADA tabla.

    # WHY (una sola prueba para las seis): la regla es la misma; lo que cambia es
    # la tabla. Con una prueba por tabla, la septima tabla nace sin prueba y nadie
    # se entera — que es C-06 otra vez. Aqui el universo lo da
    # `RECURSOS_DE_CLIENTE`, y `test_toda_tabla_de_clase_cliente_usa_la_matriz_
    # canonica` comprueba contra el CATALOGO que ese registro no se quede corto.
    """
    observado = await _medir(motor, caso, tabla)
    assert observado is caso.veredicto, _reproche(tabla, caso, observado)


def test_toda_tabla_de_clase_cliente_esta_en_el_registro(catalogo_de_tablas) -> None:
    """El registro se compara con el CATALOGO, no con la memoria de nadie."""
    del_catalogo = {
        tabla
        for tabla, columnas in catalogo_de_tablas.items()
        if clase_de(columnas) == CLASE_CLIENTE
    }
    faltan = sorted(del_catalogo - set(POR_TABLA))
    sobran = sorted(set(POR_TABLA) - del_catalogo)
    assert not faltan, (
        f"estas tablas son de la clase *de cliente* y no estan en RECURSOS_DE_CLIENTE: "
        f"{faltan}. Sin registro no tienen sondas, y el CI saldria verde midiendo menos"
    )
    assert not sobran, (
        f"RECURSOS_DE_CLIENTE habla de tablas que no existen o no son de esa clase: "
        f"{sobran}. Una sonda contra una tabla que no esta mide el aire"
    )


def test_toda_tabla_de_clase_cliente_usa_la_matriz_canonica() -> None:
    """Ninguna tabla puede traerse una matriz mas floja que la de las demas.

    # WHY: el veredicto DECLARADO es contra lo que se compara la medida. Si una
    # tabla nueva llegara con su propia matriz —escrita a mano, con un `INVISIBLE`
    # donde iba un `RECHAZADO`— la sonda saldria verde contra una expectativa
    # equivocada. Aqui se exige que la matriz de cada tabla de esta clase sea
    # EXACTAMENTE la que produce el generador para sus verbos.
    """
    for recurso in RECURSOS_DE_CLIENTE:
        esperada = casos_de_clase_cliente(puede_actualizar=recurso.puede_actualizar)
        assert MATRICES[recurso.tabla] == esperada, (
            f"la matriz de {recurso.tabla!r} no es la canonica de su clase. Si de "
            "verdad esta tabla se comporta distinto, la diferencia va en "
            "`casos_de_clase_cliente`, donde la heredan todas"
        )


@pytest.mark.parametrize("caso", CASOS_CLIENTES, ids=_ids(CASOS_CLIENTES, "clientes"))
async def test_matriz_clientes(motor, caso: Caso) -> None:
    """Clase DE AGENCIA con columna propia: el portal ve SU ficha y ninguna mas."""
    observado = await _medir(motor, caso, "clientes")
    assert observado is caso.veredicto, _reproche("clientes", caso, observado)


@pytest.mark.parametrize("caso", CASOS_AGENCIAS, ids=_ids(CASOS_AGENCIAS, "agencias"))
async def test_matriz_agencias(motor, caso: Caso) -> None:
    """Clase DE AGENCIA sin columna propia: invisible al portal (marca blanca)."""
    observado = await _medir(motor, caso, "agencias")
    assert observado is caso.veredicto, _reproche("agencias", caso, observado)


# --------------------------------------------------------------------------
# El meta-test: «completa» deja de ser una opinion
# --------------------------------------------------------------------------
def test_la_matriz_declara_cada_celda_de_la_rejilla() -> None:
    """Cada celda (alcance x relacion x direccion) esta medida o justificada."""
    for tabla, casos in MATRICES.items():
        declaradas = [c.celda for c in casos]
        repetidas = sorted({c for c in declaradas if declaradas.count(c) > 1})
        assert not repetidas, f"{tabla}: celdas declaradas dos veces, {repetidas}"
        medidas = set(declaradas)
        for alcance in ALCANCES:
            for relacion in RELACIONES:
                for direccion in DIRECCIONES:
                    if (alcance, relacion, direccion) in medidas:
                        continue
                    motivo = NO_APLICA[tabla].get((relacion, direccion))
                    assert motivo and len(motivo.strip()) >= 40, (
                        f"la celda {tabla} · sesion de {alcance} -> dato {relacion} · "
                        f"{direccion} no tiene sonda NI motivo escrito en NO_APLICA. "
                        "O es un hueco de la bateria, o es una excepcion que alguien "
                        "tiene que justificar POR ESCRITO"
                    )


def test_la_lista_de_no_aplicables_no_tiene_entradas_muertas() -> None:
    """Un motivo escrito para una celda que SI tiene sonda es un motivo que miente."""
    for tabla, casos in MATRICES.items():
        medidas = {(c.relacion, c.direccion) for c in casos}
        sobrantes = sorted(medidas & set(NO_APLICA[tabla]))
        assert not sobrantes, (
            f"{tabla}: {sobrantes} estan declaradas NO APLICABLES y ademas tienen "
            "sonda. Una excepcion caducada tapa la siguiente"
        )


def test_la_bateria_mide_los_dos_ejes_y_las_dos_direcciones() -> None:
    """Un eje o una direccion sin probar = CE-01 no cumplido. Se cuenta, no se cree."""
    todos = [c for casos in MATRICES.values() for c in casos]
    assert len(todos) >= 20, f"CE-01 exige >=20 sondas de acceso cruzado; hay {len(todos)}"

    assert [c for c in todos if c.relacion == "vecino"], (
        "no hay ni una sonda del eje cliente<->cliente (C-01)"
    )
    assert [c for c in todos if c.relacion == "ajeno"], (
        "no hay ni una sonda del eje agencia<->agencia"
    )

    for direccion in ("insercion", "reasignacion", "usurpacion"):
        assert any(c.direccion == direccion for c in todos), (
            f"RF-02-bis: no hay ni una sonda de {direccion}; el aparato volveria a "
            "tener forma de lectura (K-02)"
        )

    assert [c for c in todos if c.veredicto is PERMITIDO], (
        "no hay ni una celda cuyo veredicto sea PERMITIDO: una politica que lo "
        "negara todo pasaria esta bateria entera, y el producto no funcionaria"
    )
    assert any(
        c.alcance == "agencia" and c.relacion == "vecino" and c.veredicto is PERMITIDO
        for c in todos
    ), (
        "K-04: falta el par `sesion-de-agencia -> sus propios clientes` en PERMITIDO. "
        "Sin el, un aislamiento perfecto y un panel roto son indistinguibles"
    )


def test_ninguna_tabla_del_catalogo_queda_fuera_de_la_bateria(catalogo_de_tablas) -> None:
    """C-06 aplicado a la bateria: una tabla nueva entra sola en la medida.

    # WHY: el gate de cobertura (T-014-bis) ya obliga a que una tabla nueva encaje
    # en su clase. Eso es ESTRUCTURA. Esto es el otro lado: que ademas alguien la
    # haya medido POR EFECTO, o haya escrito por que no hace falta.
    """
    for tabla in sorted(catalogo_de_tablas):
        if tabla in MATRICES:
            continue
        motivo = SIN_MATRIZ_PROPIA.get(tabla)
        assert motivo and len(motivo.strip()) >= 40, (
            f"la tabla {tabla!r} existe en el esquema y no tiene matriz de sondas ni "
            "motivo escrito en SIN_MATRIZ_PROPIA. La bateria se autorizo una vez, "
            "sobre las tablas que existian ENTONCES (C-06)"
        )


def test_la_bateria_no_habla_de_tablas_que_no_existen(catalogo_de_tablas) -> None:
    fantasmas = sorted((set(SIN_MATRIZ_PROPIA) | set(MATRICES)) - set(catalogo_de_tablas))
    assert not fantasmas, (
        f"la bateria habla de tablas que no existen en el esquema: {fantasmas}. "
        "Una sonda contra una tabla inexistente no mide nada y su verde es mentira"
    )


# ==========================================================================
# T-014·quinquies — el par que faltaba, DERIVADO del catalogo
# ==========================================================================
#: `qual` viene normalizada por Postgres, con los `::text` puestos. La expresion
#: busca la COLUMNA PROPIA de una tabla de nivel agencia: la que la politica
#: compara contra `app.cliente_id`. Si no aparece ninguna, la tabla no expone
#: fila propia y tiene que ser INVISIBLE al alcance cliente.
_COLUMNA_PROPIA = re.compile(
    r"([a-z_][a-z0-9_]*)\s*=\s*\(?\s*current_setting\(\s*'app\.cliente_id'(?:::text)?\s*\)\s*\)?"
    r"::uuid"
)


def columna_propia_de(expresion: str) -> str | None:
    """Que columna declara esta politica como identidad del cliente, si alguna."""
    encontrada = _COLUMNA_PROPIA.search(expresion or "")
    return encontrada.group(1) if encontrada else None


async def test_una_sesion_de_cliente_no_alcanza_las_tablas_de_nivel_agencia(
    motor, motor_admin, tablas_de_nivel_agencia
) -> None:
    """T-014·quinquies (RF-01·bis, L-02) — y DERIVADA, no una lista de tres nombres.

    El portal de un cliente pide cada tabla de NIVEL AGENCIA que exista en el
    esquema y tiene que ver **cero filas** — salvo la suya en las tablas que
    declaren su columna de identidad.

    # WHY (C-4A-08): la tarea nombra tres tablas concretas —la de clientes, la de
    # usuarios operadores, la de credenciales del proveedor—. Escribir tres sondas
    # con esos tres nombres taparia el agujero POR DATO: la cuarta tabla de nivel
    # agencia que alguien anada no tendria sonda y el CI saldria verde. Aqui el
    # universo se DERIVA del catalogo, igual que en `test_rls_cobertura.py`: de las
    # tres que nombra la tarea hoy existe `clientes` (y `agencias`, que la tarea no
    # nombra); las otras dos llegan en fases posteriores y entraran solas en esta
    # medida el dia que la migracion las cree.
    #
    # # WHY (el control dentro de la sonda): `0 == 0` sobre una tabla vacia es un
    # verde que no midio nada. Por eso se exige que la tabla TENGA filas y que
    # existan filas que el cliente NO debe ver: sin las dos cosas, la sonda no
    # puede distinguir un aislamiento que funciona de una tabla que no tiene datos.
    """
    assert tablas_de_nivel_agencia, (
        "no hay ninguna tabla de la clase *de agencia* en el esquema: esta sonda "
        "saldria verde POR AUSENCIA, que es como L-02 se perdio dos veces"
    )

    portal = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    for tabla, datos in sorted(tablas_de_nivel_agencia.items()):
        politicas = datos["politicas"]
        assert len(politicas) == 1, (
            f"{tabla} tiene {len(politicas)} politicas; el acceso efectivo seria su "
            "union y esta sonda no sabria contra que compararse"
        )
        columna = columna_propia_de(politicas[0]["qual"])
        contar = _contar(tabla)
        contar_propias = None if columna is None else _contar_propias(tabla, columna)

        with motor_admin.connect() as conexion:
            total = conexion.execute(contar).scalar_one()
            esperado = (
                0
                if contar_propias is None
                else conexion.execute(
                    contar_propias, {"agencia": AGENCIA_A, "cliente": CLIENTE_A1}
                ).scalar_one()
            )

        assert total > 0, (
            f"{tabla} esta vacia: un `0 == 0` sobre una tabla sin filas no distingue "
            "el aislamiento de la ausencia de datos"
        )
        assert esperado < total, (
            f"en {tabla} TODAS las filas ({total}) son alcanzables por el cliente A1, "
            "asi que no hay nada que esta sonda pueda demostrar que se oculta"
        )

        async with sesion_de_inquilino(motor, portal) as conexion:
            visto = (await conexion.execute(contar)).scalar_one()

        assert visto == esperado, (
            f"el portal del cliente A1 ve {visto} filas de {tabla!r} y deberia ver "
            f"{esperado} (de {total} que hay). Columna de identidad declarada: "
            f"{columna!r}. Sin el predicado `app.alcance` esta tabla queda legible "
            "entera por una sesion de cliente — es exactamente L-02"
        )


async def test_una_sesion_de_agencia_si_ve_sus_tablas_de_nivel_agencia(
    motor, motor_admin, tablas_de_nivel_agencia
) -> None:
    """El control de la sonda anterior: el operador SI trabaja con esas tablas.

    Sin esto, una politica que ocultara las tablas de nivel agencia a TODO el
    mundo pasaria la sonda de arriba con nota — y el panel del operador estaria
    vacio.
    """
    operador = sesion_de_agencia(AGENCIA_A)
    for tabla in sorted(tablas_de_nivel_agencia):
        contar = _contar(tabla)
        contar_de_mi_agencia = _contar_de_mi_agencia(tabla)
        with motor_admin.connect() as conexion:
            esperado = conexion.execute(contar_de_mi_agencia, {"agencia": AGENCIA_A}).scalar_one()
        assert esperado > 0, f"la agencia A no tiene ninguna fila en {tabla}: nada que medir"

        async with sesion_de_inquilino(motor, operador) as conexion:
            visto = (await conexion.execute(contar)).scalar_one()

        assert visto == esperado, (
            f"el operador de la agencia A ve {visto} filas de {tabla!r} y deberia ver "
            f"{esperado}: las de su agencia, todas y solo esas"
        )


# ==========================================================================
# CE-02 — EL ENDPOINT-TRAMPA. Esta prueba es el producto.
# ==========================================================================
#
# # WHY: la diferencia entre «no se nos olvido filtrar» y «olvidarlo no sirve de
# nada». Estas consultas estan escritas A PROPOSITO sin una sola clausula de
# inquilino: son literalmente lo que alguien va a escribir un martes por la
# tarde. Si el aislamiento dependiera del filtro, aqui se veria TODO.
#
# ALCANCE HONESTO DE LO QUE ESTO MIDE: en F0 no existe capa HTTP (no hay
# framework web en las dependencias del repositorio), asi que el «endpoint» es su
# MANEJADOR: la consulta sin filtro, corriendo por el camino de sesion de
# PRODUCCION (`sesion_de_inquilino` + el pool en su configuracion de produccion).
# Lo que CE-02 mide es la consulta olvidada, y eso es exactamente lo que corre
# aqui. Lo que NO queda medido es el enrutado HTTP —porque todavia no existe—: la
# primera ruta que se escriba tiene que entrar por esta misma dependencia, y esa
# obligacion la vigila `test_escalada_alcance.py`.

#: El manejador olvidadizo, uno por recurso. Ni un WHERE.
CONSULTAS_SIN_FILTRO: dict[str, TextClause] = {
    "heraldos": text("SELECT id FROM heraldos"),
    "clientes": text("SELECT id FROM clientes"),
    "agencias": text("SELECT agencia_id AS id FROM agencias"),
    # Las cinco de la revision 0003. Todas sin una sola clausula de inquilino: si
    # el aislamiento viviera en el filtro, aqui se veria el secreto, la bitacora,
    # la cola y el registro de mensajes de TODOS los inquilinos de la instalacion.
    "secretos": text("SELECT id FROM secretos"),
    "bitacora": text("SELECT id FROM bitacora"),
    "trabajos": text("SELECT id FROM trabajos"),
    "trabajos_archivados": text("SELECT id FROM trabajos_archivados"),
    "mensajes_entrantes": text("SELECT id FROM mensajes_entrantes"),
}

#: Y sus versiones destructivas: las escrituras a las que se les olvido el WHERE.
#: Son el mismo olvido que la lectura, con consecuencias que no se deshacen.
BORRADO_SIN_FILTRO = text("DELETE FROM heraldos")
ACTUALIZACION_SIN_FILTRO = text("UPDATE heraldos SET nombre = 'pisado'")


async def _lo_que_ve_el_endpoint_trampa(motor, inquilino: Inquilino) -> dict[str, set[UUID]]:
    visto: dict[str, set[UUID]] = {}
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        for recurso, consulta in CONSULTAS_SIN_FILTRO.items():
            visto[recurso] = {f.id for f in (await conexion.execute(consulta)).all()}
    return visto


async def test_el_endpoint_trampa_no_ve_datos_ajenos_desde_un_portal_de_cliente(motor) -> None:
    """CE-02, eje CLIENTE<->CLIENTE y eje AGENCIA<->AGENCIA, sobre los tres recursos."""
    visto = await _lo_que_ve_el_endpoint_trampa(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1))

    assert visto["heraldos"] == {HERALDO_A1}, (
        f"el endpoint sin filtro devolvio {sorted(visto['heraldos'])} al portal de A1. "
        "Deberia devolver solo su heraldo: si aparece el de A2 la fuga es "
        "cliente<->cliente; si aparece el de B1, agencia<->agencia"
    )
    assert HERALDO_A2 not in visto["heraldos"], "EJE CLIENTE<->CLIENTE abierto (C-01)"
    assert HERALDO_B1 not in visto["heraldos"], "EJE AGENCIA<->AGENCIA abierto"

    assert visto["clientes"] == {CLIENTE_A1}, (
        f"el endpoint sin filtro devolvio la cartera {sorted(visto['clientes'])} al "
        "portal de A1: solo puede ver su propia ficha (RF-01·bis)"
    )
    assert visto["agencias"] == set(), (
        f"el endpoint sin filtro le enseno agencias ({sorted(visto['agencias'])}) a un "
        "portal de cliente: la marca blanca (RF-59) se rompe ahi mismo"
    )


async def test_el_endpoint_trampa_no_ve_otra_agencia_desde_un_operador(motor) -> None:
    """CE-02, el otro alcance: el operador ve TODO lo suyo y NADA de la otra agencia."""
    visto = await _lo_que_ve_el_endpoint_trampa(motor, sesion_de_agencia(AGENCIA_A))

    assert visto["heraldos"] == {HERALDO_A1, HERALDO_A2}, (
        f"el endpoint sin filtro devolvio {sorted(visto['heraldos'])} al operador de A. "
        "Tiene que ver los de SUS dos clientes (K-04: si ve menos, el panel esta roto) "
        "y ninguno de la agencia B"
    )
    assert visto["clientes"] == {CLIENTE_A1, CLIENTE_A2}, (
        f"la cartera del operador salio {sorted(visto['clientes'])}: son sus dos "
        "clientes, ni uno mas ni uno menos"
    )
    assert visto["agencias"] == {AGENCIA_A}, (
        f"el operador vio {sorted(visto['agencias'])} en la tabla de agencias: solo la "
        "suya, jamas la de otra"
    )


async def test_el_borrado_sin_where_del_endpoint_trampa_solo_alcanza_lo_propio(
    motor, motor_admin
) -> None:
    """El olvido mas caro que existe: `DELETE` sin `WHERE`, contenido por la politica.

    # WHY: la lectura no es el peor caso. Un `DELETE FROM heraldos` escrito desde
    # el portal de un cliente borraria la tabla entera de todos los inquilinos si el
    # aislamiento viviera en el filtro. Aqui borra UNA fila: la suya.
    """
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        borradas = (await conexion.execute(BORRADO_SIN_FILTRO)).rowcount

    with motor_admin.connect() as conexion:
        quedan = {f.id for f in conexion.execute(text("SELECT id FROM heraldos")).all()}

    assert borradas == 1, (
        f"el DELETE sin WHERE del portal de A1 borro {borradas} filas: deberia haber "
        "alcanzado exactamente la suya"
    )
    assert quedan == {HERALDO_A2, HERALDO_B1}, (
        f"despues del DELETE sin WHERE quedan {sorted(quedan)}. El portal de un cliente "
        "se llevo por delante datos de otro inquilino: es el peor modo de fallo del "
        "producto entero"
    )


async def test_la_actualizacion_sin_where_del_endpoint_trampa_solo_alcanza_lo_propio(
    motor, motor_admin
) -> None:
    """El otro olvido caro: `UPDATE` sin `WHERE` — pisa lo suyo y nada mas.

    # WHY: el `DELETE` sin `WHERE` es el olvido famoso, pero el `UPDATE` sin
    # `WHERE` es igual de destructivo y menos visible: no vacia una tabla, la
    # corrompe en silencio. Si el aislamiento viviera en el filtro, un portal
    # renombraria los heraldos de todos los inquilinos de la instalacion.
    """
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        pisadas = (await conexion.execute(ACTUALIZACION_SIN_FILTRO)).rowcount

    with motor_admin.connect() as conexion:
        nombres = dict(conexion.execute(text("SELECT id, nombre FROM heraldos")).all())

    assert pisadas == 1, (
        f"el UPDATE sin WHERE del portal de A1 toco {pisadas} filas: tenia que "
        "alcanzar exactamente la suya"
    )
    assert nombres[HERALDO_A1] == "pisado", (
        "el portal no consiguio editar ni su propia fila: el aislamiento seria "
        "perfecto y el producto, inutil"
    )
    assert nombres[HERALDO_A2] == "Heraldo A2", (
        f"el heraldo del cliente A2 quedo con el nombre {nombres[HERALDO_A2]!r}: un "
        "portal reescribio datos de otro cliente de su misma agencia (EJE "
        "CLIENTE<->CLIENTE)"
    )
    assert nombres[HERALDO_B1] == "Heraldo B1", (
        f"el heraldo de la agencia B quedo con el nombre {nombres[HERALDO_B1]!r}: un "
        "portal reescribio datos de otra agencia (EJE AGENCIA<->AGENCIA)"
    )


async def test_el_endpoint_trampa_solo_ve_lo_propio_en_toda_tabla_de_cliente(motor) -> None:
    """CE-02 DERIVADO: la trampa sobre CADA tabla de la clase *de cliente*.

    # WHY: las tres afirmaciones escritas a mano de arriba cubren `heraldos`,
    # `clientes` y `agencias` — las tablas que existian cuando se escribieron. Esta
    # las deriva del registro, asi que la tabla de inquilino numero siete entra sola
    # en la medida. Es exactamente el defecto C-06 aplicado al endpoint-trampa: «se
    # autorizo una vez, sobre las tablas de ENTONCES».
    #
    # # WHY (los dos alcances en la misma sonda): el portal tiene que ver UNA fila
    # —la suya— y el operador DOS —las de sus dos clientes—. Sin la segunda mitad,
    # una politica que lo negara todo pasaria con nota y el panel estaria vacio
    # (K-04).
    """
    portal = await _lo_que_ve_el_endpoint_trampa(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1))
    operador = await _lo_que_ve_el_endpoint_trampa(motor, sesion_de_agencia(AGENCIA_A))

    for recurso in RECURSOS_DE_CLIENTE:
        tabla = recurso.tabla
        propia = recurso.fila_de["mio"]
        vecina = recurso.fila_de["vecino"]
        ajena = recurso.fila_de["ajeno"]

        assert portal[tabla] == {propia}, (
            f"el endpoint sin filtro devolvio {sorted(portal[tabla])} de {tabla!r} al "
            f"portal de A1, y solo puede ver {propia}. Si aparece {vecina} la fuga es "
            f"CLIENTE<->CLIENTE dentro de la misma agencia; si aparece {ajena}, "
            "AGENCIA<->AGENCIA"
        )
        assert operador[tabla] == {propia, vecina}, (
            f"el endpoint sin filtro devolvio {sorted(operador[tabla])} de {tabla!r} al "
            f"operador de la agencia A. Tiene que ver las de SUS dos clientes "
            f"({propia}, {vecina}) — si ve menos, el panel esta roto (K-04) — y "
            f"ninguna de la agencia B ({ajena})"
        )


def test_el_endpoint_trampa_cubre_todos_los_recursos_de_la_bateria() -> None:
    """Un recurso nuevo sin su trampa seria CE-02 verde POR AUSENCIA."""
    assert set(CONSULTAS_SIN_FILTRO) == set(MATRICES), (
        "el endpoint-trampa y la matriz de sondas hablan de conjuntos de recursos "
        f"distintos: trampa={sorted(CONSULTAS_SIN_FILTRO)} vs "
        f"matriz={sorted(MATRICES)}. CE-02 exige la trampa sobre TODOS los tipos de "
        "recurso, y un recurso que solo esta en una de las dos listas queda a medias"
    )


# --------------------------------------------------------------------------
# El punto ciego de la limpieza: que la cascada de verdad la alcance
# --------------------------------------------------------------------------
#
# # WHY: `resembrar` borra `agencias` y confia en que la cascada de claves
# foraneas se lleve el resto. Eso es DERIVAR el universo de la limpieza del
# modelo — mejor que una lista escrita a mano (P-12) — pero solo mientras el
# modelo lo sostenga. Una tabla de inquilino futura sin clave foranea, o con
# `ON DELETE SET NULL`, sobreviviria al borrado en silencio y contaminaria las
# corridas siguientes: exactamente P-10, P-11 y P-12 una cuarta vez, un nivel
# mas arriba. Aqui se comprueba la PROPIEDAD que hace correcta a la limpieza, y
# ademas se mide su EFECTO.
TABLA_RAIZ_DE_LA_CASCADA = "agencias"

_CLAVES_FORANEAS = text(
    """
    SELECT c.conrelid::regclass::text AS hija,
           c.confrelid::regclass::text AS padre,
           c.confdeltype AS al_borrar
    FROM pg_constraint c
    JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE c.contype = 'f' AND n.nspname = 'public'
    """
)

def _alcanzables_por_la_cascada(conexion) -> set[str]:
    """Que tablas se vacian solas al borrar la raiz. BFS sobre el grafo de claves."""
    aristas: dict[str, set[str]] = {}
    for fila in conexion.execute(_CLAVES_FORANEAS).all():
        if fila.al_borrar != "c":  # 'c' = ON DELETE CASCADE
            continue
        aristas.setdefault(fila.padre, set()).add(fila.hija)

    alcanzadas = {TABLA_RAIZ_DE_LA_CASCADA}
    pendientes = [TABLA_RAIZ_DE_LA_CASCADA]
    while pendientes:
        actual = pendientes.pop()
        for hija in aristas.get(actual, set()):
            if hija not in alcanzadas:
                alcanzadas.add(hija)
                pendientes.append(hija)
    return alcanzadas


def test_la_cascada_alcanza_toda_tabla_de_inquilino(catalogo_de_tablas, motor_admin) -> None:
    """La limpieza deriva del modelo — y esto comprueba que el modelo la sostiene."""
    with motor_admin.connect() as conexion:
        alcanzables = _alcanzables_por_la_cascada(conexion)

    huerfanas = sorted(
        tabla
        for tabla, columnas in catalogo_de_tablas.items()
        if clase_de(columnas) != CLASE_NO_INQUILINO and tabla not in alcanzables
    )
    assert not huerfanas, (
        f"estas tablas de inquilino NO se vacian al borrar {TABLA_RAIZ_DE_LA_CASCADA}: "
        f"{huerfanas}. `resembrar` las dejaria vivas entre sondas y el ORDEN de "
        "ejecucion volveria a formar parte del resultado — es P-10/P-11/P-12 otra vez, "
        "un nivel mas arriba. O cuelgan de la cascada, o la limpieza deja de ser derivada"
    )


def test_tras_resembrar_no_queda_nada_de_otra_corrida(
    catalogo_de_tablas, motor_admin, motor_de_siembra
) -> None:
    """Y el EFECTO: despues de limpiar, ninguna tabla guarda filas de un inquilino ajeno.

    # WHY: la propiedad estructural de arriba dice que la cascada PUEDE alcanzar;
    # esto dice que de hecho ALCANZO. Un control que no vuelve a verde no es ruido:
    # es la prueba de que el setup no devuelve la base al estado que dice devolver.
    """
    intrusa = UUID("dddddddd-0000-4000-8000-00000000000d")
    with motor_admin.connect() as conexion:
        conexion.execute(
            text("INSERT INTO agencias (agencia_id, nombre) VALUES (:id, 'intrusa')"),
            {"id": intrusa},
        )

    resembrar(motor_de_siembra)

    with motor_admin.connect() as conexion:
        for tabla, columnas in sorted(catalogo_de_tablas.items()):
            if clase_de(columnas) == CLASE_NO_INQUILINO:
                continue
            sobrantes = conexion.execute(
                _contar_ajenas(tabla), {"a": AGENCIA_A, "b": AGENCIA_B}
            ).scalar_one()
            assert sobrantes == 0, (
                f"tras resembrar, {tabla} conserva {sobrantes} filas que no son de "
                "ninguna agencia sembrada: la limpieza no devolvio la base a su sitio"
            )
