"""T-021·quinquies (RF-66) — la aceptacion contractual: sin ella no hay alta.

RF-66: «CUANDO se da de alta un cliente, EL SISTEMA DEBE registrar la **aceptacion**
de la version vigente del contrato y de su anexo de tratamiento (quien, cuando, que
version) y **rechazar el alta** si falta; y CUANDO se publica una **version nueva**,
DEBE pedir la re-aceptacion y, pasado el plazo declarado sin ella, tratar la falta
como **suspension con aviso** — nunca un corte en silencio ni un cliente operando
bajo una version que ya no es la publicada.»

Tres piezas:

1. **El catalogo** (`versiones_publicadas`) — entidad de PLATAFORMA (plan §3.0):
   no cuelga de ningun inquilino porque no es de ninguno. Cada fila es una version
   de un documento, con si **declara la instruccion permanente de derechos**
   (RF-62), si es de **desarrollo**, y la huella del texto publicado.
2. **La aceptacion** (`aceptaciones_contractuales`) — clase *de cliente*: quien,
   cuando y **que version**, con el «quien» en la forma opaca de RF-10.
3. **La re-aceptacion** (`revisar_reaceptaciones`) — al publicarse una version
   nueva, el cliente que sigue con la anterior queda pendiente; se le AVISA, y
   pasado el plazo de gracia se le suspende con ese motivo.

# WHY (fail-closed, la misma regla que el guard sanitario): un alta sin aceptacion
# no se admite «porque ya se registrara»; una version que no existe en el catalogo
# no se acepta «porque el operador la escribio bien». Las dos son la misma clase de
# «no pude determinarlo», y aqui eso NO es «adelante». Sin catalogo poblado no hay
# altas con datos de personas reales: solo altas de DESARROLLO.
#
# # WHY (la version aceptada tiene que EXISTIR en el catalogo, y por que eso no es
# ceremonia): sin catalogo, «version aceptada» seria una cadena que alguien tecleo,
# y dos clientes podrian estar bajo textos distintos con el mismo nombre. La fila
# del catalogo es lo que ata la aceptacion a un texto concreto — de ahi
# `hash_del_texto`: dos versiones con el mismo nombre y distinto texto son dos
# filas, y la aceptacion apunta a UNA.
#
# # WHY (`solo_desarrollo` se DERIVA y no es una columna): la marca la fija el
# estado de las aceptaciones vigentes del cliente. Guardarla ademas en `clientes`
# la pondria en dos sitios, y el dia que un cliente re-acepte la version real la
# copia se quedaria vieja diciendo «este no puede tratar datos de personas» — o,
# peor, al reves. Es la misma regla que gobierna la suspension en `suspension.py`.
# ==Y ojo: `clientes.desarrollo` (B4) NO es esto==: aquella marca dice quien paga
# el modelo; esta dice bajo que contrato opera el cliente.
#
# # WHY (aqui NO se escribe texto contractual): el texto de la v2 —y la fila real
# del catalogo— los publica T-030·quater. Este modulo sabe registrar y comprobar
# aceptaciones; no sabe que dice el contrato, y no debe. Inventar aqui un texto
# «de ejemplo» seria publicar un contrato que nadie reviso.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text

from app.audit.bitacora import apuntar, es_actor_opaco
from app.tenancy.inquilino import Alcance, Inquilino
from app.tenancy.politicas import ALCANCE_AGENCIA, VARIABLE_ALCANCE
from app.tenancy.suspension import MOTIVO_REACEPTACION_PENDIENTE, suspender_cliente


class Documento(StrEnum):
    """Los documentos que hay que aceptar. ALLOWLIST: lo que no este, no cuenta."""

    CONTRATO = "contrato"
    ANEXO_TRATAMIENTO = "anexo_tratamiento"


#: Los documentos que un alta DEBE traer aceptados. Se derivan del enum: un
#: documento nuevo entra solo, y el alta que no lo traiga se rechaza sin que nadie
#: tenga que acordarse de anadirlo a una segunda lista.
DOCUMENTOS_EXIGIDOS: frozenset[Documento] = frozenset(Documento)

#: Plazo de gracia entre que se publica una version y que la falta de re-aceptacion
#: se trata como suspension.
#:
#: # WHY (el valor y su cita): spec §9 · **B12** — «plazo objetivo de resolucion de
#: una peticion de derechos; validez de la ficha de condiciones de un proveedor de
#: modelo; **gracia** tras caducar antes de bloquear credenciales nuevas · valores:
#: **30 dias** · 180 dias · **30 dias**». La gracia de B12 es el plazo declarado que
#: RF-66 nombra sin cifra: los demas documentos citan «B12» y no repiten el numero,
#: asi que la unica copia del numero dentro del producto es esta.
GRACIA_DE_REACEPTACION = timedelta(days=30)

#: Las acciones con las que esto queda escrito en la bitacora (RF-10).
ACCION_ACEPTACION = "aceptacion-contractual"
ACCION_REACEPTACION_PENDIENTE = "re-aceptacion-pendiente"

#: Asunto del aviso interno de RF-46 que produce el barrido.
ASUNTO_REACEPTACION = "re-aceptacion pendiente del contrato"

_CATALOGO = (
    "SELECT id, documento, version, publicada_en, declara_instruccion_de_derechos, "
    "       es_desarrollo, hash_del_texto "
    "FROM versiones_publicadas"
)

_POR_IDS = text(_CATALOGO + " WHERE id IN :ids").bindparams(
    bindparam("ids", expanding=True)
)

_ALCANCE_DECLARADO = text(f"SELECT current_setting('{VARIABLE_ALCANCE}') AS alcance")

_PUBLICAR = text(
    "INSERT INTO versiones_publicadas "
    "(documento, version, declara_instruccion_de_derechos, es_desarrollo, hash_del_texto) "
    "VALUES (:documento, :version, :declara, :desarrollo, :hash) "
    "RETURNING id, documento, version, publicada_en, declara_instruccion_de_derechos, "
    "          es_desarrollo, hash_del_texto"
)

_ACEPTAR = text(
    "INSERT INTO aceptaciones_contractuales "
    "(agencia_id, cliente_id, version_id, aceptada_por) "
    "VALUES (:agencia, :cliente, :version, :actor) "
    "RETURNING id"
)

#: La ULTIMA aceptacion de cada documento, para un cliente. `DISTINCT ON` con el
#: orden explicito: sin el desempate por `id`, dos aceptaciones del mismo instante
#: darian una respuesta u otra segun el plan de ejecucion.
_ULTIMAS_ACEPTACIONES = text(
    "SELECT DISTINCT ON (v.documento) "
    "       v.id, v.documento, v.version, v.publicada_en, "
    "       v.declara_instruccion_de_derechos, v.es_desarrollo, v.hash_del_texto "
    "FROM aceptaciones_contractuales a "
    "JOIN versiones_publicadas v ON v.id = a.version_id "
    "WHERE a.agencia_id = :agencia AND a.cliente_id = :cliente "
    "ORDER BY v.documento, a.aceptada_en DESC, a.id DESC"
)

#: Los clientes ALCANZABLES en esta sesion que no tienen aceptada esa version.
#: Incluye al que nunca acepto nada: no tenerlo tambien es no tenerlo al dia.
_CLIENTES_SIN_ESTA_VERSION = text(
    "SELECT c.agencia_id, c.id AS cliente_id FROM clientes c "
    "WHERE NOT EXISTS ("
    "    SELECT 1 FROM aceptaciones_contractuales a "
    "    WHERE a.agencia_id = c.agencia_id AND a.cliente_id = c.id "
    "      AND a.version_id = :version) "
    "ORDER BY c.id"
)


class VersionInexistente(Exception):
    """Se acepto una version que el catalogo no publica. Falla cerrado."""


class AceptacionAusente(Exception):
    """El alta no trae aceptacion, o no cubre todos los documentos exigidos."""


class AceptacionNoAutorizada(Exception):
    """Publicar o barrer es un acto de la agencia, no de un portal de cliente."""


@dataclass(frozen=True, slots=True)
class VersionPublicada:
    """Una fila del catalogo, ya leida."""

    id: UUID
    documento: Documento
    version: str
    publicada_en: datetime
    declara_instruccion_de_derechos: bool
    es_desarrollo: bool
    hash_del_texto: str


@dataclass(frozen=True, slots=True)
class Aceptacion:
    """Lo que el alta tiene que traer: que versiones se aceptaron y quien.

    `versiones` son identificadores del catalogo; `aceptada_por` es el «quien» de
    RF-10 —rol + identificador opaco—, **nunca** un nombre ni un correo.
    """

    versiones: tuple[UUID, ...]
    aceptada_por: str


@dataclass(frozen=True, slots=True)
class Aviso:
    """Un aviso INTERNO (RF-46) ya materializado, listo para que alguien lo entregue.

    # WHY (se DEVUELVE en vez de enviarse): el transporte es el punto unico de
    # salida (plan §4.1, rama INTERNO) y la lista de destinos declarados es
    # `destinos_de_aviso` (RF-46·bis) — ninguna de las dos existe todavia. Un
    # modulo que «enviara» hoy tendria que abrirse su propio transporte, que es
    # exactamente el segundo camino sin auditar que §4.1 prohibe. Aqui el aviso se
    # materializa y se apunta; entregarlo es de quien tenga el riel.
    """

    inquilino: Inquilino
    asunto: str
    detalle: Mapping[str, Any] = field(default_factory=dict)
    suspendido: bool = False


async def _exigir_alcance_de_agencia(conexion) -> None:
    """El alcance se LEE de la transaccion, no se recibe por parametro.

    # WHY: pedirlo dejaria que quien llama declarase «soy agencia» sin serlo, que es
    # justo la escalada que `Inquilino.desde_usuario` existe para cerrar. La sesion
    # ya esta declarada en la conexion (`sesion_de_inquilino`), asi que lo unico que
    # hace falta es preguntarselo a ella. `current_setting` sin su segundo argumento
    # LANZA si nadie declaro el inquilino: una transaccion sin sesion no se cuela
    # como «no es agencia», se cae.
    """
    alcance = (await conexion.execute(_ALCANCE_DECLARADO)).scalar_one()
    if alcance != ALCANCE_AGENCIA:
        raise AceptacionNoAutorizada(
            f"esta operacion es de la agencia y la sesion declara alcance {alcance!r}. "
            "El catalogo de versiones gobierna a TODOS los clientes, asi que no lo "
            "escribe —ni lo barre— uno de ellos"
        )


def _exigir_cliente(inquilino: Inquilino) -> None:
    if inquilino.alcance is not Alcance.CLIENTE:
        raise AceptacionAusente(
            "la aceptacion es POR CLIENTE y este inquilino no nombra a ninguno "
            f"(alcance {inquilino.alcance})"
        )


def _version(fila) -> VersionPublicada:
    return VersionPublicada(
        id=fila.id,
        documento=Documento(fila.documento),
        version=fila.version,
        publicada_en=fila.publicada_en,
        declara_instruccion_de_derechos=fila.declara_instruccion_de_derechos,
        es_desarrollo=fila.es_desarrollo,
        hash_del_texto=fila.hash_del_texto,
    )


def exigir_aceptacion(aceptacion: object) -> Aceptacion:
    """El paso del alta, en su forma PURA: ¿trae algo que se pueda comprobar?

    Lo que depende de la base —que las versiones existan y cubran los documentos—
    se comprueba en `registrar_aceptacion`, dentro de la transaccion del alta.
    Aqui se rechaza lo que ni siquiera hace falta consultar.
    """
    if not isinstance(aceptacion, Aceptacion):
        raise AceptacionAusente(
            "el alta no trae aceptacion contractual (RF-66): sin ella no hay alta. "
            "Componla con `Aceptacion(versiones=..., aceptada_por=...)`"
        )
    if not aceptacion.versiones:
        raise AceptacionAusente(
            "la aceptacion no nombra ninguna version: aceptar «algo» sin decir que es "
            "no ata al cliente a ningun texto"
        )
    if len(set(aceptacion.versiones)) != len(aceptacion.versiones):
        raise AceptacionAusente(
            "la aceptacion repite una version: dos filas de la misma no cubren los "
            "dos documentos que RF-66 exige"
        )
    if not es_actor_opaco(aceptacion.aceptada_por):
        raise AceptacionAusente(
            "el «quien» de la aceptacion se escribe como rol + identificador opaco "
            "(RF-10), nunca como nombre ni correo"
        )
    return aceptacion


async def catalogo(conexion, *, documento: Documento | None = None) -> list[VersionPublicada]:
    """Lo que la plataforma tiene publicado, de lo mas nuevo a lo mas viejo."""
    consulta = _CATALOGO
    parametros: dict[str, Any] = {}
    if documento is not None:
        consulta += " WHERE documento = :documento"
        parametros["documento"] = Documento(documento).value
    consulta += " ORDER BY publicada_en DESC, id DESC"
    filas = (await conexion.execute(text(consulta), parametros)).all()
    return [_version(f) for f in filas]


async def version_vigente(conexion, documento: Documento) -> VersionPublicada | None:
    """La ultima version publicada de ese documento, o `None` si no hay ninguna."""
    publicadas = await catalogo(conexion, documento=documento)
    return publicadas[0] if publicadas else None


async def versiones_vigentes(conexion) -> dict[Documento, VersionPublicada]:
    """La vigente de CADA documento exigido. Un documento sin fila no aparece."""
    vigentes: dict[Documento, VersionPublicada] = {}
    for documento in sorted(DOCUMENTOS_EXIGIDOS):
        actual = await version_vigente(conexion, documento)
        if actual is not None:
            vigentes[documento] = actual
    return vigentes


async def publicar_version(
    conexion,
    *,
    documento: Documento,
    version: str,
    declara_instruccion_de_derechos: bool,
    es_desarrollo: bool,
    hash_del_texto: str,
) -> VersionPublicada:
    """Publica una version en el catalogo. Solo alcance agencia.

    # WHY (no deja apunte en la bitacora, y se dice en voz alta): la bitacora de
    # RF-10 es POR CLIENTE —cuelga de `clientes` con clave foranea— y publicar una
    # version no es un acto sobre los datos de ningun cliente: es un acto de la
    # plataforma. Escribirlo ahi obligaria a elegir un cliente al azar, que seria un
    # asiento falso. Lo que SI queda por cliente es la re-aceptacion que esta
    # publicacion provoca, y eso lo apunta `revisar_reaceptaciones`.
    """
    await _exigir_alcance_de_agencia(conexion)
    if not isinstance(version, str) or not version.strip():
        raise VersionInexistente("una version publicada necesita nombre de version")
    if not isinstance(hash_del_texto, str) or not hash_del_texto.strip():
        raise VersionInexistente(
            "una version publicada necesita la huella de SU texto: sin ella, dos "
            "textos distintos con el mismo nombre serian indistinguibles"
        )
    fila = (
        await conexion.execute(
            _PUBLICAR,
            {
                "documento": Documento(documento).value,
                "version": version.strip(),
                "declara": bool(declara_instruccion_de_derechos),
                "desarrollo": bool(es_desarrollo),
                "hash": hash_del_texto.strip(),
            },
        )
    ).one()
    return _version(fila)


async def registrar_aceptacion(
    conexion, inquilino: Inquilino, aceptacion: Aceptacion
) -> tuple[UUID, ...]:
    """Escribe la aceptacion del cliente. Falla cerrado y NO escribe nada a medias.

    Comprueba, contra el CATALOGO, que cada version exista y que entre todas cubran
    exactamente los documentos exigidos. Va dentro de la transaccion del alta: si
    algo de esto falla, el alta entera se deshace — que es como «sin aceptacion no
    hay alta» deja de ser una frase.
    """
    _exigir_cliente(inquilino)
    exigida = exigir_aceptacion(aceptacion)

    filas = (await conexion.execute(_POR_IDS, {"ids": list(exigida.versiones)})).all()
    encontradas = {f.id: _version(f) for f in filas}
    ausentes = [str(v) for v in exigida.versiones if v not in encontradas]
    if ausentes:
        raise VersionInexistente(
            f"estas versiones no estan publicadas en el catalogo: {ausentes}. Una "
            "version que nadie publico no ata al cliente a ningun texto, asi que el "
            "alta se rechaza (RF-66)"
        )

    por_documento = sorted(encontradas.values(), key=lambda v: v.documento.value)
    cubiertos = {version.documento for version in por_documento}
    if len(cubiertos) != len(por_documento):
        raise AceptacionAusente(
            "la aceptacion trae dos versiones del mismo documento: cual manda no seria "
            "decidible, y el cliente quedaria bajo dos textos a la vez"
        )
    if cubiertos != DOCUMENTOS_EXIGIDOS:
        faltan = sorted(d.value for d in DOCUMENTOS_EXIGIDOS - cubiertos)
        sobran = sorted(d.value for d in cubiertos - DOCUMENTOS_EXIGIDOS)
        raise AceptacionAusente(
            f"la aceptacion no cubre los documentos exigidos: faltan {faltan}, "
            f"sobran {sobran}. RF-66 exige el contrato Y su anexo de tratamiento"
        )

    escritas: list[UUID] = []
    for version in por_documento:
        fila = (
            await conexion.execute(
                _ACEPTAR,
                {
                    "agencia": inquilino.agencia_id,
                    "cliente": inquilino.cliente_id,
                    "version": version.id,
                    "actor": exigida.aceptada_por,
                },
            )
        ).one()
        escritas.append(fila.id)

    await apuntar(
        conexion,
        inquilino,
        actor=exigida.aceptada_por,
        accion=ACCION_ACEPTACION,
        recurso=f"cliente:{inquilino.cliente_id}",
        detalle={
            "versiones": {v.documento.value: v.version for v in por_documento},
            "solo_desarrollo": any(v.es_desarrollo for v in por_documento),
            "declara_instruccion_de_derechos": all(
                v.declara_instruccion_de_derechos for v in por_documento
            ),
        },
    )
    return tuple(escritas)


async def aceptaciones_vigentes(
    conexion, inquilino: Inquilino
) -> dict[Documento, VersionPublicada]:
    """La ULTIMA version aceptada por este cliente, por documento."""
    _exigir_cliente(inquilino)
    filas = (
        await conexion.execute(
            _ULTIMAS_ACEPTACIONES,
            {"agencia": inquilino.agencia_id, "cliente": inquilino.cliente_id},
        )
    ).all()
    return {Documento(f.documento): _version(f) for f in filas}


async def esta_en_solo_desarrollo(conexion, inquilino: Inquilino) -> bool:
    """¿Este cliente opera bajo un contrato de DESARROLLO? Se deriva, no se guarda.

    Devuelve `True` tambien cuando falta la aceptacion de algun documento exigido:
    un cliente del que no consta bajo que texto opera **no** puede tratar datos de
    personas reales. Es la direccion en la que el defecto no cuesta datos de nadie.
    """
    vigentes = await aceptaciones_vigentes(conexion, inquilino)
    if set(vigentes) != DOCUMENTOS_EXIGIDOS:
        return True
    return any(version.es_desarrollo for version in vigentes.values())


async def revisar_reaceptaciones(
    conexion, hoy: datetime, *, actor: str, gracia: timedelta = GRACIA_DE_REACEPTACION
) -> list[Aviso]:
    """El barrido de RF-66: avisa a quien no re-acepto y suspende al que se pasa.

    Funcion PURA respecto del reloj —`hoy` entra por parametro— y respecto del
    transporte: devuelve los avisos y no envia ninguno. Corre bajo una sesion de
    alcance **agencia**, que es la unica que alcanza a la cartera.

    # WHY (avisa SIEMPRE y suspende solo pasado el plazo): RF-66 exige «suspension
    # con aviso — nunca un corte en silencio». Un barrido que solo hablara al
    # suspender dejaria al cliente enterandose del corte por el corte.
    #
    # # WHY (sin cablear al worker): quien lo llama cada dia es `apps/worker`, y esa
    # costura la toca otra casilla. Dejarlo aqui como funcion con reloj inyectable
    # es lo que permite medir los dos lados del plazo sin esperar reloj real.
    """
    await _exigir_alcance_de_agencia(conexion)
    if not es_actor_opaco(actor):
        raise AceptacionAusente(
            "el actor del barrido se escribe como rol + identificador opaco (RF-10)"
        )

    vigentes = await versiones_vigentes(conexion)
    pendientes: dict[tuple[UUID, UUID], list[VersionPublicada]] = {}
    for version in vigentes.values():
        filas = (
            await conexion.execute(_CLIENTES_SIN_ESTA_VERSION, {"version": version.id})
        ).all()
        for fila in filas:
            pendientes.setdefault((fila.agencia_id, fila.cliente_id), []).append(version)

    avisos: list[Aviso] = []
    for (agencia_id, cliente_id), versiones in sorted(
        pendientes.items(), key=lambda par: str(par[0][1])
    ):
        inquilino = Inquilino.desde_usuario(agencia_id=agencia_id, cliente_id=cliente_id)
        # La mas ANTIGUA de las pendientes: es la que lleva mas tiempo sin aceptarse,
        # y por tanto la que decide si la gracia ya se agoto.
        desde = min(version.publicada_en for version in versiones)
        vencida = (hoy - desde) > gracia
        detalle: dict[str, Any] = {
            "documentos": sorted(v.documento.value for v in versiones),
            "publicada_en": desde.isoformat(),
            "gracia_dias": int(gracia.total_seconds() // 86400),
            "vencida": vencida,
        }
        await apuntar(
            conexion,
            inquilino,
            actor=actor,
            accion=ACCION_REACEPTACION_PENDIENTE,
            recurso=f"cliente:{cliente_id}",
            detalle=detalle,
        )
        if vencida:
            await suspender_cliente(
                conexion, inquilino, motivo=MOTIVO_REACEPTACION_PENDIENTE, actor=actor
            )
        avisos.append(
            Aviso(
                inquilino=inquilino,
                asunto=ASUNTO_REACEPTACION,
                detalle=detalle,
                suspendido=vencida,
            )
        )
    return avisos
