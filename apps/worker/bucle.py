"""T-020 — el worker asincrono: reclama, ejecuta, cierra, y hace su mantenimiento.

# WHY (dos transacciones y no una): reclamar CONFIRMA antes de ejecutar. Si el
# trabajo se ejecutara dentro de la misma transaccion que lo reclamo, la fila
# seguiria bloqueada todo el rato y `SKIP LOCKED` no serviria de nada: el segundo
# worker no la saltaria, se quedaria esperando. Confirmando el `en_curso`, los
# demas workers la ven reclamada y siguen a lo suyo. El precio de esa eleccion es
# el trabajo que se queda `en_curso` porque su worker murio a medias, y ese precio
# se paga con `rescatar_abandonados` — no se ignora.
#
# WHY (el manejador no recibe la conexion): el trabajo se ejecuta FUERA de
# cualquier transaccion abierta. Un manejador que hable con un modelo o con un
# canal externo puede tardar segundos; con una transaccion abierta detras, esos
# segundos son una conexion del pool retenida y un `xid` que el autovacuum no
# puede limpiar. Si un manejador necesita la base, abre su propia sesion de
# inquilino — que es la unica forma de abrir una.
#
# WHY (esta capa recibe el inquilino, no lo busca): ver el bloque
# «LO QUE ESTE MODULO NO RESUELVE» al final del archivo. Es una deuda DECLARADA,
# con su plan, no un hueco silencioso.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.tenancy import sesion_de_inquilino
from app.tenancy.inquilino import Inquilino
from worker import cola

#: Cuanto espera el bucle cuando la cola esta vacia antes de volver a mirar.
PAUSA_SIN_TRABAJO = timedelta(seconds=1)

#: Cada cuanto corre el mantenimiento (rescate, archivado, purga).
CADA_CUANTO_EL_MANTENIMIENTO = timedelta(minutes=5)

#: Un manejador recibe el trabajo y o bien termina, o bien lanza. No devuelve
#: «exito/fracaso»: un booleano de exito se ignora sin querer; una excepcion no.
Manejador = Callable[[cola.Trabajo], Awaitable[None]]


def ahora_utc() -> datetime:
    """El reloj, en un solo sitio, para que las pruebas puedan sustituirlo."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Mantenimiento:
    """Lo que hizo una pasada de mantenimiento. Se mide, no se supone."""

    rescatados: int
    archivados: int
    purgados: int


async def procesar_uno(
    motor, inquilino: Inquilino, manejador: Manejador, *, ahora: datetime
) -> cola.Estado | None:
    """Reclama UN trabajo y lo lleva hasta su estado final. `None` si no habia.

    Devuelve `HECHO`, `PENDIENTE` (reintentara mas tarde) o `FALLIDO` (se rindio).

    # WHY (==entrega AL MENOS UNA VEZ, y el manejador tiene que ser idempotente==):
    # el manejador corre FUERA de transaccion y `completar` va despues, en la suya.
    # Si el manejador termina bien y `completar` no llega —la base se cayo, el
    # proceso murio—, el trabajo se queda `en_curso`, `rescatar_abandonados` lo
    # devuelve a la cola y se EJECUTA OTRA VEZ. Eso no es un defecto que se pueda
    # arreglar aqui: es la garantia que da una cola cuyo efecto vive fuera de la
    # base. Quien escriba un manejador lo escribe idempotente, y para lo que sale
    # hacia terceros esa idempotencia la sostiene `packages/egress`.
    #
    # # WHY (un fallo de `completar` NO se traga): sube y mata el bucle a proposito.
    # La revision cruzada propuso capturarlo; capturarlo dejaria un worker vivo
    # hablando con una base que no responde, o sea el FALSO VERDE que T-025 existe
    # para no tener. Muriendo, el trabajo se queda `en_curso`, el rescate lo
    # devuelve y la alarma por ANTIGUEDAD suena. El mecanismo ya esta; taparlo con
    # un `except` lo desactivaria.
    """
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        trabajo = await cola.reclamar(conexion, ahora=ahora)
    if trabajo is None:
        return None

    try:
        await manejador(trabajo)
    except Exception as fallo:  # noqa: BLE001 - un manejador falla como le da la gana
        # WHY: se guarda el TIPO y el texto del fallo, no su traza. Una traza en una
        # columna acaba llevandose dentro el contenido del trabajo, y el contenido
        # del trabajo es dato de cliente.
        motivo = f"{type(fallo).__name__}: {fallo}"
        async with sesion_de_inquilino(motor, inquilino) as conexion:
            return await cola.fallar(conexion, trabajo, error=motivo, ahora=ahora)

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await cola.completar(conexion, trabajo.id, ahora=ahora)
    return cola.Estado.HECHO


async def mantenimiento(
    motor, inquilino: Inquilino, *, ahora: datetime, purgar: bool = False
) -> Mantenimiento:
    """Rescate, archivado y —solo si se pide— purga. En ese orden.

    # WHY (el orden): primero se rescata —un trabajo abandonado no puede quedarse
    # fuera de la cola—, despues se archiva lo terminado y por ultimo se purga lo
    # archivado. Al reves, la purga trabajaria sobre un archivo al que todavia le
    # faltan las filas que el archivado va a meter.
    #
    # # WHY (`purgar=False` por defecto — RNF-06, P-31): ==el rescate y el archivado
    # no destruyen nada== —uno devuelve una fila a la cola, el otro la mueve de
    # tabla dentro de la misma sentencia—, asi que el bucle puede correrlos solo.
    # `purgar` SI destruye, y RNF-06 nombra expresamente la «purga por retencion»
    # entre las operaciones que exigen confirmacion. Dejarla en el barrido
    # automatico habria sido destruccion desatendida de datos de un cliente sin que
    # nadie diga que ni cuanto. Se queda apagada hasta que exista la politica de
    # retencion de RF-50 (T-213), que es donde esa confirmacion se da UNA vez sobre
    # la politica en vez de en cada corrida.
    #
    # # WHY (esto NO reabre R-07): lo que R-07 teme es que la tabla de COLA crezca
    # y el camino caliente se degrade, y de eso se encarga el archivado, que si
    # corre solo. El archivo crece despacio y no lo consulta nadie en el camino
    # caliente.
    """
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        rescatados = await cola.rescatar_abandonados(conexion, ahora=ahora)
        archivados = await cola.archivar(conexion, ahora=ahora)
        purgados = await cola.purgar(conexion, ahora=ahora) if purgar else 0
    return Mantenimiento(rescatados=rescatados, archivados=archivados, purgados=purgados)


async def bucle(
    motor,
    inquilino: Inquilino,
    manejador: Manejador,
    *,
    parar: asyncio.Event,
    reloj: Callable[[], datetime] = ahora_utc,
    pausa: timedelta = PAUSA_SIN_TRABAJO,
    cada_cuanto: timedelta = CADA_CUANTO_EL_MANTENIMIENTO,
) -> None:
    """Consume la cola hasta que alguien pida parar. Es todo el worker.

    # WHY (`parar` es un `Event` y no un `while True`): un bucle sin salida se mata
    # con una senal, y matar un proceso a mitad de un trabajo es justo lo que
    # produce los `en_curso` huerfanos. Con el evento, el bucle acaba el trabajo que
    # tiene entre manos y sale por su propio pie.
    """
    ultimo_mantenimiento = reloj() - cada_cuanto
    while not parar.is_set():
        ahora = reloj()
        if ahora - ultimo_mantenimiento >= cada_cuanto:
            await mantenimiento(motor, inquilino, ahora=ahora)
            ultimo_mantenimiento = ahora

        estado = await procesar_uno(motor, inquilino, manejador, ahora=ahora)
        if estado is None:
            # Cola vacia: se espera, pero de forma interrumpible. Un `sleep` a secas
            # retrasaria la parada hasta el final de la pausa.
            try:
                await asyncio.wait_for(parar.wait(), timeout=pausa.total_seconds())
            except TimeoutError:
                continue


# ==========================================================================
# LO QUE ESTE MODULO NO RESUELVE — declarado, no escondido
# ==========================================================================
#
# `bucle` recibe el inquilino con el que va a trabajar. NO sabe descubrir por su
# cuenta que inquilinos tienen trabajo pendiente, y eso NO es un descuido: es una
# decision de plataforma que todavia no esta tomada y que no se puede improvisar
# aqui sin romper el mecanismo que funda el producto.
#
# El nudo, en una frase: con `FORCE` RLS, NADIE ve la cola de todos los
# inquilinos. Ni el rol de aplicacion, ni el dueño de las tablas. Una sesion de
# alcance `agencia` ve toda la cola DE SU AGENCIA —eso ya alcanza para que un
# worker sirva a todos los clientes de una agencia—, pero para saber QUE AGENCIAS
# existen hace falta un dato que ninguna sesion de inquilino puede leer.
#
# Las salidas posibles, con lo que cuesta cada una:
#   (a) Un rol con `BYPASSRLS` para el despachador. **Descartada**: es exactamente
#       la escotilla que `rol.py` cierra a proposito, y abrirla vuelve decorativas
#       todas las politicas.
#   (b) `LISTEN`/`NOTIFY` desde `encolar`. Insuficiente sola: un aviso se pierde si
#       no hay nadie escuchando, asi que hace falta un barrido de respaldo — y ese
#       barrido es otra vez el mismo problema.
#   (c) ==Recomendada:== un rol **de despacho** distinto del de aplicacion, con
#       `SELECT` sobre UNA tabla de plataforma que solo tenga identificadores de
#       agencia y ninguna columna de inquilino. Ese rol aprende que agencias
#       existen y NADA mas: no alcanza ni una fila de ningun inquilino. El worker
#       abre despues su sesion de alcance `agencia` por cada una, como hoy.
#
# La (c) introduce un SEGUNDO camino de conexion, y «no existe una segunda forma
# de abrir conexion» (plan §4) es una regla del producto. Por eso no la toma este
# modulo por su cuenta: se registra como hallazgo (P-28 en
# `docs/heraldo-problemas.md`) y se decide con quien manda. Mientras tanto, todo
# lo que este archivo hace pasa por la unica sesion que existe hoy.
