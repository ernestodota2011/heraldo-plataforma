"""T-015 (RF-08) — la sesion vive en Redis, y revocar es BORRAR.

# WHY (D-08): identidad por cookie de sesion + sesion en Redis + RBAC por rol.
# El JWT autocontenido —lo del referente— quedo descartado por una razon que no
# es de gusto: un JWT NO SE PUEDE REVOCAR. El referente deja una sesion robada
# viva SIETE DIAS, porque el permiso viaja dentro del propio papel y el servidor
# no tiene donde ir a preguntar. Aqui el papel no dice nada: solo nombra una
# entrada de Redis. Si esa entrada no esta, no hay sesion.
#
# WHY (D-05): la sesion vive en Redis y no en memoria del proceso. En memoria,
# «revocada» significaria «revocada en el worker que atendio la revocacion», y el
# siguiente uso caeria en otro worker donde la sesion sigue viva. Redis es el
# unico sitio donde «revocada» significa revocada para todos (RNF-03).
#
# WHY (la vara de RF-08 es «en su siguiente uso», no «borre la clave»): por eso
# `usar()` LEE Redis en cada llamada y no guarda nada entre llamadas. En este
# modulo no hay cache de sesion a proposito: una cache seria exactamente la
# ventana que RF-08 existe para cerrar.
#
# WHY (la forma del testigo): el testigo es `<sesion_id>.<secreto>`, y en Redis se
# guarda el sesion_id como CLAVE y solo la HUELLA del secreto como valor. Dos
# consecuencias, las dos deliberadas:
#   - un volcado de Redis no entrega testigos utilizables, solo huellas;
#   - se puede revocar SIN tener el testigo. Un operador que revoca la sesion de
#     otro no conoce su secreto; si la clave fuera la huella del testigo entero,
#     la revocacion administrativa seria imposible y RF-08 quedaria cumplido solo
#     para quien ya tiene el testigo — es decir, para el ladron.
#
# WHY (RBAC y RF-01): el rol NO produce el alcance por su cuenta. El alcance sigue
# saliendo de `Inquilino.desde_usuario(...)`, que lo DERIVA de `cliente_id`. El rol
# es la segunda redaccion del mismo hecho, y por eso se comprueba que las dos
# COINCIDAN: una sesion cuyo rol dice `operador_agencia` y cuya fila trae un
# cliente es una contradiccion, y una contradiccion falla CERRADA
# (`feedback_protocolo_proveedor_seam`). Sin esa comprobacion un usuario de
# cliente con el rol mal escrito no escalaria —el alcance lo salva— pero pasaria
# los permisos de operador: el RBAC seria una sugerencia.
#
# WHY (fail-closed ante Redis): este modulo NO captura errores de Redis. Si el
# almacen no responde, `usar()` propaga el fallo y la peticion muere. Tragarse el
# error y devolver una sesion convertiria una caida de infraestructura en un
# bypass de autenticacion — la forma mas cara de
# `feedback_fail_open_traga_al_guard`.
#
# WHY (dos cosas se llaman «rol» en este paquete y NO son la misma): `app.tenancy.rol`
# es el rol de POSTGRES con el que corre la aplicacion; `Rol` de aqui es el rol de
# NEGOCIO de una persona. Se dejan separados a proposito: mezclarlos invitaria a
# «resolver» un permiso de producto concediendo un privilegio de base de datos.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never
from uuid import UUID, uuid4

from redis.asyncio import Redis

from app.tenancy.inquilino import Inquilino

#: Prefijo de las claves de sesion. Se cambia para aislar bancos de pruebas; en
#: produccion se deja el que viene.
PREFIJO_POR_DEFECTO = "heraldo"

#: Vida maxima de una sesion. Es un techo ABSOLUTO, no deslizante: una sesion no
#: se renueva sola por usarla. La renovacion deslizante es comoda y alarga
#: indefinidamente la vida de un testigo robado que se sigue usando.
TTL_SESION_SEGUNDOS = 8 * 60 * 60

#: Separador entre el identificador de la sesion y su secreto. No aparece en el
#: alfabeto de `secrets.token_urlsafe` (A-Z a-z 0-9 `-` `_`) ni en el hex de un
#: uuid, asi que partir por el primero es inequivoco.
_SEPARADOR = "."


class Rol(StrEnum):
    """Los dos roles de NEGOCIO de v1. RBAC: no hay un tercero ni un comodin."""

    OPERADOR_AGENCIA = "operador_agencia"
    USUARIO_CLIENTE = "usuario_cliente"


class SesionInvalida(Exception):
    """El testigo no nombra ninguna sesion viva. Es el veredicto de RF-08.

    Cubre cuatro caminos y a proposito NO los distingue hacia fuera: revocada,
    caducada, mal formada o con el secreto equivocado. Distinguirlos le diria a
    quien prueba testigos cual de sus intentos iba por buen camino.
    """


class PermisoDenegado(Exception):
    """La sesion es valida y su rol no alcanza para lo que se pide (RBAC)."""


class RolIncoherente(SesionInvalida):
    """El rol guardado y la fila del usuario dicen cosas distintas: falla cerrada."""


def cliente_es_exigido_por(rol: Rol) -> bool:
    """Tabla rol -> ¿la fila DEBE traer cliente?, con exhaustividad comprobada.

    # WHY: es una TABLA y no una cadena de `if`, y termina en `assert_never`. Un
    # rol nuevo que nadie clasifique no cae en una rama por defecto permisiva:
    # revienta. Una justificacion en prosa caduca; esto no
    # (`feedback_justificacion_caduca`). `test_cada_rol_declara_si_exige_cliente`
    # recorre el enum entero, asi que anadir un rol sin clasificarlo pone el CI en
    # rojo antes de que ninguna peticion lo encuentre.
    """
    match rol:
        case Rol.OPERADOR_AGENCIA:
            return False
        case Rol.USUARIO_CLIENTE:
            return True
    assert_never(rol)


@dataclass(frozen=True, slots=True)
class Sesion:
    """Una sesion viva, ya comprobada contra Redis.

    Inmutable: nadie le cambia el rol a mitad de peticion.
    """

    sesion_id: str
    agencia_id: UUID
    cliente_id: UUID | None
    rol: Rol

    def __post_init__(self) -> None:
        exige_cliente = cliente_es_exigido_por(self.rol)
        if exige_cliente and self.cliente_id is None:
            raise RolIncoherente(
                f"el rol {self.rol.value!r} exige un cliente en la fila y no lo trae"
            )
        if not exige_cliente and self.cliente_id is not None:
            raise RolIncoherente(
                f"el rol {self.rol.value!r} no admite cliente en la fila y trae uno"
            )

    @property
    def inquilino(self) -> Inquilino:
        """El alcance sale de la FILA, igual que en T-014·ter — nunca del rol.

        # WHY: `desde_usuario` es la unica puerta (guard por AST en
        # `test_escalada_alcance.py`). Este modulo la ALIMENTA, no la puentea.
        """
        return Inquilino.desde_usuario(agencia_id=self.agencia_id, cliente_id=self.cliente_id)

    def exige(self, rol: Rol) -> None:
        """RBAC en el punto de uso. Levanta `PermisoDenegado` si el rol no es ese."""
        if self.rol is not rol:
            raise PermisoDenegado(
                f"esta operacion exige el rol {rol.value!r} y la sesion tiene "
                f"{self.rol.value!r}"
            )


def _huella(secreto: str) -> str:
    """Huella del secreto. En Redis no se guarda nunca el secreto en claro."""
    return hashlib.sha256(secreto.encode("utf-8")).hexdigest()


class AlmacenDeSesiones:
    """Las sesiones de Heraldo, en Redis. No hay una segunda forma de tenerlas."""

    def __init__(self, redis: Redis, *, prefijo: str = PREFIJO_POR_DEFECTO) -> None:
        self._redis = redis
        self._prefijo = prefijo

    @property
    def prefijo(self) -> str:
        return self._prefijo

    def clave(self, sesion_id: str) -> str:
        return f"{self._prefijo}:sesion:{sesion_id}"

    async def abrir(
        self,
        *,
        agencia_id: UUID,
        cliente_id: UUID | None,
        rol: Rol,
        ttl_segundos: int = TTL_SESION_SEGUNDOS,
    ) -> tuple[str, Sesion]:
        """Abre una sesion y devuelve `(testigo, sesion)`.

        El testigo se entrega UNA vez y no se puede volver a obtener: de el solo
        queda la huella. La coherencia rol <-> fila se comprueba AQUI, al
        construir la `Sesion`, para que una incoherente no llegue ni a guardarse.
        """
        sesion_id = uuid4().hex
        secreto = secrets.token_urlsafe(32)
        sesion = Sesion(sesion_id=sesion_id, agencia_id=agencia_id, cliente_id=cliente_id, rol=rol)
        registro = {
            "huella": _huella(secreto),
            "agencia_id": str(agencia_id),
            "cliente_id": None if cliente_id is None else str(cliente_id),
            "rol": rol.value,
        }
        await self._redis.set(self.clave(sesion_id), json.dumps(registro), ex=ttl_segundos)
        return f"{sesion_id}{_SEPARADOR}{secreto}", sesion

    async def usar(self, testigo: str) -> Sesion:
        """LEE Redis. Es el momento en el que la revocacion se hace efectiva.

        # WHY: aqui no hay cache, ni memoizacion, ni «si ya la vi hace un segundo
        # me la creo». Cada uso pregunta. Ese viaje a Redis ES el requisito RF-08.
        """
        sesion_id, secreto = _partir_testigo(testigo)
        crudo = await self._redis.get(self.clave(sesion_id))
        if crudo is None:
            raise SesionInvalida("la sesion no existe: revocada, caducada o inventada")
        registro = _leer_registro(crudo)

        # Comparacion en tiempo constante: el secreto es material de autenticacion.
        if not hmac.compare_digest(str(registro.get("huella", "")), _huella(secreto)):
            raise SesionInvalida("el secreto del testigo no corresponde a esa sesion")

        return _sesion_desde_registro(sesion_id, registro)

    async def revocar(self, sesion_id: str) -> bool:
        """Revocacion ADMINISTRATIVA: por identificador, sin conocer el secreto.

        Devuelve si habia algo que borrar. Revocar dos veces no es un error.
        """
        return bool(await self._redis.delete(self.clave(sesion_id)))

    async def revocar_testigo(self, testigo: str) -> bool:
        """Revocacion por el propio dueño (cerrar sesion)."""
        sesion_id, _ = _partir_testigo(testigo)
        return await self.revocar(sesion_id)


def _partir_testigo(testigo: str) -> tuple[str, str]:
    if not isinstance(testigo, str) or _SEPARADOR not in testigo:
        raise SesionInvalida("el testigo no tiene la forma <sesion_id>.<secreto>")
    sesion_id, _, secreto = testigo.partition(_SEPARADOR)
    if not sesion_id or not secreto:
        raise SesionInvalida("el testigo tiene una de sus dos mitades vacia")
    return sesion_id, secreto


def _leer_registro(crudo: bytes | str) -> dict[str, Any]:
    texto = crudo.decode("utf-8") if isinstance(crudo, bytes) else crudo
    try:
        registro = json.loads(texto)
    except json.JSONDecodeError as fallo:
        # Un registro ilegible NO es un registro valido: se trata como sesion
        # invalida, nunca como «no pude comprobarlo, adelante»
        # (`feedback_fail_open_traga_al_guard`).
        raise SesionInvalida(f"el registro de sesion no es legible: {fallo}") from fallo
    if not isinstance(registro, dict):
        raise SesionInvalida("el registro de sesion no es un objeto")
    return registro


def _sesion_desde_registro(sesion_id: str, registro: dict[str, Any]) -> Sesion:
    crudo_rol = registro.get("rol")
    try:
        rol = Rol(crudo_rol)
    except ValueError as fallo:
        # Un rol que este producto no conoce no se degrada al mas debil: se
        # rechaza. Degradarlo seria inventar una politica que nadie escribio.
        raise SesionInvalida(f"rol desconocido en el registro: {crudo_rol!r}") from fallo

    crudo_cliente = registro.get("cliente_id")
    try:
        agencia_id = UUID(str(registro["agencia_id"]))
        cliente_id = None if crudo_cliente is None else UUID(str(crudo_cliente))
    except (KeyError, ValueError, TypeError) as fallo:
        raise SesionInvalida(f"el registro de sesion esta mal formado: {fallo}") from fallo

    # `Sesion.__post_init__` levanta `RolIncoherente` (que ES `SesionInvalida`) si
    # el rol y la fila se contradicen.
    return Sesion(sesion_id=sesion_id, agencia_id=agencia_id, cliente_id=cliente_id, rol=rol)
