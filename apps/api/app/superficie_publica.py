"""T-033 / RF-61 — lo que sale en TODA respuesta, y quien puede ver el mapa.

Dos afirmaciones, y ninguna se hereda del marco:

1. **Las cabeceras de seguridad se FIJAN**, en toda respuesta servida, tambien en
   las de error y tambien en las que produce un middleware antes de llegar a
   ninguna ruta.
2. **La exposicion de la documentacion de interfaz** —el esquema y el navegador
   interactivo— **es una decision declarada por entorno**: publica, autenticada o
   apagada, pero *elegida*.

# WHY (por que esto no es «poner unas cabeceras»): el valor por defecto de FastAPI
# publica `/docs`, `/redoc` y `/openapi.json` sin credencial. Eso es el mapa
# completo de la interfaz —cada ruta, cada parametro, cada forma de cuerpo— y
# ademas lleva el `title` de la aplicacion, que dice «Heraldo». Sobre el dominio
# de un cliente eso choca de frente con CE-17 (marca blanca de verdad). No hubo
# ninguna decision: hubo un valor por defecto que nadie miro.
#
# WHY (por que FALLA CERRADO y no cae en «apagada»): la tentacion es que, si falta
# la declaracion, la exposicion sea `apagada` «que es lo seguro». Pero entonces un
# despliegue al que se le olvido la variable arranca igual y en silencio — y el
# dia que alguien invierta el defecto por comodidad, nadie se entera. Aqui la
# ausencia **rompe el arranque**, igual que el entorno y que los origenes
# (`main.py`): un arranque que se niega se arregla en un minuto; una exposicion
# equivocada se descubre cuando el mapa ya lleva meses publicado.
#
# WHY (por que la politica de marcos vive en la SUPERFICIE y no es una constante
# global): el widget se **incrusta** en la pagina del cliente, o sea que su
# `frame-ancestors` NO puede ser `'none'`. Si esta capa fijara `'none'` para todo,
# el dia que el widget exista alguien lo quitaria *del sitio comun* para
# desbloquearse, y con ello lo quitaria tambien del panel y de la interfaz. Por
# eso la politica se **declara al construir la superficie**: cada una responde por
# la suya y ninguna puede aflojar la de la vecina sin decirlo.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# La decision declarada
# ---------------------------------------------------------------------------

#: De donde sale la decision. Se declara; no se hereda del marco.
VARIABLE_DOCUMENTACION = "HERALDO_DOC_INTERFAZ"


class ExposicionDeDocumentacion(StrEnum):
    """Las tres exposiciones que existen. No hay una cuarta implicita."""

    #: Cualquiera con la URL ve el mapa. Solo tiene sentido en desarrollo.
    PUBLICA = "publica"
    #: El mapa existe, pero lo ve un operador de la agencia autenticado.
    AUTENTICADA = "autenticada"
    #: No hay mapa. Las tres rutas responden lo mismo que una ruta que no existe.
    APAGADA = "apagada"


class DocumentacionNoDeclarada(RuntimeError):
    """Sin declaracion no se arranca. Ver el WHY de la cabecera."""


class DocumentacionDesconocida(RuntimeError):
    """El valor declarado no es ninguna de las exposiciones que existen."""


def exposicion_declarada(valor: str | None = None) -> ExposicionDeDocumentacion:
    """Lee la exposicion del ambiente. Falla cerrado por los dos lados.

    Falta -> error. Valor raro -> error. Nunca `PUBLICA` por defecto, que es
    justo lo que hace el marco y lo que esta tarea existe para corregir.
    """
    crudo = os.environ.get(VARIABLE_DOCUMENTACION) if valor is None else valor
    if not crudo or not crudo.strip():
        raise DocumentacionNoDeclarada(
            f"falta {VARIABLE_DOCUMENTACION}: la exposicion de la documentacion de "
            "interfaz se DECLARA por entorno. El valor por defecto del marco "
            "—publica, sin credencial y con el nombre del producto en el titulo— "
            "no vale como decision (RF-61). Valores admitidos: "
            f"{', '.join(e.value for e in ExposicionDeDocumentacion)}"
        )
    try:
        return ExposicionDeDocumentacion(crudo.strip().lower())
    except ValueError as fallo:
        raise DocumentacionDesconocida(
            f"{VARIABLE_DOCUMENTACION}={crudo!r} no es una exposicion declarada. "
            "Valores admitidos: "
            f"{', '.join(e.value for e in ExposicionDeDocumentacion)}"
        ) from fallo


# ---------------------------------------------------------------------------
# Las cabeceras
# ---------------------------------------------------------------------------

#: Las que no dependen ni del entorno ni de la superficie. Cada una con su motivo:
#:
#: - `X-Content-Type-Options` — sin esto, un cuerpo que el navegador «adivina»
#:   como HTML puede ejecutarse: el eco de un dato del cliente se vuelve guion.
#: - `Referrer-Policy` — una URL nuestra puede llevar identificadores de inquilino
#:   en la ruta; sin esto viajan al sitio de destino en cada salto.
#: - `Cross-Origin-Opener-Policy` — corta el acceso a nuestra ventana desde la
#:   pestana que nos abrio.
#: - `X-Permitted-Cross-Domain-Policies` — apaga el `crossdomain.xml` heredado de
#:   los complementos; es una via entre dominios que ya nadie mira.
#: - `Permissions-Policy` — una interfaz de programacion no necesita camara,
#:   microfono, ubicacion ni pagos. Denegarlos aqui evita heredarlos al incrustar.
CABECERAS_INVARIANTES: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "x-permitted-cross-domain-policies": "none",
    "permissions-policy": "geolocation=(), microphone=(), camera=(), payment=()",
}

#: HSTS. Un ano, y con subdominios porque cada cliente vive en uno.
#:
#: # WHY (por que NO en desarrollo): el navegador la recuerda por dominio, y
#: `localhost` es un dominio compartido por todos los proyectos de la maquina. Una
#: cabecera puesta «por si acaso» mientras se desarrolla deja al desarrollador sin
#: poder abrir NINGUN proyecto por `http` durante un ano, y deshacerlo no es
#: obvio. Se pone donde sirve: donde hay TLS.
CABECERA_HSTS: tuple[str, str] = (
    "strict-transport-security",
    "max-age=31536000; includeSubDomains",
)

#: La politica de contenido de una interfaz que devuelve JSON: no carga nada, no
#: se deja incrustar, no envia formularios.
CSP_INTERFAZ = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

#: La del navegador interactivo, que SI carga guion y hoja de estilo — del mismo
#: CDN que usa el marco. Se nombra el origen exacto en vez de aflojar a `'self'` o
#: al comodin: si manana el marco cambiara de CDN, esto se rompe a la vista en vez
#: de callarse.
ORIGEN_DEL_NAVEGADOR_INTERACTIVO = "https://cdn.jsdelivr.net"
CSP_DOCUMENTACION = (
    "default-src 'none'; "
    f"script-src {ORIGEN_DEL_NAVEGADOR_INTERACTIVO}; "
    f"style-src {ORIGEN_DEL_NAVEGADOR_INTERACTIVO} 'unsafe-inline'; "
    f"img-src {ORIGEN_DEL_NAVEGADOR_INTERACTIVO} data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

#: Las tres rutas del mapa. Viven aqui para poder APAGARLAS y para saber a cuales
#: les toca la politica de contenido de arriba.
RUTA_ESQUEMA = "/openapi.json"
RUTA_NAVEGADOR = "/docs"
RUTA_NAVEGADOR_ALTERNO = "/redoc"
RUTAS_DE_DOCUMENTACION = (RUTA_ESQUEMA, RUTA_NAVEGADOR, RUTA_NAVEGADOR_ALTERNO)


@dataclass(frozen=True, slots=True)
class PoliticaDeCabeceras:
    """Lo que esta superficie fija en cada respuesta. Se declara al construirla.

    `politica_de_contenido` es lo unico que cambia entre superficies: la interfaz
    y el panel no se dejan incrustar; el widget, por definicion, si. Ver el WHY de
    la cabecera del modulo sobre por que esto no es una constante global.
    """

    #: Si va HSTS. Lo decide el entorno: solo donde hay TLS.
    exigir_transporte_seguro: bool
    #: La politica de contenido de las respuestas que NO son documentacion.
    politica_de_contenido: str = CSP_INTERFAZ
    #: Cabeceras extra de esta superficie, si las hubiera.
    adicionales: dict[str, str] = field(default_factory=dict)

    def para(self, ruta: str) -> dict[str, str]:
        """Las cabeceras que le tocan a una ruta concreta."""
        cabeceras = dict(CABECERAS_INVARIANTES)
        cabeceras["content-security-policy"] = (
            CSP_DOCUMENTACION
            if ruta in RUTAS_DE_DOCUMENTACION
            else self.politica_de_contenido
        )
        if self.exigir_transporte_seguro:
            nombre, valor = CABECERA_HSTS
            cabeceras[nombre] = valor
        cabeceras.update({k.lower(): v for k, v in self.adicionales.items()})
        return cabeceras


class CabecerasDeSeguridad(BaseHTTPMiddleware):
    """Fija las cabeceras en TODA respuesta que salga por debajo de el.

    # WHY (por que un middleware y no un manejador por ruta): las respuestas que
    # mas importan son las que ninguna ruta produce — el 404 de una ruta que no
    # existe, el 400 del corte de CORS, el 500 de una excepcion no capturada. Un
    # decorador por ruta las deja todas fuera, que es exactamente como una
    # cabecera «puesta» acaba faltando donde hacia falta.
    #
    # WHY (por que se monta el ULTIMO, o sea el mas externo): el middleware de
    # CORS corta el `preflight` de un origen ajeno y responde el mismo, sin llamar
    # a lo que tiene debajo. Si las cabeceras estuvieran por dentro, esa respuesta
    # saldria pelada. `starlette` ejecuta el ultimo anadido como el mas externo.
    """

    def __init__(self, aplicacion, politica: PoliticaDeCabeceras) -> None:
        super().__init__(aplicacion)
        self._politica = politica

    async def dispatch(self, request: Request, call_next) -> Response:
        respuesta = await call_next(request)
        for nombre, valor in self._politica.para(request.url.path).items():
            # WHY: `setdefault` y no asignacion. Si una ruta futura declaro su
            # propia politica de contenido —el widget incrustable, por ejemplo—,
            # esta capa no se la pisa por detras. Lo que garantiza es que NINGUNA
            # respuesta salga sin ella.
            respuesta.headers.setdefault(nombre, valor)
        return respuesta


# ---------------------------------------------------------------------------
# El mapa de la interfaz
# ---------------------------------------------------------------------------


async def exigir_operador_de_agencia(request: Request):
    """Dependencia de la exposicion `AUTENTICADA`: el mapa es de la agencia.

    Devuelve 404 —no 403— a quien no lo sea: para el portal de un cliente, ese
    mapa no existe.

    # WHY (lo que esto NO tapa hoy, dicho en voz alta): mientras la identidad no
    # este cableada (T-015), el proveedor por defecto responde 503, y un 503 en
    # `/docs` frente al 404 de una ruta inexistente SIGUE distinguiendo. Por eso
    # produccion declara `apagada` hasta que T-015 exista; esta via es para el dia
    # que exista, y la prueba mide las dos cosas.
    """
    from app.tenancy.inquilino import Alcance

    superficie = request.app.state.superficie
    inquilino = await superficie.proveedor_de_inquilino(request)
    if inquilino.alcance is not Alcance.AGENCIA:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return inquilino


def montar_documentacion(
    aplicacion: FastAPI, *, exposicion: ExposicionDeDocumentacion
) -> None:
    """Monta —o no— las tres rutas del mapa, segun lo declarado.

    La aplicacion se construye SIEMPRE con las tres apagadas (`docs_url=None` y
    companeras) y aqui se vuelven a montar a mano si la decision lo dice. Asi el
    camino por defecto es el cerrado, y abrirlo cuesta una linea explicita.

    # WHY (por que `APAGADA` no monta NADA en vez de montar un 403): un 403 en
    # `/openapi.json` confirma que ahi hay un mapa. Sin ruta, esas tres URL
    # responden exactamente lo mismo que `/cualquier-cosa`: no existe. Es la misma
    # doctrina de RF-60 —no distinguir para no enumerar— aplicada al mapa.
    """
    if exposicion is ExposicionDeDocumentacion.APAGADA:
        return

    dependencias: Sequence = (
        [Depends(exigir_operador_de_agencia)]
        if exposicion is ExposicionDeDocumentacion.AUTENTICADA
        else []
    )

    @aplicacion.get(RUTA_ESQUEMA, include_in_schema=False, dependencies=dependencias)
    async def esquema(request: Request) -> JSONResponse:
        return JSONResponse(
            get_openapi(
                title=request.app.title,
                version=request.app.version,
                summary=request.app.summary,
                routes=request.app.routes,
            )
        )

    @aplicacion.get(RUTA_NAVEGADOR, include_in_schema=False, dependencies=dependencias)
    async def navegador() -> Response:
        return get_swagger_ui_html(openapi_url=RUTA_ESQUEMA, title=aplicacion.title)

    @aplicacion.get(
        RUTA_NAVEGADOR_ALTERNO, include_in_schema=False, dependencies=dependencias
    )
    async def navegador_alterno() -> Response:
        return get_redoc_html(openapi_url=RUTA_ESQUEMA, title=aplicacion.title)
