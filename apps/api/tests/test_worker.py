"""T-020 — el bucle del worker: reclama, ejecuta, cierra y hace su mantenimiento.

# WHY (el manejador es una funcion de la prueba): lo que se mide aqui no es lo que
# el worker GENERA —eso no existe todavia— sino que el ciclo de vida de un trabajo
# llegue a su estado final pase lo que pase con el manejador: que termine, que
# lance, o que lance tantas veces como intentos le queden.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.tenancy import sesion_de_inquilino
from conftest import AGENCIA_A, CLIENTE_A1, resembrar, sesion_de_cliente
from worker import bucle, cola

AHORA = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    resembrar(motor_de_siembra)


@pytest.fixture
def inquilino():
    return sesion_de_cliente(AGENCIA_A, CLIENTE_A1)


async def _vaciar(motor, quien) -> None:
    async with sesion_de_inquilino(motor, quien) as conexion:
        await conexion.execute(text("DELETE FROM trabajos"))
        await conexion.execute(text("DELETE FROM trabajos_archivados"))


async def test_un_trabajo_que_sale_bien_acaba_hecho(motor, motor_admin, inquilino) -> None:
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="ok", disponible_en=AHORA - timedelta(hours=1)
        )

    vistos: list[str] = []

    async def manejador(trabajo: cola.Trabajo) -> None:
        vistos.append(trabajo.tipo)

    estado = await bucle.procesar_uno(motor, inquilino, manejador, ahora=AHORA)
    assert estado is cola.Estado.HECHO
    assert vistos == ["ok"], "el manejador no llego a ver el trabajo"

    with motor_admin.connect() as conexion:
        fila = conexion.execute(
            text("SELECT estado, terminado_en FROM trabajos WHERE id = :id"),
            {"id": identificador},
        ).one()
    assert fila.estado == "hecho" and fila.terminado_en is not None


async def test_un_manejador_que_lanza_deja_el_trabajo_para_reintentar(
    motor, motor_admin, inquilino
) -> None:
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        identificador = await cola.encolar(
            conexion, inquilino, tipo="falla", disponible_en=AHORA - timedelta(hours=1)
        )

    async def manejador(_trabajo: cola.Trabajo) -> None:
        raise TimeoutError("el proveedor no contesto")

    estado = await bucle.procesar_uno(motor, inquilino, manejador, ahora=AHORA)
    assert estado is cola.Estado.PENDIENTE

    with motor_admin.connect() as conexion:
        fila = conexion.execute(
            text("SELECT estado, disponible_en, ultimo_error FROM trabajos WHERE id = :id"),
            {"id": identificador},
        ).one()
    assert fila.estado == "pendiente"
    assert fila.disponible_en > AHORA, "el reintento se programo para el pasado"
    assert fila.ultimo_error.startswith("TimeoutError"), (
        f"el error guardado no dice que paso: {fila.ultimo_error!r}"
    )


async def test_el_bucle_procesa_lo_que_hay_y_para_cuando_se_le_pide(motor, inquilino) -> None:
    """El bucle sale por su propio pie, sin matar a nadie a mitad de un trabajo."""
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        for indice in range(3):
            await cola.encolar(
                conexion,
                inquilino,
                tipo=f"t{indice}",
                disponible_en=AHORA - timedelta(hours=1),
            )

    parar = asyncio.Event()
    hechos: list[str] = []

    async def manejador(trabajo: cola.Trabajo) -> None:
        hechos.append(trabajo.tipo)
        if len(hechos) == 3:
            parar.set()

    await asyncio.wait_for(
        bucle.bucle(
            motor,
            inquilino,
            manejador,
            parar=parar,
            reloj=lambda: AHORA,
            pausa=timedelta(milliseconds=10),
            # Muy grande: el mantenimiento no entra en esta sonda, que mide el ciclo.
            cada_cuanto=timedelta(days=365),
        ),
        timeout=20,
    )
    assert sorted(hechos) == ["t0", "t1", "t2"]


async def test_el_mantenimiento_rescata_archiva_y_purga_en_una_pasada(
    motor, motor_admin, inquilino
) -> None:
    """Las tres, y en ese orden. Se comprueba el EFECTO de cada una.

    # WHY (el orden importa): si la purga corriera antes que el archivado,
    # trabajaria sobre un archivo al que todavia le faltan las filas que el
    # archivado va a meter — y esas se quedarian una ronda entera de mas.
    """
    await _vaciar(motor, inquilino)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        # (a) uno abandonado hace mucho -> se rescata
        await cola.encolar(
            conexion, inquilino, tipo="abandonado", disponible_en=AHORA - timedelta(days=10)
        )
        await cola.reclamar(conexion, ahora=AHORA - timedelta(days=10))
        # (b) uno hecho hace dias -> se archiva
        hecho = await cola.encolar(
            conexion, inquilino, tipo="hecho", disponible_en=AHORA - timedelta(days=10)
        )
        await cola.reclamar(conexion, ahora=AHORA - timedelta(days=10))
        await cola.completar(conexion, hecho, ahora=AHORA - timedelta(days=10))

    resultado = await bucle.mantenimiento(motor, inquilino, ahora=AHORA)

    assert resultado.rescatados == 1, "el trabajo abandonado no volvio a la cola"
    assert resultado.archivados == 1, "el trabajo hecho no salio de la tabla caliente"
    assert resultado.purgados == 0, (
        "el mantenimiento purgo sin que nadie se lo pidiera: la purga DESTRUYE y "
        "RNF-06 no admite destruccion desatendida de datos de un cliente (P-31)"
    )

    with motor_admin.connect() as conexion:
        calientes = (
            conexion.execute(
                text("SELECT estado FROM trabajos WHERE cliente_id = :c"), {"c": CLIENTE_A1}
            )
            .scalars()
            .all()
        )
        archivados = conexion.execute(
            text("SELECT count(*) FROM trabajos_archivados WHERE cliente_id = :c"),
            {"c": CLIENTE_A1},
        ).scalar_one()
    assert calientes == ["pendiente"]
    assert archivados == 1

    # Y una segunda pasada MUCHO despues, PIDIENDO la purga: el archivo no crece
    # para siempre — pero solo cuando alguien lo pide. Es el control de la asercion
    # de arriba: sin el, un `purgar` roto tambien daria `purgados == 0`.
    mucho_despues = AHORA + timedelta(days=60)
    sin_pedirla = await bucle.mantenimiento(motor, inquilino, ahora=mucho_despues)
    assert sin_pedirla.purgados == 0, (
        "el archivo caducado se purgo solo: la purga desatendida es justo lo que "
        "P-31 dejo apagado hasta que exista la politica de retencion (T-213)"
    )
    segunda = await bucle.mantenimiento(motor, inquilino, ahora=mucho_despues, purgar=True)
    assert segunda.purgados == 1
    with motor_admin.connect() as conexion:
        quedan = conexion.execute(
            text("SELECT count(*) FROM trabajos_archivados WHERE cliente_id = :c"),
            {"c": CLIENTE_A1},
        ).scalar_one()
    assert quedan == 0
