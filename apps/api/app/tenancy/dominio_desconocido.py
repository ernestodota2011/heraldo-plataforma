"""T-032 / RF-60 — la respuesta al dominio que no atendemos, y por que es UNA sola.

Una peticion cuyo dominio **no esta verificado para ningun inquilino** recibe
siempre **lo mismo** —mismo codigo y mismo cuerpo— sin importar si ese dominio:

1. es **desconocido** (nadie lo dio de alta nunca),
2. esta **dado de alta pero sin verificar** (alguien empezo y no termino),
3. pertenece a un **inquilino suspendido**.

# WHY (esto es AISLAMIENTO, no acabado): las respuestas que distinguen esos tres
# casos convierten la plataforma en un directorio de clientes consultable **sin
# credencial y desde fuera**. Quien sospeche que una empresa es cliente nuestro
# solo tiene que apuntar su dominio aqui: un 404 dice «no esta», un 403 dice
# «esta y no puedes», un 402 dice «esta y no paga». Tres codigos distintos son
# tres bits de la cartera regalados por peticion. RNF-05 lo prohibe por las otras
# tres superficies; esta era la cuarta y no tenia contrato.
#
# WHY (por que la respuesta se construye en UN solo sitio y la excepcion NO lleva
# NADA dentro): la forma barata de escribir esto es un `if` por caso, cada uno con
# su `raise`, y confiar en que los tres sigan iguales para siempre. No duran: el
# dia que alguien anada un motivo «para depurar» al caso suspendido, la fuga
# vuelve y ninguna prueba de ese cambio la mira. Aqui `DominioNoReconocido` es una
# excepcion **sin campos**: no se le puede pasar un motivo aunque se quiera, y la
# respuesta la fabrica `respuesta_de_dominio_no_reconocido()`, que no recibe
# argumentos. Los tres casos convergen ANTES de que exista una respuesta, asi que
# no pueden divergir. La prueba comprueba la propiedad; el diseno la hace cara de
# romper.
#
# WHY (por que 404 y no 403 ni 421): un 403 dice «esto existe y no te toca», y un
# 421 dice «este servidor no atiende ese nombre» —los dos confirman algo—. El 404
# es lo que recibe cualquier direccion que no lleva a ningun sitio: es la
# respuesta que **no anade informacion**.
#
# WHY (por que el cuerpo no nombra el producto): esta respuesta se sirve **sobre
# el dominio de un cliente**. Un cuerpo que diga «Heraldo» o «AetherLogik» rompe
# CE-17 justo en la pagina que nadie mira (RNF-10).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum

from fastapi import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_404_NOT_FOUND


class EstadoDeDominio(StrEnum):
    """En que situacion esta un dominio respecto de la plataforma."""

    #: Verificado y con un inquilino activo detras. El unico que se atiende.
    VERIFICADO = "verificado"
    #: Nadie lo dio de alta.
    DESCONOCIDO = "desconocido"
    #: Dado de alta, verificacion sin completar.
    SIN_VERIFICAR = "sin_verificar"
    #: Verificado en su dia; el inquilino esta suspendido.
    SUSPENDIDO = "suspendido"


#: Los tres que NO se atienden — y que, hacia fuera, son **el mismo**.
#:
#: # WHY (por que se derivan y no se enumeran a mano): asi, si manana aparece un
#: cuarto estado no atendido —«en migracion», «moroso»—, entra solo en este
#: conjunto y en las pruebas que lo recorren, en vez de quedarse fuera. Un estado
#: nuevo que nadie clasifique **no puede** colarse como atendible: atendible es
#: solo `VERIFICADO`, por definicion.
ESTADOS_NO_ATENDIDOS: frozenset[EstadoDeDominio] = frozenset(
    estado for estado in EstadoDeDominio if estado is not EstadoDeDominio.VERIFICADO
)

#: El cuerpo. Uno, fijo, sin nombres y sin eco de lo que se pidio.
#:
#: # WHY (por que no lleva el dominio ni la ruta): repetir lo que el visitante
#: mando parece inofensivo y es la via mas comun de convertir una respuesta fija
#: en una respuesta variable — y de paso mete texto de un tercero en una pagina
#: nuestra. El cuerpo no depende de la peticion en ninguna de sus letras.
CUERPO_UNICO: dict[str, str] = {"error": "no encontrado"}
CODIGO_UNICO: int = HTTP_404_NOT_FOUND


class DominioNoReconocido(Exception):  # noqa: N818 - no es un error, es un veredicto
    """Se lanza para los tres estados no atendidos. **No lleva nada dentro.**

    # WHY (el `__init__` vacio NO es ceremonia): una subclase de `Exception`
    # acepta `*args` sin rechistar, asi que `raise DominioNoReconocido("suspendido")`
    # funcionaria y el motivo quedaria guardado en `.args` — de donde alguien
    # acabaria sacandolo «solo para el registro» y, un dia, para la respuesta. Con
    # este constructor esa linea **no llega a ejecutarse**: revienta con `TypeError`
    # en el sitio donde la escribieron. Lo comprobo la prueba, que tumbo la primera
    # version de este mismo comentario: yo habia escrito que no se le podia pasar un
    # motivo, y se podia.
    #
    # # WHY (lo que esto NO impide, dicho en voz alta): asignar el atributo despues
    # (`fallo = DominioNoReconocido(); fallo.motivo = ...`). No hay forma de cerrar
    # esa puerta en Python, y tampoco es el camino por el que vuelve la fuga: el
    # camino real es pasarlo al lanzarla. Lo que se cierra es ese.
    """

    def __init__(self) -> None:
        super().__init__()


def respuesta_de_dominio_no_reconocido() -> Response:
    """LA respuesta. Sin argumentos: no hay nada que pueda variar entre casos."""
    return JSONResponse(status_code=CODIGO_UNICO, content=CUERPO_UNICO)


async def manejar_dominio_no_reconocido(
    request: Request, excepcion: DominioNoReconocido
) -> Response:
    """Manejador de la aplicacion. Ignora la peticion y la excepcion a proposito."""
    return respuesta_de_dominio_no_reconocido()


# ---------------------------------------------------------------------------
# La costura que resuelve el dominio — y que hoy NO existe, asi que falla cerrado
# ---------------------------------------------------------------------------

ResolutorDeDominio = Callable[[str], Awaitable[EstadoDeDominio]]


async def sin_registro_de_dominios(dominio: str) -> EstadoDeDominio:
    """El resolutor por defecto: **todo dominio es desconocido**.

    # WHY: el registro de dominios verificados todavia no existe (no hay tabla y
    # no hay alta). Lo honesto mientras tanto es no atender a nadie por nombre, en
    # vez de inventar una resolucion provisional — que es como se cuela un dominio
    # atendido que nadie verifico. Cuando exista el registro se sustituye esta
    # costura por el que consulta la base; el contrato de arriba no cambia.
    """
    return EstadoDeDominio.DESCONOCIDO


def dominio_de(request: Request) -> str:
    """El nombre de maquina que el visitante pidio, sin puerto y en minusculas.

    # WHY (por que `url.hostname` y no la cabecera `Host` en crudo): el crudo trae
    # el puerto (`ejemplo.com:8443`), puede venir en mayusculas y puede venir dos
    # veces. Tres formas de escribir el mismo dominio son tres entradas distintas
    # en cualquier comparacion — y la que falla abre el paso o lo cierra, segun el
    # dia.
    """
    return (request.url.hostname or "").lower()


async def exigir_dominio_atendible(request: Request) -> EstadoDeDominio:
    """Compuerta de las superficies servidas bajo el dominio de un cliente.

    Devuelve el estado solo cuando es atendible; en los otros tres lanza la
    excepcion sin contenido. ==Es un `is VERIFICADO`, no un `not in NO_ATENDIDOS`==:

    # WHY: preguntar «¿no esta en la lista de prohibidos?» es una denylist, y a una
    # denylist siempre le falta una entrada — un estado nuevo que nadie anada a esa
    # lista quedaria **atendido**. Preguntar «¿es el unico que se atiende?» deja
    # fuera tambien lo que todavia no se ha inventado.
    """
    superficie = request.app.state.superficie
    estado = await superficie.resolutor_de_dominio(dominio_de(request))
    if estado is not EstadoDeDominio.VERIFICADO:
        raise DominioNoReconocido
    return estado
