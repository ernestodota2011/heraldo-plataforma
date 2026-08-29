"""Una INSTANCIA de la API, en su propio proceso del sistema operativo (T-014·quater).

Este archivo no es una prueba: es el programa que `test_multiproceso.py` arranca
**dos veces** para medir RNF-03 —«ningun estado compartido vive en memoria de
proceso»— contra dos procesos de verdad.

# WHY (por que hacia falta): la suite ya media el estado compartido con dos
# CLIENTES de Redis dentro de un mismo interprete, y quien lo construyo lo dijo
# con todas las letras: «"dos procesos" son dos conexiones Redis independientes en
# un proceso: mide el estado compartido, **no** mide GIL, ni pool bajo carga, ni
# reconexion». Aqui se cierra ese hueco. Cada instancia tiene su propio
# interprete, su propio GIL, su propio pool de conexiones a Postgres, su propio
# cliente de Redis y su propia memoria: un `dict` de modulo de una NO existe en la
# otra, que es exactamente la propiedad que el defecto 6 del referente rompia.
#
# # WHY (un programa con ordenes por linea, y no dos servidores HTTP): la API
# todavia no expone ninguna ruta que ejercite el limite, la idempotencia ni la
# sesion — esas tres piezas existen como modulos y su cableado HTTP no esta
# construido. Levantar dos `uvicorn` mediria dos procesos sirviendo `/salud`, que
# no es lo que RNF-03 promete. Aqui cada proceso ejecuta EL MISMO codigo de
# produccion (`app.tenancy.limits`, `app.tenancy.auth`, `app.channels.idempotency`)
# que ejecutaria atendiendo una peticion. ==Lo que esto NO mide, dicho en voz
# alta: la pila HTTP.== El dia que esas tres piezas tengan ruta, la sonda tiene
# que apuntar a la ruta.
#
# # WHY (el SABOTEADOR vive aqui, en el andamiaje, y JAMAS en la aplicacion):
# `HERALDO_SABOTAJE_MEMORIA` cambia una de las tres piezas por una version que
# guarda su estado en un `dict` de modulo — el defecto que D-05 descarta. Sirve
# para lo unico que hace que una sonda valga: comprobar que se pone ROJA cuando lo
# que mide se rompe (`feedback_sabotaje_audita_al_test`). Que este interruptor
# viviera en `apps/api/app` seria una puerta trasera en produccion, y por eso
# `test_multiproceso.py` comprueba que no aparece ahi.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.channels.idempotency import puerta
from app.tenancy import crear_motor, sesion_de_inquilino
from app.tenancy.auth import AlmacenDeSesiones, Rol, Sesion, SesionInvalida
from app.tenancy.inquilino import Inquilino
from app.tenancy.limits import Direccion, LimitadorCompartido, Limite

#: DSN del rol de aplicacion y del Redis. Los declara quien arranca el proceso,
#: igual que en un despliegue: aqui no hay ningun valor por defecto.
VARIABLE_DSN = "HERALDO_DATABASE_URL"
VARIABLE_REDIS = "HERALDO_REDIS_URL"

#: Prefijo del banco de pruebas, para que dos corridas no se pisen en Redis.
VARIABLE_PREFIJO = "HERALDO_PREFIJO_DE_PRUEBA"

#: El interruptor del saboteador. Ver el WHY de la cabecera.
VARIABLE_SABOTAJE = "HERALDO_SABOTAJE_MEMORIA"

SABOTAJES = ("limite", "sesion", "idempotencia")


# --------------------------------------------------------------------------
# Los tres saboteadores: el defecto 6, escrito a proposito
# --------------------------------------------------------------------------
#: El estado que NO deberia estar aqui. Un `dict` de modulo es privado del
#: proceso: la instancia de al lado no lo ve, no lo hereda y no se entera.
_CUBOS_EN_MEMORIA: dict[str, int] = {}
_SESIONES_EN_MEMORIA: dict[str, dict[str, Any]] = {}
_MENSAJES_EN_MEMORIA: set[str] = set()


class LimitadorEnMemoriaDelProceso:
    """El limitador que D-05 descarta: cada worker lleva su propia cuenta."""

    def __init__(self, prefijo: str) -> None:
        self._prefijo = prefijo

    async def consumir(
        self, limite: Limite, inquilino: Inquilino, direccion: Direccion
    ) -> dict[str, Any]:
        clave = f"{self._prefijo}:{limite.nombre}:{inquilino.agencia_id}:{direccion.canonica}"
        _CUBOS_EN_MEMORIA[clave] = _CUBOS_EN_MEMORIA.get(clave, 0) + 1
        consumido = _CUBOS_EN_MEMORIA[clave]
        return {"permitido": consumido <= limite.cuota, "consumido": consumido}


class AlmacenEnMemoriaDelProceso:
    """La sesion que D-05 descarta: «revocada» significa «revocada aqui»."""

    async def abrir(
        self, *, agencia_id: UUID, cliente_id: UUID | None, rol: Rol
    ) -> tuple[str, Sesion]:
        sesion_id = os.urandom(8).hex()
        _SESIONES_EN_MEMORIA[sesion_id] = {
            "agencia_id": agencia_id,
            "cliente_id": cliente_id,
            "rol": rol,
        }
        return f"{sesion_id}.secreto", Sesion(
            sesion_id=sesion_id, agencia_id=agencia_id, cliente_id=cliente_id, rol=rol
        )

    async def usar(self, testigo: str) -> Sesion:
        sesion_id, _, _ = testigo.partition(".")
        guardada = _SESIONES_EN_MEMORIA.get(sesion_id)
        if guardada is None:
            raise SesionInvalida("no la conozco: mi memoria no es la del vecino")
        return Sesion(sesion_id=sesion_id, **guardada)

    async def revocar(self, sesion_id: str) -> bool:
        return _SESIONES_EN_MEMORIA.pop(sesion_id, None) is not None


# --------------------------------------------------------------------------
# El estado de la instancia
# --------------------------------------------------------------------------
class Instancia:
    """Lo que un proceso de la API tendria montado para atender."""

    def __init__(self) -> None:
        self.sabotaje = os.environ.get(VARIABLE_SABOTAJE) or None
        if self.sabotaje is not None and self.sabotaje not in SABOTAJES:
            raise SystemExit(
                f"{VARIABLE_SABOTAJE}={self.sabotaje!r} no es ninguno de {list(SABOTAJES)}"
            )
        self.prefijo = os.environ[VARIABLE_PREFIJO]

        from redis.asyncio import Redis

        self.redis = Redis.from_url(os.environ[VARIABLE_REDIS], decode_responses=True)
        self.motor = crear_motor(os.environ[VARIABLE_DSN])

        self.limitador = (
            LimitadorEnMemoriaDelProceso(self.prefijo)
            if self.sabotaje == "limite"
            else LimitadorCompartido(self.redis, prefijo=self.prefijo)
        )
        self.sesiones = (
            AlmacenEnMemoriaDelProceso()
            if self.sabotaje == "sesion"
            else AlmacenDeSesiones(self.redis, prefijo=self.prefijo)
        )

    async def cerrar(self) -> None:
        await self.motor.dispose()
        await self.redis.aclose()


def _inquilino(orden: dict[str, Any]) -> Inquilino:
    crudo = orden.get("cliente_id")
    return Inquilino.desde_usuario(
        agencia_id=UUID(orden["agencia_id"]),
        cliente_id=None if crudo is None else UUID(crudo),
    )


# --------------------------------------------------------------------------
# Las ordenes
# --------------------------------------------------------------------------
async def _identidad(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    return {"pid": os.getpid(), "sabotaje": instancia.sabotaje}


async def _consumir(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    limite = Limite(
        nombre=orden["limite"], cuota=orden["cuota"], ventana_segundos=orden["ventana"]
    )
    veredicto = await instancia.limitador.consumir(
        limite, _inquilino(orden), Direccion.desde_texto(orden["direccion"])
    )
    if isinstance(veredicto, dict):  # el saboteador ya devuelve la forma serializable
        return veredicto
    return {"permitido": veredicto.permitido, "consumido": veredicto.consumido}


async def _abrir_sesion(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    crudo = orden.get("cliente_id")
    testigo, sesion = await instancia.sesiones.abrir(
        agencia_id=UUID(orden["agencia_id"]),
        cliente_id=None if crudo is None else UUID(crudo),
        rol=Rol(orden["rol"]),
    )
    return {"testigo": testigo, "sesion_id": sesion.sesion_id}


async def _usar_sesion(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    try:
        sesion = await instancia.sesiones.usar(orden["testigo"])
    except SesionInvalida as fallo:
        return {"valida": False, "motivo": str(fallo)}
    return {"valida": True, "sesion_id": sesion.sesion_id, "rol": sesion.rol.value}


async def _revocar_sesion(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    return {"borrada": bool(await instancia.sesiones.revocar(orden["sesion_id"]))}


async def _ingerir(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    """Un mensaje externo entra por esta instancia. Debe producir UN procesamiento."""
    inquilino = _inquilino(orden)
    canal, id_externo = orden["canal"], orden["id_externo"]

    if instancia.sabotaje == "idempotencia":
        # La idempotencia «obvia»: un conjunto en memoria del proceso. Ni toca la
        # base ni pregunta a nadie — el mismo aviso entra dos veces si lo reciben
        # dos instancias distintas.
        clave = f"{inquilino.agencia_id}:{inquilino.cliente_id}:{canal}:{id_externo}"
        if clave in _MENSAJES_EN_MEMORIA:
            return {"veredicto": "duplicado", "guardian": "memoria", "mensaje_id": None}
        _MENSAJES_EN_MEMORIA.add(clave)
        return {"veredicto": "nuevo", "guardian": None, "mensaje_id": None}

    async with puerta(
        instancia.motor, instancia.redis, inquilino, canal=canal, id_externo=id_externo
    ) as (reserva, _conexion):
        respuesta = {
            "veredicto": reserva.veredicto.value,
            "guardian": None if reserva.guardian is None else reserva.guardian.value,
            "mensaje_id": None if reserva.mensaje_id is None else str(reserva.mensaje_id),
        }
    return respuesta


async def _contar_mensajes(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    """Cuantas filas hay para ese identificador externo, vistas por ESTA instancia."""
    async with sesion_de_inquilino(instancia.motor, _inquilino(orden)) as conexion:
        cuantas = (
            await conexion.execute(
                text(
                    "SELECT count(*) FROM mensajes_entrantes "
                    "WHERE canal = :canal AND id_externo = :externo"
                ),
                {"canal": orden["canal"], "externo": orden["id_externo"]},
            )
        ).scalar_one()
    return {"filas": int(cuantas)}


ORDENES = {
    "identidad": _identidad,
    "consumir": _consumir,
    "abrir_sesion": _abrir_sesion,
    "usar_sesion": _usar_sesion,
    "revocar_sesion": _revocar_sesion,
    "ingerir": _ingerir,
    "contar_mensajes": _contar_mensajes,
}


async def _atender(instancia: Instancia, orden: dict[str, Any]) -> dict[str, Any]:
    # WHY (la barrera por reloj de pared): para que «a la vez» sea a la vez. Las
    # dos instancias corren en la misma maquina y comparten el reloj, asi que una
    # marca de tiempo comun las hace arrancar juntas de verdad. El veredicto de la
    # sonda NO depende de que la carrera sea apretada —«exactamente uno» vale igual
    # si llegan separados—; la barrera solo garantiza que la carrera EXISTE.
    espera = orden.get("no_antes_de")
    if espera is not None:
        await asyncio.sleep(max(0.0, float(espera) - time.time()))
    return await ORDENES[orden["orden"]](instancia, orden)


async def _bucle() -> None:
    instancia = Instancia()
    lazo = asyncio.get_running_loop()
    try:
        while True:
            linea = await lazo.run_in_executor(None, sys.stdin.readline)
            if not linea:
                return
            orden = json.loads(linea)
            if orden["orden"] == "fin":
                return
            try:
                respuesta = await _atender(instancia, orden)
                respuesta["ok"] = True
            except Exception as fallo:  # noqa: BLE001 - se reporta, no se traga
                # WHY: un fallo se DEVUELVE como respuesta en vez de matar el
                # proceso. Si el hijo muriera, la prueba se quedaria esperando una
                # linea que no llega y fallaria por plazo agotado — un mensaje que
                # no dice nada. Asi el reproche llega con el error dentro.
                respuesta = {"ok": False, "error": f"{type(fallo).__name__}: {fallo}"}
            sys.stdout.write(json.dumps(respuesta) + "\n")
            sys.stdout.flush()
    finally:
        await instancia.cerrar()


def main() -> None:
    if sys.platform == "win32":
        # La misma razon que el hook de `conftest.py`: psycopg no funciona sobre el
        # bucle Proactor. El hijo es OTRO proceso, asi que el hook del padre no le
        # alcanza y tiene que declararlo por su cuenta.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_bucle())


if __name__ == "__main__":
    main()
