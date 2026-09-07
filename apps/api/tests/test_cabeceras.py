"""T-033 (RF-61): las cabeceras que salen siempre, y quien ve el mapa.

Dos afirmaciones, medidas POR EFECTO sobre la aplicacion real:

1. **Toda respuesta servida lleva las cabeceras declaradas** — la de una ruta que
   existe, la de una ruta que NO existe, y la que produce el corte de CORS sin
   llegar a ninguna ruta. Esa tercera es la que de verdad prueba algo: es la que
   se escapa si el middleware no es el mas externo.
2. **La exposicion de la documentacion de interfaz es una decision declarada** —
   y quitar la declaracion **no puede caer en «publica»**.

# WHY (que pondria cada prueba en ROJO — el sabotaje que las audita):
# - Las de cabeceras: montar el middleware por DENTRO del de CORS (la respuesta
#   del `preflight` ajeno saldria pelada) o quitar la asignacion.
# - La de HSTS: ponerla tambien en desarrollo — y entonces el control de
#   desarrollo se pone rojo. Las dos mitades estan escritas.
# - Las de exposicion: devolver `PUBLICA` cuando falta la variable. Es LITERALMENTE
#   el defecto que RF-61 existe para corregir, y por eso tiene prueba propia en
#   los dos escalones (falta -> error; valor raro -> error).
# - La de `APAGADA`: montar un 403 en vez de no montar nada. La prueba compara
#   contra una ruta inexistente, asi que un 403 la pone roja: distinguir es
#   enumerar (misma doctrina que RF-60).
#
# WHY (lo que estas pruebas NO miden, dicho en voz alta): que el navegador OBEDEZCA
# las cabeceras. Se mide que salen y con que valor; el efecto en un navegador real
# —que la politica de contenido no rompa el navegador interactivo, que HSTS fuerce
# el salto a TLS— es del barrido de CE-18 sobre la superficie SERVIDA, que es otra
# cosa y esta declarada como otra cosa.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.health import RUTA_VIVACIDAD
from app.main import Entorno, crear_aplicacion
from app.superficie_publica import (
    CABECERA_HSTS,
    CABECERAS_INVARIANTES,
    CSP_DOCUMENTACION,
    CSP_INTERFAZ,
    ORIGEN_DEL_NAVEGADOR_INTERACTIVO,
    RUTA_NAVEGADOR,
    RUTAS_DE_DOCUMENTACION,
    VARIABLE_DOCUMENTACION,
    DocumentacionDesconocida,
    DocumentacionNoDeclarada,
    ExposicionDeDocumentacion,
    exposicion_declarada,
)
from app.tenancy import crear_motor
from app.tenancy.inquilino import Inquilino

ORIGEN_DECLARADO = "https://panel.aetherlogik.example"
ORIGEN_AJENO = "https://panel.impostor.example"

#: Igual que en `test_superficie`: un extremo donde no escucha nadie. Ninguna de
#: estas pruebas toca la base — la aplicacion se construye, no se consulta.
DSN_INALCANZABLE = "postgresql+psycopg://heraldo_app@127.0.0.1:1/no_existe"

#: Una ruta que no existe y que nunca va a existir. Es el patron de comparacion
#: de `APAGADA`: el mapa apagado tiene que responder EXACTAMENTE esto.
RUTA_QUE_NO_EXISTE = "/no-existe-y-nunca-existira"

AGENCIA = UUID("aaaaaaaa-0000-4000-8000-000000000001")
CLIENTE = UUID("aaaaaaaa-0000-4000-8000-0000000000a1")


def _identidad(inquilino: Inquilino):
    async def proveedor(request) -> Inquilino:
        return inquilino

    return proveedor


OPERADOR_DE_AGENCIA = _identidad(
    Inquilino.desde_usuario(agencia_id=AGENCIA, cliente_id=None)
)
PORTAL_DE_CLIENTE = _identidad(
    Inquilino.desde_usuario(agencia_id=AGENCIA, cliente_id=CLIENTE)
)


def _aplicacion(**extras: Any):
    parametros: dict[str, Any] = {
        "entorno": Entorno.PRODUCCION,
        "origenes": (ORIGEN_DECLARADO,),
        "motor": crear_motor(DSN_INALCANZABLE),
        "exposicion_de_documentacion": ExposicionDeDocumentacion.APAGADA,
    }
    parametros.update(extras)
    return crear_aplicacion(**parametros)


def _cliente(aplicacion) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=aplicacion), base_url="http://prueba"
    )


# ==========================================================================
# 1 — las cabeceras salen SIEMPRE
# ==========================================================================
@pytest.mark.parametrize("nombre", sorted(CABECERAS_INVARIANTES))
async def test_una_ruta_que_existe_lleva_cada_cabecera_declarada(nombre: str) -> None:
    async with _cliente(_aplicacion()) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD)
    assert respuesta.headers.get(nombre) == CABECERAS_INVARIANTES[nombre], (
        f"la respuesta de una ruta viva salio sin {nombre!r} o con otro valor "
        f"({respuesta.headers.get(nombre)!r}). RF-61 dice «toda respuesta servida»"
    )


@pytest.mark.parametrize("nombre", sorted(CABECERAS_INVARIANTES))
async def test_una_ruta_que_NO_existe_lleva_cada_cabecera_declarada(nombre: str) -> None:
    """El 404 no lo produce ninguna ruta nuestra, y es donde mas se olvida."""
    async with _cliente(_aplicacion()) as cliente:
        respuesta = await cliente.get(RUTA_QUE_NO_EXISTE)
    assert respuesta.status_code == 404
    assert respuesta.headers.get(nombre) == CABECERAS_INVARIANTES[nombre], (
        f"el 404 salio sin {nombre!r}. Una cabecera que solo aparece en las rutas "
        "que alguien escribio no protege las respuestas que nadie escribio"
    )


async def test_la_respuesta_del_corte_de_CORS_tambien_las_lleva() -> None:
    """EL CASO QUE IMPORTA: la produce el middleware de CORS, no una ruta.

    Si `CabecerasDeSeguridad` no fuera el mas externo, esta respuesta saldria
    pelada — y es una respuesta que se sirve a un origen hostil.
    """
    async with _cliente(_aplicacion()) as cliente:
        respuesta = await cliente.options(
            RUTA_VIVACIDAD,
            headers={"Origin": ORIGEN_AJENO, "Access-Control-Request-Method": "GET"},
        )
    assert respuesta.status_code == 400, "el preflight ajeno deberia cortarse en CORS"
    faltan = [
        nombre
        for nombre in CABECERAS_INVARIANTES
        if respuesta.headers.get(nombre) != CABECERAS_INVARIANTES[nombre]
    ]
    assert not faltan, (
        f"la respuesta del corte de CORS salio sin {faltan}: el middleware de "
        "cabeceras no es el mas externo, asi que no alcanza a las respuestas que "
        "se producen ANTES de llegar a una ruta"
    )


async def test_la_politica_de_contenido_de_una_ruta_normal_es_la_estricta() -> None:
    async with _cliente(_aplicacion()) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD)
    assert respuesta.headers.get("content-security-policy") == CSP_INTERFAZ


async def test_las_rutas_del_mapa_declaran_el_origen_del_navegador_interactivo() -> None:
    """CONTROL de la de arriba: si TODAS llevaran la estricta, el navegador
    interactivo no cargaria ni su guion y «seguro» seria «roto»."""
    aplicacion = _aplicacion(
        exposicion_de_documentacion=ExposicionDeDocumentacion.PUBLICA
    )
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(RUTA_NAVEGADOR)
    politica = respuesta.headers.get("content-security-policy")
    assert politica == CSP_DOCUMENTACION
    assert ORIGEN_DEL_NAVEGADOR_INTERACTIVO in politica


# --------------------------------------------------------------------------
# HSTS: el caso y su control, porque depende del entorno
# --------------------------------------------------------------------------
@pytest.mark.parametrize("entorno", [e for e in Entorno if e is not Entorno.DESARROLLO])
async def test_fuera_de_desarrollo_la_respuesta_exige_transporte_seguro(entorno) -> None:
    aplicacion = _aplicacion(entorno=entorno)
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD)
    nombre, valor = CABECERA_HSTS
    assert respuesta.headers.get(nombre) == valor, (
        f"el entorno {entorno.value!r} sirvio sin {nombre!r}: la sesion del cliente "
        "puede acabar viajando en claro tras un solo salto sin cifrar"
    )


async def test_en_desarrollo_NO_se_exige_transporte_seguro() -> None:
    """EL CONTROL, y no es cosmetico: ver el WHY de `CABECERA_HSTS`.

    `localhost` es un dominio compartido por todos los proyectos de la maquina, y
    el navegador recuerda esta cabecera un ano.
    """
    aplicacion = _aplicacion(
        entorno=Entorno.DESARROLLO, origenes=("http://localhost:5173",)
    )
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD)
    nombre, _ = CABECERA_HSTS
    assert nombre not in respuesta.headers, (
        "desarrollo sirvio con HSTS: eso deja al desarrollador sin poder abrir "
        "NINGUN proyecto de esta maquina por http durante un ano"
    )


# ==========================================================================
# 2 — la exposicion del mapa es una DECISION, y falla cerrado
# ==========================================================================
def test_sin_declaracion_no_se_arranca_y_NO_cae_en_publica() -> None:
    """EL SABOTAJE que nombra la tarea: quitar la declaracion.

    Lo que no puede pasar es que caiga en `PUBLICA` —el defecto del marco— ni que
    caiga en silencio en `APAGADA`, que arrancaria un despliegue al que se le
    olvido la variable sin que nadie se entere.
    """
    with pytest.raises(DocumentacionNoDeclarada) as fallo:
        exposicion_declarada("")
    assert VARIABLE_DOCUMENTACION in str(fallo.value)


@pytest.mark.parametrize("crudo", ["publico", "PUBLIC", "si", "true", "0", "  "])
def test_un_valor_desconocido_no_cae_en_ninguna_exposicion_por_defecto(crudo) -> None:
    with pytest.raises((DocumentacionDesconocida, DocumentacionNoDeclarada)):
        exposicion_declarada(crudo)


@pytest.mark.parametrize("exposicion", list(ExposicionDeDocumentacion))
def test_control_las_tres_exposiciones_declaradas_si_se_leen(exposicion) -> None:
    """Sin este control, una funcion que rechaza TODO pasaria las dos de arriba."""
    assert exposicion_declarada(exposicion.value) is exposicion
    assert exposicion_declarada(f"  {exposicion.value.upper()}  ") is exposicion


def test_la_fabrica_no_trae_ninguna_exposicion_por_defecto() -> None:
    """La decision no tiene valor por defecto NI en la firma de la fabrica."""
    por_defecto = (
        inspect.signature(crear_aplicacion)
        .parameters["exposicion_de_documentacion"]
        .default
    )
    assert por_defecto is None, (
        f"la fabrica trae {por_defecto!r} como exposicion por defecto. Un valor por "
        "defecto aqui es exactamente lo que RF-61 prohibe: una exposicion que nadie "
        "decidio"
    )


# --------------------------------------------------------------------------
# APAGADA: indistinguible de una ruta que no existe
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ruta", RUTAS_DE_DOCUMENTACION)
async def test_apagada_responde_lo_mismo_que_una_ruta_inexistente(ruta: str) -> None:
    """No basta con «no sirve el mapa»: tiene que ser INDISTINGUIBLE.

    Un 403 en `/openapi.json` confirma que ahi hay un mapa que proteger.
    """
    async with _cliente(_aplicacion()) as cliente:
        patron = await cliente.get(RUTA_QUE_NO_EXISTE)
        respuesta = await cliente.get(ruta)
    assert (respuesta.status_code, respuesta.text) == (patron.status_code, patron.text), (
        f"{ruta} responde {respuesta.status_code}/{respuesta.text!r} y una ruta "
        f"inexistente responde {patron.status_code}/{patron.text!r}. La diferencia "
        "confirma que ahi hay algo: distinguir es enumerar"
    )


@pytest.mark.parametrize("ruta", RUTAS_DE_DOCUMENTACION)
async def test_control_publica_si_sirve_las_tres_rutas(ruta: str) -> None:
    """EL CONTROL. Sin el, una aplicacion que rompio el mapa entero —y no lo sirve
    en NINGUNA exposicion— pasaria la de arriba con las dos manos."""
    aplicacion = _aplicacion(
        exposicion_de_documentacion=ExposicionDeDocumentacion.PUBLICA
    )
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(ruta)
    assert respuesta.status_code == 200, (
        f"{ruta} salio {respuesta.status_code} con la exposicion `publica`: la "
        "decision dice que se sirve y no se sirve"
    )


# --------------------------------------------------------------------------
# AUTENTICADA: el mapa existe, pero es de la agencia
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ruta", RUTAS_DE_DOCUMENTACION)
async def test_autenticada_con_la_identidad_SIN_CABLEAR_no_sirve_el_mapa(ruta) -> None:
    """Con el proveedor por defecto —el que falla cerrado— no sale ningun mapa."""
    aplicacion = _aplicacion(
        exposicion_de_documentacion=ExposicionDeDocumentacion.AUTENTICADA
    )
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(ruta)
    assert (
        respuesta.status_code != 200
    ), f"{ruta} sirvio el mapa sin identidad ninguna: `autenticada` no autentica"


@pytest.mark.parametrize("ruta", RUTAS_DE_DOCUMENTACION)
async def test_autenticada_el_portal_de_un_cliente_recibe_un_404(ruta) -> None:
    """Para el portal de un cliente, ese mapa NO EXISTE — 404, no 403."""
    aplicacion = _aplicacion(
        exposicion_de_documentacion=ExposicionDeDocumentacion.AUTENTICADA,
        proveedor_de_inquilino=PORTAL_DE_CLIENTE,
    )
    async with _cliente(aplicacion) as cliente:
        patron = await cliente.get(RUTA_QUE_NO_EXISTE)
        respuesta = await cliente.get(ruta)
    assert respuesta.status_code == patron.status_code == 404, (
        f"{ruta} respondio {respuesta.status_code} a un inquilino de alcance "
        "cliente. Un 403 le confirma que el mapa existe"
    )


@pytest.mark.parametrize("ruta", RUTAS_DE_DOCUMENTACION)
async def test_control_autenticada_un_operador_de_agencia_SI_ve_el_mapa(ruta) -> None:
    """EL CONTROL: sin el, `autenticada` podria ser `apagada` con otro nombre."""
    aplicacion = _aplicacion(
        exposicion_de_documentacion=ExposicionDeDocumentacion.AUTENTICADA,
        proveedor_de_inquilino=OPERADOR_DE_AGENCIA,
    )
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(ruta)
    assert respuesta.status_code == 200, (
        f"{ruta} salio {respuesta.status_code} para un operador de la agencia: la "
        "exposicion `autenticada` no sirve el mapa a nadie, o sea que es `apagada` "
        "con otro nombre"
    )


# ==========================================================================
# Guard estructural: que volver al valor por defecto del marco NO pase inadvertido
# ==========================================================================
def test_la_fabrica_construye_la_aplicacion_con_las_tres_rutas_apagadas() -> None:
    """Se lee el ARBOL del codigo, no el comportamiento.

    # WHY: sin esto, alguien que quite `docs_url=None` reabre `/docs` en TODOS los
    # entornos y las pruebas de arriba siguen verdes — porque `montar_documentacion`
    # tambien monta las suyas y la primera que gane responde 200. El defecto
    # volveria por la puerta de atras, que es por donde entro la primera vez.
    """
    from app import main

    arbol = ast.parse(inspect.getsource(main))
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id == "FastAPI"
    ]
    assert len(llamadas) == 1, (
        f"esperaba una construccion de FastAPI, hay {len(llamadas)}"
    )
    apagadas = {
        palabra.arg: palabra.value
        for palabra in llamadas[0].keywords
        if palabra.arg in {"docs_url", "redoc_url", "openapi_url"}
    }
    faltan = {"docs_url", "redoc_url", "openapi_url"} - set(apagadas)
    assert not faltan, (
        f"la fabrica construye FastAPI sin apagar {sorted(faltan)}: vuelve el valor "
        "por defecto del marco, que publica el mapa sin credencial y con el nombre "
        "del producto en el titulo (RF-61, CE-17)"
    )
    encendidas = [
        nombre
        for nombre, valor in apagadas.items()
        if not (isinstance(valor, ast.Constant) and valor.value is None)
    ]
    assert not encendidas, f"{encendidas} no se apaga con None en la fabrica"
