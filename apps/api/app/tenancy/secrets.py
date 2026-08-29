"""T-016 (RF-09, CE-06) — el secreto se cifra en reposo y NO SALE POR NINGUNA VIA.

Este modulo hace tres cosas que se sostienen entre si:

1. **Cifra en reposo** con una clave que vive en el entorno, nunca en el
   repositorio, y ata el cifrado al inquilino: un texto cifrado sacado de la fila
   de un inquilino no se puede descifrar como si fuera de otro.
2. **Serializa por ALLOWLIST.** El serializador no sabe que es un secreto — y esa
   es exactamente su virtud. Solo sabe que campos son PUBLICOS de cada recurso;
   todo lo demas se queda fuera por defecto.
3. **Barre toda respuesta** antes de que salga, y aborta si encuentra material que
   no puede estar ahi.

# WHY (allowlist y no denylist, `feedback_denylist_por_allowlist`): un
# serializador que conociera «los campos de secreto» seria una FOTO del dia que se
# escribio. El campo secreto de manana —el token del proveedor del canal, la clave
# del webhook, la cookie de sesion del portal— no esta en esa foto, y el dia que
# alguien lo anada a una tabla se filtraria sin que nada se ponga rojo. Aqui es al
# reves: un campo nuevo NO SALE hasta que alguien lo declare publico a proposito.
# La prueba de esa propiedad es su control: se le pasa al serializador una fila con
# un campo que nadie declaro y el campo no aparece en la salida.
#
# WHY (el serializador no nombra ninguna columna): la propiedad de arriba se puede
# perder de una forma muy concreta —alguien escribe `if campo == "cifrado":
# continue` y el serializador vuelve a ser una denylist con otro nombre—. Por eso
# `test_el_serializador_no_nombra_ninguna_columna_del_esquema` compara las cadenas
# literales de estas funciones contra los nombres de columna que existen EN LA
# BASE: no es una revision humana, es un rojo del CI.
#
# WHY (`bytea` y el rechazo por TIPO): el texto cifrado se guarda como `bytes`. El
# barrido rechaza `bytes` alli donde va una respuesta, asi que el material cifrado
# tiene un tipo que el barrido sabe cazar aunque alguien lo saque por un camino que
# nadie previo. Y el valor EN CLARO nunca es una cadena suelta: viaja envuelto en
# `SecretoEnClaro`, que el barrido tambien rechaza y cuyo `repr` es `[REDACTADO]`.
#
# WHY (RF-09 dice «ni siquiera al rechazarlo»): ningun error de este modulo cita
# el valor, ni el texto cifrado, ni un trozo de ninguno de los dos. Es la misma
# regla de la casa que prohibe ecoar una credencial incluso cuando se la rechaza:
# citarla para decir QUE se rechaza la deja escrita en el registro.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from app.tenancy.inquilino import Inquilino

#: Clave maestra de cifrado, en base64url. Se declara POR ENTORNO: en el
#: repositorio no vive ninguna credencial, ni siquiera de ejemplo.
VARIABLE_DE_ENTORNO_CLAVE = "HERALDO_SECRETOS_CLAVE"

#: AES-256-GCM. 32 bytes de clave, 12 de nonce — el tamano que el modo recomienda.
LONGITUD_DE_CLAVE = 32
LONGITUD_DE_NONCE = 12

#: Marca de version del formato del texto cifrado. Va DENTRO del material
#: autenticado, asi que nadie puede degradar el formato retocando un prefijo.
VERSION_DE_FORMATO = b"h1"


class ClaveNoDeclarada(RuntimeError):
    """No hay clave de cifrado en el entorno: se falla, no se inventa una."""


class ClaveInvalida(ValueError):
    """La clave existe pero no tiene la forma que AES-256-GCM exige."""


class SecretoNoDescifrable(ValueError):
    """El texto cifrado no corresponde a este inquilino, o esta alterado.

    Su mensaje NUNCA cita el valor ni el texto cifrado (RF-09).
    """


class SecretoEnLaRespuesta(AssertionError):
    """El barrido encontro material que no puede salir. Es un fallo, no un aviso."""


class RecursoNoDeclarado(KeyError):
    """Nadie declaro que campos de este recurso son publicos: no se adivina."""


class CampoDeclaradoQueNoLlega(KeyError):
    """La fila no trae un campo que el recurso declara publico: se falla ruidoso."""


class SecretoEnClaro:
    """El valor descifrado, envuelto para que no se pueda enseñar por descuido.

    # WHY: una cadena suelta acaba en un `print`, en un mensaje de error, en el
    # `repr` de un objeto que la contiene, en la salida de un depurador. Este
    # envoltorio corta las cuatro: su `repr` y su `str` dicen `[REDACTADO]`, el
    # barrido lo rechaza por TIPO, y el unico camino al valor es un metodo con
    # nombre incomodo que se ve en cualquier revision.
    """

    __slots__ = ("_valor",)

    def __init__(self, valor: str) -> None:
        self._valor = valor

    def revelar(self) -> str:
        """El unico camino al valor. Si esto aparece en un diff, se mira."""
        return self._valor

    def __repr__(self) -> str:
        return "[REDACTADO]"

    __str__ = __repr__

    def __eq__(self, otro: object) -> bool:
        if not isinstance(otro, SecretoEnClaro):
            return NotImplemented
        # Comparacion de tiempo constante: comparar secretos con `==` filtra por
        # el tiempo cuantos caracteres coinciden.
        return hmac.compare_digest(self._valor, otro._valor)

    def __hash__(self) -> int:  # pragma: no cover - no se usa como clave
        raise TypeError("un secreto no se usa como clave de diccionario")


# --------------------------------------------------------------------------
# Cifrado en reposo
# --------------------------------------------------------------------------
def clave_de_cifrado(crudo: str | None = None) -> bytes:
    """Lee la clave del entorno y comprueba su forma. Sin clave no se guarda nada."""
    material = os.environ.get(VARIABLE_DE_ENTORNO_CLAVE) if crudo is None else crudo
    if not material:
        raise ClaveNoDeclarada(
            f"falta {VARIABLE_DE_ENTORNO_CLAVE}: la clave de cifrado de secretos se "
            "declara por entorno, nunca en el repositorio"
        )
    try:
        clave = base64.urlsafe_b64decode(material)
    except Exception as fallo:  # noqa: BLE001 - la causa concreta no aporta
        raise ClaveInvalida(f"{VARIABLE_DE_ENTORNO_CLAVE} no es base64url valido") from fallo
    if len(clave) != LONGITUD_DE_CLAVE:
        raise ClaveInvalida(
            f"{VARIABLE_DE_ENTORNO_CLAVE} tiene {len(clave)} bytes y AES-256-GCM "
            f"exige {LONGITUD_DE_CLAVE}"
        )
    return clave


def genera_clave() -> str:
    """Una clave nueva, en la forma que el entorno espera. Para desplegar, no para el repo."""
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


def _material_autenticado(agencia_id: UUID, cliente_id: UUID, nombre: str) -> bytes:
    """Lo que el cifrado ATA al texto cifrado, sin guardarlo dentro.

    # WHY: AES-GCM admite «datos asociados» que no se cifran pero SI se autentican.
    # Metiendo aqui el inquilino y el nombre, un texto cifrado copiado de la fila de
    # un inquilino a la de otro deja de descifrar: falla con `InvalidTag`. Sin esto,
    # alguien con acceso de escritura a la tabla —o un fallo de la politica— podria
    # MOVER un secreto entre inquilinos y seguiria funcionando. Con esto el
    # aislamiento tambien lo sostiene la criptografia, no solo RLS.
    """
    partes = (
        VERSION_DE_FORMATO,
        str(agencia_id).encode(),
        str(cliente_id).encode(),
        nombre.encode("utf-8"),
    )
    return b"|".join(partes)


def cifrar(
    clave: bytes, *, agencia_id: UUID, cliente_id: UUID, nombre: str, valor: SecretoEnClaro
) -> bytes:
    """Cifra el valor y lo ata al inquilino. Devuelve `nonce || texto cifrado`."""
    nonce = os.urandom(LONGITUD_DE_NONCE)
    sellado = AESGCM(clave).encrypt(
        nonce,
        valor.revelar().encode("utf-8"),
        _material_autenticado(agencia_id, cliente_id, nombre),
    )
    return nonce + sellado


def descifrar(
    clave: bytes, *, agencia_id: UUID, cliente_id: UUID, nombre: str, cifrado: bytes
) -> SecretoEnClaro:
    """Descifra, o falla SIN citar nada de lo que fallo (RF-09)."""
    material = bytes(cifrado)
    if len(material) <= LONGITUD_DE_NONCE:
        raise SecretoNoDescifrable(
            f"el material cifrado de {nombre!r} es mas corto que su propio nonce"
        )
    try:
        claro = AESGCM(clave).decrypt(
            material[:LONGITUD_DE_NONCE],
            material[LONGITUD_DE_NONCE:],
            _material_autenticado(agencia_id, cliente_id, nombre),
        )
    except InvalidTag as fallo:
        raise SecretoNoDescifrable(
            f"el secreto {nombre!r} no descifra para este inquilino: o la clave no es "
            "la suya, o el material se movio de inquilino, o alguien lo altero"
        ) from fallo
    return SecretoEnClaro(claro.decode("utf-8"))


# --------------------------------------------------------------------------
# Guardar y leer, siempre dentro de una sesion de inquilino
# --------------------------------------------------------------------------
_GUARDAR = text(
    "INSERT INTO secretos (agencia_id, cliente_id, nombre, cifrado) "
    "VALUES (:agencia_id, :cliente_id, :nombre, :cifrado) "
    "ON CONFLICT (agencia_id, cliente_id, nombre) DO UPDATE "
    "SET cifrado = EXCLUDED.cifrado, actualizado_en = now() "
    "RETURNING id"
)

_LEER = text("SELECT cifrado FROM secretos WHERE nombre = :nombre")


async def guardar(
    conexion, inquilino: Inquilino, *, nombre: str, valor: SecretoEnClaro, clave: bytes
) -> UUID:
    """Guarda el secreto CIFRADO en la sesion de inquilino que se le pasa.

    # WHY (no abre conexion): la recibe, nunca la crea. «No existe una segunda
    # forma de abrir conexion» (plan §4) — el aislamiento de la fila que se escribe
    # lo pone la politica de la sesion que ya esta abierta, no este modulo.
    """
    cifrado = cifrar(
        clave,
        agencia_id=inquilino.agencia_id,
        cliente_id=inquilino.cliente_id,
        nombre=nombre,
        valor=valor,
    )
    resultado = await conexion.execute(
        _GUARDAR,
        {
            "agencia_id": inquilino.agencia_id,
            "cliente_id": inquilino.cliente_id,
            "nombre": nombre,
            "cifrado": cifrado,
        },
    )
    return resultado.scalar_one()


async def leer(conexion, inquilino: Inquilino, *, nombre: str, clave: bytes) -> SecretoEnClaro:
    """Lee y descifra. La politica de RLS ya decidio si la fila es alcanzable."""
    cifrado = (await conexion.execute(_LEER, {"nombre": nombre})).scalar_one_or_none()
    if cifrado is None:
        raise SecretoNoDescifrable(f"no hay ningun secreto {nombre!r} alcanzable en esta sesion")
    return descifrar(
        clave,
        agencia_id=inquilino.agencia_id,
        cliente_id=inquilino.cliente_id,
        nombre=nombre,
        cifrado=cifrado,
    )


# --------------------------------------------------------------------------
# El serializador que NO conoce los campos de secreto
# --------------------------------------------------------------------------
#: ==Los campos PUBLICOS de cada recurso.== Es la unica lista que existe: no hay
#: ninguna lista de campos prohibidos, y ese es el diseño.
#:
#: # WHY: cada entrada dice lo que SI sale. El recurso `secretos` es el caso que lo
#: explica solo: declara su nombre y sus fechas —lo que el panel necesita para
#: enseñar que existe— y NO declara la columna del material cifrado. No porque esa
#: columna este en ninguna lista negra, sino porque no esta en esta.
#:
#: # WHY (la clave es el nombre de la TABLA): asi el universo de esta declaracion
#: se puede comparar con el CATALOGO de la base. `test_todo_recurso_del_esquema_
#: declara_sus_campos_publicos` exige una entrada por cada tabla de inquilino que
#: exista: una tabla nueva sin declaracion no «sale como venga», no sale.
CAMPOS_PUBLICOS: dict[str, frozenset[str]] = {
    "agencias": frozenset({"agencia_id", "nombre", "creada_en"}),
    "clientes": frozenset({"id", "agencia_id", "nombre", "creado_en"}),
    "heraldos": frozenset({"id", "agencia_id", "cliente_id", "nombre", "creado_en"}),
    "secretos": frozenset(
        {"id", "agencia_id", "cliente_id", "nombre", "creado_en", "actualizado_en"}
    ),
    "bitacora": frozenset(
        {"id", "agencia_id", "cliente_id", "ocurrido_en", "actor", "accion", "recurso"}
    ),
    "trabajos": frozenset(
        {
            "id",
            "agencia_id",
            "cliente_id",
            "tipo",
            "estado",
            "intentos",
            "maximo_intentos",
            "disponible_en",
            "creado_en",
            "actualizado_en",
            "terminado_en",
            "ultimo_error",
        }
    ),
    "trabajos_archivados": frozenset(
        {
            "id",
            "agencia_id",
            "cliente_id",
            "tipo",
            "estado",
            "intentos",
            "maximo_intentos",
            "creado_en",
            "terminado_en",
            "archivado_en",
            "ultimo_error",
        }
    ),
    "mensajes_entrantes": frozenset(
        {"id", "agencia_id", "cliente_id", "canal", "id_externo", "trabajo_id", "recibido_en"}
    ),
}


def _valor_publico(valor: Any) -> Any:
    """Deja pasar lo que puede salir; ABORTA ante lo que no, sin citarlo.

    # WHY: rechaza por TIPO, no por nombre. `bytes` es como viaja el texto cifrado
    # y `SecretoEnClaro` es como viaja el descifrado: cualquiera de los dos en una
    # respuesta es un fallo del producto, venga de la columna que venga y se llame
    # esa columna como se llame.
    """
    if isinstance(valor, SecretoEnClaro):
        raise SecretoEnLaRespuesta(
            "una respuesta lleva un secreto descifrado. Un secreto no sale de la "
            "aplicacion: se usa dentro y se olvida"
        )
    if isinstance(valor, bytes | bytearray | memoryview):
        raise SecretoEnLaRespuesta(
            f"una respuesta lleva {len(bytes(valor))} bytes crudos. En este esquema el "
            "material cifrado es lo unico que viaja asi, y no sale"
        )
    return valor


def serializar(recurso: str, fila: Mapping[str, Any]) -> dict[str, Any]:
    """La salida de un recurso: EXACTAMENTE lo declarado publico, y nada mas.

    No mira la fila para decidir: mira la declaracion. Lo que la fila traiga de mas
    —una columna nueva, un campo calculado, lo que sea— no aparece en la salida
    porque este bucle no lo recorre.
    """
    try:
        permitidos: Iterable[str] = CAMPOS_PUBLICOS[recurso]
    except KeyError as fallo:
        raise RecursoNoDeclarado(
            f"nadie declaro los campos publicos de {recurso!r}. Un recurso sin "
            "declaracion no se serializa 'como venga': se declara"
        ) from fallo
    salida: dict[str, Any] = {}
    for campo in sorted(permitidos):
        if campo not in fila:
            raise CampoDeclaradoQueNoLlega(
                f"{recurso}: se declaro publico un campo que la fila no trae: {campo!r}. "
                "O la consulta dejo de proyectarlo, o la columna se renombro y la "
                "declaracion se quedo atras"
            )
        salida[campo] = _valor_publico(fila[campo])
    return salida


def barrer(carga: Any) -> Any:
    """El barrido AUTOMATICO de CE-06: toda respuesta pasa por aqui antes de salir.

    Recorre la estructura entera —diccionarios, listas, tuplas— y aplica la misma
    comprobacion a cada hoja. No conoce recursos ni campos: solo tipos.

    # WHY: `serializar` protege el camino previsto. Esto protege los demas: una
    # respuesta armada a mano, un diccionario que alguien anadio «temporalmente», el
    # objeto que un error devuelve. CE-06 pide un barrido sobre TODAS las
    # respuestas, y «todas» solo es cierto si el barrido no necesita saber de que
    # recurso viene lo que barre.
    """
    if isinstance(carga, Mapping):
        return {clave: barrer(valor) for clave, valor in carga.items()}
    if isinstance(carga, list | tuple):
        return type(carga)(barrer(valor) for valor in carga)
    return _valor_publico(carga)


def a_json(carga: Any) -> str:
    """Barre y serializa a texto. Es el ULTIMO sitio por el que pasa una respuesta."""
    return json.dumps(barrer(carga), default=str, ensure_ascii=False)
