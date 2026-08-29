"""La superficie HTTP de Heraldo: la primera ruta fija el patron de todas.

Aqui viven tres cosas y ninguna es decorativa:

1. **T-018 / RF-30 — los origenes se DECLARAN por entorno.** Fuera de desarrollo
   no hay comodin ni bucle local. El defecto del referente no fue «no habia
   CORS»: fue que un `localhost` puesto para desarrollar SOBREVIVIO a produccion.
2. **T-024 / RF-51 — dos sondas separadas** (`app/health.py`).
3. **T-025·bis / RNF-06 — nada irreversible sin confirmacion** que nombre QUE y
   CUANTO (`app/tenancy/confirmacion.py`), montada en la RUTA. Un guard que solo
   vive en la suite autoriza en produccion.

# WHY (no hay `app` de modulo): construir la aplicacion al importar obliga a que
# el entorno este completo para poder importar — y entonces cualquier
# herramienta que solo quiera leer el modulo (el linter, un guard estructural,
# la documentacion) necesita un DSN. Se sirve con la fabrica:
#
#     uvicorn app.main:crear_aplicacion --factory
#
# WHY (el alcance NO viaja en la peticion): esta capa no lee `alcance` de ninguna
# cabecera ni de ningun cuerpo. La identidad la resuelve un proveedor inyectado,
# que hoy **no existe** (lo construye T-015, `app/tenancy/auth.py`) y por eso
# falla cerrado: sin identidad no se atiende. Es lo contrario de inventar una
# autenticacion provisional, que es como se cuela una escalada de alcance.
#
# WHY (la bitacora tambien es un cerrojo, no un adorno): RF-10 exige registrar
# quien, que y cuando sobre datos de un cliente. La bitacora de solo insercion la
# construye T-017 (`app/audit/`). Hasta que exista, la ruta destructiva **no
# puede ejecutarse**: se le exige un registrador y el que trae por defecto se
# niega. Asi esta ruta no puede destruir nada sin dejar rastro ni siquiera por
# descuido — y el sitio donde T-017 se enchufa esta escrito, no supuesto.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.middleware.cors import CORSMiddleware

from app.health import crear_router_de_salud
from app.tenancy import Inquilino, crear_motor, sesion_de_inquilino
from app.tenancy.confirmacion import (
    ConfirmacionInvalida,
    ConfirmacionRequerida,
    Inventario,
    NoSePuedeContar,
    exigir_confirmacion,
    inventariar_baja_de_cliente,
)
from app.tenancy.inquilino import Alcance

# ---------------------------------------------------------------------------
# T-018 / RF-30 — entorno y origenes
# ---------------------------------------------------------------------------

#: De donde sale el entorno. Se declara; no se adivina.
VARIABLE_ENTORNO = "HERALDO_ENTORNO"
#: De donde salen los origenes. Lista separada por comas.
VARIABLE_ORIGENES = "HERALDO_ORIGENES_PERMITIDOS"

#: Los dos esquemas que un navegador puede poner en una cabecera `Origin`.
ESQUEMAS_ADMITIDOS = ("http", "https")

#: Nombres de maquina que significan «esta misma maquina». Viven aqui para poder
#: RECHAZARLOS, no para usarlos: este modulo nunca los propone como valor.
NOMBRES_DE_BUCLE_LOCAL = ("localhost", "0.0.0.0", "[::1]", "::1")  # noqa: S104

#: Fuera de desarrollo, la maquina de un origen tiene que ser un NOMBRE DNS: al
#: menos dos etiquetas y una terminacion alfabetica.
#:
#: # WHY (allowlist por FORMA, y por que no basta la lista de arriba) — MEDIDO: la
#: primera version comprobaba el bucle local con `ipaddress.ip_address()`, y esa
#: funcion NO acepta las formas abreviadas ni las bases alternativas que un
#: resolutor si acepta. Comprobado sobre este mismo codigo: `https://127.1`,
#: `https://2130706433`, `https://017700000001` y `https://0x7f.0.0.1` pasaban
#: como origenes legitimos de PRODUCCION — y `127.1` es bucle local de verdad, no
#: un caso de laboratorio. Enumerar formas de escribir una direccion es una
#: denylist, y siempre falta una (`feedback_denylist_por_allowlist`). Exigir un
#: nombre DNS las descarta TODAS de una vez —incluidas las que nadie ha inventado
#: todavia— porque ninguna direccion numerica termina en etiqueta alfabetica.
#: Lo levanto Crisol como hallazgo, y la sonda le dio la razon (P-19).
_NOMBRE_DNS = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*\.[a-z]{2,63}$"
)


class Entorno(StrEnum):
    """Los entornos declarados. `desarrollo` es el UNICO que relaja algo."""

    DESARROLLO = "desarrollo"
    PRUEBAS = "pruebas"
    PRODUCCION = "produccion"


class EntornoNoDeclarado(RuntimeError):
    """Sin entorno no se arranca. Adivinar `desarrollo` es como nace el defecto."""


class EntornoDesconocido(RuntimeError):
    """El valor declarado no es ninguno de los entornos que existen."""


class OrigenesNoDeclarados(RuntimeError):
    """Ni siquiera en desarrollo hay una lista por defecto: se declara siempre."""


class OrigenInvalido(ValueError):
    """El origen no se puede admitir en ese entorno, y el motivo va en el texto."""


def entorno_declarado(valor: str | None = None) -> Entorno:
    """Lee el entorno del ambiente. Falla cerrado por los dos lados.

    # WHY: falta -> error, y valor raro -> error. La tentacion es que un valor
    # desconocido caiga en `produccion` «por si acaso», pero eso convierte una
    # errata (`produccio`) en un despliegue silenciosamente distinto del que
    # alguien creia estar haciendo. Un arranque que se niega se arregla en un
    # minuto; un entorno equivocado se descubre en el incidente.
    """
    crudo = os.environ.get(VARIABLE_ENTORNO) if valor is None else valor
    if not crudo or not crudo.strip():
        raise EntornoNoDeclarado(
            f"falta {VARIABLE_ENTORNO}: el entorno se declara, no se adivina. "
            f"Valores admitidos: {', '.join(e.value for e in Entorno)}"
        )
    try:
        return Entorno(crudo.strip().lower())
    except ValueError as fallo:
        raise EntornoDesconocido(
            f"{VARIABLE_ENTORNO}={crudo!r} no es un entorno declarado. "
            f"Valores admitidos: {', '.join(e.value for e in Entorno)}"
        ) from fallo


def _es_bucle_local(host: str) -> bool:
    if host in NOMBRES_DE_BUCLE_LOCAL or host.endswith(".localhost"):
        return True
    try:
        direccion = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return (
        direccion.is_loopback
        or direccion.is_private
        or direccion.is_link_local
        or direccion.is_unspecified
    )


def validar_origen(origen: str, entorno: Entorno) -> str:
    """Un origen a la vez, con el entorno como parte del criterio.

    Rechaza, por este orden:

    - el **comodin** (`*`) y `null` — en `starlette`, `allow_origins=["*"]` junto
      con credenciales devuelve el origen de la peticion en la cabecera, o sea
      que refleja a CUALQUIERA. El comodin no es «abierto de mas»: es «abierto a
      todos, y encima con la sesion del usuario».
    - la **forma**: el navegador compara la cabecera `Origin` con el texto EXACTO
      del origen declarado. Un `https://panel.ejemplo.com/` con barra final no
      casa jamas, asi que la configuracion parece puesta y no admite a nadie.
    - el **bucle local y las direcciones privadas** fuera de desarrollo — RF-30.
    - el esquema **`http` fuera de desarrollo**: el portal en marca blanca vive en
      la Internet publica; un origen sin cifrar ahi significa que la sesion del
      cliente viaja en claro. Es la misma clase de defecto que el `localhost`
      superviviente: un valor de desarrollo que llego a produccion.
    """
    candidato = origen.strip()
    if not candidato:
        raise OrigenInvalido("hay un origen vacio en la lista declarada")
    if candidato == "*" or candidato.lower() == "null":
        raise OrigenInvalido(
            f"{candidato!r} es un comodin: con credenciales acaba reflejando el "
            "origen de quien pregunte. Los origenes se enumeran uno a uno (RF-30)"
        )

    partes = urlsplit(candidato)
    if partes.scheme not in ESQUEMAS_ADMITIDOS:
        raise OrigenInvalido(
            f"{candidato!r} no declara un esquema http/https: un origen es "
            "esquema + maquina + puerto, nada mas"
        )
    if not partes.hostname:
        raise OrigenInvalido(f"{candidato!r} no declara ninguna maquina")
    if partes.path or partes.query or partes.fragment or partes.username:
        raise OrigenInvalido(
            f"{candidato!r} lleva ruta, consulta o usuario. El navegador compara el "
            "origen por texto EXACTO: asi declarado no casaria con ninguna peticion "
            "y la lista parecera puesta sin admitir a nadie"
        )

    if entorno is Entorno.DESARROLLO:
        return candidato

    if _es_bucle_local(partes.hostname):
        raise OrigenInvalido(
            f"{candidato!r} apunta a esta misma maquina o a una red privada, y el "
            f"entorno es {entorno.value!r}. Un origen de desarrollo que sobrevive al "
            "despliegue es el defecto que RF-30 existe para impedir"
        )
    if partes.scheme != "https":
        raise OrigenInvalido(
            f"{candidato!r} viaja sin cifrar y el entorno es {entorno.value!r}: la "
            "sesion del cliente iria en claro"
        )
    if not _NOMBRE_DNS.match(partes.hostname):
        raise OrigenInvalido(
            f"{candidato!r} no declara un nombre DNS y el entorno es "
            f"{entorno.value!r}. Fuera de desarrollo, la maquina de un origen se "
            "nombra, no se direcciona: una direccion numerica se puede escribir de "
            "media docena de formas —`127.1`, `2130706433`, `0x7f.0.0.1`— y "
            "enumerarlas todas es una lista a la que siempre le falta una"
        )
    return candidato


def origenes_declarados(entorno: Entorno, valor: str | None = None) -> tuple[str, ...]:
    """La lista completa, ya validada. Vacia = error, tambien en desarrollo.

    # WHY: no hay lista por defecto en NINGUN entorno. Un valor por defecto
    # comodo para desarrollar es exactamente el `localhost` que despues aparece
    # en produccion — y ademas obligaria a escribir ese nombre en este archivo,
    # que es el sitio donde no debe estar.
    """
    crudo = os.environ.get(VARIABLE_ORIGENES, "") if valor is None else valor
    candidatos = [trozo.strip() for trozo in crudo.split(",") if trozo.strip()]
    if not candidatos:
        raise OrigenesNoDeclarados(
            f"falta {VARIABLE_ORIGENES}: los origenes admitidos se declaran por "
            f"entorno (aqui, {entorno.value!r}), separados por comas. No hay lista "
            "por defecto ni siquiera en desarrollo"
        )
    return tuple(validar_origen(candidato, entorno) for candidato in candidatos)


# ---------------------------------------------------------------------------
# Las dos costuras que esta tarea NO construye, y que fallan cerradas
# ---------------------------------------------------------------------------

ProveedorDeInquilino = Callable[[Request], Awaitable[Inquilino]]
Registrador = Callable[[Inquilino, Inventario], Awaitable[None]]


async def identidad_no_cableada(request: Request) -> Inquilino:
    """Sin autenticacion no se atiende. La construye T-015 (`tenancy/auth.py`)."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "todavia no hay identidad autenticada en este despliegue: el alcance se "
            "lee de la fila del usuario y ese camino aun no existe (T-015). Hasta "
            "entonces esta ruta no atiende a nadie"
        ),
    )


async def bitacora_no_cableada(inquilino: Inquilino, inventario: Inventario) -> None:
    """Sin bitacora no se destruye. La construye T-017 (`app/audit/`)."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "todavia no hay bitacora de solo insercion (T-017): una operacion "
            "irreversible sobre datos de un cliente no se ejecuta sin dejar quien, "
            "que y cuando (RF-10)"
        ),
    )


@dataclass(frozen=True, slots=True)
class Superficie:
    """Todo lo que la capa HTTP necesita, en un solo sitio y explicito."""

    entorno: Entorno
    origenes: tuple[str, ...]
    motor: AsyncEngine
    proveedor_de_inquilino: ProveedorDeInquilino
    registrador: Registrador


@dataclass(frozen=True, slots=True)
class OperacionAutorizada:
    """Lo que la compuerta entrega a la ruta: ya inventariado y ya confirmado."""

    inquilino: Inquilino
    inventario: Inventario
    conexion: AsyncConnection


async def confirmacion_de_operacion_destructiva(
    request: Request,
    cliente_id: UUID,
    confirmacion: str | None = None,
) -> AsyncIterator[OperacionAutorizada]:
    """La compuerta de RNF-06, en el camino de la peticion.

    Resuelve la identidad, abre LA transaccion de inquilino, inventaria por ese
    mismo camino y exige la confirmacion. La ruta solo recibe el control si las
    cuatro cosas salieron bien; si alguna falla, la transaccion se deshace sin
    haber tocado nada.
    """
    superficie: Superficie = request.app.state.superficie
    inquilino = await superficie.proveedor_de_inquilino(request)

    if inquilino.alcance is not Alcance.AGENCIA:
        # WHY: la baja de un cliente la ejecuta un operador de la agencia. El
        # portal del propio cliente no puede darse de baja a si mismo por esta
        # via — no porque no tenga derecho, sino porque ese camino tiene su
        # propio flujo (RF-49) y no es este.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="esta operacion la ejecuta un operador de la agencia",
        )

    async with sesion_de_inquilino(superficie.motor, inquilino) as conexion:
        try:
            inventario = await inventariar_baja_de_cliente(conexion, cliente_id=cliente_id)
        except NoSePuedeContar as fallo:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "no se puede contar lo que se destruiria",
                    "motivo": str(fallo),
                },
            ) from fallo

        try:
            exigir_confirmacion(inventario, confirmacion)
        except ConfirmacionRequerida as pide:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "falta la confirmacion explicita",
                    "que_se_destruye": inventario.como_dict(),
                    "como_confirmar": (
                        "repite la peticion con ?confirmacion=<confirmacion>, "
                        "usando el valor que acompana a este inventario"
                    ),
                },
            ) from pide
        except ConfirmacionInvalida as mala:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": str(mala),
                    "que_se_destruye": inventario.como_dict(),
                },
            ) from mala

        yield OperacionAutorizada(
            inquilino=inquilino, inventario=inventario, conexion=conexion
        )


def crear_aplicacion(
    *,
    entorno: Entorno | None = None,
    origenes: tuple[str, ...] | None = None,
    motor: AsyncEngine | None = None,
    proveedor_de_inquilino: ProveedorDeInquilino = identidad_no_cableada,
    registrador: Registrador = bitacora_no_cableada,
    tiempo_limite_de_salud: float | None = None,
) -> FastAPI:
    """Fabrica de la aplicacion. Todo lo que puede fallar, falla AQUI.

    Los parametros existen para poder medir la configuracion de PRODUCCION desde
    la suite sin variables de entorno globales: la prueba construye la
    aplicacion con `entorno=PRODUCCION` y comprueba por efecto que un origen no
    declarado se rechaza. Cuando no se pasan, salen del ambiente.
    """
    entorno_real = entorno if entorno is not None else entorno_declarado()
    if origenes is None:
        origenes_reales = origenes_declarados(entorno_real)
    else:
        origenes_reales = tuple(validar_origen(o, entorno_real) for o in origenes)

    aplicacion = FastAPI(
        title="Heraldo",
        summary="Plataforma multi-inquilino de agentes en marca blanca",
    )
    aplicacion.state.superficie = Superficie(
        entorno=entorno_real,
        origenes=origenes_reales,
        motor=motor if motor is not None else crear_motor(),
        proveedor_de_inquilino=proveedor_de_inquilino,
        registrador=registrador,
    )

    # WHY: `allow_origins` con la lista literal y NUNCA `allow_origin_regex`. El
    # defecto medido en el referente era una expresion regular; una lista no se
    # puede escribir «casi bien».
    aplicacion.add_middleware(
        CORSMiddleware,
        allow_origins=list(origenes_reales),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    salud = crear_router_de_salud(
        motor=aplicacion.state.superficie.motor,
        **({} if tiempo_limite_de_salud is None else {"tiempo_limite": tiempo_limite_de_salud}),
    )
    aplicacion.include_router(salud)

    @aplicacion.delete("/clientes/{cliente_id}", tags=["destructiva"])
    async def baja_de_cliente(
        operacion: Annotated[
            OperacionAutorizada, Depends(confirmacion_de_operacion_destructiva)
        ],
    ) -> dict:
        """Da de baja a un cliente. Irreversible: la cascada se lleva lo suyo.

        Cuando el control llega hasta aqui, la confirmacion YA se comprobo — no
        hay forma de entrar sin pasar por la compuerta, porque la compuerta es la
        que produce el argumento.
        """
        superficie: Superficie = aplicacion.state.superficie
        # La bitacora ANTES del borrado: si se registrara despues, un fallo entre
        # medias dejaria la destruccion hecha y sin rastro.
        await superficie.registrador(operacion.inquilino, operacion.inventario)
        resultado = await operacion.conexion.execute(
            text("DELETE FROM clientes WHERE id = :cliente"),
            {"cliente": UUID(operacion.inventario.identificador)},
        )
        # WHY (lo levanto Crisol, y la sonda le dio la razon — P-19): entre el
        # inventario y este borrado hay una ventana, aunque sea la misma
        # transaccion: en READ COMMITTED otra sesion puede llevarse la fila. Sin
        # esta comprobacion la respuesta diria «destruidas 2 filas» y la bitacora
        # habria registrado una destruccion que no ocurrio — el informe mentiria
        # en la direccion mas cara. Medido con una bitacora que borra por detras.
        #
        # WHY (por que no `SELECT ... FOR UPDATE` en el inventario): tomar el
        # cerrojo lo PREVENDRIA en vez de detectarlo, pero significa retener un
        # bloqueo de fila durante toda la operacion —bitacora externa incluida— y
        # eso es una decision de concurrencia con sus propias consecuencias.
        # Detectar y fallar cerrado es correcto y no cambia las reglas del juego.
        if resultado.rowcount != 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "el cliente dejo de existir entre el inventario y el borrado: no "
                    "se destruyo nada y la operacion se deshace entera. Vuelve a pedir "
                    "el inventario"
                ),
            )
        return {"destruido": operacion.inventario.como_dict()}

    return aplicacion
