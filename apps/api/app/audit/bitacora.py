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

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.tenancy.auth import Rol
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

#: Cuantos caracteres hexadecimales de la huella entran en el actor. Suficientes
#: para no colisionar y demasiado pocos para invertir.
LARGO_DE_LA_HUELLA = 16

#: La FORMA del «quien» de RF-10: `<rol>:<huella>`, nunca nombre ni correo.
#:
#: # WHY (se DERIVA del enum de roles y no se escribe a mano): un rol nuevo en
#: `Rol` entra solo en esta forma. Una lista escrita aqui se quedaria corta el dia
#: que alguien anada el tercero, y entonces el guard rechazaria actores legitimos
#: —o, peor, alguien lo aflojaria a `.*` para que dejara de molestar.
_ACTOR_OPACO = re.compile(
    r"^(?:" + "|".join(sorted(re.escape(r.value) for r in Rol)) + r"):"
    rf"[0-9a-f]{{{LARGO_DE_LA_HUELLA}}}$"
)


def actor_opaco(rol: Rol, identificador: str) -> str:
    """El «quien» de RF-10: el rol, y una HUELLA del identificador de sesion.

    # WHY (huella y no el identificador): el identificador de sesion es la clave de
    # Redis con la que se REVOCA. Escribirlo en una tabla que nadie puede corregir
    # dejaria una lista de mangos de revocacion vivos, para siempre, dentro del
    # propio inquilino. La huella identifica igual —quien tenga el identificador
    # puede recalcularla y correlar— y no sirve para tocar nada.
    #
    # # WHY (lleva el rol delante): un identificador opaco no le dice nada a quien
    # lee la bitacora. `operador_agencia:9f18…` si se lee. Y es lo que permite que
    # `es_actor_opaco` compruebe la FORMA sin tener que consultar a nadie.
    #
    # # WHY (vive aqui y no en cada llamador): hasta esta revision la forma se
    # componia dentro de `baa_guard._actor_de`. Dos redacciones del mismo hecho
    # divergen, y la que se queda vieja es la que nadie mira: el dia que una de las
    # dos escribiera el identificador en claro, la bitacora —que no se puede
    # corregir— lo guardaria para siempre.
    """
    if not isinstance(rol, Rol):
        raise TypeError(
            f"actor_opaco espera un Rol y recibio {type(rol).__name__}: el «quien» de "
            "RF-10 se compone de un rol declarado, no de una cadena cualquiera"
        )
    if not isinstance(identificador, str) or not identificador:
        raise ValueError(
            "actor_opaco necesita un identificador de sesion no vacio: una huella de "
            "la cadena vacia seria la MISMA para todo el mundo"
        )
    huella = hashlib.sha256(identificador.encode("utf-8")).hexdigest()[:LARGO_DE_LA_HUELLA]
    return f"{rol.value}:{huella}"


def es_actor_opaco(valor: object) -> bool:
    """¿Este «quien» tiene la forma que RF-10 exige? Se comprueba, no se confia."""
    return isinstance(valor, str) and bool(_ACTOR_OPACO.match(valor))


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
