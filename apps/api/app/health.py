"""Dos sondas que NO son la misma sonda escrita dos veces (T-024, RF-51).

# WHY: `vivacidad` contesta «el proceso esta en pie». `disponibilidad` contesta
# «puedo recibir trafico», y eso incluye a sus dependencias. Son preguntas
# DISTINTAS y el orquestador hace cosas DISTINTAS con cada respuesta: la primera
# gobierna el REINICIO del contenedor, la segunda gobierna si el balanceador le
# manda peticiones.
#
# Si las dos contestaran lo mismo, el fallo no seria cosmetico. Con la base
# caida, una vivacidad que tambien mira la base devuelve fallo -> el orquestador
# REINICIA el proceso -> al arrancar la base sigue caida -> vuelve a reiniciar.
# El bucle de reinicio destruye el trabajo en curso y borra los registros del
# proceso justo cuando hacen falta para entender el incidente. Por eso
# `vivacidad` no toca la base: **no sabe** que existe.
#
# WHY (el limite, declarado): `disponibilidad` prueba que la aplicacion puede
# abrir una conexion con su propio rol y que el motor contesta. NO comprueba que
# las migraciones esten al dia, ni la cola, ni Redis — esas dependencias todavia
# no existen en el producto. Cuando existan, se anaden AQUI, no en `vivacidad`.
#
# WHY (el tiempo limite): una sonda de disponibilidad que se QUEDA COLGADA es
# peor que una que falla. El orquestador se queda esperando y el proceso ni entra
# ni sale de rotacion. La comprobacion lleva su propio techo de tiempo y, al
# agotarse, responde «no disponible» — que es una respuesta, no un silencio.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: Rutas. Se exportan como constantes para que la sonda del orquestador y la
#: prueba que la mide nombren LA MISMA ruta: un test que golpea una ruta que
#: nadie despliega es teatro.
RUTA_VIVACIDAD = "/salud/vivacidad"
RUTA_DISPONIBILIDAD = "/salud/disponibilidad"

#: Techo de tiempo de la comprobacion de dependencias, en segundos.
TIEMPO_LIMITE_SEGUNDOS = 2.0

#: La UNICA consulta que hace la sonda de disponibilidad. No nombra ninguna tabla
#: de inquilino a proposito: la sonda no tiene inquilino declarado, asi que
#: cualquier tabla bajo RLS abortaria (RF-03) y la sonda estaria midiendo el
#: mecanismo de aislamiento en vez de la disponibilidad del motor.
_LATIDO = text("SELECT 1")


async def vivacidad() -> JSONResponse:
    """El proceso esta en pie. Y eso es TODO lo que afirma.

    No recibe el motor, no lo importa y no puede alcanzarlo: la separacion de
    RF-51 es estructural, no una promesa del docstring. Si esta funcion algun dia
    necesitara un parametro, la firma cambiaria y el guard estructural de la
    suite se pondria en rojo.
    """
    return JSONResponse({"estado": "vivo"})


async def _disponibilidad(motor: AsyncEngine, tiempo_limite: float) -> JSONResponse:
    """Puedo recibir trafico: mis dependencias contestan.

    Devuelve 503 cuando NO puede atender. El codigo importa tanto como el cuerpo:
    un 200 con `{"estado": "no disponible"}` deja al balanceador mandando trafico
    a un proceso que no puede atenderlo.
    """
    detalle: dict[str, Any] = {}
    try:
        async with asyncio.timeout(tiempo_limite):
            async with motor.connect() as conexion:
                await conexion.execute(_LATIDO)
    except TimeoutError:
        detalle["base"] = f"sin respuesta en {tiempo_limite} s"
    except Exception as fallo:  # noqa: BLE001 - cualquier fallo = no disponible
        # WHY: se captura ancho a proposito. La pregunta es «¿puedo atender?», y
        # la respuesta es «no» para CUALQUIER motivo por el que la base no
        # conteste. Una lista de excepciones esperadas seria una denylist, y la
        # que faltara se convertiria en un 500 en vez de en un 503.
        detalle["base"] = type(fallo).__name__
    else:
        return JSONResponse({"estado": "disponible", "dependencias": {"base": "ok"}})

    # WHY: el motivo se nombra por su TIPO, nunca con el texto del fallo. El
    # mensaje de un error de conexion lleva el maquinista, el host y a veces el
    # usuario del DSN; esta ruta no lleva autenticacion.
    return JSONResponse(
        {"estado": "no disponible", "dependencias": detalle},
        status_code=503,
    )


def crear_router_de_salud(
    *,
    motor: AsyncEngine,
    tiempo_limite: float = TIEMPO_LIMITE_SEGUNDOS,
) -> APIRouter:
    """Monta las dos sondas. `vivacidad` entra tal cual: sin cierre sobre el motor."""
    router = APIRouter(tags=["salud"])
    router.add_api_route(RUTA_VIVACIDAD, vivacidad, methods=["GET"], name="vivacidad")

    async def disponibilidad() -> JSONResponse:
        return await _disponibilidad(motor, tiempo_limite)

    router.add_api_route(
        RUTA_DISPONIBILIDAD, disponibilidad, methods=["GET"], name="disponibilidad"
    )
    return router
