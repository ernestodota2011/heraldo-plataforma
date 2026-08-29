"""T-021·bis (RNF-04) — el alta de un cliente sanitario se RECHAZA, y hay centinela.

RNF-04 dice: «El sistema no aloja datos de salud protegidos ni opera bajo BAA en
v1. El cliente sanitario de la agencia no es inquilino». Hasta este modulo era una
PROMESA SIN MECANISMO (I-03): una frase en un documento que ningun codigo hacia
cumplir, en el gate que existe justo para romper esa clase de frase.

# WHY (el guard vive en la RUTA, no en la suite): un guard que solo existe en el
# test AUTORIZA en produccion (`feedback_guard_solo_en_el_test`). Por eso el alta
# y su guard son LA MISMA funcion: `alta_de_cliente` no puede escribir la fila sin
# haber pasado por `evaluar_alta`, porque el `INSERT` esta despues del rechazo, en
# el mismo cuerpo. Y para que nadie escriba un segundo camino, `test_baa_guard.py`
# audita el arbol: NINGUN otro modulo de la aplicacion puede contener un
# `INSERT INTO clientes`. Olvidarlo no compila.
#
# WHY (fail-closed, en los TRES sitios donde se puede no saber):
#   1. el regimen del repositorio no se puede leer   -> RECHAZA;
#   2. el sector no se declara, o no es uno conocido -> RECHAZA;
#   3. el sector declarado CONTRADICE lo que dice el nombre -> RECHAZA.
# «No pude determinarlo» no es «adelante». Un `no-op` que devuelve «todo bien»
# seria el peor resultado posible aqui: convertiria RNF-04 de promesa sin
# mecanismo en promesa con mecanismo FALSO, que es peor, porque ahora hay una
# casilla marcada.
#
# WHY (por que el veredicto se equivoca hacia el RECHAZO, y se dice en voz alta):
# esta es una ruta de cumplimiento, y ahi manda el analisis burdo-total sobre el
# preciso-incompleto (`feedback_analisis_incompleto_falla_caro`): una falsa alarma
# cuesta un alta que un humano tendra que decidir; un falso OK cuesta PHI dentro
# de un producto que declara no alojarlo. El tamiz lexico rechazara de mas — un
# salon de belleza llamado «Estetica Ana», una agencia llamada «Salud Marketing».
# ==Eso es deliberado y NO tiene escotilla en v1==: admitir un caso limite tiene
# que ser una decision humana registrada, no un valor por defecto del codigo. El
# mecanismo de excepcion firmada NO existe todavia y esta declarado como deuda.
#
# WHY (T-021·ter — el sector SI se persiste, y por que ese era el hueco): hasta la
# revision 0004 el sector declarado se evaluaba y se TIRABA. El guard media el
# ALTA, no la VIDA del cliente: un comercio que manana se convierte en clinica no
# lo volvia a mirar nadie, y RNF-04 dejaba de cumplirse **sin que nada se pusiera
# rojo**. Ahora el sector vive en `clientes.sector` y hay UN camino para cambiarlo
# —`reverificar_sector`— que vuelve a pasar por la MISMA regla del alta, con el
# mismo fail-closed, y deja su asiento en la bitacora de RF-10.
#
# WHY (la segunda fuente de la reverificacion es el nombre PERSISTIDO, no uno que
# traiga quien llama): si el nombre viniera por parametro, quien quisiera colar un
# cliente sanitario reverificaria con un nombre limpio y el tamiz no veria nada. El
# nombre sale de la FILA, dentro de la sesion de inquilino, asi que lo que se tamiza
# es lo que el producto de verdad tiene guardado. La `descripcion` si se admite por
# parametro, y es seguro: los marcadores solo se SUMAN — mas texto solo puede
# endurecer el veredicto, nunca ablandarlo.
#
# WHY (el asiento va a la bitacora que YA existe, y no a una tabla de historia):
# una tabla «historial de sectores» seria un SEGUNDO camino de registro del mismo
# hecho, y dos registros del mismo hecho divergen. RF-10 ya tiene su tabla, ya es
# de solo insercion por PERMISO, y ya la gobierna la misma cascada.
#
# WHY (lo que sigue faltando, dicho en voz alta): la columna hace POSIBLE la
# re-evaluacion periodica; no la programa. Nada recorre hoy `sector_verificado_en`
# buscando clasificaciones viejas, asi que un cliente cuya realidad cambie sin que
# nadie reverifique se queda con su sector de ayer. Deuda declarada, no cerrada.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.audit.bitacora import apuntar
from app.tenancy.auth import Rol, Sesion
from app.tenancy.inquilino import Inquilino
from app.tenancy.sesion import sesion_de_inquilino

#: Nombre del centinela, en la raiz del repositorio. Mismo patron que `.crisol-baa`.
NOMBRE_DEL_CENTINELA = ".heraldo-baa"

#: Variable para apuntar al centinela cuando el arbol no esta donde este modulo
#: cree. NO es una escotilla: el contenido del archivo al que apunte sigue
#: teniendo que declarar un regimen que el guard sepa interpretar.
VARIABLE_DE_ENTORNO_CENTINELA = "HERALDO_CENTINELA_BAA"

#: Raiz del repositorio, derivada de la posicion de este modulo:
#: <raiz>/apps/api/app/tenancy/baa_guard.py
RAIZ_DEL_REPOSITORIO = Path(__file__).resolve().parents[4]

#: El UNICO regimen que este codigo sabe interpretar. Un valor distinto no se
#: adivina: deja el regimen indeterminado y todas las altas se rechazan.
REGIMEN_SIN_BAA = "sin-baa"

#: Donde vive el sector persistido (revision 0004). Son constantes y no una lista
#: que haya que mantener: es la FORMA del modelo, igual que `TABLA_REGISTRO_DE_
#: CLIENTES` en `confirmacion.py`.
COLUMNA_SECTOR = "sector"
COLUMNA_SECTOR_VERIFICADO = "sector_verificado_en"

#: La accion con la que la reverificacion queda escrita en la bitacora (RF-10).
ACCION_REVERIFICACION = "reverificacion-de-sector"


class Sector(StrEnum):
    """Sectores que el alta puede declarar. ALLOWLIST: lo que no este, no entra.

    `SALUD` esta en la lista a proposito — no para admitirlo, sino para que se
    pueda DECLARAR con honestidad y ser rechazado por la razon correcta. Quitarlo
    obligaria a un operador honesto a mentir para poder seguir.
    """

    SALUD = "salud"
    COMERCIO = "comercio"
    HOSTELERIA = "hosteleria"
    INMOBILIARIA = "inmobiliaria"
    LEGAL = "legal"
    FINANZAS = "finanzas"
    EDUCACION = "educacion"
    TECNOLOGIA = "tecnologia"
    CONSTRUCCION = "construccion"
    AUTOMOCION = "automocion"
    TURISMO = "turismo"
    MARKETING = "marketing"
    LOGISTICA = "logistica"
    OTRO = "otro"


class Clasificacion(StrEnum):
    """El veredicto del guard. Solo uno de los tres admite el alta."""

    NO_SANITARIA = "no_sanitaria"
    SANITARIA = "sanitaria"
    INDETERMINADA = "indeterminada"


class RegimenIndeterminado(RuntimeError):
    """El centinela no dice nada que este codigo sepa interpretar."""


@dataclass(frozen=True, slots=True)
class VeredictoDeAlta:
    """Que se decidio y por que. El motivo se escribe para un humano."""

    clasificacion: Clasificacion
    motivo: str
    marcadores: tuple[str, ...] = ()

    @property
    def admitida(self) -> bool:
        return self.clasificacion is Clasificacion.NO_SANITARIA


class GuardDeBaaRechaza(Exception):
    """Base comun: el guard de RNF-04 dijo que no. Lleva su veredicto dentro.

    # WHY (una base y no dos excepciones sueltas): el alta y la reverificacion
    # aplican LA MISMA regla en dos momentos distintos. Con dos tipos hermanos sin
    # padre, quien capturase uno se dejaria el otro fuera sin enterarse — y el que
    # se quedaria fuera seria el nuevo, que es el que mide la VIDA del cliente.
    # `except GuardDeBaaRechaza` cubre los dos, hoy y cuando aparezca el tercero.
    """

    def __init__(self, veredicto: VeredictoDeAlta) -> None:
        super().__init__(veredicto.motivo)
        self.veredicto = veredicto


class AltaRechazada(GuardDeBaaRechaza):
    """El alta no pasa el guard de RNF-04."""


class ReverificacionRechazada(GuardDeBaaRechaza):
    """El cambio de sector no pasa el guard: nada se escribe, ni siquiera la fecha.

    Cubre los dos motivos y NO los mezcla en el mensaje: el veredicto que lleva
    dentro dice si el cliente se declaro sanitario, si el texto lo contradice, o si
    no se pudo determinar nada — incluido el caso de un cliente que esta sesion no
    alcanza, que tambien es un indeterminado y tambien se rechaza.
    """


# --------------------------------------------------------------------------
# El tamiz lexico
# --------------------------------------------------------------------------
# WHY: los patrones llevan la raiz explicita y `\w*` al final, nunca un `in` sobre
# la cadena: `"salud" in texto` casaria dentro de «saludable» y de cualquier
# palabra que la contenga. Y el texto se normaliza sin acentos antes de mirarlo,
# para que «clínica» y «clinica» sean la misma palabra.
_MARCADORES_SANITARIOS: tuple[tuple[str, str], ...] = (
    # Sin `\b` delante a proposito: en `Policlinico` —y en los nombres compuestos que
    # RNF-04 nombra— no hay frontera de palabra antes de «clinic». Anclarlo
    # dejaba fuera justo el caso que este guard existe para atrapar (medido).
    ("clinica", r"clinic\w*"),
    ("hospital", r"hospital\w*"),
    ("medico", r"\b(?:medic[oa]s?|medicin\w*|medical|medicare|medicaid)\b"),
    ("salud", r"\b(?:salud|health\w*)\b"),
    ("paciente", r"\b(?:pacientes?|patients?)\b"),
    ("dental", r"\b(?:dental\w*|dentist\w*|odontolog\w*)\b"),
    ("dermatologia", r"(?:dermatolog\w*|dermato\w*|\bderma\b)"),
    ("farmacia", r"\b(?:farmac\w*|pharmac\w*|drugstore)\b"),
    ("psico", r"\b(?:psicolog\w*|psiquiatr\w*|psycholog\w*|psychiatr\w*)\b"),
    ("terapia", r"\b(?:terapia\w*|terapeut\w*|therapy|therapist\w*|fisioterap\w*)\b"),
    ("cirugia", r"\b(?:cirug\w*|cirujan\w*|surgery|surgical|surgeon\w*)\b"),
    ("enfermeria", r"\b(?:enfermer\w*|nursing|nurses?)\b"),
    (
        "especialidad",
        r"\b(?:radiolog\w*|oncolog\w*|cardiolog\w*|pediatr\w*"
        r"|ginecolog\w*|urolog\w*|oftalmolog\w*|traumatolog\w*)\b",
    ),
    ("laboratorio", r"\b(?:laboratorio clinico|clinical lab\w*|analisis clinicos)\b"),
    ("telemedicina", r"\b(?:telemedicin\w*|telehealth\w*)\b"),
    ("estetica", r"\b(?:estetic\w*|aesthetic\w*|medspa|med spa|botox|rellenos dermicos)\b"),
    (
        "expediente",
        r"\b(?:historia clinica|historias clinicas|medical record\w*"
        r"|expediente medico|expedientes medicos)\b",
    ),
    ("regimen", r"\b(?:hipaa|hippa|phi|baa|protected health information)\b"),
)

_TAMIZ = tuple((etiqueta, re.compile(patron)) for etiqueta, patron in _MARCADORES_SANITARIOS)


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto.casefold())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def marcadores_sanitarios(*textos: str) -> tuple[str, ...]:
    """Etiquetas de los marcadores sanitarios que aparecen en los textos dados."""
    plano = _sin_acentos(" ".join(t for t in textos if t))
    return tuple(sorted({etiqueta for etiqueta, patron in _TAMIZ if patron.search(plano)}))


# --------------------------------------------------------------------------
# El centinela del repositorio
# --------------------------------------------------------------------------
def ruta_del_centinela() -> Path:
    declarada = os.environ.get(VARIABLE_DE_ENTORNO_CENTINELA)
    if declarada:
        return Path(declarada)
    return RAIZ_DEL_REPOSITORIO / NOMBRE_DEL_CENTINELA


def regimen_declarado() -> str:
    """Lee el centinela. Lo que no se pueda interpretar es INDETERMINADO.

    # WHY: `RegimenIndeterminado` no se captura aqui. Sube hasta `evaluar_alta`,
    # que lo convierte en un rechazo. Si esta funcion devolviera un valor por
    # defecto ante un archivo ausente, borrar el centinela seria la forma mas
    # comoda de desactivar RNF-04, y el CI seguiria verde.
    """
    ruta = ruta_del_centinela()
    try:
        contenido = ruta.read_text(encoding="utf-8")
    except OSError as fallo:
        raise RegimenIndeterminado(
            f"no se pudo leer el centinela {ruta}: {fallo}. Sin centinela el regimen "
            "sanitario es indeterminado y ninguna alta se admite (RNF-04). Si el "
            "despliegue no lleva el arbol del repositorio, declara "
            f"{VARIABLE_DE_ENTORNO_CENTINELA} apuntando al centinela versionado "
            f"({NOMBRE_DEL_CENTINELA}). No hay forma de seguir sin el: fallar "
            "cerrado tiene que ser ACCIONABLE, no solo cerrado"
        ) from fallo

    for linea in contenido.splitlines():
        limpia = linea.strip()
        if not limpia or limpia.startswith("#"):
            continue
        if limpia == REGIMEN_SIN_BAA:
            return REGIMEN_SIN_BAA
        raise RegimenIndeterminado(
            f"el centinela {ruta} declara {limpia!r}, que este codigo no sabe "
            f"interpretar. El unico regimen implementado es {REGIMEN_SIN_BAA!r}"
        )
    raise RegimenIndeterminado(
        f"el centinela {ruta} no declara ningun regimen (solo comentarios o lineas vacias)"
    )


# --------------------------------------------------------------------------
# El veredicto
# --------------------------------------------------------------------------
def evaluar_alta(*, nombre: str, sector: object, descripcion: str = "") -> VeredictoDeAlta:
    """Decide si este cliente puede darse de alta bajo RNF-04.

    `sector` se recibe como `object` a proposito: lo que llega de una peticion es
    texto de un tercero, y aqui es donde se comprueba que sea uno de los
    declarados. Aceptar `Sector` en la firma habria movido la validacion a algun
    sitio de arriba, que es como se pierde.
    """
    try:
        regimen_declarado()
    except RegimenIndeterminado as fallo:
        return VeredictoDeAlta(Clasificacion.INDETERMINADA, str(fallo))

    if not isinstance(nombre, str) or not nombre.strip():
        return VeredictoDeAlta(
            Clasificacion.INDETERMINADA,
            "el alta no trae nombre: sin nombre no hay nada que tamizar",
        )

    try:
        declarado = Sector(sector)
    except ValueError:
        return VeredictoDeAlta(
            Clasificacion.INDETERMINADA,
            f"sector {sector!r} no declarado o desconocido. Los admitidos son "
            f"{sorted(s.value for s in Sector)}. Un sector que no se puede determinar "
            "se rechaza: no poder preguntar no es una respuesta afirmativa (RNF-04)",
        )

    marcadores = marcadores_sanitarios(nombre, descripcion)

    if declarado is Sector.SALUD:
        return VeredictoDeAlta(
            Clasificacion.SANITARIA,
            "el alta se declara del sector salud y Heraldo v1 no aloja datos de salud "
            "protegidos ni opera bajo BAA (RNF-04)",
            marcadores,
        )

    if marcadores:
        return VeredictoDeAlta(
            Clasificacion.INDETERMINADA,
            f"el sector declarado es {declarado.value!r} y el texto del alta contiene "
            f"marcadores sanitarios {list(marcadores)}. Dos fuentes que se contradicen "
            "no dan una respuesta: dan un indeterminado, y un indeterminado se rechaza. "
            "Si el caso es legitimo, lo decide un humano y se registra — no el codigo",
            marcadores,
        )

    return VeredictoDeAlta(
        Clasificacion.NO_SANITARIA,
        f"sector {declarado.value!r}, sin marcadores sanitarios en el texto del alta",
    )


# --------------------------------------------------------------------------
# El alta — la RUTA. El guard esta aqui dentro, no al lado.
# --------------------------------------------------------------------------
async def alta_de_cliente(
    motor: AsyncEngine,
    *,
    sesion: Sesion,
    nombre: str,
    sector: object,
    descripcion: str = "",
    cliente_id: UUID | None = None,
) -> UUID:
    """Da de alta un cliente. Es el UNICO camino que escribe la tabla `clientes`.

    Cuatro cosas, en este orden y sin forma de saltarse ninguna:

    1. **RBAC (T-015)** — solo un operador de agencia da de alta clientes. Un
       usuario de portal de cliente recibe `PermisoDenegado`.
    2. **Guard de RNF-04 (T-021·bis)** — el veredicto decide, y solo
       `NO_SANITARIA` continua.
    3. **Aislamiento (T-012)** — la fila se escribe por `sesion_de_inquilino`, con
       las tres variables declaradas. La agencia sale de la SESION, jamas de un
       parametro: por eso esta funcion no acepta `agencia_id`.
    4. **Persistencia del sector (T-021·ter)** — lo declarado queda EN la fila,
       junto con la fecha en que se verifico. Sin eso el guard solo puede medir
       este instante, y RNF-04 caduca al dia siguiente en silencio.
    """
    sesion.exige(Rol.OPERADOR_AGENCIA)

    veredicto = evaluar_alta(nombre=nombre, sector=sector, descripcion=descripcion)
    if not veredicto.admitida:
        raise AltaRechazada(veredicto)

    # El veredicto admitido garantiza que `sector` convierte: `evaluar_alta` ya lo
    # paso por el enum y devolvio INDETERMINADA si no.
    declarado = Sector(sector)
    nuevo = cliente_id or uuid4()
    async with sesion_de_inquilino(motor, sesion.inquilino) as conexion:
        # WHY (el sector va en la MISMA sentencia que la ficha): si se escribiera
        # despues, un fallo entre medias dejaria un cliente sin sector —o sea, sin
        # clasificacion— y el producto no tendria forma de saber si es que nadie lo
        # miro o es que el segundo `INSERT` no llego. Una fila que existe es una
        # fila clasificada.
        await conexion.execute(
            text(
                "INSERT INTO clientes (id, agencia_id, nombre, sector, sector_verificado_en) "
                "VALUES (:id, :agencia, :nombre, :sector, now())"
            ),
            {
                "id": nuevo,
                "agencia": sesion.agencia_id,
                "nombre": nombre,
                "sector": declarado.value,
            },
        )
    return nuevo


# --------------------------------------------------------------------------
# T-021·ter — el sector persistido, y su reverificacion
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SectorPersistido:
    """Lo que la fila dice hoy sobre el sector de un cliente.

    `sector` es `None` cuando la fila no declara ninguno — un cliente anterior a la
    revision 0004. `None` NO es «sin sector» en el sentido de «da igual»: es
    **indeterminado**, y quien decide algo con el tiene que fallar cerrado.
    """

    cliente_id: UUID
    nombre: str
    sector: Sector | None
    verificado_en: datetime | None


@dataclass(frozen=True, slots=True)
class Reverificacion:
    """Que se volvio a mirar, que se decidio, y donde quedo escrito."""

    cliente_id: UUID
    sector_anterior: Sector | None
    sector: Sector
    cambio: bool
    apunte_id: UUID
    veredicto: VeredictoDeAlta


class SectorPersistidoIlegible(ValueError):
    """La fila guarda algo que este codigo no sabe leer como sector."""


def _leer_sector_guardado(crudo: object) -> Sector | None:
    """Traduce lo que hay en la columna. Lo que no se entienda, LEVANTA.

    # WHY (`salud` guardado tambien levanta): ningun camino de este modulo admite
    # el sector salud, ni en el alta ni en la reverificacion. Encontrarlo escrito
    # significa que la fila la puso otro camino —uno sin guard— y entonces lo que
    # dice la columna no es una clasificacion verificada: es un dato de origen
    # desconocido. Tratarlo como bueno seria dar por verificada la unica cosa que
    # RNF-04 existe para no admitir.
    """
    if crudo is None:
        return None
    try:
        guardado = Sector(crudo)
    except ValueError as fallo:
        raise SectorPersistidoIlegible(
            f"la fila guarda el sector {crudo!r}, que no es ninguno de los declarados "
            f"({sorted(s.value for s in Sector)}). Un sector que este codigo no sabe "
            "leer no se interpreta al mejor postor: deja al cliente indeterminado"
        ) from fallo
    if guardado is Sector.SALUD:
        raise SectorPersistidoIlegible(
            "la fila guarda el sector 'salud', que ningun camino de este modulo admite "
            "(RNF-04). Que este escrito significa que lo puso un camino sin guard, asi "
            "que no es una clasificacion verificada"
        )
    return guardado


async def sector_persistido(
    conexion: AsyncConnection, *, cliente_id: UUID, para_actualizar: bool = False
) -> SectorPersistido | None:
    """El sector que la fila declara hoy, leido DENTRO de la sesion de inquilino.

    Devuelve `None` cuando el cliente no esta al alcance de esta sesion — que
    incluye «no existe» y «es de otra agencia», y no los distingue: distinguirlos
    seria un oraculo de enumeracion, igual que en el inventario de RNF-06.

    # WHY (`para_actualizar`, y por que aqui SI se toma el cerrojo cuando en el
    # inventario de RNF-06 se decidio NO tomarlo) — ==MEDIDO, y lo levanto la
    # revision cruzada==: sin `FOR UPDATE`, dos reverificaciones a la vez LEEN el
    # mismo sector viejo y despues escriben una detras de otra. El estado final es
    # correcto —manda la ultima— pero la bitacora, que NADIE puede corregir,
    # registra DOS transiciones saliendo del mismo sector: la segunda no salio de
    # ahi. Reproducido 3 de 3 veces antes del arreglo. `rowcount` no lo ve, porque
    # la fila si existe; lo que caduco es lo LEIDO.
    #
    # # WHY (la diferencia con `confirmacion.py`): alli se descarto `FOR UPDATE`
    # porque el cerrojo habria durado toda la operacion, bitacora EXTERNA incluida.
    # Aqui la bitacora se escribe en la MISMA transaccion y no hay ninguna llamada
    # fuera entre la lectura y el `UPDATE`: la ventana es corta y sobre una sola
    # fila. Detectar en vez de prevenir tampoco servia — el defecto no es que la
    # fila cambie, es que el asiento diga de donde venia.
    #
    # # WHY (por defecto NO bloquea): una lectura ordinaria del sector no tiene por
    # que retener una fila. Lo pide quien va a escribir.
    """
    cerrojo = " FOR UPDATE" if para_actualizar else ""
    fila = (
        await conexion.execute(
            text(
                f"SELECT nombre, {COLUMNA_SECTOR}, {COLUMNA_SECTOR_VERIFICADO} "  # noqa: S608
                f"FROM clientes WHERE id = :cliente{cerrojo}"
            ),
            {"cliente": cliente_id},
        )
    ).first()
    if fila is None:
        return None
    return SectorPersistido(
        cliente_id=cliente_id,
        nombre=fila.nombre,
        sector=_leer_sector_guardado(fila.sector),
        verificado_en=fila.sector_verificado_en,
    )


def _actor_de(sesion: Sesion) -> str:
    """Quien lo pidio, en la unica forma de identidad que hoy existe.

    # WHY (la HUELLA del identificador de sesion y no el identificador): el
    # identificador de sesion es la clave de Redis con la que se REVOCA. Escribirlo
    # en una tabla que nadie puede corregir dejaria una lista de mangos de
    # revocacion vivos, para siempre, dentro del propio inquilino. La huella
    # identifica igual —quien tenga el identificador puede recalcularla y correlar—
    # y no sirve para tocar nada.
    #
    # # WHY (lleva el rol delante): un identificador opaco no dice nada a quien lee
    # la bitacora. `operador_agencia:9f18…` se lee.
    """
    huella = hashlib.sha256(sesion.sesion_id.encode("utf-8")).hexdigest()[:16]
    return f"{sesion.rol.value}:{huella}"


async def reverificar_sector(
    motor: AsyncEngine,
    *,
    sesion: Sesion,
    cliente_id: UUID,
    sector: object,
    descripcion: str = "",
) -> Reverificacion:
    """Vuelve a mirar el sector de un cliente que YA existe. Es el UNICO camino.

    Cuatro cosas, en este orden y sin forma de saltarse ninguna:

    1. **RBAC (T-015)** — la clasificacion de un cliente la decide un operador de
       la agencia, nunca el portal del propio cliente.
    2. **Lectura dentro del inquilino** — el nombre con el que se tamiza sale de la
       FILA, no de quien llama, y una fila fuera de alcance no se ve.
    3. **Guard de RNF-04** — el MISMO `evaluar_alta` del alta, con el mismo
       fail-closed: indeterminado ⇒ se rechaza y NO se escribe nada.
    4. **Asiento en la bitacora (RF-10)** — en la MISMA transaccion que el cambio.
       Si el asiento fallara, el cambio se deshace: un cambio de clasificacion sin
       rastro es exactamente lo que RF-10 existe para impedir.

    Se registra SIEMPRE, cambie o no el sector. Refrescar `sector_verificado_en`
    tambien es un hecho —«esto se volvio a mirar tal dia»— y una escritura que no
    deja rastro es una escritura silenciosa.
    """
    sesion.exige(Rol.OPERADOR_AGENCIA)

    async with sesion_de_inquilino(motor, sesion.inquilino) as conexion:
        try:
            # `para_actualizar=True`: la fila se lee CON su cerrojo, en la misma
            # transaccion que despues la escribe. Sin el, dos reverificaciones a la
            # vez dejan dos asientos que dicen venir del mismo sector.
            actual = await sector_persistido(
                conexion, cliente_id=cliente_id, para_actualizar=True
            )
        except SectorPersistidoIlegible as fallo:
            raise ReverificacionRechazada(
                VeredictoDeAlta(Clasificacion.INDETERMINADA, str(fallo))
            ) from fallo

        if actual is None:
            raise ReverificacionRechazada(
                VeredictoDeAlta(
                    Clasificacion.INDETERMINADA,
                    "no hay ningun cliente con ese identificador dentro de tu alcance: "
                    "no se reverifica lo que no se ve, y no se escribe a ciegas",
                )
            )

        veredicto = evaluar_alta(
            nombre=actual.nombre, sector=sector, descripcion=descripcion
        )
        if not veredicto.admitida:
            raise ReverificacionRechazada(veredicto)

        declarado = Sector(sector)
        cambio = actual.sector is not declarado

        resultado = await conexion.execute(
            text(
                "UPDATE clientes "  # noqa: S608
                f"SET {COLUMNA_SECTOR} = :sector, {COLUMNA_SECTOR_VERIFICADO} = now() "
                "WHERE id = :cliente"
            ),
            {"sector": declarado.value, "cliente": cliente_id},
        )
        if resultado.rowcount != 1:
            # La misma comprobacion que la ruta destructiva: entre la lectura y la
            # escritura hay una ventana, y sin mirar las filas afectadas el asiento
            # afirmaria un cambio que no ocurrio.
            raise ReverificacionRechazada(
                VeredictoDeAlta(
                    Clasificacion.INDETERMINADA,
                    "el cliente dejo de estar a tu alcance entre la lectura y la "
                    "escritura: no se cambio nada y la operacion se deshace entera",
                )
            )

        # WHY (el apunte lleva el inquilino del CLIENTE, no el de la sesion): quien
        # opera es la agencia, y su inquilino trae el centinela en `cliente_id`. Un
        # asiento con el centinela no colgaria de ningun cliente —la clave foranea
        # lo rechazaria— y, si colgara, la bitacora del cliente no tendria ni rastro
        # de que a el le cambiaron la clasificacion. El apunte pertenece al cliente
        # afectado; la politica lo admite porque la sesion es de alcance agencia.
        inquilino_del_apunte = Inquilino.desde_usuario(
            agencia_id=sesion.agencia_id, cliente_id=cliente_id
        )
        apunte_id = await apuntar(
            conexion,
            inquilino_del_apunte,
            actor=_actor_de(sesion),
            accion=ACCION_REVERIFICACION,
            recurso=f"cliente:{cliente_id}",
            detalle={
                "sector_anterior": None if actual.sector is None else actual.sector.value,
                "sector": declarado.value,
                "cambio": cambio,
                "clasificacion": veredicto.clasificacion.value,
                "marcadores": list(veredicto.marcadores),
                "motivo": veredicto.motivo,
            },
        )

    return Reverificacion(
        cliente_id=cliente_id,
        sector_anterior=actual.sector,
        sector=declarado,
        cambio=cambio,
        apunte_id=apunte_id,
        veredicto=veredicto,
    )
