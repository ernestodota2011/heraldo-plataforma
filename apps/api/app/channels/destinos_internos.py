"""T-118 (RF-46-bis, CE-07) — la lista declarada de destinos INTERNOS de aviso.

El plan (S4.1) fija que `entregar()`, para un destinatario INTERNO (un operador
de la agencia o del cliente, nunca un contacto externo), comprueba como PRIMER
paso: *"esta en la lista de destinos declarados del inquilino?"*. Hasta esta
casilla esa lista no vivia en ningun sitio — con ella vacia, todos los avisos de
RF-16/39/45/46 se habrian rechazado ahi mismo (L-05). Este modulo ES esa lista y
su guard.

Cada inquilino declara, por su cuenta, A QUIEN avisa (`etiqueta`) y POR QUE
CANAL (`canal`, uno de los tres declarables: `correo`, `webhook_interno`,
`mensajeria`) hacia un `destino` concreto. `exigir_destino_declarado` es el
unico camino por el que `entregar()` puede confirmar que un destino existe y
esta activo; todo lo demas es administracion de esa lista.

# WHY (esto NO es el guard de RED, aunque para `webhook_interno` llame a
`egress.red.validar`): son DOS preguntas distintas. `egress.red` (T-300)
responde *"a esta direccion se puede salir sin que sea una red interna"* — se
comprueba SIEMPRE, en cada uso, porque el DNS puede cambiar entre el guardado y
el envio (RF-05). Este modulo responde *"este inquilino declaro que quiere
avisos en este destino"* — se comprueba tambien SIEMPRE (aqui, en
`exigir_destino_declarado`), pero es una pregunta de PERMISO declarado, no de
topologia de red. Un `webhook_interno` puede estar declarado y activo Y AUN ASI
ser rechazado despues por T-300 si su direccion cambio de forma para peor — las
dos comprobaciones son independientes y las dos tienen que pasar. La llamada a
`egress.red.validar` que hace `declarar_destino` es solo una comprobacion de
FORMA al momento de declarar (para no aceptar de entrada un webhook que jamas
podria salir); NO sustituye la revalidacion de T-300 en el momento de usar.

# WHY (`DestinoNoDeclarado` nunca cita el destino contra el que comprobo): es la
misma regla de la casa que en `secrets.SecretoNoDescifrable` o en
`dominio_desconocido.DominioNoReconocido` — un mensaje de rechazo que repite lo
que se le paso queda escrito en un registro o en una respuesta de error, y ese
destino puede ser el correo o el webhook de otro inquilino si quien llama se
equivoco de sesion. Solo se cita el `canal` (uno de tres valores publicos, sin
nada que revelar) para que quien opera sepa AL MENOS por donde buscar.

# WHY (retirar marca inactivo, nunca borra): la fila que declaro un destino es
la prueba de que existio, y `retirar_destino` deja constancia en la bitacora
(RF-10) de QUIEN lo retiro y CUANDO — esta tabla no tiene columnas
`retirado_en`/`retirado_por` a proposito: ese historial completo lo sostiene la
bitacora, que es de solo insercion y nadie la reescribe (a diferencia de esta
tabla, que SI se actualiza si el mismo destino se vuelve a declarar).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.audit.bitacora import apuntar
from app.tenancy.inquilino import Inquilino
from egress.red import validar


class Canal(StrEnum):
    """Los TRES canales declarables. El CHECK de `destinos_de_aviso` (0009)
    nombra los mismos tres valores — se mantienen sincronizados a mano porque
    una migracion aplicada no importa `app.*` (ver el WHY de la 0009)."""

    CORREO = "correo"
    WEBHOOK_INTERNO = "webhook_interno"
    MENSAJERIA = "mensajeria"


class CanalNoAdmitido(ValueError):
    """Un canal fuera de los tres declarados: no se guarda ni se busca lo que
    nadie definio."""


class DestinoNoDeclarado(LookupError):
    """El guard dijo que NO. Nunca cita el destino contra el que comprobo —
    solo el canal, que es publico. Ver el WHY del modulo."""

    def __init__(self, canal: str) -> None:
        super().__init__(
            f"no hay ningun destino ACTIVO declarado para el canal {canal!r} en "
            "este inquilino: el aviso se rechaza (RF-46-bis). Declaralo primero "
            "con declarar_destino, o revisa si lo retiraron"
        )


@dataclass(frozen=True, slots=True)
class DestinoDeAviso:
    """Una fila de `destinos_de_aviso`, ya leida. Inmutable, como la fila que representa."""

    id: UUID
    agencia_id: UUID
    cliente_id: UUID
    canal: str
    destino: str
    etiqueta: str
    activo: bool
    declarado_en: datetime
    declarado_por: str


_CANALES_VALIDOS = frozenset(canal.value for canal in Canal)

_DECLARAR = text(
    "INSERT INTO destinos_de_aviso "
    "(agencia_id, cliente_id, canal, destino, etiqueta, declarado_por) "
    "VALUES (:agencia_id, :cliente_id, :canal, :destino, :etiqueta, :declarado_por) "
    "ON CONFLICT (agencia_id, cliente_id, canal, destino) DO UPDATE "
    "SET etiqueta = EXCLUDED.etiqueta, "
    "    activo = true, "
    "    declarado_en = now(), "
    "    declarado_por = EXCLUDED.declarado_por "
    "RETURNING id, agencia_id, cliente_id, canal, destino, etiqueta, activo, "
    "          declarado_en, declarado_por"
)

# WHY (el UPDATE no lleva `WHERE activo = true`): un retiro es idempotente sobre
# la IDENTIDAD del destino (canal + destino), no sobre su estado actual. Si ya
# estaba inactivo, retirarlo otra vez no cambia nada observable y NO es un error
# — lo que si es un error es retirar algo que nunca se declaro, y de eso avisa
# `fila is None` mas abajo. Acotar por `activo = true` convertiria ese segundo
# retiro, inofensivo, en un `DestinoNoDeclarado` falso.
_RETIRAR = text(
    "UPDATE destinos_de_aviso SET activo = false "
    "WHERE canal = :canal AND destino = :destino "
    "RETURNING id"
)

_CAMPOS = (
    "id, agencia_id, cliente_id, canal, destino, etiqueta, activo, "
    "declarado_en, declarado_por"
)
_BUSCAR_ACTIVO = text(
    f"SELECT {_CAMPOS} FROM destinos_de_aviso "  # noqa: S608 - sin entrada de usuario
    "WHERE canal = :canal AND destino = :destino AND activo = true"
)
_LISTAR_ACTIVOS = text(f"SELECT {_CAMPOS} FROM destinos_de_aviso WHERE activo = true "  # noqa: S608
                       "ORDER BY canal, destino")
_LISTAR_TODOS = text(f"SELECT {_CAMPOS} FROM destinos_de_aviso ORDER BY canal, destino")  # noqa: S608


def _validar_canal(canal: str) -> str:
    if canal not in _CANALES_VALIDOS:
        raise CanalNoAdmitido(
            f"{canal!r} no es un canal declarable. Solo se admiten "
            f"{sorted(_CANALES_VALIDOS)}"
        )
    # WHY (`str(canal)`, no devolver el argumento tal cual): un llamador puede
    # pasar el miembro de `Canal` (un `StrEnum`, subtipo de `str`) o un texto
    # suelto — los dos comparan igual y los dos son validos. Lo que sale de aqui
    # SIEMPRE es `str` puro, para que el adaptador de parametros del driver no
    # tenga que decidir que hacer con un subtipo.
    return str(canal)


def _exigir_no_vacio(valor: str, *, campo: str, motivo: str) -> str:
    limpio = valor.strip()
    if not limpio:
        raise ValueError(f"{campo} vacio: {motivo}")
    return limpio


def _fila_a_destino(fila: Any) -> DestinoDeAviso:
    return DestinoDeAviso(
        id=fila.id,
        agencia_id=fila.agencia_id,
        cliente_id=fila.cliente_id,
        canal=fila.canal,
        destino=fila.destino,
        etiqueta=fila.etiqueta,
        activo=fila.activo,
        declarado_en=fila.declarado_en,
        declarado_por=fila.declarado_por,
    )


async def declarar_destino(
    conexion,
    inquilino: Inquilino,
    *,
    canal: str,
    destino: str,
    etiqueta: str,
    declarado_por: str,
) -> DestinoDeAviso:
    """Declara (o REACTIVA, si ya existia y estaba retirado) un destino de aviso.

    Un `webhook_interno` pasa por `egress.red.validar` ANTES de guardarse — es
    una comprobacion de FORMA (https, sin puerto fuera de 443, sin direccion
    interna): declarar uno que nunca podria salir es un error que conviene
    atrapar aqui, no en el primer intento real de aviso. `egress.red.validar`
    lanza `DestinoRechazado` (ella misma, con su propio motivo) si la forma no
    sirve; este modulo no la envuelve ni la oculta.

    # WHY (`ON CONFLICT ... DO UPDATE`, no un INSERT que falle): la identidad de
    # un destino es (agencia, cliente, canal, destino) — la MISMA que su
    # restriccion unica. Volver a declarar un destino RETIRADO tiene que
    # reactivarlo (RF-46-bis no dice «declaralo una sola vez en la vida»); un
    # INSERT desnudo chocaria con la fila retirada que ya existe y una operacion
    # legitima —reactivar— saldria como un error de unicidad.
    """
    canal = _validar_canal(canal)
    destino = _exigir_no_vacio(destino, campo="destino", motivo="no declara nada")
    etiqueta = _exigir_no_vacio(
        etiqueta, campo="etiqueta", motivo="no dice A QUIEN corresponde (RF-46-bis)"
    )
    declarado_por = _exigir_no_vacio(
        declarado_por, campo="declarado_por", motivo="no deja rastro de QUIEN lo declaro"
    )

    if canal == Canal.WEBHOOK_INTERNO:
        validar(destino)

    fila = (
        await conexion.execute(
            _DECLARAR,
            {
                "agencia_id": inquilino.agencia_id,
                "cliente_id": inquilino.cliente_id,
                "canal": canal,
                "destino": destino,
                "etiqueta": etiqueta,
                "declarado_por": declarado_por,
            },
        )
    ).one()
    return _fila_a_destino(fila)


async def retirar_destino(
    conexion, inquilino: Inquilino, *, canal: str, destino: str, retirado_por: str
) -> None:
    """Marca el destino inactivo. NO lo borra: `exigir_destino_declarado` volvera
    a rechazarlo, y la bitacora (RF-10) deja quien y cuando lo retiro."""
    canal = _validar_canal(canal)
    destino = _exigir_no_vacio(destino, campo="destino", motivo="no se puede retirar")
    retirado_por = _exigir_no_vacio(
        retirado_por, campo="retirado_por", motivo="no deja rastro de QUIEN lo retiro"
    )

    fila = (
        await conexion.execute(_RETIRAR, {"canal": canal, "destino": destino})
    ).one_or_none()
    if fila is None:
        raise DestinoNoDeclarado(canal)

    await apuntar(
        conexion,
        inquilino,
        actor=retirado_por,
        accion="retirar_destino_de_aviso",
        recurso=f"destinos_de_aviso:{canal}",
        detalle={"id": str(fila.id)},
    )


async def listar_destinos(
    conexion, inquilino: Inquilino, *, solo_activos: bool = True
) -> list[DestinoDeAviso]:
    """Los destinos ALCANZABLES en esta sesion. La politica de RLS ya decidio cuales son.

    # WHY (`inquilino` no se usa dentro): quien filtra es RLS, ya activo sobre
    # `conexion` (la abrio `sesion_de_inquilino`). Se recibe explicito para que
    # la firma diga PARA QUIEN se pregunta — igual que `secrets.leer` recibe el
    # inquilino aunque la fila ya llegue filtrada.
    """
    consulta = _LISTAR_ACTIVOS if solo_activos else _LISTAR_TODOS
    filas = (await conexion.execute(consulta)).all()
    return [_fila_a_destino(fila) for fila in filas]


async def exigir_destino_declarado(
    conexion, inquilino: Inquilino, canal: str, destino: str
) -> DestinoDeAviso:
    """EL guard de T-118: paso 1 de la rama INTERNO del contrato de entrega (plan S4.1).

    Devuelve el destino cuando esta declarado y activo. Levanta
    `DestinoNoDeclarado` en cualquier otro caso — no declarado nunca, declarado
    y retirado, o declarado para otro inquilino (RLS lo hace invisible, asi que
    para esta sesion es indistinguible de "no declarado": ningun mensaje de
    error revela que existe en otro sitio).

    # WHY (`inquilino` no se usa dentro): la misma razon que en `listar_destinos`
    — RLS ya filtra por el inquilino de `conexion`. Se recibe explicito porque
    es el contrato que el plan (S4.1, paso 1 de la rama INTERNO) nombra: la
    firma dice PARA QUIEN se pregunta, no solo POR DONDE.

    # WHY (`_exigir_no_vacio` en vez de un `.strip()` suelto): un destino vacio
    # es un error de ENTRADA de quien llama, no una pregunta legitima sobre si
    # algo esta declarado — `declarar_destino` nunca guarda un destino vacio, asi
    # que buscar uno solo podia terminar en `DestinoNoDeclarado`, un `LookupError`
    # que mezclaria dos causas distintas bajo el mismo mensaje (hallazgo Crisol).
    """
    canal = _validar_canal(canal)
    destino = _exigir_no_vacio(
        destino, campo="destino", motivo="no se puede exigir un destino vacio"
    )
    fila = await _buscar_destino_activo(conexion, canal=canal, destino=destino)
    if fila is None:
        raise DestinoNoDeclarado(canal)
    return fila


async def _buscar_destino_activo(conexion, *, canal: str, destino: str) -> DestinoDeAviso | None:
    """El unico lugar que sabe MIRAR. `exigir_destino_declarado` es quien decide
    que hacer con lo que encuentra — separar las dos cosas es lo que permite
    sabotear el guard sin tocar la consulta (ver `test_destinos_internos.py`)."""
    fila = (
        await conexion.execute(_BUSCAR_ACTIVO, {"canal": canal, "destino": destino})
    ).one_or_none()
    return None if fila is None else _fila_a_destino(fila)
