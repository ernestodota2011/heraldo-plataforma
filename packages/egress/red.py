"""T-300 (RF-04, RF-05, CE-07) — el guard de red UNICO. Aqui se cierra el defecto #1.

La auditoria del producto de referencia midio un **SSRF confirmado por efecto**: su
guard vivia dentro de las herramientas HTTP, asi que cualquier otro camino de salida
—el servidor de herramientas, el aviso web— salia sin pasar por el. RF-04 lo dice
con todas las letras: *«en todos los caminos, sin excepcion por tipo de destino»*.

Este modulo es ese unico camino, y su diseno se sostiene sobre cuatro decisiones:

1. **Se valida por ALLOWLIST, no por lista de direcciones prohibidas.** Solo pasa lo
   que es **globalmente enrutable**. Una lista de rangos privados es la foto del RFC
   del dia que se escribio: no ve el rango que IANA reserve manana, ni el que a nadie
   se le ocurrio (`feedback_denylist_por_allowlist`).

2. **Se conecta a la direccion que se VALIDO** (`DestinoValidado.direccion`), nunca
   al nombre. Validar un nombre y despues conectarse a el es la carrera clasica:
   entre las dos cosas el DNS puede contestar otra direccion —*rebinding*— y el guard
   habria dicho que si a una IP mientras el socket iba a otra.

3. **`pedir()` valida SIEMPRE**, en cada uso y en cada salto de redireccion. No
   existe forma de pasarle un destino ya validado para ahorrarse la comprobacion. Eso
   es RF-05 como mecanismo: no depende de que quien llame se acuerde.

4. **Fallar es rechazar.** Si el nombre no resuelve, si resuelve a nada, si una sola
   de sus direcciones no es global — se rechaza. Un guard que ante la duda deja pasar
   no es un guard (`feedback_fail_open_traga_al_guard`).

# WHY (por que rechaza si UNA sola direccion es interna, en vez de elegir una buena):
# un nombre que contesta a la vez `93.184.216.34` y `127.0.0.1` no es un servidor con
# suerte variable: es un rebinding en curso, o una configuracion rota. Elegir la buena
# y seguir seria tratar el sintoma. Se rechaza el destino entero.

# WHY (por que se desenvuelven las IPv6 que ESCONDEN una IPv4): `::ffff:127.0.0.1`,
# `2002:7f00:0001::` (6to4) y las de Teredo llevan una IPv4 dentro. Para `ipaddress`
# la de fuera puede ser perfectamente global; el paquete, en cambio, acaba en la IPv4
# de dentro. Se comprueban TODAS las capas, y basta que una no sea global para
# rechazar.

# WHY (por que solo `https` y solo el 443): un destino declarado por un inquilino sale
# de nuestra red con contenido suyo dentro; en claro, ese contenido lo lee cualquiera
# en el camino. Y restringir el esquema mata de paso una familia entera de trucos
# (`file://`, `gopher://`, `dict://`) que convierten un cliente HTTP descuidado en un
# lector de archivos locales. Es una decision, y se escribe aqui para que se pueda
# discutir en vez de descubrirse leyendo el codigo.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

#: Lo unico que puede salir. Allowlist, no lista de prohibidos.
ESQUEMAS_PERMITIDOS = frozenset({"https"})
PUERTOS_PERMITIDOS = frozenset({443})

#: Cuantos saltos de redireccion se siguen. Cada uno se REVALIDA entero.
#: # WHY: seguir redirecciones sin limite es una denegacion de servicio con permiso,
#: y seguirlas sin revalidar es el SSRF por la puerta de atras: un destino publico e
#: inocente contesta `302 Location: http://169.254.169.254/`.
MAXIMO_DE_REDIRECCIONES = 3

#: Segundos. Un destino que no contesta no puede dejar colgado a un worker.
TIEMPO_LIMITE = 10.0

#: Metodos que este guard sabe emitir. Allowlist tambien aqui.
METODOS_PERMITIDOS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})

#: Los codigos que HTTP define como redireccion con destino en `Location`.
REDIRECCIONES = frozenset({301, 302, 303, 307, 308})


class ErrorDeSalida(Exception):
    """Lo que puede salir mal al intentar salir. Tiene DOS hijos, y no son lo mismo."""


class DestinoRechazado(ErrorDeSalida, ValueError):
    """El guard dijo que NO. El motivo dice QUE regla lo rechazo.

    # WHY (el mensaje nombra la regla y la direccion, y eso es deliberado): la
    # direccion rechazada no es un secreto —la declaro el propio inquilino— y sin
    # ella el operador no puede arreglar su configuracion. Lo que NUNCA aparece aqui
    # es el contenido del mensaje ni ninguna credencial.
    """


class SalidaFallida(ErrorDeSalida):
    """El destino era legitimo y la red no llego: sin ruta, sin DNS, sin tiempo, TLS roto.

    # WHY (por que NO es un `DestinoRechazado`, aunque sea mas corto tener uno solo):
    # la diferencia tiene consecuencia de producto. Un rechazo es una decision del
    # guard y **no se reintenta jamas** — reintentar una direccion prohibida es
    # insistir en el error. Un fallo de red es transitorio y el trabajo **si vuelve a
    # la cola**. Con una sola excepcion, `entregar()` tendria que adivinar cual de las
    # dos cosas paso leyendo un texto, y acabaria reintentando destinos prohibidos o
    # descartando envios legitimos porque el DNS parpadeo.
    #
    # # WHY (por que no se deja escapar la excepcion cruda de la biblioteca): lo
    # levanto la revision cruzada, y es el mismo defecto que ya se acepto en P-39: un
    # fallo de transporte que sale como rastreo crudo lo lee quien opera como «el
    # codigo esta roto», no como «ese destino no contesta».
    """


@dataclass(frozen=True)
class DestinoValidado:
    """Un destino que paso el guard, con la direccion a la que hay que conectarse.

    `direccion` es lo importante: es la IP **ya comprobada**. Quien se conecte usa
    esto, no `host`. `host` sobrevive solo para la cabecera `Host` y para el nombre
    del certificado (SNI), que son cosas del protocolo, no del enrutado.
    """

    url: str
    esquema: str
    host: str
    puerto: int
    direccion: str
    ruta: str


# --------------------------------------------------------------------------
# Que direcciones son alcanzables
# --------------------------------------------------------------------------
def _capas(
    direccion: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> Iterator[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """La direccion, y toda IPv4 que venga escondida dentro de ella."""
    yield direccion
    if isinstance(direccion, ipaddress.IPv6Address):
        if direccion.ipv4_mapped is not None:
            yield direccion.ipv4_mapped
        if direccion.sixtofour is not None:
            yield direccion.sixtofour
        if direccion.teredo is not None:
            servidor, cliente = direccion.teredo
            yield servidor
            yield cliente


def es_alcanzable(direccion: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allowlist: alcanzable = **todas** sus capas son globales Y no son multicast.

    `is_global` es casi toda la respuesta, y ademas la parte que envejece bien: la
    mantiene la biblioteca estandar contra los registros de IANA, no nosotros.

    # WHY (por que `is_global` NO basta, y como se supo): al escribir la bateria se
    # barrio el espacio en vez de confiar en el nombre del predicado, y aparecio un
    # hueco real -- `224.0.0.1`, `239.255.255.250` (SSDP, la que usa media LAN para
    # descubrirse) y `ff02::1` dan **is_global=True** en Python, porque el predicado
    # se define contra los registros de direcciones *privadas* y el multicast no es
    # privado: es otra cosa. Un guard construido solo sobre `is_global` habria dejado
    # salir trafico hacia la propia red del despliegue.
    #
    # # WHY (por que no se anade tambien `is_reserved`): se midio, y sobra. `240.0.0.1`,
    # `255.255.255.254` y `100::1` ya salen con `is_global=False`. Una comprobacion
    # redundante con una justificacion al lado envejece hasta que nadie sabe si sigue
    # haciendo algo (`feedback_justificacion_caduca`); las dos que estan aqui son las
    # dos que la medicion dijo que hacian falta, y `test_ninguna_comprobacion_de_
    # alcance_es_redundante` las vuelve a medir en cada corrida.
    """
    return all(capa.is_global and not capa.is_multicast for capa in _capas(direccion))


def _resolver(host: str, puerto: int) -> tuple[str, ...]:
    """Todas las direcciones del nombre. Sin resolucion, se rechaza."""
    try:
        infos = socket.getaddrinfo(host, puerto, type=socket.SOCK_STREAM)
    except socket.gaierror as fallo:
        raise DestinoRechazado(
            f"el destino {host!r} no resuelve a ninguna direccion: sin poder comprobar "
            "a donde va, no sale (se falla cerrado)"
        ) from fallo
    direcciones = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not direcciones:
        raise DestinoRechazado(f"el destino {host!r} resolvio a una lista vacia de direcciones")
    return direcciones


def _direcciones_de(host: str, puerto: int, resolver) -> tuple[str, ...]:
    """Las direcciones del destino. **Un literal es su propia respuesta.**

    # WHY (por que el literal NO pasa por el resolutor): si el inquilino declaro
    # `https://169.254.169.254/`, la direccion ya esta escrita y no hay nada que
    # preguntar. Consultarla igual abre una puerta absurda —que la respuesta del
    # resolutor CONTRADIGA lo que dice la URL— y el socket acabaria yendo a la
    # direccion literal de todas formas, porque es la que viaja en la peticion.
    #
    # # WHY (como se supo): lo destapo la bateria. La prueba de la redireccion hacia
    # los metadatos de la nube salia en VERDE POR EL MOTIVO EQUIVOCADO: su resolutor
    # de mentira contestaba una direccion publica para cualquier nombre, literales
    # incluidos, asi que `169.254.169.254` se validaba como publica y lo que se
    # medía era el tope de saltos, no el rechazo. Un guard cuya comprobacion la puede
    # anular quien conteste el DNS no es un guard.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return resolver(host, puerto)
    return (host,)


# --------------------------------------------------------------------------
# RF-04: validar el destino
# --------------------------------------------------------------------------
def validar(url: str, *, resolver=_resolver) -> DestinoValidado:
    """Comprueba el destino ENTERO y devuelve la direccion a la que conectarse.

    `resolver` se inyecta para poder medir la carrera del DNS en la bateria; en
    produccion nadie le pasa nada y usa el resolutor del sistema.
    """
    if not isinstance(url, str) or not url.strip():
        raise DestinoRechazado("un destino vacio no es un destino")

    partes = urlsplit(url.strip())

    if partes.scheme not in ESQUEMAS_PERMITIDOS:
        raise DestinoRechazado(
            f"el esquema {partes.scheme!r} no puede salir. Solo se permite "
            f"{sorted(ESQUEMAS_PERMITIDOS)}: en claro el contenido lo lee cualquiera en "
            "el camino, y los demas esquemas convierten un cliente HTTP en un lector de "
            "archivos locales"
        )

    if partes.username is not None or partes.password is not None:
        raise DestinoRechazado(
            "el destino lleva credenciales dentro de la URL. No salen: acabarian en "
            "registros y en cabeceras de redireccion, y ademas confunden a los "
            "analizadores de URL, que es como se cuela un destino por otro"
        )

    host = partes.hostname
    if not host:
        raise DestinoRechazado(f"el destino {url!r} no nombra ningun equipo")

    try:
        puerto = 443 if partes.port is None else partes.port
    except ValueError as fallo:
        raise DestinoRechazado(f"el destino {url!r} lleva un puerto que no es un numero") from fallo
    if puerto not in PUERTOS_PERMITIDOS:
        raise DestinoRechazado(
            f"el puerto {puerto} no esta permitido. Solo {sorted(PUERTOS_PERMITIDOS)}: un "
            "puerto arbitrario es el camino corto a un servicio interno que no habla HTTP "
            "y contesta igual"
        )

    direcciones = _direcciones_de(host, puerto, resolver)

    # ==Sin esto, una resolucion VACIA pasaba entera==: el bucle de abajo no recorre
    # nada y el destino sale validado sin que nadie haya mirado una sola direccion —
    # el verde por ausencia, dentro del guard que existe para que no lo haya. La
    # comprobacion vivia solo en `_resolver`, es decir, en el camino por defecto; el
    # dia que alguien inyecta otro resolutor, o que `_resolver` cambia, la unica
    # defensa desaparece sin que nada se ponga rojo. Va donde se USA el dato.
    if not direcciones:
        raise DestinoRechazado(
            f"el destino {host!r} resolvio a una lista vacia de direcciones: no hay nada "
            "que comprobar, asi que no sale"
        )

    for cruda in direcciones:
        try:
            direccion = ipaddress.ip_address(cruda)
        except ValueError as fallo:
            raise DestinoRechazado(
                f"el destino {host!r} resolvio a algo que no es una direccion IP: {cruda!r}"
            ) from fallo
        if not es_alcanzable(direccion):
            raise DestinoRechazado(
                f"el destino {host!r} resuelve a {cruda}, que no es una direccion "
                "globalmente enrutable (privada, reservada, de bucle local, de enlace "
                "local, o con una IPv4 interna escondida dentro). Basta UNA para rechazar "
                "el destino entero: un nombre que contesta a la vez una direccion publica "
                "y una interna no es un servidor con suerte variable, es un rebinding"
            )

    return DestinoValidado(
        url=url.strip(),
        esquema=partes.scheme,
        host=host,
        puerto=puerto,
        direccion=direcciones[0],
        ruta=partes.path or "/",
    )


# --------------------------------------------------------------------------
# RF-05: volver a comprobar EN EL MOMENTO DE USAR
# --------------------------------------------------------------------------
def _url_fijada(destino: DestinoValidado, url: str) -> str:
    """La misma URL, apuntando a la direccion YA VALIDADA en vez de al nombre.

    # WHY: es lo que cierra la carrera. El nombre se resolvio una vez, se comprobo, y
    # a partir de ahi el socket va a esa IP. Aunque el DNS conteste otra cosa un
    # milisegundo despues, ya no interviene: nadie vuelve a preguntarle.
    """
    partes = urlsplit(url.strip())
    anfitrion = destino.direccion
    if ":" in anfitrion:  # literal IPv6: la URL lo exige entre corchetes
        anfitrion = f"[{anfitrion}]"
    cola = partes.path or "/"
    if partes.query:
        cola = f"{cola}?{partes.query}"
    return f"{destino.esquema}://{anfitrion}:{destino.puerto}{cola}"


def _cabecera_host(destino: DestinoValidado) -> str:
    """Lo que el servidor debe seguir viendo: su nombre, no la IP a la que fuimos."""
    return destino.host if destino.puerto == 443 else f"{destino.host}:{destino.puerto}"


async def pedir(
    url: str,
    *,
    metodo: str = "POST",
    contenido: bytes | None = None,
    cabeceras: Mapping[str, str] | None = None,
    resolver=_resolver,
    transporte: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """La UNICA salida de red del producto. Valida, fija la direccion y pide.

    Cada salto de redireccion vuelve a pasar por `validar()` entero: una respuesta
    `302` hacia una direccion interna se rechaza igual que si se hubiera pedido
    directamente, que es justo el agujero que deja seguir redirecciones a ciegas.
    """
    if metodo.upper() not in METODOS_PERMITIDOS:
        raise DestinoRechazado(
            f"el metodo {metodo!r} no esta permitido. Solo {sorted(METODOS_PERMITIDOS)}"
        )

    siguiente = url
    saltos = 0
    async with httpx.AsyncClient(
        timeout=TIEMPO_LIMITE,
        follow_redirects=False,
        transport=transporte,
    ) as cliente:
        while True:
            # ==RF-05 vive en esta linea==: se valida AHORA, en cada uso y en cada
            # salto, y no hay ninguna manera de entrar aqui con el trabajo ya hecho.
            destino = validar(siguiente, resolver=resolver)

            propias = {"Host": _cabecera_host(destino)}
            propias.update(dict(cabeceras or {}))

            try:
                respuesta = await cliente.request(
                    metodo.upper(),
                    _url_fijada(destino, siguiente),
                    content=contenido,
                    headers=propias,
                    # El certificado se valida contra el NOMBRE, no contra la IP a la que
                    # conectamos: sin esto, fijar la direccion romperia el TLS.
                    extensions={"sni_hostname": destino.host},
                )
            except httpx.HTTPError as fallo:
                # El mensaje nombra al destino y al TIPO de fallo, no el rastreo: quien
                # opera necesita saber a quien no se llego, no la pila de la biblioteca.
                # Y jamas se cita el contenido que se iba a enviar.
                raise SalidaFallida(
                    f"no se pudo alcanzar {destino.host!r} en {destino.direccion}: "
                    f"{type(fallo).__name__}. El destino no esta prohibido — la red fallo, "
                    "asi que esto se puede reintentar"
                ) from fallo

            if respuesta.status_code not in REDIRECCIONES:
                return respuesta

            adonde = respuesta.headers.get("location")
            if not adonde:
                raise DestinoRechazado(
                    f"el destino {destino.host!r} contesto {respuesta.status_code} sin decir "
                    "a donde: una redireccion sin destino no se adivina"
                )
            saltos += 1
            if saltos > MAXIMO_DE_REDIRECCIONES:
                raise DestinoRechazado(
                    f"el destino {destino.host!r} encadeno mas de {MAXIMO_DE_REDIRECCIONES} "
                    "redirecciones: se corta"
                )
            # Relativa a la URL del salto anterior, como manda HTTP — y vuelve al
            # principio del bucle, donde `validar()` la mira con los mismos ojos.
            siguiente = urljoin(destino.url, adonde)
