"""La bitacora: quien, que y cuando — y nadie la corrige despues (RF-10).

# WHY (el mecanismo esta en el PERMISO, no aqui): este modulo solo sabe insertar
# y leer. Podria escribirse un `UPDATE` cinco lineas mas abajo y no serviria de
# nada: la revision 0003 concede al rol de aplicacion `SELECT, INSERT` sobre
# `bitacora` y nada mas. `test_la_bitacora_rechaza_el_update_con_el_rol_de_la_
# aplicacion` lo comprueba POR EFECTO, intentando el `UPDATE` y el `DELETE` con el
# rol real y exigiendo que los dos fallen — con su control: el `INSERT` SI pasa.
# Un `# no borrar` en un comentario no es un mecanismo.
#
# WHY (el detalle pasa por el barrido de secretos): lo mas facil de todo seria
# escribir un secreto DENTRO del apunte —«guardo el cuerpo entero de la peticion
# por si acaso»— y entonces RF-09 se rompe por la puerta de RF-10, en una tabla
# que ademas nadie puede corregir. `apuntar` barre el detalle antes de escribirlo:
# si lleva material cifrado o un secreto descifrado, aborta la escritura.
#
# WHY (no hay `borrar_apunte` ni `anular_apunte`): tampoco hay columna de estado
# en la tabla. Una columna `anulado` no reescribe la fila — la esconde, que a
# efectos de una auditoria es lo mismo.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.tenancy.inquilino import Inquilino
from app.tenancy.secrets import barrer

_APUNTAR = text(
    "INSERT INTO bitacora (agencia_id, cliente_id, actor, accion, recurso, detalle) "
    "VALUES (:agencia_id, :cliente_id, :actor, :accion, :recurso, "
    "        CAST(:detalle AS jsonb)) "
    "RETURNING id, ocurrido_en"
)

_LEER = text(
    "SELECT id, agencia_id, cliente_id, ocurrido_en, actor, accion, recurso, detalle "
    "FROM bitacora ORDER BY ocurrido_en DESC, id DESC LIMIT :tope"
)

#: Tope por defecto de una lectura. Una bitacora crece sin limite; una lectura sin
#: tope acaba trayendose la tabla entera a memoria el dia que de verdad importa.
TOPE_POR_DEFECTO = 200


@dataclass(frozen=True, slots=True)
class Apunte:
    """Una fila de la bitacora, ya leida. Inmutable, como la fila que representa."""

    id: UUID
    agencia_id: UUID
    cliente_id: UUID
    ocurrido_en: datetime
    actor: str
    accion: str
    recurso: str
    detalle: Mapping[str, Any]


async def apuntar(
    conexion,
    inquilino: Inquilino,
    *,
    actor: str,
    accion: str,
    recurso: str,
    detalle: Mapping[str, Any] | None = None,
) -> UUID:
    """Escribe un apunte en la sesion de inquilino que se le pasa.

    # WHY (recibe la conexion): el apunte tiene que poder ir en LA MISMA
    # transaccion que la accion que describe. Si abriera la suya, una accion podria
    # confirmarse sin su apunte —o al reves— y la bitacora dejaria de decir lo que
    # paso para decir lo que casi paso.
    """
    limpio = barrer(dict(detalle or {}))
    fila = (
        await conexion.execute(
            _APUNTAR,
            {
                "agencia_id": inquilino.agencia_id,
                "cliente_id": inquilino.cliente_id,
                "actor": actor,
                "accion": accion,
                "recurso": recurso,
                "detalle": json.dumps(limpio, default=str, ensure_ascii=False),
            },
        )
    ).one()
    return fila.id


async def leer_apuntes(conexion, *, tope: int = TOPE_POR_DEFECTO) -> list[Apunte]:
    """Los apuntes ALCANZABLES en esta sesion. La politica ya decidio cuales son.

    # WHY (un `tope` de cero no es una lectura vacia, es una llamada mal escrita):
    # `LIMIT 0` devuelve una lista vacia sin decir nada, y una bitacora que sale
    # vacia se lee como «no paso nada» — que es justo la confusion que RF-03 evita
    # en el camino de datos. Aqui se falla ruidoso. Lo senalo la revision cruzada.
    """
    if tope < 1:
        raise ValueError(
            f"tope={tope}: una lectura de la bitacora con tope menor que 1 devolveria "
            "una lista vacia en silencio, y una bitacora vacia se confunde con «no paso nada»"
        )
    filas = (await conexion.execute(_LEER, {"tope": tope})).all()
    return [
        Apunte(
            id=f.id,
            agencia_id=f.agencia_id,
            cliente_id=f.cliente_id,
            ocurrido_en=f.ocurrido_en,
            actor=f.actor,
            accion=f.accion,
            recurso=f.recurso,
            detalle=f.detalle,
        )
        for f in filas
    ]
