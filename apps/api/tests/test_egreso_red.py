"""T-300 (RF-04, RF-05, CE-07) — el guard de red, medido por efecto.

# WHY: este es el defecto #1 de la auditoria del producto de referencia, y un guard
# de SSRF tiene una forma de fallar que no se ve: pasar. Por eso aqui no hay ninguna
# prueba que se limite a llamar a la funcion y comprobar que no explota. Cada regla
# tiene su caso que DEBE ser rechazado y su **control** que debe pasar; sin el control,
# un guard que rechazara absolutamente todo saldria verde y seria inutil
# (`feedback_toda_sonda_lleva_control`).
#
# # WHY (por que casi nada de esto toca la red de verdad): las direcciones se dan como
# literales o por un resolutor inyectado, asi que la bateria mide la REGLA y no el
# estado del DNS del dia. Las dos unicas que usan el resolutor real son la del nombre
# que no existe —`.invalid` no resuelve nunca, por RFC 2606— y la de `localhost`, que
# prueba que el camino completo, con resolucion de verdad, tambien rechaza.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import httpx
import pytest

from egress.red import (
    MAXIMO_DE_REDIRECCIONES,
    DestinoRechazado,
    ErrorDeSalida,
    SalidaFallida,
    es_alcanzable,
    pedir,
    validar,
)

RAIZ = Path(__file__).resolve().parents[3]

#: Una direccion publica de verdad. Se usa como CONTROL: lo que debe seguir pasando.
PUBLICA = "93.184.216.34"

#: Los tres rangos privados de RFC 1918, por su PREFIJO. Las direcciones concretas se
#: DERIVAN de aqui (`_primera_de`) y no se escriben.
#:
#: # WHY: la primera version de esta bateria escribio tres direcciones a mano y las
#: tres describian una red real —dos de la agencia, y al corregirlas, una del rango de
#: un cliente—. El gate de publicabilidad rechazo las primeras en el CI y el guard de
#: aislamiento rechazo las segundas. Los dos tenian razon, y el arreglo de fondo no era
#: elegir mejor: era ==dejar de elegir==. La primera direccion de un prefijo no
#: describe la red de nadie, y ademas dice lo que la prueba quiere decir —*«todo el
#: rango»*— en vez de un ejemplo suelto que alguien tomaria por una direccion nuestra.
#: ==El fixture de un guard es el sitio mas probable para reintroducir justo lo que el
#: guard prohibe, porque ahi el material prohibido es legitimo== (P-41).
RANGOS_PRIVADOS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _primera_de(prefijo: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """La primera direccion utilizable del rango. Derivada, nunca escrita."""
    return ipaddress.ip_network(prefijo)[1]


def _resolutor(*direcciones: str):
    """Un resolutor que contesta lo que se le diga. No toca la red."""

    def resolver(host: str, puerto: int) -> tuple[str, ...]:  # noqa: ARG001
        return tuple(direcciones)

    return resolver


# ==========================================================================
# CONTROL — sin esto, todo lo de abajo pasaria aunque el guard rechazara todo
# ==========================================================================
def test_control_un_destino_publico_pasa() -> None:
    destino = validar(f"https://{PUBLICA}/aviso")
    assert destino.direccion == PUBLICA
    assert destino.puerto == 443
    assert destino.ruta == "/aviso"


def test_control_un_nombre_que_resuelve_a_publica_pasa() -> None:
    destino = validar("https://ejemplo.test/x", resolver=_resolutor(PUBLICA))
    assert destino.host == "ejemplo.test"
    assert destino.direccion == PUBLICA


# ==========================================================================
# RF-04 — la direccion
# ==========================================================================
@pytest.mark.parametrize(
    ("clase", "url"),
    [
        ("bucle local IPv4", "https://127.0.0.1/"),
        ("bucle local IPv6", "https://[::1]/"),
        *(
            (f"privada {rango}", f"https://{_primera_de(rango)}/")
            for rango in RANGOS_PRIVADOS
        ),
        ("enlace local — metadatos de nube", "https://169.254.169.254/latest/meta-data/"),
        ("CGNAT 100.64/10", "https://100.64.0.1/"),
        ("sin especificar", "https://0.0.0.0/"),
        ("difusion", "https://255.255.255.255/"),
        ("reservada 240/4", "https://240.0.0.1/"),
        ("banco de pruebas 198.18/15", "https://198.18.0.1/"),
        ("multicast IPv4", "https://224.0.0.1/"),
        ("multicast SSDP — descubrimiento en la LAN", "https://239.255.255.250/"),
        ("multicast IPv6 todos-los-nodos", "https://[ff02::1]/"),
        ("IPv6 unica local", "https://[fc00::1]/"),
        ("IPv6 de enlace local", "https://[fe80::1]/"),
        ("IPv4 mapeada dentro de IPv6", "https://[::ffff:127.0.0.1]/"),
        ("6to4 escondiendo el bucle local", "https://[2002:7f00:1::]/"),
        ("6to4 escondiendo una privada", "https://[2002:c0a8:1::]/"),
        ("Teredo con cliente en 192.0.2/24", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/"),
    ],
)
def test_cada_clase_de_direccion_interna_se_rechaza(clase: str, url: str) -> None:
    with pytest.raises(DestinoRechazado):
        validar(url)


def test_el_camino_completo_con_resolucion_real_tambien_rechaza() -> None:
    """`localhost` con el resolutor DE VERDAD.

    Sin esta, todo lo anterior mide la REGLA y nadie mide que el resolutor real este
    conectado a ella.
    """
    with pytest.raises(DestinoRechazado, match="globalmente enrutable"):
        validar("https://localhost/")


def test_un_nombre_que_no_resuelve_se_rechaza_no_se_ignora() -> None:
    """Fail-closed con el resolutor real: `.invalid` no resuelve nunca (RFC 2606)."""
    with pytest.raises(DestinoRechazado, match="no resuelve"):
        validar("https://no-existe-jamas.invalid/")


def test_si_UNA_sola_de_las_direcciones_es_interna_se_rechaza_el_destino_entero() -> None:
    """El nombre contesta una publica y una interna: es un rebinding, no suerte."""
    with pytest.raises(DestinoRechazado, match="rebinding"):
        validar("https://ejemplo.test/", resolver=_resolutor(PUBLICA, "127.0.0.1"))


def test_control_dos_direcciones_publicas_si_pasan() -> None:
    destino = validar("https://ejemplo.test/", resolver=_resolutor(PUBLICA, "1.1.1.1"))
    assert destino.direccion == PUBLICA


def test_una_lista_de_direcciones_vacia_se_rechaza() -> None:
    with pytest.raises(DestinoRechazado, match="vacia"):
        validar("https://ejemplo.test/", resolver=_resolutor())


# ==========================================================================
# RF-04 — la forma de la URL
# ==========================================================================
@pytest.mark.parametrize(
    ("clase", "url"),
    [
        ("sin cifrar", f"http://{PUBLICA}/"),
        ("archivo local", "file:///etc/passwd"),
        ("gopher — el clasico para hablar con servicios internos", "gopher://x/"),
        ("dict", "dict://x/"),
        ("ftp", "ftp://x/"),
        ("sin esquema", f"//{PUBLICA}/"),
    ],
)
def test_solo_sale_https(clase: str, url: str) -> None:
    with pytest.raises(DestinoRechazado, match="esquema"):
        validar(url)


def test_una_url_con_credenciales_dentro_se_rechaza() -> None:
    with pytest.raises(DestinoRechazado, match="credenciales"):
        validar(f"https://usuario:clave@{PUBLICA}/")


def test_un_puerto_que_no_es_el_443_se_rechaza() -> None:
    with pytest.raises(DestinoRechazado, match="puerto"):
        validar(f"https://{PUBLICA}:8080/")


@pytest.mark.parametrize("vacio", ["", "   ", "\n"])
def test_un_destino_vacio_no_es_un_destino(vacio: str) -> None:
    with pytest.raises(DestinoRechazado):
        validar(vacio)


def test_una_url_sin_equipo_se_rechaza() -> None:
    with pytest.raises(DestinoRechazado, match="equipo"):
        validar("https:///solo-ruta")


# ==========================================================================
# RF-05 — volver a comprobar EN EL MOMENTO DE USAR
# ==========================================================================
class _ResolutorQueCambia:
    """Contesta una cosa la primera vez y otra despues. Es el rebinding, en vivo."""

    def __init__(self, *respuestas: tuple[str, ...]) -> None:
        self._respuestas = list(respuestas)
        self.llamadas = 0

    def __call__(self, host: str, puerto: int) -> tuple[str, ...]:  # noqa: ARG002
        indice = min(self.llamadas, len(self._respuestas) - 1)
        self.llamadas += 1
        return self._respuestas[indice]


@pytest.mark.asyncio
async def test_un_destino_que_valido_al_guardarse_se_vuelve_a_comprobar_al_usarlo() -> None:
    """==El corazon de RF-05.==

    El destino se guardo siendo publico y ahora resuelve a una direccion interna:
    usarlo tiene que fallar, no confiar en lo que se comprobo ayer.
    """
    resolver = _ResolutorQueCambia((PUBLICA,), ("127.0.0.1",))

    # Momento de GUARDAR: pasa, y con razon.
    assert validar("https://ejemplo.test/aviso", resolver=resolver).direccion == PUBLICA

    # Momento de USAR: el mundo cambio debajo.
    with pytest.raises(DestinoRechazado, match="globalmente enrutable"):
        await pedir("https://ejemplo.test/aviso", resolver=resolver)


@pytest.mark.asyncio
async def test_control_si_el_destino_sigue_siendo_publico_el_envio_sale() -> None:
    resolver = _ResolutorQueCambia((PUBLICA,), (PUBLICA,))
    transporte = httpx.MockTransport(lambda _: httpx.Response(200, text="ok"))

    assert validar("https://ejemplo.test/aviso", resolver=resolver).direccion == PUBLICA
    respuesta = await pedir("https://ejemplo.test/aviso", resolver=resolver, transporte=transporte)
    assert respuesta.status_code == 200


# ==========================================================================
# La conexion va a la direccion VALIDADA, no al nombre
# ==========================================================================
@pytest.mark.asyncio
async def test_la_conexion_va_a_la_direccion_validada_y_el_nombre_sobrevive() -> None:
    """Lo que cierra la carrera: se comprobo una IP y el socket va a ESA IP.

    Y el servidor sigue viendo su nombre —en `Host` y en el nombre del certificado—,
    porque si no, fijar la direccion romperia el TLS y nadie usaria este guard.
    """
    vistas: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        return httpx.Response(204)

    await pedir(
        "https://ejemplo.test/aviso?x=1",
        resolver=_resolutor(PUBLICA),
        transporte=httpx.MockTransport(manejador),
    )

    assert len(vistas) == 1
    peticion = vistas[0]
    assert peticion.url.host == PUBLICA, "el socket fue al NOMBRE: la carrera sigue abierta"
    assert peticion.url.path == "/aviso"
    assert peticion.url.params["x"] == "1"
    assert peticion.headers["host"] == "ejemplo.test"
    assert peticion.extensions.get("sni_hostname") == "ejemplo.test"


# ==========================================================================
# Redirecciones: cada salto se revalida entero
# ==========================================================================
@pytest.mark.asyncio
async def test_una_redireccion_hacia_dentro_se_rechaza() -> None:
    """El agujero clasico: el destino declarado es publico e inocente, y contesta
    `302` hacia los metadatos de la nube."""

    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/"})

    with pytest.raises(DestinoRechazado, match="globalmente enrutable"):
        await pedir(
            "https://ejemplo.test/aviso",
            resolver=_resolutor(PUBLICA),
            transporte=httpx.MockTransport(manejador),
        )


@pytest.mark.asyncio
async def test_control_una_redireccion_hacia_otro_destino_publico_se_sigue() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.path == "/aviso":
            return httpx.Response(302, headers={"location": "/mudado"})
        return httpx.Response(200, text="llegue")

    respuesta = await pedir(
        "https://ejemplo.test/aviso",
        resolver=_resolutor(PUBLICA),
        transporte=httpx.MockTransport(manejador),
    )
    assert respuesta.status_code == 200
    assert respuesta.text == "llegue"


@pytest.mark.asyncio
async def test_una_cadena_de_redirecciones_sin_fin_se_corta() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://ejemplo.test/otra-vez"})

    with pytest.raises(DestinoRechazado, match=str(MAXIMO_DE_REDIRECCIONES)):
        await pedir(
            "https://ejemplo.test/aviso",
            resolver=_resolutor(PUBLICA),
            transporte=httpx.MockTransport(manejador),
        )


@pytest.mark.asyncio
async def test_una_redireccion_sin_destino_se_rechaza_no_se_adivina() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(DestinoRechazado, match="sin decir"):
        await pedir(
            "https://ejemplo.test/aviso",
            resolver=_resolutor(PUBLICA),
            transporte=httpx.MockTransport(manejador),
        )


@pytest.mark.asyncio
async def test_una_redireccion_hacia_una_url_con_credenciales_se_rechaza() -> None:
    """Lo levanto la revision cruzada, y esta cubierto — pero no estaba MEDIDO.

    # WHY: `urljoin` conserva el `usuario:clave@` del destino, asi que la comprobacion
    # de la URL lo caza en el salto igual que en la primera peticion. Que este cubierto
    # «por como funciona urljoin» no es garantia de nada mientras nadie lo compruebe:
    # el dia que alguien cambie como se resuelve el salto, esto dejaria de ser cierto
    # en silencio.
    """

    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://usuario:clave@93.184.216.34/robado"}
        )

    with pytest.raises(DestinoRechazado, match="credenciales"):
        await pedir(
            "https://ejemplo.test/aviso",
            resolver=_resolutor(PUBLICA),
            transporte=httpx.MockTransport(manejador),
        )


@pytest.mark.asyncio
async def test_un_fallo_de_RED_no_es_un_rechazo_del_guard() -> None:
    """==La distincion que tiene consecuencia de producto.==

    Un rechazo es una decision del guard y no se reintenta jamas. Un fallo de red es
    transitorio y el trabajo vuelve a la cola. Si las dos cosas llegaran como la misma
    excepcion, `entregar()` acabaria reintentando destinos prohibidos o tirando envios
    legitimos porque el DNS parpadeo.
    """

    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no hay ruta al equipo", request=peticion)

    with pytest.raises(SalidaFallida, match="se puede reintentar") as capturado:
        await pedir(
            "https://ejemplo.test/aviso",
            resolver=_resolutor(PUBLICA),
            transporte=httpx.MockTransport(manejador),
        )

    assert not isinstance(capturado.value, DestinoRechazado), (
        "un fallo de red llega como rechazo del guard: quien entrega no puede "
        "distinguir «prohibido» de «no contesta»"
    )
    assert isinstance(capturado.value, ErrorDeSalida), (
        "los dos comparten raiz, para quien quiera cazar ambos de una vez"
    )
    assert "ConnectError" in str(capturado.value), "el mensaje no dice QUE tipo de fallo fue"


@pytest.mark.asyncio
async def test_un_metodo_fuera_de_la_lista_se_rechaza() -> None:
    with pytest.raises(DestinoRechazado, match="metodo"):
        await pedir("https://ejemplo.test/", metodo="TRACE", resolver=_resolutor(PUBLICA))


# ==========================================================================
# Las dos comprobaciones de alcance, y por que son DOS
# ==========================================================================
def test_ninguna_comprobacion_de_alcance_es_redundante() -> None:
    """Cada predicado caza algo que el otro no, y `is_reserved` no anadiria nada.

    # WHY: es lo que impide que alguien simplifique `es_alcanzable` a `is_global` —que
    # es lo que decia la primera version, y dejaba pasar el multicast— o que anada un
    # tercer predicado decorativo que nadie sabra si sigue haciendo algo.
    """
    solo_la_caza_global = ipaddress.ip_address("127.0.0.1")
    assert not solo_la_caza_global.is_global and not solo_la_caza_global.is_multicast

    solo_la_caza_multicast = ipaddress.ip_address("239.255.255.250")
    assert solo_la_caza_multicast.is_global, (
        "si esta direccion dejara de dar is_global=True, la comprobacion de multicast "
        "seria redundante y habria que quitarla"
    )
    assert solo_la_caza_multicast.is_multicast
    assert not es_alcanzable(solo_la_caza_multicast)

    for reservada in ("240.0.0.1", "255.255.255.254", "100::1"):
        direccion = ipaddress.ip_address(reservada)
        assert direccion.is_reserved and not direccion.is_global, (
            f"{reservada} deja de estar cubierta por is_global: haria falta is_reserved"
        )


# ==========================================================================
# Lo que hace que este guard sea el UNICO — si no, es un consejo
# ==========================================================================
#: Modulos que ABREN una conexion. No entra `urllib.parse`, que solo analiza texto y
#: la usa `main.py` legitimamente: la distincion es «¿abre un socket?», no «¿se llama
#: parecido?».
CLIENTES_DE_RED = (
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
    "aiohttp",
    "http.client",
    "socket",
    "smtplib",
    "ftplib",
    "websockets",
    "pycurl",
)

_IMPORTA = re.compile(
    r"^[ \t]*(?:import|from)[ \t]+(" + "|".join(re.escape(m) for m in CLIENTES_DE_RED) + r")\b",
    re.MULTILINE,
)

#: Donde SI puede vivir un cliente de red, con su motivo escrito.
DONDE_SI: dict[str, str] = {
    "packages/egress": "es el guard: aqui vive la unica salida, y por eso existe",
    "apps/api/tests": (
        "la suite golpea la aplicacion con el transporte ASGI de httpx —en memoria, sin "
        "abrir ningun puerto— y necesita httpx para fabricar los transportes falsos de "
        "esta misma bateria"
    ),
}


def _modulos_del_producto() -> list[Path]:
    fuera = []
    for carpeta in ("apps/api/app", "apps/api/tests", "apps/worker", "packages"):
        for archivo in (RAIZ / carpeta).rglob("*.py"):
            if "__pycache__" in archivo.parts or "egg-info" in str(archivo):
                continue
            fuera.append(archivo)
    return fuera


def test_control_el_barrido_encuentra_modulos_que_mirar() -> None:
    """Sin este control, un barrido sobre una lista vacia sale verde sin mirar nada."""
    assert len(_modulos_del_producto()) >= 20


def test_ningun_modulo_fuera_del_guard_abre_una_conexion_de_red() -> None:
    """==Lo que convierte a `red.py` de consejo en cerradura.==

    # WHY: un guard que hay que acordarse de llamar no es un guard. El defecto #1 del
    # producto de referencia no fue que su validacion estuviera mal escrita — fue que
    # existia OTRO camino que no pasaba por ella. Esta prueba dice que aqui no lo hay:
    # si alguien escribe `import httpx` en el worker para «una llamada rapida», el CI
    # se pone rojo antes de que el segundo camino exista (`feedback_mecanismo_cableado_a_uno`).
    """
    culpables = []
    for archivo in _modulos_del_producto():
        relativa = archivo.relative_to(RAIZ).as_posix()
        if any(relativa.startswith(permitida) for permitida in DONDE_SI):
            continue
        for encontrado in _IMPORTA.finditer(archivo.read_text(encoding="utf-8")):
            culpables.append(f"{relativa}: importa {encontrado.group(1)!r}")

    assert culpables == [], (
        "hay un camino de salida de red fuera del guard unico (RF-04 dice «en todos los "
        "caminos, sin excepcion por tipo de destino»):\n  " + "\n  ".join(culpables)
    )


def test_el_barrido_de_caminos_distingue_lo_que_abre_socket_de_lo_que_no() -> None:
    """El sabotaje del barrido: si no distingue, pasa sin haber probado nada."""
    assert _IMPORTA.search("import httpx\n")
    assert _IMPORTA.search("from urllib.request import urlopen\n")
    assert _IMPORTA.search("    import socket\n")
    # Su control: lo que NO debe cazar.
    assert not _IMPORTA.search("from urllib.parse import urlsplit\n")
    assert not _IMPORTA.search("# import httpx en un comentario\n")


def test_ninguna_excepcion_del_barrido_esta_muerta() -> None:
    """Una excepcion que ya no apunta a nada parece cobertura y no la da."""
    for ruta, motivo in DONDE_SI.items():
        assert (RAIZ / ruta).is_dir(), f"la excepcion {ruta!r} no apunta a ninguna carpeta"
        assert motivo.strip(), f"la excepcion {ruta!r} no tiene motivo escrito"
