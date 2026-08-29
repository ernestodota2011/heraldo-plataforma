"""T-021 (RF-12) — las DOS defensas, y cada una medida por separado.

La afirmacion «hay idempotencia» no vale nada si no se sabe QUIEN la sostiene en
cada caso. Aqui se mide:

1. El duplicado lo atrapa **Redis**, sin tocar la base · y se demuestra con una
   conexion que REVIENTA si alguien la usa.
2. ==Con Redis **vacio**, el duplicado lo atrapa la **restriccion unica**.== Es
   el caso real: Redis se reinicia y su memoria desaparece.
3. La idempotencia es **por inquilino**: el mismo identificador externo en dos
   inquilinos son dos mensajes distintos, no un duplicado.
4. ==Una transaccion que se deshace **NO** deja marca en Redis.== Si la dejara,
   el mensaje se perderia para siempre en el siguiente reenvio.
"""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.channels.idempotency import (
    PREFIJO,
    Guardian,
    Veredicto,
    clave_de,
    puerta,
    recordar,
    reservar,
    ya_visto,
)
from app.tenancy import sesion_de_inquilino
from conftest import (
    AGENCIA_A,
    AGENCIA_B,
    CLIENTE_A1,
    CLIENTE_A2,
    CLIENTE_B1,
    resembrar,
    sesion_de_cliente,
)
from worker import cola

CANAL = "whatsapp"
EXTERNO = "wamid.HBgLNTQ5MTEyMzQ1Njc"


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    resembrar(motor_de_siembra)


@pytest.fixture
def inquilino():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


class ConexionQueRevienta:
    """Una conexion que falla si alguien la usa. Es el instrumento de la sonda 1.

    # WHY: «Redis lo atrapo sin tocar la base» es una afirmacion sobre lo que NO
    # paso, y eso no se puede medir mirando el resultado — el veredicto seria el
    # mismo lo atrape quien lo atrape. Con esta conexion, que la base se toque deja
    # de ser invisible: revienta.
    """

    def __init__(self) -> None:
        self.usada = False

    async def execute(self, *_args, **_kwargs):
        self.usada = True
        raise AssertionError("se consulto la base habiendo dicho que el duplicado lo atrapo Redis")


# --------------------------------------------------------------------------
# 1 — el duplicado lo atrapa Redis, y se demuestra
# --------------------------------------------------------------------------
async def test_el_duplicado_lo_atrapa_redis_sin_tocar_la_base(motor, redis, inquilino) -> None:
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (primera, _):
        assert primera.veredicto is Veredicto.NUEVO
        assert primera.guardian is None

    # Al salir del bloque, la marca YA esta puesta (la transaccion se confirmo).
    assert await ya_visto(redis, clave_de(inquilino, canal=CANAL, id_externo=EXTERNO))

    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (
        segunda,
        conexion,
    ):
        assert segunda.veredicto is Veredicto.DUPLICADO
        assert segunda.guardian is Guardian.REDIS, (
            f"el duplicado lo atrapo {segunda.guardian}, no Redis: el camino rapido no "
            "esta funcionando y cada reenvio de la plataforma cuesta una transaccion"
        )
        assert conexion is None, "no hay nada que hacer con un duplicado: no se abre sesion"


async def test_la_sonda_de_redis_lo_demuestra_con_una_conexion_que_revienta(
    redis, inquilino
) -> None:
    """El instrumento: si `reservar` tocara la base, esto seria un fallo ruidoso."""
    clave = clave_de(inquilino, canal=CANAL, id_externo=EXTERNO)
    await recordar(redis, clave)
    espia = ConexionQueRevienta()
    assert await ya_visto(redis, clave) is True
    assert espia.usada is False

    # Control del instrumento: si SE usa, revienta de verdad.
    with pytest.raises(AssertionError):
        await reservar(espia, inquilino, canal=CANAL, id_externo=EXTERNO)
    assert espia.usada is True


# --------------------------------------------------------------------------
# 2 — con Redis VACIO, la restriccion unica atrapa el duplicado
# --------------------------------------------------------------------------
async def test_con_redis_vacio_el_duplicado_lo_atrapa_la_restriccion_unica(
    motor, redis, inquilino
) -> None:
    """==El caso que de verdad importa: Redis se reinicio y no recuerda nada.==

    # WHY: es la unica sonda que dice si la promesa de RF-12 es real. La marca de
    # Redis es una comodidad; lo que impide de verdad el segundo procesamiento es la
    # restriccion en la base. Si esta sonda se pusiera roja, el producto tendria una
    # idempotencia que se evapora en cada reinicio del servidor de estado.
    """
    clave = clave_de(inquilino, canal=CANAL, id_externo=EXTERNO)
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (primera, _):
        assert primera.veredicto is Veredicto.NUEVO

    # Redis pierde la memoria: se borra la clave, como si se hubiera reiniciado.
    await redis.delete(clave)
    assert not await ya_visto(redis, clave)

    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (segunda, _):
        assert segunda.veredicto is Veredicto.DUPLICADO, (
            "con Redis vacio el mensaje entro por segunda vez: la unica defensa era la "
            "cache, y una cache no es una garantia"
        )
        assert segunda.guardian is Guardian.BASE, (
            f"el duplicado lo atrapo {segunda.guardian} con Redis vacio: imposible, "
            "salvo que la sonda no este midiendo lo que cree"
        )


async def test_la_restriccion_unica_existe_de_verdad_en_la_base(motor, inquilino) -> None:
    """Sin `ON CONFLICT`: el `INSERT` crudo del duplicado REVIENTA.

    # WHY: `reservar` usa `ON CONFLICT DO NOTHING`, asi que su comportamiento seria
    # el mismo si la restriccion no existiera — devolveria «nuevo» las dos veces y
    # nadie lo notaria hasta produccion. Esto ataca la restriccion de frente.
    """
    crudo = text(
        "INSERT INTO mensajes_entrantes (agencia_id, cliente_id, canal, id_externo) "
        "VALUES (:a, :c, :canal, :externo)"
    )
    parametros = {"a": AGENCIA_A, "c": CLIENTE_A1, "canal": CANAL, "externo": EXTERNO}
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await conexion.execute(crudo, parametros)

    with pytest.raises(IntegrityError) as capturado:
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await conexion.execute(crudo, parametros)
    assert "mensajes_entrantes_externo_key" in str(capturado.value), (
        "el duplicado fallo por otra restriccion: la que RF-12 necesita es la que "
        "cubre (agencia, cliente, canal, identificador externo)"
    )


# --------------------------------------------------------------------------
# 3 — la idempotencia es POR INQUILINO
# --------------------------------------------------------------------------
async def test_el_mismo_identificador_externo_en_otro_inquilino_es_un_mensaje_nuevo(
    motor, redis, inquilino
) -> None:
    """Los identificadores los numera la plataforma del canal, no nosotros.

    # WHY: con una clave global, el mensaje del segundo inquilino se descartaria
    # como «duplicado» del primero. Serian las dos cosas a la vez: una FUGA —el
    # segundo se entera de que otro recibio ese identificador— y una PERDIDA.
    """
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (primera, _):
        assert primera.veredicto is Veredicto.NUEVO

    for otro in (
        sesion_de_cliente(AGENCIA_A, CLIENTE_A2),  # vecino, MISMA agencia
        sesion_de_cliente(AGENCIA_B, CLIENTE_B1),  # otra agencia
    ):
        async with puerta(motor, redis, otro, canal=CANAL, id_externo=EXTERNO) as (reserva, _):
            assert reserva.veredicto is Veredicto.NUEVO, (
                f"el inquilino {otro.cliente_id} vio su propio mensaje como duplicado del "
                "de otro inquilino: la clave de idempotencia no lleva el inquilino dentro"
            )


async def test_un_identificador_distinto_es_un_mensaje_nuevo(motor, redis, inquilino) -> None:
    """Control: una puerta que dijera «duplicado» a todo pasaria media suite."""
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (primera, _):
        assert primera.veredicto is Veredicto.NUEVO
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO + "-otro") as (
        otra,
        _,
    ):
        assert otra.veredicto is Veredicto.NUEVO


# --------------------------------------------------------------------------
# 4 — la trampa fina: Redis nunca por delante de la base
# --------------------------------------------------------------------------
async def test_una_transaccion_deshecha_no_deja_marca_en_redis(
    motor, motor_admin, redis, inquilino
) -> None:
    """==Si la marca se pusiera al reservar, este mensaje se perderia PARA SIEMPRE.==

    # WHY: es el fallo mas caro que puede tener una idempotencia, y el mas facil de
    # escribir sin querer. Marca en Redis + ninguna fila en la base = el siguiente
    # reenvio se descarta por la marca y el mensaje no se procesa nunca. La sonda
    # provoca exactamente esa situacion: revienta la transaccion despues de reservar
    # y comprueba que Redis quedo limpio Y que el mensaje puede volver a entrar.
    """
    clave = clave_de(inquilino, canal=CANAL, id_externo=EXTERNO)
    with pytest.raises(RuntimeError):
        async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (
            reserva,
            conexion,
        ):
            assert reserva.veredicto is Veredicto.NUEVO
            await cola.encolar(conexion, inquilino, tipo="responder", carga={"id": EXTERNO})
            raise RuntimeError("el encolado revienta despues de reservar")

    assert not await ya_visto(redis, clave), (
        "quedo una marca en Redis para un mensaje que NO esta en la base: el proximo "
        "reenvio se descartaria y ese mensaje no se procesaria nunca"
    )
    with motor_admin.connect() as conexion:
        filas = conexion.execute(
            text("SELECT count(*) FROM mensajes_entrantes WHERE id_externo = :e"),
            {"e": EXTERNO},
        ).scalar_one()
    assert filas == 0, "la fila sobrevivio a una transaccion que se deshizo"

    # Y el control que lo cierra: el mensaje SI puede volver a entrar.
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (reintento, _):
        assert reintento.veredicto is Veredicto.NUEVO


async def test_el_trabajo_se_encola_en_la_misma_transaccion_que_la_reserva(
    motor, motor_admin, redis, inquilino
) -> None:
    """RF-11 + D-04: acusar recibo y encolar son UN acto, no dos.

    Control de la sonda anterior: si la transaccion se confirma, tienen que estar
    las DOS cosas — la fila de idempotencia y el trabajo.
    """
    async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=EXTERNO) as (
        reserva,
        conexion,
    ):
        assert reserva.veredicto is Veredicto.NUEVO
        await cola.encolar(conexion, inquilino, tipo="responder", carga={"id": EXTERNO})

    with motor_admin.connect() as conexion:
        mensajes = conexion.execute(
            text("SELECT count(*) FROM mensajes_entrantes WHERE id_externo = :e"),
            {"e": EXTERNO},
        ).scalar_one()
        trabajos = conexion.execute(
            text("SELECT count(*) FROM trabajos WHERE tipo = 'responder' AND cliente_id = :c"),
            {"c": CLIENTE_A1},
        ).scalar_one()
    assert (mensajes, trabajos) == (1, 1)


# --------------------------------------------------------------------------
# 5 — dos llegadas A LA VEZ: «exactamente un procesamiento» de verdad
# --------------------------------------------------------------------------
async def test_dos_llegadas_simultaneas_producen_exactamente_un_procesamiento(
    motor, motor_admin, redis, inquilino
) -> None:
    """==RF-12 dice «exactamente un», y hasta aqui se medía en serie.==

    # WHY (esta sonda nace de la revision cruzada, y la CONTRADICE): Crisol senalo
    # una supuesta condicion de carrera entre la comprobacion de Redis y la reserva
    # en la base, y propuso capturar `IntegrityError` dentro de `puerta`. La sonda
    # se escribio para comprobarlo y mide lo contrario: con `ON CONFLICT DO NOTHING`
    # la segunda transaccion NO revienta — se queda esperando a que la primera
    # confirme y despues no inserta nada, asi que devuelve `DUPLICADO` por la via
    # normal. Capturar `IntegrityError` ahi habria sido codigo muerto: un `except`
    # que nunca se ejecuta y que, el dia que alguien cambie el `ON CONFLICT`, taparia
    # el fallo real en vez de dejarlo salir.
    #
    # # WHY (aqui SI se paraleliza, y no contradice la regla de la casa): la regla
    # es no paralelizar pruebas que comparten estado sin quererlo. Aqui la
    # concurrencia ES lo que se mide, ocurre dentro de una sola prueba y sobre una
    # clave que solo usa esta prueba.
    """
    externo = EXTERNO + "-simultaneo"

    async def llegada() -> Veredicto:
        async with puerta(motor, redis, inquilino, canal=CANAL, id_externo=externo) as (
            reserva,
            conexion,
        ):
            if reserva.es_nuevo:
                await cola.encolar(conexion, inquilino, tipo="responder", carga={"id": externo})
            return reserva.veredicto

    veredictos = await asyncio.gather(llegada(), llegada())

    assert sorted(veredictos) == sorted([Veredicto.NUEVO, Veredicto.DUPLICADO]), (
        f"dos llegadas a la vez dieron {veredictos}: con dos NUEVO el mensaje se "
        "procesaria dos veces (RF-12 roto); con dos DUPLICADO no se procesaria ninguna"
    )

    with motor_admin.connect() as conexion:
        mensajes = conexion.execute(
            text("SELECT count(*) FROM mensajes_entrantes WHERE id_externo = :e"),
            {"e": externo},
        ).scalar_one()
        trabajos = conexion.execute(
            text("SELECT count(*) FROM trabajos WHERE cliente_id = :c AND carga->>'id' = :e"),
            {"c": CLIENTE_A1, "e": externo},
        ).scalar_one()
    assert (mensajes, trabajos) == (1, 1), (
        f"quedaron {mensajes} registros y {trabajos} trabajos para el mismo mensaje: "
        "«exactamente un procesamiento» no se cumple bajo concurrencia"
    )


class RedisQueSeCayo:
    """Un Redis que contesta a la lectura y revienta al escribir. Uno solo falla."""

    def __init__(self, verdadero) -> None:
        self._verdadero = verdadero

    async def exists(self, clave):
        return await self._verdadero.exists(clave)

    async def set(self, *_args, **_kwargs):
        raise RedisError("el servidor de estado no responde")


async def test_un_redis_caido_no_tumba_una_peticion_ya_procesada(
    motor, motor_admin, redis, inquilino
) -> None:
    """El acelerador no puede tumbar al camino principal.

    # WHY: nace del ultimo hallazgo de la revision cruzada. Su diagnostico —«se
    # pierde el mensaje»— era erroneo: para cuando se escribe la marca, la
    # transaccion ya se confirmo. Pero si la excepcion subiera, un Redis caido
    # convertiria en error una peticion YA PROCESADA, y la plataforma reenviaria
    # contra un 500 mientras Redis siguiera caido.
    #
    # # WHY (y la garantia sigue en pie): sin la marca, el reenvio paga una
    # transaccion de mas y ==la restriccion unica lo rechaza igual==. Esta sonda
    # comprueba las dos mitades: que no sube el error, y que el duplicado se sigue
    # atrapando — esta vez por la BASE, que es la que no miente.
    """
    externo = EXTERNO + "-sin-redis"
    caido = RedisQueSeCayo(redis)

    async with puerta(motor, caido, inquilino, canal=CANAL, id_externo=externo) as (
        primera,
        conexion,
    ):
        assert primera.veredicto is Veredicto.NUEVO
        await cola.encolar(conexion, inquilino, tipo="responder", carga={"id": externo})

    with motor_admin.connect() as conexion:
        filas = conexion.execute(
            text("SELECT count(*) FROM mensajes_entrantes WHERE id_externo = :e"),
            {"e": externo},
        ).scalar_one()
    assert filas == 1, "con Redis caido el mensaje no llego a guardarse"

    # Y el reenvio, con Redis todavia caido: lo atrapa la base.
    async with puerta(motor, caido, inquilino, canal=CANAL, id_externo=externo) as (segunda, _):
        assert segunda.veredicto is Veredicto.DUPLICADO
        assert segunda.guardian is Guardian.BASE

    # Control del instrumento: este Redis SI falla al escribir.
    assert await recordar(caido, "heraldo:idem:control") is False
    # Y el de verdad, no.
    assert await recordar(redis, f"{PREFIJO}:control") is True


# --------------------------------------------------------------------------
# La clave, y su aislamiento
# --------------------------------------------------------------------------
def test_la_clave_de_redis_lleva_el_inquilino_dentro(inquilino) -> None:
    clave = clave_de(inquilino, canal=CANAL, id_externo=EXTERNO)
    assert clave.startswith(f"{PREFIJO}:")
    assert str(AGENCIA_A) in clave and str(CLIENTE_A1) in clave
    otra = clave_de(sesion_de_cliente(AGENCIA_A, CLIENTE_A2), canal=CANAL, id_externo=EXTERNO)
    assert clave != otra, (
        "dos inquilinos distintos producen la MISMA clave de Redis: el mensaje de uno "
        "se descartaria como duplicado del otro"
    )


async def test_el_registro_de_mensajes_no_se_puede_reescribir(motor, inquilino) -> None:
    """RF-12 tambien por permiso: reescribir esta tabla es reescribir que llego."""
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await reservar(conexion, inquilino, canal=CANAL, id_externo=EXTERNO)

    with pytest.raises(DBAPIError) as capturado:
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            await conexion.execute(
                text("DELETE FROM mensajes_entrantes WHERE id_externo = :e"), {"e": EXTERNO}
            )
    assert "permission denied" in str(capturado.value).lower(), (
        "la aplicacion puede BORRAR un registro de idempotencia: con eso, volver a "
        "procesar un mensaje ya procesado es una linea de codigo"
    )
