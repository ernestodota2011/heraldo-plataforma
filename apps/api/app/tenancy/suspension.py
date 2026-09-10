"""T-021·quinquies (RF-66) — la suspension de un cliente: dejar de responder, y volver.

RF-66 define el estado y sus dos causas: la falta de **re-aceptacion** (automatica,
`aceptacion.py`) y el **impago** (manual, un operador desde el panel, T-101). Bajo
suspension el heraldo **no envia ni responde**, el portal pasa a **solo lectura**,
el **export sigue disponible** (B9) y **nada se borra**. Se levanta al subsanar.

Aqui viven los dos verbos —`suspender_cliente` y `levantar_suspension`—, el
predicado `esta_suspendido`, el guard `exigir_cliente_activo` que lo hace cumplir
en el punto donde el producto decide responder, y la tabla `suspensiones`, que es
el HISTORIAL y no un interruptor.

# WHY (el estado vigente se DERIVA y no se guarda): la forma comoda de escribir
# esto es una columna `suspendido` en `clientes` mas una tabla de historia. Son DOS
# sitios que dicen lo mismo, y dos sitios que dicen lo mismo divergen — el que se
# queda viejo manda sobre el producto sin que nadie lo mire. Aqui el unico sitio es
# la tabla: «esta suspendido» es «existe una fila sin `levantada_en`». Lo hace
# cumplir un guard de la suite que mira las COLUMNAS de `clientes` y se pone en
# rojo si alguna vuelve a nombrar la suspension.
#
# # WHY (la unicidad vive en un INDICE, no en el `if` de este modulo): dos
# llamadas a la vez —el panel de un operador y el barrido de re-aceptacion— leen
# las dos «no hay ninguna vigente» y escriben las dos. Con dos filas vigentes,
# «levantar» deja de tener una respuesta unica y el historial afirma dos cortes
# donde hubo uno. El indice unico parcial `WHERE levantada_en IS NULL` lo hace
# imposible; el `if` de aqui solo evita el error en el caso amable.
#
# # WHY (el asiento va en la MISMA transaccion que el cambio): RF-66 dice
# «suspension AVISADA, nunca un corte en silencio», y RF-10 exige quien, que y
# cuando. Si el apunte se escribiera aparte, un fallo entre medias dejaria un
# cliente apagado sin ninguna constancia de quien lo apago — que es el corte en
# silencio con otro nombre. Por eso los dos verbos reciben la CONEXION.
#
# # WHY (el guard se cablea en la RUTA, no en la suite): un
# `exigir_cliente_activo` al que solo llamaran las pruebas AUTORIZA en produccion
# (`feedback_guard_solo_en_el_test`). Hoy el unico punto del producto donde se
# decide responder a un mensaje externo es la puerta de idempotencia
# (`app.channels.idempotency.puerta`), que abre la transaccion en la que el mensaje
# se registra y su respuesta se encola: ahi esta puesto. ==Cuando exista
# `entregar()` (T-119) su paso 0a llamara a este mismo guard== — la ranura esta
# declarada en el registro de deuda de la casilla, no supuesta.
#
# # WHY (lo que este modulo NO decide, dicho en voz alta): los efectos sobre el
# **portal** (solo lectura, T-200) y sobre el **export** (sigue disponible, T-212)
# se miden donde nacen. Aqui cierra la TRANSICION: dejar de responder y volver.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from app.audit.bitacora import apuntar, es_actor_opaco
from app.tenancy.inquilino import Alcance, Inquilino

#: Las acciones con las que los dos verbos quedan escritos en la bitacora (RF-10).
ACCION_SUSPENSION = "suspension-de-cliente"
ACCION_LEVANTAMIENTO = "levantamiento-de-suspension"

#: Motivo canonico de la suspension automatica de RF-66. Vive aqui, en un solo
#: sitio, para que el barrido de re-aceptacion y el panel nombren LO MISMO.
MOTIVO_REACEPTACION_PENDIENTE = "re-aceptacion pendiente"

_VIGENTE = (
    "SELECT id, motivo, suspendida_en, suspendida_por, levantada_en, levantada_por "
    "FROM suspensiones "
    "WHERE agencia_id = :agencia AND cliente_id = :cliente AND levantada_en IS NULL"
)

_SUSPENDER = text(
    "INSERT INTO suspensiones (agencia_id, cliente_id, motivo, suspendida_por) "
    "VALUES (:agencia, :cliente, :motivo, :actor) "
    "RETURNING id, motivo, suspendida_en, suspendida_por, levantada_en, levantada_por"
)

_LEVANTAR = text(
    "UPDATE suspensiones SET levantada_en = now(), levantada_por = :actor "
    "WHERE id = :id AND levantada_en IS NULL "
    "RETURNING id, motivo, suspendida_en, suspendida_por, levantada_en, levantada_por"
)


class ClienteSuspendido(Exception):
    """El cliente esta suspendido: no se envia, no se responde, no se encola.

    # WHY (lleva el cliente y el motivo dentro, y aun asi eso no sale hacia fuera):
    # quien la captura en una superficie publica devuelve la respuesta unica de
    # RF-60 y no este texto. El contenido esta para la bitacora y para el panel del
    # operador, que son alcances con credencial.
    """

    def __init__(self, cliente_id: UUID, motivo: str) -> None:
        super().__init__(
            f"el cliente {cliente_id} esta suspendido ({motivo}): mientras dure, el "
            "heraldo no envia ni responde (RF-66)"
        )
        self.cliente_id = cliente_id
        self.motivo = motivo


class MotivoVacio(ValueError):
    """Una suspension sin motivo es inapelable: no hay nada que subsanar."""


class ActorNoOpaco(ValueError):
    """El «quien» no tiene la forma de RF-10: rol + identificador opaco."""


class AlcanceSinCliente(ValueError):
    """Se pregunto por la suspension de un alcance que no nombra a ningun cliente."""


class NoHaySuspensionVigente(Exception):
    """No hay nada que levantar. Se dice, en vez de devolver un exito vacio."""


@dataclass(frozen=True, slots=True)
class Suspension:
    """Una fila del historial, ya leida. Inmutable, como la fila que representa."""

    id: UUID
    cliente_id: UUID
    motivo: str
    suspendida_en: datetime
    suspendida_por: str
    levantada_en: datetime | None
    levantada_por: str | None

    @property
    def vigente(self) -> bool:
        return self.levantada_en is None


def _exigir_cliente(inquilino: Inquilino) -> None:
    """Fail-closed: sin cliente nombrado no hay respuesta, hay un error.

    # WHY: con alcance `agencia`, `app.cliente_id` lleva el uuid centinela, que no
    # coincide con ninguna fila. Una consulta que lo aceptara devolveria «no hay
    # suspension vigente» SIEMPRE — cero filas silenciosas leidas como «esta
    # activo», que es justo la confusion que RF-03 evita en el camino de datos.
    # Ojo: el `inquilino` de estos verbos nombra al CLIENTE afectado, no a la
    # sesion; un operador de agencia opera sobre el inquilino de su cliente.
    """
    if inquilino.alcance is not Alcance.CLIENTE:
        raise AlcanceSinCliente(
            "la suspension es POR CLIENTE y este inquilino no nombra a ninguno "
            f"(alcance {inquilino.alcance}). Preguntarlo asi devolveria «no suspendido» "
            "sin haber mirado ninguna fila"
        )


def _exigir_actor(actor: object) -> str:
    if not es_actor_opaco(actor):
        raise ActorNoOpaco(
            "el actor de una suspension se escribe como rol + identificador opaco "
            "(RF-10), nunca como nombre ni correo. Componlo con "
            "`app.audit.bitacora.actor_opaco`"
        )
    return str(actor)


def _exigir_motivo(motivo: object) -> str:
    if not isinstance(motivo, str) or not motivo.strip():
        raise MotivoVacio(
            "una suspension sin motivo no se puede subsanar: el cliente no sabria que "
            "arreglar y el operador no sabria que levantar (RF-66)"
        )
    return motivo.strip()


def _suspension(fila, cliente_id: UUID) -> Suspension:
    return Suspension(
        id=fila.id,
        cliente_id=cliente_id,
        motivo=fila.motivo,
        suspendida_en=fila.suspendida_en,
        suspendida_por=fila.suspendida_por,
        levantada_en=fila.levantada_en,
        levantada_por=fila.levantada_por,
    )


async def suspension_vigente(
    conexion, inquilino: Inquilino, *, para_actualizar: bool = False
) -> Suspension | None:
    """La suspension abierta de este cliente, o `None`. ES la derivacion del estado.

    `para_actualizar` toma el cerrojo de la fila para quien va a escribirla, igual
    que en la reverificacion de sector: sin el, dos levantamientos simultaneos
    leerian la misma fila abierta y dejarian dos asientos de un solo cierre.
    """
    _exigir_cliente(inquilino)
    consulta = _VIGENTE + (" FOR UPDATE" if para_actualizar else "")
    fila = (
        await conexion.execute(
            text(consulta),
            {"agencia": inquilino.agencia_id, "cliente": inquilino.cliente_id},
        )
    ).first()
    return None if fila is None else _suspension(fila, inquilino.cliente_id)


async def esta_suspendido(conexion, inquilino: Inquilino) -> bool:
    """El predicado. Una sola fuente: la tabla."""
    return await suspension_vigente(conexion, inquilino) is not None


async def exigir_cliente_activo(conexion, inquilino: Inquilino) -> None:
    """El guard. Se llama DONDE el producto decide responder o encolar (RF-66)."""
    vigente = await suspension_vigente(conexion, inquilino)
    if vigente is not None:
        raise ClienteSuspendido(vigente.cliente_id, vigente.motivo)


async def suspender_cliente(
    conexion, inquilino: Inquilino, *, motivo: object, actor: object
) -> Suspension:
    """Apaga a un cliente y lo deja escrito. Idempotente sobre la vigente.

    Devuelve la suspension VIGENTE. Si ya habia una, devuelve **esa** y no toca
    nada: el motivo es el de la causa que la abrio, no el de la ultima vez que
    alguien lo intento — reescribirlo borraria de la bitacora lo que el cliente
    tenia que subsanar.
    """
    _exigir_cliente(inquilino)
    escritor = _exigir_actor(actor)
    limpio = _exigir_motivo(motivo)

    ya = await suspension_vigente(conexion, inquilino, para_actualizar=True)
    if ya is not None:
        return ya

    fila = (
        await conexion.execute(
            _SUSPENDER,
            {
                "agencia": inquilino.agencia_id,
                "cliente": inquilino.cliente_id,
                "motivo": limpio,
                "actor": escritor,
            },
        )
    ).one()
    suspension = _suspension(fila, inquilino.cliente_id)
    await apuntar(
        conexion,
        inquilino,
        actor=escritor,
        accion=ACCION_SUSPENSION,
        recurso=f"cliente:{inquilino.cliente_id}",
        detalle={"motivo": limpio, "suspension": str(suspension.id)},
    )
    return suspension


async def levantar_suspension(conexion, inquilino: Inquilino, *, actor: object) -> Suspension:
    """Vuelve a encender al cliente. CIERRA la fila; no la borra.

    # WHY (falla ruidosamente si no habia ninguna): un `no-op` silencioso dejaria
    # creer que se levanto algo que nadie habia apagado — y quien lo pidio se iria
    # convencido de haber arreglado un corte que sigue en pie en otro sitio.
    """
    _exigir_cliente(inquilino)
    escritor = _exigir_actor(actor)

    vigente = await suspension_vigente(conexion, inquilino, para_actualizar=True)
    if vigente is None:
        raise NoHaySuspensionVigente(
            f"el cliente {inquilino.cliente_id} no tiene ninguna suspension vigente: "
            "no se levanta lo que nadie apago"
        )

    fila = (await conexion.execute(_LEVANTAR, {"id": vigente.id, "actor": escritor})).first()
    if fila is None:
        # Entre la lectura y la escritura hay una ventana. Sin mirar la fila
        # devuelta, el asiento afirmaria un levantamiento que no ocurrio.
        raise NoHaySuspensionVigente(
            "la suspension dejo de estar vigente entre la lectura y la escritura: no se "
            "cambio nada y la operacion se deshace entera"
        )

    levantada = _suspension(fila, inquilino.cliente_id)
    await apuntar(
        conexion,
        inquilino,
        actor=escritor,
        accion=ACCION_LEVANTAMIENTO,
        recurso=f"cliente:{inquilino.cliente_id}",
        detalle={"motivo": levantada.motivo, "suspension": str(levantada.id)},
    )
    return levantada
