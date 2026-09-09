"""T-100 (RF-18, B4) — la credencial del proveedor de IA: validada, cifrada, y de quien es.

RF-18: «CUANDO una agencia registra las credenciales de su proveedor de IA, EL SISTEMA
DEBE validarlas contra el proveedor antes de aceptarlas. El costo del modelo lo paga
siempre el dueño de la credencial». Este modulo hace cuatro cosas que se sostienen entre si:

1. **Solo proveedores de la lista declarada, y solo con ficha de condiciones.** Un proveedor
   fuera de la lista no se puede registrar; uno de la lista SIN condiciones de datos
   verificadas y fechadas, tampoco (RF-18 ampliado el 2026-09-08). Hoy ningun proveedor de
   la lista tiene ficha: la verifica y la fecha T-100·bis. Hasta entonces el registro de
   credenciales esta CERRADO por diseño, no abierto «mientras tanto».
2. **Se valida contra el proveedor antes de guardar**, por el guard de red unico (RF-04): la
   unica salida es `egress.red.pedir`, y aqui no se importa ningun cliente HTTP.
3. **Se guarda cifrada y atada al inquilino** (T-016): este modulo no cifra nada por su
   cuenta, usa `tenancy.secrets`. Y no la devuelve en claro por ninguna via: lo que sale
   es un `CredencialResuelta`, que ES un `SecretoEnClaro` y el barrido de respuestas lo
   rechaza por tipo.
4. **Decide QUIEN paga (B4).** La credencial que usa un heraldo es la de su cliente. La
   clave de agencia existe para una sola cosa: altas marcadas `desarrollo` —operadas por
   la agencia, sin cliente real ni datos de personas reales— y su consumo cuenta contra el
   techo DE LA AGENCIA. Ningun heraldo de un cliente real la consume: si un cliente real no
   tiene credencial propia, se queda sin credencial y lo dice.

# WHY (la validacion es un GET al catalogo de modelos y no una generacion): cuesta cero
# tokens, no manda ningun contenido del inquilino al proveedor, y contesta con la misma
# credencial que despues usara el heraldo. Los dos endpoints existen y contestan `401`
# sin clave (medido el 2026-09-09).

# WHY (solo `200` acepta; `401`/`403` rechazan; TODO lo demas es «no verificable»): un
# `429`, un `5xx` o un fallo de red no dicen que la credencial sea buena — dicen que hoy
# no se pudo saber. Guardar «por si acaso» seria aceptar sin validar, que es justo lo que
# RF-18 prohibe. Se falla cerrado y se puede reintentar.

# WHY (ningun error cita la credencial, ni al rechazarla): RF-09 «ni siquiera al
# rechazarlo». Los mensajes nombran al proveedor y al codigo de respuesta, nada mas.

# WHY (`CredencialResuelta` hereda de `SecretoEnClaro` en vez de envolverlo): el barrido
# de CE-06 rechaza por TIPO. Un envoltorio nuevo con el secreto dentro pasaria el barrido
# —no es `bytes` ni `SecretoEnClaro`— y saldria en una respuesta con el secreto adentro.
# Heredando, el barrido lo caza sin que nadie tenga que enseñarle un tipo mas.

# WHY (lo que este modulo NO hace): no cuenta el gasto (T-111 recibe `titular` y carga el
# techo que toca), no valida la clave de agencia contra el proveedor al resolverla (se
# valido al ponerla en el manojo; aqui solo se lee), y no verifica ninguna ficha de
# condiciones: eso es T-100·bis, y por eso la lista nace con `condiciones=None`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

from app.tenancy.inquilino import Alcance, Inquilino
from app.tenancy.secrets import SecretoEnClaro, SecretoNoDescifrable, guardar, leer
from egress.red import ErrorDeSalida, pedir

#: Prefijo del nombre con el que la credencial vive en `secretos`, por proveedor.
PREFIJO_DEL_SECRETO = "credencial_modelo"

#: La clave de agencia se declara POR ENTORNO y por proveedor, nunca en el repositorio.
PLANTILLA_DE_VARIABLE_DE_AGENCIA = "HERALDO_CREDENCIAL_DE_AGENCIA_{proveedor}"

#: Lo unico que se acepta como «la credencial es valida».
CODIGO_QUE_ACEPTA = 200
#: Lo unico que se lee como «el proveedor RECHAZO la credencial».
CODIGOS_QUE_RECHAZAN = frozenset({401, 403})


# --------------------------------------------------------------------------
# Errores. Ninguno cita la credencial.
# --------------------------------------------------------------------------
class ErrorDeProveedor(Exception):
    """Raiz de todo lo que este modulo levanta."""


class ProveedorNoDeclarado(ErrorDeProveedor, KeyError):
    """El proveedor no esta en la lista declarada: no se registra (falla cerrado)."""


class ProveedorSinCondicionesVerificadas(ErrorDeProveedor):
    """Esta en la lista pero nadie verifico ni fecho sus condiciones de datos."""


class UrlDeValidacionInvalida(ErrorDeProveedor, ValueError):
    """La lista declara una URL que el guard de red no admitiria: se cae al declararla."""


class CredencialVacia(ErrorDeProveedor, ValueError):
    """Una credencial vacia no se manda a validar: no hay nada que validar."""


class CredencialRechazadaPorElProveedor(ErrorDeProveedor):
    """El proveedor contesto que esa credencial no vale. No se guarda."""


class ProveedorNoVerificable(ErrorDeProveedor):
    """Hoy no se pudo saber si la credencial vale: no se acepta, se reintenta."""


class SinCredencial(ErrorDeProveedor, LookupError):
    """Este heraldo no tiene credencial que usar. La de agencia NO es la respuesta."""


class SesionSinCliente(ErrorDeProveedor, ValueError):
    """Un heraldo pertenece a un cliente; una sesion de agencia no resuelve credencial."""


# --------------------------------------------------------------------------
# La lista declarada
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FichaDeCondiciones:
    """Las condiciones de datos de un proveedor, verificadas y FECHADAS (T-100·bis).

    Cada campo dice lo que el proveedor hace con los datos que le mandamos, tal como
    lo verifico alguien en `fuente` el dia `verificada_en`. Caduca (spec §9 · B12).
    """

    no_entrenamiento_por_defecto: bool
    retencion: str
    acceso_humano: str
    ubicacion: str
    verificada_en: date
    fuente: str


@dataclass(frozen=True, slots=True)
class Proveedor:
    """Un proveedor de modelo que el producto sabe validar."""

    nombre: str
    url_de_validacion: str
    cabeceras: Callable[[SecretoEnClaro], Mapping[str, str]]
    condiciones: FichaDeCondiciones | None

    def __post_init__(self) -> None:
        partes = urlsplit(self.url_de_validacion)
        # El guard de red solo deja salir `https` al 443. Declararlo distinto aqui no
        # produciria un rechazo en el registro: produciria un proveedor que nunca valida.
        # Mejor que se caiga al declararlo, que es cuando alguien lo esta mirando.
        if partes.scheme != "https" or partes.port is not None or not partes.hostname:
            raise UrlDeValidacionInvalida(
                f"la URL de validacion de {self.nombre!r} tiene que ser https, sin puerto "
                "y con nombre: es lo unico que el guard de red deja salir (RF-04)"
            )


def _cabeceras_anthropic(credencial: SecretoEnClaro) -> Mapping[str, str]:
    return {"x-api-key": credencial.revelar(), "anthropic-version": "2023-06-01"}


def _cabeceras_openai(credencial: SecretoEnClaro) -> Mapping[str, str]:
    return {"Authorization": "Bearer " + credencial.revelar()}


#: ==Los proveedores que el producto sabe validar.== Ninguno tiene todavia ficha de
#: condiciones: T-100·bis las verifica y las fecha. Hasta entonces, `registrar_credencial`
#: rechaza a los dos, y eso es lo correcto — no una lista abierta «mientras tanto».
LISTA_DECLARADA: Mapping[str, Proveedor] = MappingProxyType(
    {
        "anthropic": Proveedor(
            nombre="anthropic",
            url_de_validacion="https://api.anthropic.com/v1/models",
            cabeceras=_cabeceras_anthropic,
            condiciones=None,
        ),
        "openai": Proveedor(
            nombre="openai",
            url_de_validacion="https://api.openai.com/v1/models",
            cabeceras=_cabeceras_openai,
            condiciones=None,
        ),
    }
)


def proveedor_declarado(
    nombre: str, lista: Mapping[str, Proveedor] = LISTA_DECLARADA
) -> Proveedor:
    """El proveedor de la lista, o el rechazo. No hay tercera salida."""
    try:
        proveedor = lista[nombre]
    except KeyError as fallo:
        raise ProveedorNoDeclarado(
            f"el proveedor {nombre!r} no esta en la lista declarada "
            f"({sorted(lista)}). Un proveedor fuera de la lista no se registra: "
            "exigirlo es una decision por cliente de Ernesto, registrada con su motivo"
        ) from fallo
    if proveedor.condiciones is None:
        raise ProveedorSinCondicionesVerificadas(
            f"el proveedor {nombre!r} esta en la lista pero sus condiciones de datos no "
            "estan verificadas ni fechadas (T-100·bis). Sin ficha, el piso legal no puede "
            "prometer nada sobre lo que hace con los datos — y entonces no se registra"
        )
    return proveedor


def nombre_del_secreto(proveedor: str) -> str:
    """Bajo que nombre vive la credencial de ese proveedor en `secretos`."""
    return f"{PREFIJO_DEL_SECRETO}:{proveedor}"


# --------------------------------------------------------------------------
# Registrar: validar contra el proveedor, y solo entonces guardar
# --------------------------------------------------------------------------
async def registrar_credencial(
    conexion,
    inquilino: Inquilino,
    *,
    proveedor: str,
    credencial: SecretoEnClaro,
    clave: bytes,
    pedir=pedir,
    lista: Mapping[str, Proveedor] = LISTA_DECLARADA,
) -> UUID:
    """Valida la credencial contra el proveedor y, SOLO si la acepta, la guarda cifrada.

    # WHY (`pedir` es un parametro con el guard como valor por defecto): la suite
    # necesita un proveedor que conteste lo que se le diga sin tocar la red. Y el valor
    # por defecto es `egress.red.pedir` — `test_la_validacion_sale_por_el_guard_de_red`
    # lo comprueba, para que nadie cambie el defecto por un cliente suelto.
    """
    declarado = proveedor_declarado(proveedor, lista)
    if not credencial.revelar().strip():
        raise CredencialVacia(
            f"la credencial de {proveedor!r} esta vacia: no hay nada que validar"
        )

    try:
        respuesta = await pedir(
            declarado.url_de_validacion,
            metodo="GET",
            cabeceras=declarado.cabeceras(credencial),
        )
    except ErrorDeSalida as fallo:
        # El guard ya redacto el motivo sin citar contenido. Aqui tampoco se cita nada.
        raise ProveedorNoVerificable(
            f"no se pudo validar la credencial de {proveedor!r}: {type(fallo).__name__}. "
            "No se guarda nada; se puede reintentar"
        ) from fallo

    codigo = respuesta.status_code
    if codigo in CODIGOS_QUE_RECHAZAN:
        raise CredencialRechazadaPorElProveedor(
            f"{proveedor!r} rechazo la credencial ({codigo}). No se guarda"
        )
    if codigo != CODIGO_QUE_ACEPTA:
        raise ProveedorNoVerificable(
            f"{proveedor!r} contesto {codigo} y eso no dice si la credencial vale. "
            "No se guarda nada; se puede reintentar"
        )

    return await guardar(
        conexion, inquilino, nombre=nombre_del_secreto(proveedor), valor=credencial, clave=clave
    )


# --------------------------------------------------------------------------
# Resolver: cual credencial usa este heraldo, y quien paga
# --------------------------------------------------------------------------
class Titular(StrEnum):
    """Quien es dueño de la credencial que se va a usar — y por tanto, quien paga."""

    CLIENTE = "cliente"
    AGENCIA = "agencia"


class CredencialResuelta(SecretoEnClaro):
    """La credencial que va a usar el heraldo, con su titular.

    ES un `SecretoEnClaro`: su `repr` es `[REDACTADO]` y el barrido de respuestas lo
    rechaza por tipo. `titular` es lo que T-111 necesita para cargar el techo correcto.
    """

    __slots__ = ("proveedor", "titular")

    def __init__(self, valor: str, *, proveedor: str, titular: Titular) -> None:
        super().__init__(valor)
        self.proveedor = proveedor
        self.titular = titular


def variable_de_agencia(proveedor: str) -> str:
    return PLANTILLA_DE_VARIABLE_DE_AGENCIA.format(proveedor=proveedor.upper())


def credencial_de_agencia(
    proveedor: str, entorno: Mapping[str, str] | None = None
) -> SecretoEnClaro | None:
    """La clave de agencia de ese proveedor, si el entorno la declara. Nunca se inventa."""
    valor = (os.environ if entorno is None else entorno).get(variable_de_agencia(proveedor))
    if not valor or not valor.strip():
        return None
    return SecretoEnClaro(valor)


_ES_DE_DESARROLLO = text("SELECT desarrollo FROM clientes WHERE id = :cliente_id")


async def resolver_credencial(
    conexion,
    inquilino: Inquilino,
    *,
    proveedor: str,
    clave: bytes,
    entorno: Mapping[str, str] | None = None,
) -> CredencialResuelta:
    """La credencial que usa el heraldo de este inquilino para ese proveedor.

    Orden, sin forma de saltarse ninguno:

    1. La sesion es de CLIENTE. Un heraldo pertenece a un cliente; una sesion de
       agencia no tiene credencial que resolver.
    2. Si el cliente registro la suya, es esa — titular CLIENTE — aunque sea de
       desarrollo y aunque exista clave de agencia.
    3. Si no la tiene y la fila dice `desarrollo`, la de agencia — titular AGENCIA,
       y su consumo cuenta contra el techo de la agencia (B4).
    4. Si no la tiene y NO es de desarrollo: `SinCredencial`. ==Aqui esta B4 como
       mecanismo==: la clave de agencia no se consulta siquiera, este cliente es
       real y no la toca.

    # WHY (la marca se lee de la FILA, en la sesion de inquilino): quien llama no
    # puede decir «es de desarrollo» por parametro — seria la misma escotilla que
    # `Inquilino` cerro con el alcance (plan §3.1): lo que decide dinero no se pide,
    # se deriva de lo que esta escrito.
    """
    if inquilino.alcance is not Alcance.CLIENTE:
        raise SesionSinCliente(
            "resolver una credencial exige una sesion de cliente: un heraldo pertenece a "
            "un cliente, y la de agencia no es la credencial de nadie"
        )
    nombre = nombre_del_secreto(proveedor)
    try:
        propia = await leer(conexion, inquilino, nombre=nombre, clave=clave)
    except SecretoNoDescifrable:
        propia = None
    if propia is not None:
        return CredencialResuelta(propia.revelar(), proveedor=proveedor, titular=Titular.CLIENTE)

    fila = (
        await conexion.execute(_ES_DE_DESARROLLO, {"cliente_id": inquilino.cliente_id})
    ).scalar_one_or_none()
    if fila is not True:
        # `None` (la fila no esta al alcance) y `False` (cliente real) terminan igual:
        # sin credencial. No se distingue hacia fuera y no se mira la de agencia.
        raise SinCredencial(
            f"este cliente no tiene credencial registrada para {proveedor!r}. La clave de "
            "agencia no es la respuesta: solo la consumen las altas marcadas 'desarrollo'"
        )
    de_agencia = credencial_de_agencia(proveedor, entorno)
    if de_agencia is None:
        raise SinCredencial(
            f"el alta es de desarrollo pero el entorno no declara "
            f"{variable_de_agencia(proveedor)}"
        )
    return CredencialResuelta(de_agencia.revelar(), proveedor=proveedor, titular=Titular.AGENCIA)
