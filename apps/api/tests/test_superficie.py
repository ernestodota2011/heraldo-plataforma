"""T-018 (RF-30) y T-024 (RF-51): la frontera de red y las dos sondas.

Dos afirmaciones distintas, medidas las dos POR EFECTO sobre la aplicacion real:

1. **RF-30** — en un entorno que no es de desarrollo, un origen NO declarado se
   rechaza. Se mide mirando la cabecera que de verdad sale, no la configuracion
   que acabamos de escribir. Y el control es el par: un origen declarado pasa.
   Ademas se comprueba la asimetria que da nombre a la tarea: el MISMO texto
   —`http://localhost:5173`— se admite en desarrollo y se rechaza fuera.
2. **RF-51** — con la dependencia caida, la sonda de DISPONIBILIDAD falla y la de
   VIVACIDAD no. Si las dos contestaran lo mismo, no habria dos sondas: habria
   una escrita dos veces.

# WHY (que pondria cada prueba en ROJO): la de CORS, que el middleware refleje un
# origen ajeno —lo que hace `allow_origins=["*"]` con credenciales— o que deje de
# admitir el declarado. La de las sondas, que la vivacidad empiece a mirar la
# base (dejaria de ser 200 con la base caida) o que la disponibilidad deje de
# mirarla (dejaria de ser 503). Cada una tiene su par: una defensa que rechaza a
# todo el mundo no es una defensa, es un producto roto.
#
# WHY (lo que NO se mide, declarado): la base «caida» de estas pruebas es una
# base INALCANZABLE —un extremo donde no escucha nadie y, en la sonda hermana, el
# Postgres REAL negando la conexion—, no el proceso de Postgres detenido. El
# contenedor de la base es un servicio del trabajo de CI y pararlo desde dentro
# de la suite se llevaria por delante a las otras 125 pruebas. Lo que se mide es
# exactamente lo que la sonda tiene que distinguir: la dependencia no contesta.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import make_url

from app import health
from app.health import RUTA_DISPONIBILIDAD, RUTA_VIVACIDAD
from app.main import (
    Entorno,
    EntornoDesconocido,
    EntornoNoDeclarado,
    OrigenesNoDeclarados,
    OrigenInvalido,
    crear_aplicacion,
    entorno_declarado,
    origenes_declarados,
    validar_origen,
)
from app.tenancy import crear_motor
from conftest import RAIZ
from test_escalada_alcance import _fuentes_de_la_aplicacion

#: Un origen legitimo de produccion. Se usa como CONTROL en casi todas.
ORIGEN_DECLARADO = "https://panel.aetherlogik.example"
#: Y uno que nadie declaro. Es el caso.
ORIGEN_AJENO = "https://panel.impostor.example"

#: El texto exacto del defecto que RF-30 existe para impedir: un origen de
#: desarrollo. La prueba lo pasa por los dos entornos para que la diferencia sea
#: la que se afirma —el ENTORNO— y no otra cosa.
ORIGEN_DE_DESARROLLO = "http://localhost:5173"

#: Los entornos que NO son de desarrollo. Se derivan del enum: si manana alguien
#: anade `preproduccion`, entra sola en la prueba en vez de quedarse sin medir.
ENTORNOS_QUE_NO_SON_DESARROLLO = tuple(
    entorno for entorno in Entorno if entorno is not Entorno.DESARROLLO
)

#: Un DSN cuyo extremo no escucha. `port=1` no es magia: es el puerto reservado
#: mas bajo, donde ni en el contenedor de CI ni en una maquina de desarrollo hay
#: nada — y el rechazo es inmediato, no un tiempo de espera.
DSN_INALCANZABLE = "postgresql+psycopg://heraldo_app@127.0.0.1:1/no_existe"


def _aplicacion_de_prueba(**extras: Any):
    """Aplicacion de PRODUCCION con un solo origen declarado."""
    parametros: dict[str, Any] = {
        "entorno": Entorno.PRODUCCION,
        "origenes": (ORIGEN_DECLARADO,),
    }
    parametros.update(extras)
    return crear_aplicacion(**parametros)


def _cliente(aplicacion) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=aplicacion), base_url="http://prueba"
    )


# ==========================================================================
# T-018 / RF-30 — los origenes se declaran por entorno
# ==========================================================================
async def test_un_origen_no_declarado_es_rechazado_en_produccion() -> None:
    """EL CASO: la aplicacion de produccion no devuelve cabecera para un ajeno.

    Sin `Access-Control-Allow-Origin`, el navegador descarta la respuesta: la
    pagina impostora no llega a leer nada. Se mide sobre la peticion real.
    """
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD, headers={"Origin": ORIGEN_AJENO})
    assert "access-control-allow-origin" not in respuesta.headers, (
        "la aplicacion de produccion devolvio cabecera de CORS a un origen que "
        f"nadie declaro ({respuesta.headers.get('access-control-allow-origin')!r}). "
        "Con credenciales, eso es exactamente lo que hace el comodin: reflejar a "
        "quien pregunte"
    )


async def test_control_un_origen_declarado_si_pasa_en_produccion() -> None:
    """EL CONTROL. Sin esto, una aplicacion que rechaza a TODOS pasaria la de arriba."""
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.get(RUTA_VIVACIDAD, headers={"Origin": ORIGEN_DECLARADO})
    assert respuesta.headers.get("access-control-allow-origin") == ORIGEN_DECLARADO, (
        "el origen declarado no recibio cabecera: la lista esta puesta y no admite "
        "a nadie, que es un producto roto disfrazado de producto seguro"
    )


async def test_el_preflight_de_un_origen_ajeno_se_corta_antes_de_la_ruta() -> None:
    """La otra mitad de CORS: la peticion previa del navegador."""
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.options(
            RUTA_VIVACIDAD,
            headers={"Origin": ORIGEN_AJENO, "Access-Control-Request-Method": "GET"},
        )
    assert respuesta.status_code == 400, (
        f"el preflight de un origen ajeno salio {respuesta.status_code}: tiene que "
        "cortarse en el middleware, no llegar a la ruta"
    )


async def test_control_el_preflight_de_un_origen_declarado_pasa() -> None:
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    async with _cliente(aplicacion) as cliente:
        respuesta = await cliente.options(
            RUTA_VIVACIDAD,
            headers={"Origin": ORIGEN_DECLARADO, "Access-Control-Request-Method": "GET"},
        )
    assert respuesta.status_code == 200
    assert respuesta.headers.get("access-control-allow-origin") == ORIGEN_DECLARADO


def test_el_mismo_localhost_se_admite_en_desarrollo_y_se_rechaza_fuera() -> None:
    """La asimetria que da nombre a la tarea, con el par completo.

    # WHY: es la prueba que separa «hay CORS» de «RF-30 esta cumplido». El texto
    # es EL MISMO en los dos lados; lo unico que cambia es el entorno. Si el
    # criterio dejara de mirar el entorno, uno de los dos lados caeria.
    """
    assert validar_origen(ORIGEN_DE_DESARROLLO, Entorno.DESARROLLO) == ORIGEN_DE_DESARROLLO

    for entorno in ENTORNOS_QUE_NO_SON_DESARROLLO:
        with pytest.raises(OrigenInvalido) as capturado:
            validar_origen(ORIGEN_DE_DESARROLLO, entorno)
        assert "misma maquina" in str(capturado.value), (
            f"en {entorno.value!r} el origen de desarrollo se rechazo por otra razon; "
            f"la razon importa: {capturado.value}"
        )


@pytest.mark.parametrize("comodin", ["*", "null", "NULL"])
def test_el_comodin_se_rechaza_en_todos_los_entornos(comodin: str) -> None:
    """Tambien en desarrollo: el comodin no es una comodidad, es una puerta."""
    for entorno in Entorno:
        with pytest.raises(OrigenInvalido) as capturado:
            validar_origen(comodin, entorno)
        assert "comodin" in str(capturado.value)


@pytest.mark.parametrize(
    "origen",
    [
        "http://127.0.0.1:3000",
        "https://192.168.1.40",
        "http://10.20.1.50:8080",
        "https://mi-portal.localhost",
        "https://panel.ejemplo.com/",
        "https://panel.ejemplo.com/panel",
        "panel.ejemplo.com",
        "ftp://panel.ejemplo.com",
        "http://panel.ejemplo.com",
        # Las formas abreviadas y en otra base de la MISMA direccion de bucle
        # local. `ipaddress.ip_address()` no reconoce ninguna, y un resolutor si
        # reconoce varias: `https://127.1` llega de verdad a esta maquina. Las
        # cuatro pasaban antes de exigir un nombre DNS (hallazgo de Crisol, P-19).
        "https://127.1",
        "https://2130706433",
        "https://017700000001",
        "https://0x7f.0.0.1",
    ],
)
def test_la_lista_de_produccion_rechaza_cada_forma_del_defecto(origen: str) -> None:
    """Bucle local (en todas sus formas), red privada, barra final y `http`."""
    with pytest.raises(OrigenInvalido):
        validar_origen(origen, Entorno.PRODUCCION)


def test_control_un_origen_bien_formado_de_produccion_se_admite() -> None:
    """El control de la de arriba: la lista no rechaza TODO."""
    assert validar_origen(ORIGEN_DECLARADO, Entorno.PRODUCCION) == ORIGEN_DECLARADO
    assert (
        validar_origen("https://portal.cliente.example:8443", Entorno.PRODUCCION)
        == "https://portal.cliente.example:8443"
    )


def test_la_aplicacion_de_produccion_no_se_puede_construir_con_un_comodin() -> None:
    """El comodin no llega ni a montarse: la fabrica se niega."""
    with pytest.raises(OrigenInvalido):
        crear_aplicacion(
            entorno=Entorno.PRODUCCION,
            origenes=("*",),
            motor=crear_motor(DSN_INALCANZABLE),
        )


def test_la_aplicacion_montada_no_lleva_ningun_comodin_ni_bucle_local() -> None:
    """Y se comprueba sobre la aplicacion YA CONSTRUIDA, no sobre la intencion.

    # WHY: validar la entrada y montar la salida son dos pasos, y el defecto
    # puede vivir en el segundo. Esto lee lo que de verdad quedo en el middleware.
    """
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    montados = [m for m in aplicacion.user_middleware if m.cls.__name__ == "CORSMiddleware"]
    assert montados, "la aplicacion no monta CORSMiddleware: no hay frontera que medir"
    opciones = montados[0].kwargs
    assert opciones.get("allow_origin_regex") is None, (
        "el middleware se monto con una expresion regular de origenes: es el defecto "
        "medido en el referente, donde un patron admitia el bucle local en produccion"
    )
    for origen in opciones["allow_origins"]:
        assert origen != "*"
        assert validar_origen(origen, Entorno.PRODUCCION) == origen


def test_ningun_modulo_de_la_aplicacion_usa_una_expresion_regular_de_origenes() -> None:
    """El defecto original era un patron. Que no vuelva a existir el sitio donde ponerlo.

    # WHY (por AST y no por texto): la primera version buscaba la cadena en el
    # archivo y se puso en rojo por el COMENTARIO de `main.py` que explica por
    # que no se usa. Un guard que no distingue «lo nombra» de «lo usa» obliga a
    # borrar la explicacion para pasar, y una regla que castiga documentar la
    # regla acaba borrandose. Aqui se mira si alguien lo PASA como argumento.
    #
    # WHY: el meta-control lo aporta `_fuentes_de_la_aplicacion`, que ya trae el
    # suyo en `test_escalada_alcance.py`: si la ruta se rompiera y no mirara
    # ningun archivo, aquel test se pone en rojo primero.
    """
    culpables: list[str] = []
    for ruta in _fuentes_de_la_aplicacion():
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and any(
                palabra.arg == "allow_origin_regex" for palabra in nodo.keywords
            ):
                culpables.append(f"{ruta.relative_to(RAIZ).as_posix()}:{nodo.lineno}")
    assert not culpables, (
        f"estos sitios declaran origenes por expresion regular: {culpables}. La lista "
        "de origenes se enumera; un patron se escribe «casi bien» y admite de mas — es "
        "el defecto medido en el referente"
    )


# --------------------------------------------------------------------------
# RF-30 — la declaracion falla cerrada por los dos lados
# --------------------------------------------------------------------------
def test_sin_entorno_declarado_no_se_arranca() -> None:
    with pytest.raises(EntornoNoDeclarado):
        entorno_declarado("")


def test_un_entorno_desconocido_no_cae_en_ninguno_por_defecto() -> None:
    """Una errata no puede convertirse en un despliegue distinto y silencioso."""
    with pytest.raises(EntornoDesconocido):
        entorno_declarado("produccio")


def test_control_los_entornos_declarados_si_se_leen() -> None:
    for entorno in Entorno:
        assert entorno_declarado(entorno.value) is entorno
        assert entorno_declarado(f"  {entorno.value.upper()}  ") is entorno


def test_sin_origenes_declarados_no_hay_lista_por_defecto() -> None:
    """Ni siquiera en desarrollo: el valor comodo de hoy es el defecto de manana."""
    for entorno in Entorno:
        with pytest.raises(OrigenesNoDeclarados):
            origenes_declarados(entorno, "")
        with pytest.raises(OrigenesNoDeclarados):
            origenes_declarados(entorno, " , , ")


def test_control_la_lista_declarada_se_lee_entera() -> None:
    leidos = origenes_declarados(
        Entorno.PRODUCCION, f" {ORIGEN_DECLARADO} , https://otro.example "
    )
    assert leidos == (ORIGEN_DECLARADO, "https://otro.example")


# ==========================================================================
# T-024 / RF-51 — dos sondas, no una escrita dos veces
# ==========================================================================
def test_la_vivacidad_no_puede_alcanzar_la_base() -> None:
    """Estructural: su firma no recibe nada, asi que no tiene con que mirar.

    Lo pondria en rojo anadirle un parametro — que es como se empieza a mezclar
    las dos sondas.
    """
    assert not inspect.signature(health.vivacidad).parameters, (
        "`vivacidad` recibe parametros: en cuanto reciba el motor dejara de ser una "
        "sonda de vivacidad y sera la de disponibilidad con otro nombre (RF-51)"
    )


def test_la_sonda_de_disponibilidad_no_consulta_ninguna_tabla() -> None:
    """Su unica consulta es `SELECT 1`, y eso se lee de la constante, no del texto.

    # WHY: la sonda no tiene inquilino declarado. Cualquier tabla bajo RLS la
    # haria abortar por RF-03, y entonces estaria midiendo el mecanismo de
    # aislamiento en vez de la disponibilidad del motor — un 503 permanente que
    # parece un problema de base de datos.
    """
    assert health._LATIDO.text.strip().lower() == "select 1", (
        f"el latido de la sonda es {health._LATIDO.text!r}: en cuanto nombre una tabla "
        "dejara de medir la disponibilidad y pasara a medir el aislamiento"
    )


async def test_con_la_base_caida_la_disponibilidad_falla_y_la_vivacidad_no() -> None:
    """LA prueba de T-024, en UNA sola aplicacion y con UN solo motor.

    Las dos sondas comparten proceso y comparten motor. Lo unico que las separa
    es lo que cada una decide mirar — y eso es justo lo que se mide aqui.
    """
    aplicacion = _aplicacion_de_prueba(motor=crear_motor(DSN_INALCANZABLE))
    async with _cliente(aplicacion) as cliente:
        viva = await cliente.get(RUTA_VIVACIDAD)
        disponible = await cliente.get(RUTA_DISPONIBILIDAD)

    assert viva.status_code == 200, (
        f"con la base inalcanzable, la vivacidad devolvio {viva.status_code}. Eso hace "
        "que el orquestador REINICIE el proceso una y otra vez mientras la base sigue "
        "caida, y el bucle de reinicio destruye el trabajo en curso"
    )
    assert viva.json() == {"estado": "vivo"}

    assert disponible.status_code == 503, (
        f"con la base inalcanzable, la disponibilidad devolvio {disponible.status_code}: "
        "el balanceador seguiria mandando trafico a un proceso que no puede atenderlo"
    )
    assert disponible.json()["estado"] == "no disponible"

    assert viva.json() != disponible.json(), (
        "las dos sondas contestaron lo mismo con la dependencia caida: eso no son dos "
        "sondas, es una escrita dos veces (RF-51)"
    )


def _dsn_que_el_servidor_real_rechaza(dsn_app: str) -> str:
    """El MISMO servidor, la MISMA credencial, una base que no existe.

    # WHY (y esto lo destapo la propia suite): la primera version usaba una
    # contrasena equivocada y salio **200** — el Postgres del CI arranca con
    # `POSTGRES_HOST_AUTH_METHOD: trust`, asi que acepta cualquier contrasena y
    # aquel «control» no discriminaba nada. Registrado como P-17. Una base
    # inexistente si la rechaza el servidor bajo `trust`, que es lo que hace
    # falta: que el fallo venga del SERVIDOR y no de la pila de red.
    """
    return make_url(dsn_app).set(database="base_que_no_existe").render_as_string(
        hide_password=False
    )


async def test_con_el_postgres_REAL_negando_la_conexion_pasa_lo_mismo(escenario) -> None:
    """Segunda forma de «la base no contesta»: el servidor REAL rechazando.

    # WHY: la de arriba mide un extremo donde no escucha nadie — el fallo ocurre
    # en la pila de red. Esta llega hasta el Postgres de verdad, que contesta y
    # NIEGA. Son dos capas distintas del mismo «no puedo atender», y una sonda
    # que solo cubriera una dejaria la otra sin medir.
    """
    aplicacion = _aplicacion_de_prueba(
        motor=crear_motor(_dsn_que_el_servidor_real_rechaza(escenario.dsn_app))
    )
    async with _cliente(aplicacion) as cliente:
        viva = await cliente.get(RUTA_VIVACIDAD)
        disponible = await cliente.get(RUTA_DISPONIBILIDAD)
    assert viva.status_code == 200
    assert disponible.status_code == 503, (
        "el Postgres real rechazo la conexion y la sonda de disponibilidad dijo que "
        "si: o no llega al servidor, o no mira su respuesta"
    )


async def test_control_con_la_base_en_pie_las_dos_sondas_contestan_que_si(motor) -> None:
    """EL CONTROL. Una disponibilidad que siempre falla pasaria las dos de arriba."""
    aplicacion = _aplicacion_de_prueba(motor=motor)
    async with _cliente(aplicacion) as cliente:
        viva = await cliente.get(RUTA_VIVACIDAD)
        disponible = await cliente.get(RUTA_DISPONIBILIDAD)
    assert viva.status_code == 200
    assert disponible.status_code == 200, (
        f"con la base REAL en pie la disponibilidad devolvio {disponible.status_code} "
        f"({disponible.json()}): la sonda esta rota, no la base"
    )
    assert disponible.json() == {"estado": "disponible", "dependencias": {"base": "ok"}}


async def test_una_dependencia_que_no_contesta_a_tiempo_es_no_disponible(motor) -> None:
    """El techo de tiempo tambien es una respuesta, y se mide con la base VIVA.

    # WHY: se usa el motor REAL y un techo de cero. Asi lo que se mide es la rama
    # del tiempo agotado de la sonda —que produce un 503 y no un 500— sin fingir
    # una base lenta que no existe. Con la base en pie, la unica razon por la que
    # esto puede salir 503 es el techo.
    """
    aplicacion = _aplicacion_de_prueba(motor=motor, tiempo_limite_de_salud=0.0)
    async with _cliente(aplicacion) as cliente:
        disponible = await cliente.get(RUTA_DISPONIBILIDAD)
        viva = await cliente.get(RUTA_VIVACIDAD)
    assert disponible.status_code == 503
    assert "sin respuesta" in disponible.json()["dependencias"]["base"]
    assert viva.status_code == 200, "el techo de tiempo no puede arrastrar a la vivacidad"


async def test_la_disponibilidad_no_filtra_el_dsn_en_su_respuesta(escenario) -> None:
    """Esta ruta no lleva autenticacion: su cuerpo no puede llevar el DSN.

    # WHY: el texto de un error de conexion de psycopg trae maquina, puerto y a
    # veces el usuario. Por eso la sonda nombra el TIPO del fallo, nunca su
    # mensaje.
    """
    aplicacion = _aplicacion_de_prueba(
        motor=crear_motor(_dsn_que_el_servidor_real_rechaza(escenario.dsn_app))
    )
    async with _cliente(aplicacion) as cliente:
        cuerpo = (await cliente.get(RUTA_DISPONIBILIDAD)).text
    for filtrado in ("base_que_no_existe", "127.0.0.1", "postgresql", "heraldo_app"):
        assert filtrado not in cuerpo, (
            f"la sonda de disponibilidad devolvio {filtrado!r} en su cuerpo, y esta "
            f"ruta no lleva autenticacion: {cuerpo}"
        )
