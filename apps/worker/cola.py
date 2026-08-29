"""T-020 (RF-11, RF-14, D-04) — la cola en Postgres, con su archivo y su purga.

Cuatro cosas, y las cuatro por el mismo motivo: que nada dependa de que un
proceso siga vivo.

1. **Encolar en la MISMA transaccion** que guarda el mensaje. Ese es el punto de
   D-04, no la elegancia: con un broker aparte hay dos sitios que pueden
   discrepar —el mensaje guardado y el trabajo no encolado— y el trabajo perdido
   deja de ser imposible. Aqui `encolar` RECIBE la conexion, nunca la abre.
2. **Reclamar con `FOR UPDATE SKIP LOCKED`**: dos workers pueden tirar de la
   misma cola sin bloquearse y sin coger el mismo trabajo.
3. **Reintentar con espera creciente**, y la espera vive EN LA FILA
   (`disponible_en`), no en un temporizador del proceso. Si el worker muere entre
   el fallo y el reintento, la espera sigue siendo la misma cuando otro lo recoja.
4. **Archivar y purgar desde el dia 1** (mitigacion de R-07). No es limpieza
   opcional: si se deja para cuando haga falta, para entonces la tabla de cola YA
   es el cuello y el barrido que habria que correr es el que no se puede correr
   sin bloquear el camino caliente.

# WHY (un fallido NO se archiva solo): `archivar` mueve los trabajos `hecho`. Un
# `fallido` se queda en la tabla caliente hasta que una persona lo mire, porque
# RF-14 pide justo lo contrario de esconderlo: «dejarlo VISIBLE como fallido para
# un humano». Archivarlo por antiguedad seria hacerlo desaparecer de la vista con
# otro nombre. Que un fallido se archive es un acto explicito
# (`archivar(..., incluir_fallidos=True)`), y quien lo pida esta diciendo que ya
# lo miro.
#
# WHY (todo pasa por `sesion_de_inquilino`): este modulo no abre conexiones y no
# declara variables de sesion. Recibe una conexion que ya nacio con su inquilino
# declarado, asi que la politica de RLS decide que filas ve — un worker con la
# sesion del inquilino A no puede reclamar, archivar ni purgar nada de B ni
# aunque su SQL no lleve un solo `WHERE`. Lo mide el endpoint-trampa.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text


class Estado(StrEnum):
    """Los cuatro estados de un trabajo. El CHECK de la tabla dice lo mismo."""

    PENDIENTE = "pendiente"
    EN_CURSO = "en_curso"
    HECHO = "hecho"
    FALLIDO = "fallido"


#: Estados terminales: un trabajo que llego aqui no se vuelve a ejecutar.
ESTADOS_TERMINALES = (Estado.HECHO, Estado.FALLIDO)

#: Espera base del primer reintento y tope al que la progresion deja de crecer.
#:
#: # WHY (progresion determinista, sin ruido aleatorio): la espera es
#: `base * 2^(intentos-1)`, recortada al tope. Sin ruido a proposito: el ruido
#: sirve para separar a MUCHOS workers que fallaron a la vez contra el mismo
#: servicio de fuera, y hoy no hay muchos; anadirlo ahora seria una perilla sin
#: medir que ademas volveria esta funcion no reproducible en las pruebas. Cuando
#: haya varios workers se anade CON su medida, no antes.
ESPERA_BASE = timedelta(seconds=5)
ESPERA_MAXIMA = timedelta(minutes=30)

#: Cuanto se queda un trabajo terminado en la tabla caliente antes de irse al
#: archivo, y cuanto se queda en el archivo antes de desaparecer.
RETENCION_EN_CALIENTE = timedelta(days=1)
RETENCION_EN_ARCHIVO = timedelta(days=30)

#: Cuanto puede estar un trabajo `en_curso` antes de darlo por abandonado. Es el
#: otro modo de fallo de R-03: el worker no muere del todo, muere a medias — se
#: lleva el trabajo y no vuelve. Sin esto, ese trabajo no lo recoge nadie NUNCA.
PLAZO_DE_ABANDONO = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class Trabajo:
    """Un trabajo reclamado. Inmutable: lo que cambia se escribe en la base."""

    id: UUID
    agencia_id: UUID
    cliente_id: UUID
    tipo: str
    carga: Mapping[str, Any]
    intentos: int
    maximo_intentos: int
    creado_en: datetime

    @property
    def es_ultimo_intento(self) -> bool:
        return self.intentos >= self.maximo_intentos


def _duplicaciones_hasta_el_tope(base: timedelta, tope: timedelta) -> int:
    """Cuantas veces hay que doblar `base` para llegar al tope. Ni una mas.

    # WHY (P-26): la primera version calculaba `base * 2 ** (intentos - 1)` y
    # DESPUES recortaba al tope. Con un numero de intentos alto eso desborda antes
    # de recortar nada —`OverflowError: Python int too large to convert to C int`—
    # y el desbordamiento no ocurre en una prueba: ocurre en el worker, al fallar un
    # trabajo, o sea justo cuando el sistema ya esta en problemas. Recortar el
    # EXPONENTE en vez del resultado hace que ese caso no exista.
    """
    if base <= timedelta(0):
        raise ValueError("la espera base tiene que ser positiva")
    duplicaciones = 0
    espera = base
    while espera < tope:
        espera *= 2
        duplicaciones += 1
    return duplicaciones


def espera_del_reintento(
    intentos: int, *, base: timedelta = ESPERA_BASE, tope: timedelta = ESPERA_MAXIMA
) -> timedelta:
    """La espera CRECIENTE de RF-14. Estrictamente creciente hasta el tope."""
    if intentos < 1:
        raise ValueError("la espera de un reintento se calcula sobre intentos >= 1")
    duplicaciones = min(intentos - 1, _duplicaciones_hasta_el_tope(base, tope))
    return min(base * (2**duplicaciones), tope)


# --------------------------------------------------------------------------
# Sentencias. Literales, sin f-strings: ninguna de ellas es una superficie de
# interpolacion, y los parametros viajan ligados.
# --------------------------------------------------------------------------
_ENCOLAR = text(
    "INSERT INTO trabajos (agencia_id, cliente_id, tipo, carga, maximo_intentos, "
    "                      disponible_en) "
    "VALUES (:agencia_id, :cliente_id, :tipo, CAST(:carga AS jsonb), "
    "        :maximo_intentos, COALESCE(:disponible_en, now())) "
    "RETURNING id"
)

# WHY (`SKIP LOCKED` dentro de una subconsulta con `FOR UPDATE`): el `UPDATE` de
# fuera es lo que marca el trabajo como reclamado; el `SELECT ... FOR UPDATE SKIP
# LOCKED` de dentro es lo que elige UNO y deja que otro worker se lleve el
# siguiente en vez de esperar a que este acabe.
_RECLAMAR = text(
    "UPDATE trabajos SET estado = 'en_curso', intentos = intentos + 1, "
    "                    actualizado_en = :ahora "
    "WHERE id = ( SELECT id FROM trabajos "
    "             WHERE estado = 'pendiente' AND disponible_en <= :ahora "
    "             ORDER BY disponible_en, creado_en "
    "             FOR UPDATE SKIP LOCKED "
    "             LIMIT 1 ) "
    "RETURNING id, agencia_id, cliente_id, tipo, carga, intentos, maximo_intentos, creado_en"
)

_COMPLETAR = text(
    "UPDATE trabajos SET estado = 'hecho', terminado_en = :ahora, "
    "                    actualizado_en = :ahora, ultimo_error = NULL "
    "WHERE id = :id AND estado = 'en_curso'"
)

_REPROGRAMAR = text(
    "UPDATE trabajos SET estado = 'pendiente', disponible_en = :disponible_en, "
    "                    actualizado_en = :ahora, ultimo_error = :error "
    "WHERE id = :id AND estado = 'en_curso'"
)

_RENDIRSE = text(
    "UPDATE trabajos SET estado = 'fallido', terminado_en = :ahora, "
    "                    actualizado_en = :ahora, ultimo_error = :error "
    "WHERE id = :id AND estado = 'en_curso'"
)

# WHY: un trabajo `en_curso` cuyo worker murio vuelve a `pendiente` SIN gastar un
# intento nuevo (ya se gasto al reclamarlo) y disponible ya mismo.
_RESCATAR_ABANDONADOS = text(
    "UPDATE trabajos SET estado = 'pendiente', disponible_en = :ahora, "
    "                    actualizado_en = :ahora, "
    "                    ultimo_error = 'el trabajo se reclamo y nadie lo termino' "
    "WHERE estado = 'en_curso' AND actualizado_en <= :limite "
    "RETURNING id"
)

_FALLIDOS = text(
    "SELECT id, agencia_id, cliente_id, tipo, estado, intentos, maximo_intentos, "
    "       creado_en, terminado_en, ultimo_error "
    "FROM trabajos WHERE estado = 'fallido' "
    "ORDER BY terminado_en DESC NULLS LAST, id DESC LIMIT :tope"
)

# WHY (un solo enunciado, y no «leo, inserto y luego borro»): el `DELETE ...
# RETURNING` dentro de un CTE que alimenta al `INSERT` hace que mover un trabajo
# al archivo sea ATOMICO. Con tres pasos, una caida entre el segundo y el tercero
# duplica el trabajo en el archivo o lo pierde de los dos sitios.
_ARCHIVAR = text(
    "WITH movidos AS ( "
    "  DELETE FROM trabajos "
    "  WHERE estado = ANY(:estados) AND terminado_en IS NOT NULL "
    "        AND terminado_en <= :corte "
    "  RETURNING id, agencia_id, cliente_id, tipo, carga, estado, intentos, "
    "            maximo_intentos, creado_en, terminado_en, ultimo_error ) "
    "INSERT INTO trabajos_archivados (id, agencia_id, cliente_id, tipo, carga, estado, "
    "       intentos, maximo_intentos, creado_en, terminado_en, ultimo_error) "
    "SELECT id, agencia_id, cliente_id, tipo, carga, estado, intentos, "
    "       maximo_intentos, creado_en, terminado_en, ultimo_error "
    "FROM movidos "
    "RETURNING id"
)

_PURGAR = text("DELETE FROM trabajos_archivados WHERE archivado_en <= :corte RETURNING id")


# --------------------------------------------------------------------------
# La cola
# --------------------------------------------------------------------------
async def encolar(
    conexion,
    inquilino,
    *,
    tipo: str,
    carga: Mapping[str, Any] | None = None,
    maximo_intentos: int = 5,
    disponible_en: datetime | None = None,
) -> UUID:
    """Encola EN LA CONEXION QUE SE LE PASA. No abre ninguna, y ese es el punto.

    # WHY (D-04): quien guarda el mensaje llama a esto dentro de su propia
    # transaccion. Si esa transaccion se deshace, el trabajo se deshace con ella; si
    # se confirma, el trabajo esta. No hay ningun instante en el que el mensaje este
    # guardado y el trabajo no — que es el unico modo en que se pierde un trabajo.
    """
    resultado = await conexion.execute(
        _ENCOLAR,
        {
            "agencia_id": inquilino.agencia_id,
            "cliente_id": inquilino.cliente_id,
            "tipo": tipo,
            "carga": json.dumps(dict(carga or {}), default=str, ensure_ascii=False),
            "maximo_intentos": maximo_intentos,
            "disponible_en": disponible_en,
        },
    )
    return resultado.scalar_one()


async def reclamar(conexion, *, ahora: datetime) -> Trabajo | None:
    """Coge UN trabajo disponible, o devuelve `None` si no hay ninguno."""
    fila = (await conexion.execute(_RECLAMAR, {"ahora": ahora})).one_or_none()
    if fila is None:
        return None
    return Trabajo(
        id=fila.id,
        agencia_id=fila.agencia_id,
        cliente_id=fila.cliente_id,
        tipo=fila.tipo,
        carga=fila.carga,
        intentos=fila.intentos,
        maximo_intentos=fila.maximo_intentos,
        creado_en=fila.creado_en,
    )


async def completar(conexion, trabajo_id: UUID, *, ahora: datetime) -> bool:
    """Marca el trabajo como hecho. Devuelve si de verdad alcanzo una fila."""
    resultado = await conexion.execute(_COMPLETAR, {"id": trabajo_id, "ahora": ahora})
    return bool(resultado.rowcount)


async def fallar(
    conexion,
    trabajo: Trabajo,
    *,
    error: str,
    ahora: datetime,
    base: timedelta = ESPERA_BASE,
    tope: timedelta = ESPERA_MAXIMA,
) -> Estado:
    """Reintenta con espera creciente o se rinde, y dice en cual de las dos acabo.

    # WHY (`intentos` ya viene incrementado): el contador sube al RECLAMAR, no al
    # fallar. Asi un trabajo que se reclama y cuyo worker muere sin decir nada ya
    # gasto su intento — si el contador subiera aqui, ese trabajo se reintentaria
    # eternamente sin que nadie lo declarara fallido nunca.
    """
    if trabajo.es_ultimo_intento:
        await conexion.execute(_RENDIRSE, {"id": trabajo.id, "ahora": ahora, "error": error})
        return Estado.FALLIDO
    espera = espera_del_reintento(trabajo.intentos, base=base, tope=tope)
    await conexion.execute(
        _REPROGRAMAR,
        {"id": trabajo.id, "ahora": ahora, "disponible_en": ahora + espera, "error": error},
    )
    return Estado.PENDIENTE


async def rescatar_abandonados(
    conexion, *, ahora: datetime, plazo: timedelta = PLAZO_DE_ABANDONO
) -> int:
    """Devuelve a la cola los trabajos que alguien reclamo y nadie termino."""
    filas = await conexion.execute(
        _RESCATAR_ABANDONADOS, {"ahora": ahora, "limite": ahora - plazo}
    )
    return len(filas.all())


async def fallidos(conexion, *, tope: int = 100) -> list[dict[str, Any]]:
    """RF-14: los fallidos, VISIBLES. Es lo que el panel enseña a una persona."""
    filas = (await conexion.execute(_FALLIDOS, {"tope": tope})).all()
    return [dict(f._mapping) for f in filas]


# --------------------------------------------------------------------------
# Archivado y purga (mitigacion de R-07) — desde el dia 1
# --------------------------------------------------------------------------
async def archivar(
    conexion,
    *,
    ahora: datetime,
    retencion: timedelta = RETENCION_EN_CALIENTE,
    incluir_fallidos: bool = False,
) -> int:
    """Mueve los trabajos terminados de la tabla caliente al archivo. Atomico.

    `incluir_fallidos` es `False` a proposito: un fallido tiene que seguir
    VISIBLE (RF-14). Archivarlo por antiguedad seria esconderlo con otro nombre.
    """
    estados: Sequence[str] = (
        [Estado.HECHO.value, Estado.FALLIDO.value] if incluir_fallidos else [Estado.HECHO.value]
    )
    movidos = await conexion.execute(_ARCHIVAR, {"corte": ahora - retencion, "estados": estados})
    return len(movidos.all())


async def purgar(conexion, *, ahora: datetime, retencion: timedelta = RETENCION_EN_ARCHIVO) -> int:
    """Borra del archivo lo que ya cumplio su retencion. Borra DE VERDAD.

    # WHY (no hay borrado logico): una columna `borrado` dejaria las filas donde
    # estaban, que es justo lo que R-07 dice que no puede pasar — la tabla seguiria
    # creciendo y el indice tambien. Purgar es `DELETE`.
    """
    borradas = await conexion.execute(_PURGAR, {"corte": ahora - retencion})
    return len(borradas.all())
