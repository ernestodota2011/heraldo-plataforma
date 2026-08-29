"""T-021 (RF-12) — un mensaje externo produce EXACTAMENTE un procesamiento.

Hay **dos** defensas, y no son intercambiables:

- **La clave en Redis** es el ACELERADOR. Gana cuando el mismo aviso llega por
  tercera, cuarta y quinta vez: lo descarta sin tocar la base. Deja de servir en
  cuanto Redis se reinicia y su memoria desaparece.
- **La restriccion unica en la base** es la AUTORIDAD. Gana siempre, tambien con
  Redis vacio o en llamas. No hay ningun caso en el que no sirva: es la que no
  miente.

# WHY (las dos, y en este orden): con solo Redis, un reinicio del servidor de
# estado —o un desalojo por memoria, o un `FLUSHALL` de alguien con prisa— deja
# pasar el duplicado, y ese duplicado es un mensaje enviado dos veces a una
# persona real por cuenta de un cliente. Con solo la base, cada reenvio de la
# plataforma del canal —que reenvia con ganas— es una transaccion completa. Redis
# quita el 99 % del trabajo; la restriccion unica sostiene el 100 % de la promesa.
#
# # WHY (==Redis NUNCA se escribe antes de que la base confirme==): esta es la
# trampa fina de este diseño, y es la que convierte una idempotencia en una
# perdida de mensajes. Si la marca de Redis se pusiera al RESERVAR —dentro de la
# transaccion— y esa transaccion se deshiciera despues (un fallo al encolar, un
# error de validacion, una caida), quedaria una marca en Redis diciendo «este
# mensaje ya se vio» y NINGUNA fila en la base. El siguiente reenvio se
# descartaria por la marca y el mensaje se perderia **para siempre**, en silencio.
# Por eso `reservar` no toca Redis: la marca la escribe `recordar`, y `puerta`
# —el camino recomendado— la escribe DESPUES de que la transaccion se confirme.
# `test_una_transaccion_deshecha_no_deja_marca_en_redis` es esa medida.
#
# # WHY (la clave lleva el inquilino dentro, en Redis y en la base): dos
# inquilinos pueden recibir el mismo identificador externo — los numera la
# plataforma del canal, no nosotros. Sin el inquilino en la clave, el mensaje del
# segundo se descartaria como «duplicado» del primero: una fuga y una perdida a la
# vez.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy import text

from app.tenancy import sesion_de_inquilino
from app.tenancy.inquilino import Inquilino

#: Prefijo de toda clave de idempotencia en Redis. Vive aqui, en un solo sitio:
#: la limpieza de las pruebas lo DERIVA de esta constante en vez de escribirlo.
PREFIJO = "heraldo:idem"

#: Cuanto vive la marca. Larga —una plataforma de mensajeria puede reenviar
#: durante dias— pero no eterna: una clave sin vencimiento convierte a Redis en un
#: almacen que solo crece, que es el mismo defecto que R-07 en otro sitio.
VIGENCIA = timedelta(days=7)

_RESERVAR = text(
    "INSERT INTO mensajes_entrantes (agencia_id, cliente_id, canal, id_externo, trabajo_id) "
    "VALUES (:agencia_id, :cliente_id, :canal, :id_externo, :trabajo_id) "
    "ON CONFLICT (agencia_id, cliente_id, canal, id_externo) DO NOTHING "
    "RETURNING id"
)


class Veredicto(StrEnum):
    """Lo que la puerta decidio sobre este mensaje."""

    NUEVO = "nuevo"
    DUPLICADO = "duplicado"


class Guardian(StrEnum):
    """CUAL de las dos defensas atrapo el duplicado. Se mide, no se supone."""

    REDIS = "redis"
    BASE = "base"


@dataclass(frozen=True, slots=True)
class Reserva:
    """El veredicto, con quien lo dio y —si es nuevo— el identificador de la fila."""

    veredicto: Veredicto
    guardian: Guardian | None
    mensaje_id: UUID | None
    clave: str

    @property
    def es_nuevo(self) -> bool:
        return self.veredicto is Veredicto.NUEVO


def clave_de(inquilino: Inquilino, *, canal: str, id_externo: str) -> str:
    """La clave de Redis. Lleva el inquilino DENTRO, no como contexto."""
    return f"{PREFIJO}:{inquilino.agencia_id}:{inquilino.cliente_id}:{canal}:{id_externo}"


async def ya_visto(redis, clave: str) -> bool:
    """El camino rapido. Un `False` aqui no afirma nada: dice «pregunta a la base»."""
    return bool(await redis.exists(clave))


async def recordar(redis, clave: str, *, vigencia: timedelta = VIGENCIA) -> bool:
    """Escribe la marca. ==Solo se llama cuando la base YA confirmo el hecho.==

    Devuelve si se pudo escribir. Un `False` NO es un fallo del procesamiento.

    # WHY: Redis aqui es una CACHE de un hecho que ya esta en la base. Nunca una
    # reserva, nunca una promesa, nunca un «lo estoy intentando». Todo lo que puede
    # pasar si esta marca se pierde es una consulta de mas; todo lo que puede pasar
    # si esta marca existe sin su fila es un mensaje perdido para siempre.
    #
    # # WHY (un fallo de Redis aqui NO sube): lo senalo la revision cruzada, y su
    # diagnostico —«se pierde el mensaje»— era erroneo: para cuando se llega aqui la
    # transaccion YA se confirmo, o sea el mensaje esta guardado y su trabajo
    # encolado. Pero apuntaba a algo real: si la excepcion subiera, un Redis caido
    # convertiria una peticion YA PROCESADA en un error, la plataforma del canal
    # reenviaria y el reenvio volveria a dar 500 mientras Redis siguiera caido. Que
    # el acelerador pueda tumbar al camino principal es exactamente al reves.
    #
    # # WHY (esto NO es un `fail-open` que se traga un guard): la defensa que
    # sostiene RF-12 es la restriccion unica de la base, y sigue intacta. Sin la
    # marca, el proximo reenvio paga una transaccion de mas y la base lo rechaza
    # igual. Se atrapa `RedisError` —lo que Redis puede fallar— y NO `Exception`:
    # un `TypeError` en esta funcion es un defecto nuestro y tiene que salir.
    """
    try:
        await redis.set(clave, "1", ex=int(vigencia.total_seconds()))
    except RedisError:
        return False
    return True


async def reservar(
    conexion,
    inquilino: Inquilino,
    *,
    canal: str,
    id_externo: str,
    trabajo_id: UUID | None = None,
) -> Reserva:
    """La defensa AUTORITATIVA: intenta la fila y deja que la base decida.

    No toca Redis — ni para leer ni para escribir. Se llama DENTRO de la
    transaccion que tambien guarda el mensaje y encola su trabajo (D-04).
    """
    fila = (
        await conexion.execute(
            _RESERVAR,
            {
                "agencia_id": inquilino.agencia_id,
                "cliente_id": inquilino.cliente_id,
                "canal": canal,
                "id_externo": id_externo,
                "trabajo_id": trabajo_id,
            },
        )
    ).one_or_none()
    clave = clave_de(inquilino, canal=canal, id_externo=id_externo)
    if fila is None:
        return Reserva(Veredicto.DUPLICADO, Guardian.BASE, None, clave)
    return Reserva(Veredicto.NUEVO, None, fila.id, clave)


@asynccontextmanager
async def puerta(
    motor,
    redis,
    inquilino: Inquilino,
    *,
    canal: str,
    id_externo: str,
    vigencia: timedelta = VIGENCIA,
) -> AsyncIterator[tuple[Reserva, object | None]]:
    """El camino recomendado: las dos defensas, en el orden que no pierde mensajes.

    Entrega `(reserva, conexion)`. Cuando la reserva es NUEVA, la conexion es la
    transaccion abierta: ahi dentro se guarda el mensaje y se encola su trabajo, en
    la MISMA transaccion que la reserva. Cuando es duplicada, la conexion es `None`
    porque no hay nada que hacer.

    La marca de Redis se escribe **al salir del bloque sin error**, o sea despues
    de que la transaccion se confirme. Si el bloque lanza, la transaccion se
    deshace y Redis se queda como estaba: el mensaje podra volver a entrar.
    """
    clave = clave_de(inquilino, canal=canal, id_externo=id_externo)
    if await ya_visto(redis, clave):
        yield Reserva(Veredicto.DUPLICADO, Guardian.REDIS, None, clave), None
        return

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        reserva = await reservar(conexion, inquilino, canal=canal, id_externo=id_externo)
        yield reserva, (conexion if reserva.es_nuevo else None)

    # Fuera del `async with`: la transaccion ya se confirmo. Ahora, y no antes.
    await recordar(redis, clave, vigencia=vigencia)
