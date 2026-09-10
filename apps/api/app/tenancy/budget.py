"""El techo de gasto: corta de verdad, y avisa antes de cortar (RF-16).

# WHY (por que el estado vive en Postgres y no en Redis ni en memoria, RNF-03):
# un techo guardado en la memoria del proceso se multiplica por el numero de
# procesos —cada uno cree tener el suyo— y uno guardado en Redis desaparece con
# un reinicio, dejando el mes a cero. Aqui el gasto es una tabla y el techo una
# columna, y el corte es una propiedad de la TRANSACCION.
#
# ==WHY (el corte es ATOMICO por bloqueo de fila, no por «leo, decido, escribo»)
# — esto es lo que funda la casilla.== La forma ingenua
#
#     gastado = SELECT sum(...)          # (1)
#     if gastado + monto <= techo:       # (2)
#         INSERT ...                     # (3)
#
# pasa cualquier prueba secuencial y deja pasar N veces el techo en cuanto hay
# dos procesos: entre (1) y (3) de una transaccion caben los (1) de todas las
# demas, que leen la misma suma vieja y deciden que si. `registrar_consumo`
# bloquea PRIMERO la fila del techo con `SELECT ... FOR UPDATE` y solo despues
# suma: las transacciones se serializan en esa fila, y como Postgres toma una
# instantanea nueva por SENTENCIA en `READ COMMITTED`, la suma que lee cada una
# ya incluye lo que confirmo la anterior. Lo mide
# `test_veinticuatro_consumos_concurrentes_no_superan_el_techo`.
#
# ==WHY (la VENTANA es el mes calendario de facturacion, y NO se hereda la de
# `limits.py` — P-23).== Aquel modulo usa una ventana FIJA y su propia cabecera
# declara el defecto medido: en el borde admite el DOBLE de la cuota, porque el
# contador se reinicia. Alli es tolerable —es un guard de abuso, y el limite
# declarado no es una promesa de dinero—; aqui no lo seria. La diferencia no es
# de rigor, es de QUE representa la ventana:
#
#   - En un limite de tasa, la ventana es un PROXY de «por unidad de tiempo». Que
#     el borde admita el doble es un error de la aproximacion.
#   - Aqui la ventana ES la unidad de cuenta. «20 USD/mes» significa 20 dolares
#     en el mes que se factura. Gastar 20 el 31 de agosto y 20 el 1 de septiembre
#     no es «el doble de la cuota»: son exactamente las cuotas de dos meses
#     distintos, y es lo que el cliente ve en dos facturas distintas. No hay
#     ninguna magnitud suavizada que la ventana este aproximando mal.
#
# ==Y lo que esta ventana NO garantiza, dicho aqui para que nadie lo suponga:==
#   (a) NO acota el gasto de 30 dias cualesquiera. Un cliente puede gastar dos
#       techos en 48 horas a caballo del cambio de mes. Es por diseno, no un
#       descuido: los dos meses se facturan por separado.
#   (b) NO acota el RITMO. El techo entero se puede quemar en un minuto. Eso lo
#       limita el cupo de VOLUMEN de RF-55 (paso 5b de `entregar()`), que es otra
#       cosa y otra casilla.
#   (c) El mes es UTC. Un cliente en otro huso ve el reinicio a una hora local
#       que no es medianoche. Se elige UTC porque `registrado_en` es `timestamptz`
#       y el huso de facturacion de cada cliente no es un dato que exista todavia.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.agents.providers import Titular
from app.audit.bitacora import apuntar
from app.tenancy.inquilino import CENTINELA_SIN_CLIENTE, Alcance, Inquilino

RAIZ = Path(__file__).resolve().parents[4]

#: El catalogo de precios versionado en el repositorio. Su procedencia y su fecha
#: viven DENTRO del archivo: un catalogo sin fuente no se puede auditar (P-03).
ARCHIVO_DE_PRECIOS = RAIZ / "docs" / "precios" / "mensajeria.toml"

#: Los dos unicos conceptos que cuentan contra el techo. ==G-07: la version
#: anterior de RF-16 contaba solo tokens y dejaba escapar la factura de la
#: mensajeria.== Es una ALLOWLIST: un concepto nuevo no entra en la cuenta del
#: dinero por existir, entra porque alguien lo declara aqui.
CONCEPTO_MODELO = "modelo"
CONCEPTO_MENSAJERIA = "mensajeria"
CONCEPTOS = (CONCEPTO_MODELO, CONCEPTO_MENSAJERIA)

#: Los cuatro tipos de mensaje tarifables. ==La tarificacion por CONVERSACION
#: esta deprecada desde el 1-jul-2025: hoy es por mensaje/plantilla== (T-004*pre).
TIPOS_DE_MENSAJE = ("utilidad", "marketing", "autenticacion", "servicio")

#: spec §9 · B4 — «techo de gasto por defecto de un cliente, conservador y que el
#: cliente puede subir (la plataforma nunca lo baja sola)».
TECHO_POR_DEFECTO_CLIENTE_USD = Decimal("20")

#: spec §9 · B4 (16ª I-16-04) — «lo que consume la clave de agencia cuenta contra
#: un techo de la agencia con la misma alarma de RF-16, nunca sin limite»: 100
#: USD/mes para TODA la actividad de desarrollo de la agencia. Es uno solo y
#: compartido: dos altas de desarrollo no tienen 100 cada una.
TECHO_POR_DEFECTO_AGENCIA_USD = Decimal("100")

#: Fraccion del techo a la que se avisa. 80 %: lo bastante pronto para que quede
#: margen de reaccion y lo bastante tarde para que el aviso signifique algo.
#: Vive junto al techo en la misma fila y se lee en la MISMA lectura bloqueada.
UMBRAL_DE_ALARMA_POR_DEFECTO = Decimal("0.80")

#: RF-16, rama del no: un catalogo de precios con mas de 30 dias esta CADUCADO.
DIAS_DE_VIGENCIA_DEL_CATALOGO = 30

#: Las acciones que este modulo escribe en la bitacora.
ACCION_ALARMA = "techo.alarma"
ACCION_TECHO_ALCANZADO = "techo.alcanzado"
ACCION_TECHO_SUBIDO = "techo.subido"

#: Los dos destinatarios que RF-16 nombra: «avisar al operador y al cliente».
DESTINATARIOS_DEL_TECHO = ("operador", "cliente")

#: Como se distingue en la bitacora la alarma del techo del CLIENTE de la del
#: techo de la AGENCIA. Sin esto, la idempotencia mensual de una silenciaria a la
#: otra: las dos son `techo.alarma` sobre la misma agencia.
RECURSO_TECHO_CLIENTE = "techo:cliente"
RECURSO_TECHO_AGENCIA = "techo:agencia"


# ==========================================================================
# Lo que se levanta cuando algo no se puede hacer
# ==========================================================================
class MontoNoAdmitido(ValueError):
    """Un importe que no es un gasto: cero o negativo."""


class ConceptoNoAdmitido(ValueError):
    """Un concepto fuera de la allowlist: no entra en la cuenta del dinero."""


class ConsumoSinCliente(ValueError):
    """Gasto sin dueno: en alcance agencia hay que nombrar al cliente."""


class SinTechoAlcanzable(RuntimeError):
    """No hay ninguna fila de techo que esta sesion pueda bloquear.

    # WHY (esto es fail-closed y ademas es un MECANISMO, no un `if`): pasa cuando
    # una sesion de alcance cliente intenta cargar contra el techo de la agencia.
    # La consulta no devuelve fila porque la politica de `agencias` esconde la
    # tabla del alcance cliente (L-02), no porque este modulo lo compruebe. Sin
    # techo que bloquear no se consume: «no encontre el limite» jamas significa
    # «no hay limite».
    """


class TechoNoSeBaja(ValueError):
    """B4: «la plataforma nunca lo baja sola». Bajarlo es OTRO acto, y no existe."""


class CatalogoDePreciosVacio(RuntimeError):
    """No se conoce ningun precio: no hay «el mas alto conocido» que usar.

    Se falla en voz alta en vez de contar cero, que es contar mal en la direccion
    cara — y ademas en silencio.
    """


@dataclass(frozen=True, slots=True)
class Aviso:
    """Un aviso INTERNO que sale de este modulo (RF-46).

    # WHY (es un OBJETO devuelto y no un envio): el punto unico de salida de
    # mensajes es `entregar()` (D-17, plan §4.1), y este modulo no lo llama —
    # llamarlo desde aqui seria el segundo camino que D-17 existe para que no
    # haya. El aviso viaja con el resultado (o con la excepcion) y lo entrega
    # quien tiene el contrato: la ranura la ocupan T-118 (destinos declarados,
    # RF-46·bis) y T-119 (`entregar()`, rama INTERNO).
    """

    motivo: str
    destinatario: str
    detalle: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Consumo:
    """Un gasto ya registrado, con el estado del techo despues de registrarlo."""

    id: UUID
    gastado_usd: Decimal
    techo_usd: Decimal
    avisos: tuple[Aviso, ...]


class TechoAlcanzado(RuntimeError):
    """RF-16: se deja de consumir. No se registra la fila y se avisa.

    # WHY (se levanta en vez de devolverse): la transaccion del llamante tiene
    # que DESHACERSE. Un rechazo que dejara su fila haria el techo irreversible —
    # la suma subiria con cada intento fallido y el cliente no podria volver a
    # gastar ni subiendo el techo. Lo mide
    # `test_el_consumo_rechazado_no_deja_fila`.
    #
    # # WHY (los avisos viajan en la excepcion): al deshacerse la transaccion se
    # deshace tambien cualquier apunte que este modulo hubiera escrito, asi que
    # el rastro del rechazo lo escribe quien decide que hacer con el —`entregar()`
    # paso 5, que ya registra en la bitacora en ambos casos (plan §4.1)—. Se
    # entrega el aviso ya construido para que no tenga que reconstruirlo.
    """

    def __init__(
        self,
        *,
        gastado_usd: Decimal,
        techo_usd: Decimal,
        solicitado_usd: Decimal,
        avisos: tuple[Aviso, ...],
    ) -> None:
        super().__init__(
            f"techo alcanzado: lleva {gastado_usd} USD de {techo_usd} en el mes y "
            f"se pedian {solicitado_usd} mas. Se deja de consumir (RF-16)"
        )
        self.gastado_usd = gastado_usd
        self.techo_usd = techo_usd
        self.solicitado_usd = solicitado_usd
        self.avisos = avisos


@dataclass(frozen=True, slots=True)
class Precio:
    """Un precio de mensajeria ya resuelto, con su procedencia."""

    pais: str
    tipo: str
    precio_usd: Decimal
    vigente_desde: date
    fuente: str
    #: `True` cuando NO es el precio de ese pais y ese tipo, sino el mas alto
    #: conocido usado como respaldo. El hecho viaja con el resultado.
    es_respaldo: bool


@dataclass(frozen=True, slots=True)
class Costo:
    """El precio a cobrar y los avisos que su procedencia obliga a dar."""

    precio: Precio
    avisos: tuple[Aviso, ...]


@dataclass(frozen=True, slots=True)
class ArchivoDePrecios:
    """El catalogo versionado, leido del repositorio."""

    fecha: str
    fuente: str
    verificado: bool
    entradas: tuple[Mapping[str, Any], ...]


# ==========================================================================
# La ventana
# ==========================================================================
def inicio_del_mes(momento: datetime) -> datetime:
    """El primer instante del mes de facturacion al que pertenece `momento`.

    # WHY (rechaza un `datetime` sin zona): un instante ingenuo no dice a que mes
    # pertenece. Compararlo con `registrado_en`, que es `timestamptz`, levantaria
    # en Postgres o —peor— acertaria por casualidad en la maquina donde el huso
    # local coincide con UTC y fallaria en produccion.
    """
    if momento.tzinfo is None:
        raise ValueError(
            f"{momento!r} viene sin zona horaria: no se puede decir a que mes de "
            "facturacion pertenece un instante que no dice donde ocurrio"
        )
    en_utc = momento.astimezone(UTC)
    return datetime(en_utc.year, en_utc.month, 1, tzinfo=UTC)


def _fin_del_mes(momento: datetime) -> datetime:
    inicio = inicio_del_mes(momento)
    if inicio.month == 12:
        return datetime(inicio.year + 1, 1, 1, tzinfo=UTC)
    return datetime(inicio.year, inicio.month + 1, 1, tzinfo=UTC)


# ==========================================================================
# Las sentencias. Literales y nombradas: nada se construye con f-strings.
# ==========================================================================
_BLOQUEAR_TECHO_DEL_CLIENTE = (
    "SELECT techo_usd_mes, umbral_de_alarma FROM clientes "
    "WHERE agencia_id = :agencia_id AND id = :cliente_id"
)

_BLOQUEAR_TECHO_DE_LA_AGENCIA = (
    "SELECT techo_usd_mes, umbral_de_alarma FROM agencias WHERE agencia_id = :agencia_id"
)

#: El sufijo que convierte la lectura del techo en el CERROJO. Se anade aqui, en
#: un solo sitio, para que la version bloqueante y la que solo lee no puedan
#: divergir en el resto de la consulta.
_CERROJO = " FOR UPDATE"

_GASTO_DEL_CLIENTE = text(
    "SELECT COALESCE(SUM(monto_usd), 0) AS gastado FROM consumos "
    "WHERE agencia_id = :agencia_id AND cliente_id = :cliente_id "
    "  AND titular = :titular "
    "  AND registrado_en >= :desde AND registrado_en < :hasta"
)

_GASTO_DE_LA_AGENCIA = text(
    "SELECT COALESCE(SUM(monto_usd), 0) AS gastado FROM consumos "
    "WHERE agencia_id = :agencia_id AND titular = :titular "
    "  AND registrado_en >= :desde AND registrado_en < :hasta"
)

_REGISTRAR = text(
    "INSERT INTO consumos (agencia_id, cliente_id, heraldo_id, titular, concepto, "
    "                      monto_usd, detalle, registrado_en) "
    "VALUES (:agencia_id, :cliente_id, :heraldo_id, :titular, :concepto, "
    "        :monto_usd, CAST(:detalle AS jsonb), :registrado_en) "
    "RETURNING id"
)

#: ==WHY (la ventana se lee del DETALLE del apunte y NO de `ocurrido_en`): son
#: dos relojes distintos y decidian lo mismo.== `ocurrido_en` lo pone la base con
#: `now()` —el instante en que se ESCRIBIO el apunte—, mientras que el mes de
#: facturacion sale de `ahora`, el instante del CONSUMO. En produccion casi
#: siempre coinciden, y por eso el defecto es del tipo que no se ve: en cuanto un
#: consumo se registra con fecha pasada —un reintento diferido, un relleno— la
#: alarma consulta un mes y se escribe en otro, y el resultado es que se silencia
#: la alarma de un mes por un apunte de otro. Se midio en
#: `test_la_alarma_del_mes_anterior_no_silencia_la_de_este` (P-07). El mes es una
#: propiedad del consumo, asi que se ESCRIBE en el apunte y se consulta de ahi:
#: un solo reloj gobierna la ventana entera.
_ALARMA_YA_EMITIDA = text(
    "SELECT 1 FROM bitacora "
    "WHERE agencia_id = :agencia_id AND accion = :accion AND recurso = :recurso "
    "  AND detalle ->> 'mes' = :mes "
    "  AND detalle ->> 'techo_usd' = :techo "
    "LIMIT 1"
)

_SUBIR_TECHO = text(
    "UPDATE clientes SET techo_usd_mes = :techo, techo_actualizado_en = :ahora "
    "WHERE agencia_id = :agencia_id AND id = :cliente_id "
    "RETURNING techo_usd_mes"
)

_CATALOGO_CARGADO_EN = text("SELECT max(cargada_en) AS cargada_en FROM precios_por_pais")

_PRECIO_VIGENTE = text(
    "SELECT pais, tipo, precio_usd, vigente_desde, fuente FROM precios_por_pais "
    "WHERE pais = :pais AND tipo = :tipo AND vigente_desde <= :hoy "
    "ORDER BY vigente_desde DESC LIMIT 1"
)

_PRECIO_MAS_ALTO = text(
    "SELECT pais, tipo, precio_usd, vigente_desde, fuente FROM precios_por_pais "
    "ORDER BY precio_usd DESC, pais, tipo LIMIT 1"
)

_VACIAR_PRECIOS = text("DELETE FROM precios_por_pais")

_INSERTAR_PRECIO = text(
    "INSERT INTO precios_por_pais (pais, tipo, precio_usd, vigente_desde, fuente, "
    "                              cargada_en) "
    "VALUES (:pais, :tipo, :precio_usd, :vigente_desde, :fuente, :cargada_en)"
)


# ==========================================================================
# Registrar un consumo — el corte
# ==========================================================================
async def registrar_consumo(
    conexion,
    inquilino: Inquilino,
    *,
    concepto: str,
    monto_usd: Decimal,
    detalle: Mapping[str, Any],
    heraldo_id: UUID | None = None,
    cliente_id: UUID | None = None,
    titular: Titular = Titular.CLIENTE,
    ahora: datetime | None = None,
) -> Consumo:
    """Registra un gasto si cabe bajo el techo, y CORTA si no cabe (RF-16).

    Devuelve el `Consumo` con el gasto acumulado del mes y los avisos que haya
    que entregar; levanta `TechoAlcanzado` cuando no cabe, con lo cual la
    transaccion del llamante se deshace y la fila no se escribe.

    `titular` decide CONTRA QUE TECHO se carga: el del cliente, o el de la
    agencia cuando el consumo salio de la clave de agencia (B4). Lo devuelve
    `resolver_credencial` (T-100); aqui no se adivina.
    """
    momento = ahora or datetime.now(UTC)
    monto = _monto_valido(monto_usd)
    _concepto_valido(concepto)
    dueno = _cliente_del_consumo(inquilino, cliente_id)

    desde = inicio_del_mes(momento)
    hasta = _fin_del_mes(momento)

    # (1) EL CERROJO. Todo lo que sigue ocurre con la fila del techo bloqueada,
    # asi que dos transacciones no pueden decidir a la vez sobre el mismo techo.
    techo, umbral = await _leer_techo(conexion, inquilino, titular, dueno, bloquear=True)

    # (2) La suma del mes. Se lee DESPUES del cerrojo: en READ COMMITTED esta
    # sentencia toma una instantanea nueva, asi que ya ve lo que confirmo quien
    # tenia el cerrojo antes.
    gastado = await _gasto_del_mes(conexion, inquilino, titular, dueno, desde, hasta)

    # (3) La decision. `<=` y no `<`: un techo de 10 admite gastar exactamente 10.
    if gastado + monto > techo:
        raise TechoAlcanzado(
            gastado_usd=gastado,
            techo_usd=techo,
            solicitado_usd=monto,
            avisos=_avisos(
                ACCION_TECHO_ALCANZADO,
                gastado_usd=gastado,
                techo_usd=techo,
                titular=titular,
            ),
        )

    fila = (
        await conexion.execute(
            _REGISTRAR,
            {
                "agencia_id": inquilino.agencia_id,
                "cliente_id": dueno,
                "heraldo_id": heraldo_id,
                "titular": titular.value,
                "concepto": concepto,
                "monto_usd": monto,
                "detalle": json.dumps(dict(detalle), default=str, ensure_ascii=False),
                "registrado_en": momento,
            },
        )
    ).one()

    nuevo_total = gastado + monto
    avisos = await _alarma_si_toca(
        conexion,
        inquilino,
        titular=titular,
        dueno=dueno,
        gastado_usd=nuevo_total,
        techo_usd=techo,
        umbral=umbral,
        desde=desde,
    )
    return Consumo(id=fila.id, gastado_usd=nuevo_total, techo_usd=techo, avisos=avisos)


async def exigir_margen(
    conexion,
    inquilino: Inquilino,
    monto_estimado: Decimal,
    *,
    cliente_id: UUID | None = None,
    titular: Titular = Titular.CLIENTE,
    ahora: datetime | None = None,
) -> Decimal:
    """Comprueba ANTES de gastar que el estimado cabe. Devuelve lo que queda.

    Lo llaman el manejador de generacion (T-109) y el paso 5 TECHO de
    `entregar()` (T-119): si no cabe, el modelo NO se invoca y el mensaje NO se
    envia — que es la unica forma de que el techo ahorre dinero en vez de
    limitarse a contarlo despues de haberlo gastado.

    # WHY (esto es una COMPROBACION y NO una reserva, y decirlo importa): no
    # escribe nada y no bloquea nada, asi que entre este «si cabe» y el
    # `registrar_consumo` que venga despues pueden entrar otros consumos. La
    # garantia de que el techo no se supera la da `registrar_consumo`, que es
    # atomico; esta funcion solo evita el gasto que ya se sabe que sobra. Una
    # reserva de verdad exigiria una fila de retencion con su caducidad — y
    # entonces un fallo del proceso dejaria saldo bloqueado sin dueno hasta que
    # algo lo barriera. Lo mide `test_exigir_margen_no_registra_gasto`.
    """
    momento = ahora or datetime.now(UTC)
    monto = _monto_valido(monto_estimado)
    dueno = _cliente_del_consumo(inquilino, cliente_id)
    desde = inicio_del_mes(momento)
    hasta = _fin_del_mes(momento)

    techo, _ = await _leer_techo(conexion, inquilino, titular, dueno, bloquear=False)
    gastado = await _gasto_del_mes(conexion, inquilino, titular, dueno, desde, hasta)
    if gastado + monto > techo:
        raise TechoAlcanzado(
            gastado_usd=gastado,
            techo_usd=techo,
            solicitado_usd=monto,
            avisos=_avisos(
                ACCION_TECHO_ALCANZADO,
                gastado_usd=gastado,
                techo_usd=techo,
                titular=titular,
            ),
        )
    return techo - gastado


async def subir_techo(
    conexion,
    inquilino: Inquilino,
    *,
    cliente_id: UUID,
    nuevo_techo_usd: Decimal,
    actor: str,
) -> Decimal:
    """Sube el techo de un cliente. Acto del OPERADOR, con su apunte.

    # WHY (solo sube): B4 dice «la plataforma nunca lo baja sola». Bajarlo es
    # otro acto —y con otras consecuencias: un cliente cortado a mitad de mes por
    # una bajada silenciosa no sabria por que— y por eso no existe aqui. Que la
    # unica primitiva sea «subir» hace que bajarlo sea imposible por omision.
    """
    if inquilino.alcance is not Alcance.AGENCIA:
        raise PermissionError(
            "subir el techo es un acto del operador de la agencia: esta sesion tiene "
            f"alcance {inquilino.alcance}. El alcance lo decide la identidad de la "
            "sesion, no quien llama a la funcion (plan §3.1 punto 5)"
        )
    nuevo = _monto_valido(nuevo_techo_usd)
    momento = datetime.now(UTC)

    fila = (
        await conexion.execute(
            text(_BLOQUEAR_TECHO_DEL_CLIENTE + _CERROJO),
            {"agencia_id": inquilino.agencia_id, "cliente_id": cliente_id},
        )
    ).one_or_none()
    if fila is None:
        raise SinTechoAlcanzable(
            f"no hay ningun cliente {cliente_id} alcanzable desde esta sesion cuyo "
            "techo subir"
        )
    actual = Decimal(fila.techo_usd_mes)
    if nuevo <= actual:
        raise TechoNoSeBaja(
            f"el techo vigente es {actual} USD y se pedia dejarlo en {nuevo}. Este "
            "verbo solo SUBE: la plataforma nunca baja el techo sola (B4), y bajarlo "
            "es otro acto que no existe"
        )

    resultado = (
        await conexion.execute(
            _SUBIR_TECHO,
            {
                "techo": nuevo,
                "ahora": momento,
                "agencia_id": inquilino.agencia_id,
                "cliente_id": cliente_id,
            },
        )
    ).one()
    await apuntar(
        conexion,
        Inquilino.desde_usuario(agencia_id=inquilino.agencia_id, cliente_id=cliente_id),
        actor=actor,
        accion=ACCION_TECHO_SUBIDO,
        recurso=RECURSO_TECHO_CLIENTE,
        detalle={"techo_anterior_usd": str(actual), "techo_usd": str(nuevo)},
    )
    return Decimal(resultado.techo_usd_mes)


# ==========================================================================
# El precio del mensaje — y su RAMA DEL NO
# ==========================================================================
async def costo_de_mensaje(conexion, pais: str, tipo: str, ahora: datetime) -> Costo:
    """Cuanto cuesta entregar un mensaje de `tipo` a un destinatario de `pais`.

    ==RAMA DEL NO (RF-16, CS-01): si el catalogo vigente tiene mas de 30 dias, o
    si no hay fila para ese pais y ese tipo, se usa el PRECIO MAS ALTO CONOCIDO y
    se avisa.== Nunca se deja de contar por no tener el dato, porque es ruta de
    dinero.

    # WHY (el mas alto, y no el ultimo ni la media): sobrecontar corta antes de
    # tiempo, y eso cuesta una conversacion; subcontar deja pasar gasto por
    # encima del techo, y eso cuesta dinero real — que es exactamente lo que
    # RF-16 existe para impedir. Ante la duda, el error va en la direccion barata.
    """
    _tipo_valido(tipo)
    cargada_en = (await conexion.execute(_CATALOGO_CARGADO_EN)).one().cargada_en
    if cargada_en is None:
        raise CatalogoDePreciosVacio(
            "la tabla de precios de mensajeria esta vacia: no se conoce ningun precio, "
            "asi que tampoco hay «el mas alto conocido» que usar de respaldo. Se falla "
            "en voz alta en vez de contar cero, que es contar mal y encima en silencio"
        )

    if ahora - cargada_en > timedelta(days=DIAS_DE_VIGENCIA_DEL_CATALOGO):
        return Costo(
            precio=await _precio_mas_alto(conexion, pais, tipo),
            avisos=_avisos_de_precio(
                "precios.caducados",
                {
                    "cargada_en": cargada_en.isoformat(),
                    "dias_de_vigencia": DIAS_DE_VIGENCIA_DEL_CATALOGO,
                    "pais": pais,
                    "tipo": tipo,
                },
            ),
        )

    fila = (
        await conexion.execute(
            _PRECIO_VIGENTE, {"pais": pais, "tipo": tipo, "hoy": ahora.date()}
        )
    ).one_or_none()
    if fila is None:
        return Costo(
            precio=await _precio_mas_alto(conexion, pais, tipo),
            avisos=_avisos_de_precio("precios.sin_pais", {"pais": pais, "tipo": tipo}),
        )
    return Costo(
        precio=Precio(
            pais=fila.pais,
            tipo=fila.tipo,
            precio_usd=Decimal(fila.precio_usd),
            vigente_desde=fila.vigente_desde,
            fuente=fila.fuente,
            es_respaldo=False,
        ),
        avisos=(),
    )


def cargar_precios(
    conexion,
    *,
    entradas: Sequence[Mapping[str, Any]],
    fuente: str,
    cargada_en: datetime,
) -> int:
    """Reemplaza el catalogo de precios ENTERO. Lo corre el rol migrador.

    # WHY (reemplaza y no fusiona): «el catalogo vigente» tiene que ser UNA cosa.
    # Una carga parcial dejaria filas viejas conviviendo con las nuevas y la
    # pregunta «¿cual es el precio de hoy?» pasaria a tener dos respuestas.
    #
    # ==WHY (se valida TODO antes de borrar NADA): un error de ENTRADA no puede
    # matar el DESTINO.== Si se borrara primero y una entrada resultara invalida a
    # mitad, el catalogo quedaria vacio o a medias — y con el catalogo vacio
    # `costo_de_mensaje` levanta, o sea que un archivo mal escrito apagaria el
    # conteo de dinero de todo el producto.
    """
    if not entradas:
        raise ValueError(
            "no se admite cargar un catalogo de precios VACIO: dejaria la tabla sin "
            "ninguna fila, y sin ninguna fila no hay «precio mas alto conocido» — la "
            "rama del no de RF-16 se quedaria sin respaldo"
        )
    validadas = [_entrada_valida(entrada) for entrada in entradas]

    conexion.execute(_VACIAR_PRECIOS)
    for entrada in validadas:
        conexion.execute(
            _INSERTAR_PRECIO, entrada | {"fuente": fuente, "cargada_en": cargada_en}
        )
    return len(validadas)


def leer_archivo_de_precios(ruta: Path) -> ArchivoDePrecios:
    """Lee el catalogo versionado del repositorio, con su procedencia.

    # WHY (`verificado` es obligatorio y no tiene valor por defecto): la pregunta
    # «¿estas cifras estan contrastadas contra la fuente primaria?» tiene que
    # contestarla quien escribe el archivo. Con un valor por defecto la
    # contestaria este codigo, y la contestaria en la direccion comoda — que es
    # como una cifra sin fuente acaba pareciendo verificada (P-03).
    """
    datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    for clave in ("fecha", "fuente", "verificado", "precios"):
        if clave not in datos:
            raise ValueError(
                f"{ruta} no declara {clave!r}. Un catalogo de precios sin fecha, sin "
                "fuente, sin decir si esta verificado o sin entradas no se puede "
                "auditar ni caducar, y gobierna dinero"
            )
    return ArchivoDePrecios(
        fecha=str(datos["fecha"]),
        fuente=str(datos["fuente"]),
        verificado=bool(datos["verificado"]),
        entradas=tuple(datos["precios"]),
    )


# ==========================================================================
# Piezas internas
# ==========================================================================
def _monto_valido(monto: Decimal) -> Decimal:
    valor = Decimal(monto)
    if valor <= 0:
        raise MontoNoAdmitido(
            f"monto {valor}: un gasto es estrictamente positivo. Un cero no mueve el "
            "techo, y un negativo lo BAJARIA — o sea, seria una forma de devolver "
            "saldo que nadie aprueba y que no deja rastro de quien la hizo"
        )
    return valor


def _concepto_valido(concepto: str) -> str:
    if concepto not in CONCEPTOS:
        raise ConceptoNoAdmitido(
            f"concepto {concepto!r} fuera de {list(CONCEPTOS)}. Es una allowlist: lo "
            "que no esta declarado no entra en la cuenta del dinero, en vez de entrar "
            "con una etiqueta que nadie sabe sumar"
        )
    return concepto


def _tipo_valido(tipo: str) -> str:
    if tipo not in TIPOS_DE_MENSAJE:
        raise ValueError(
            f"tipo de mensaje {tipo!r} fuera de {list(TIPOS_DE_MENSAJE)}. La "
            "tarificacion por CONVERSACION esta deprecada desde el 1-jul-2025: hoy es "
            "por mensaje/plantilla"
        )
    return tipo


def _cliente_del_consumo(inquilino: Inquilino, cliente_id: UUID | None) -> UUID:
    """De quien es el gasto. En alcance agencia hay que nombrarlo."""
    if cliente_id is not None:
        return cliente_id
    if inquilino.cliente_id == CENTINELA_SIN_CLIENTE:
        raise ConsumoSinCliente(
            "esta sesion es de alcance agencia y no se nombro el cliente del consumo. "
            "El centinela no es un cliente: cargar el gasto contra el seria gasto sin "
            "dueno, y ninguna factura lo recogeria"
        )
    return inquilino.cliente_id


async def _leer_techo(
    conexion, inquilino: Inquilino, titular: Titular, dueno: UUID, *, bloquear: bool
) -> tuple[Decimal, Decimal]:
    """Techo y umbral vigentes del titular que paga.

    Con `bloquear`, la fila queda tomada hasta el final de la transaccion: es el
    cerrojo sobre el que se serializan los consumos concurrentes.

    # WHY (el techo de la agencia tambien se bloquea): es UNO y compartido por
    # todas sus altas de desarrollo. Sin cerrojo, dos altas gastando a la vez lo
    # superarian igual que dos peticiones del mismo cliente.
    """
    if titular is Titular.AGENCIA:
        consulta = _BLOQUEAR_TECHO_DE_LA_AGENCIA
        parametros: dict[str, Any] = {"agencia_id": inquilino.agencia_id}
    else:
        consulta = _BLOQUEAR_TECHO_DEL_CLIENTE
        parametros = {"agencia_id": inquilino.agencia_id, "cliente_id": dueno}

    # `FOR UPDATE` exige privilegio de escritura y ademas serializa; para una
    # comprobacion previa no hace falta ninguna de las dos cosas.
    sentencia = text(consulta + _CERROJO if bloquear else consulta)

    fila = (await conexion.execute(sentencia, parametros)).one_or_none()
    if fila is None:
        raise SinTechoAlcanzable(
            f"no hay fila de techo alcanzable para el titular {titular.value!r} desde "
            f"esta sesion (alcance {inquilino.alcance}). Sin techo que bloquear no se "
            "consume: «no encontre el limite» no significa «no hay limite»"
        )
    return Decimal(fila.techo_usd_mes), Decimal(fila.umbral_de_alarma)


async def _gasto_del_mes(
    conexion,
    inquilino: Inquilino,
    titular: Titular,
    dueno: UUID,
    desde: datetime,
    hasta: datetime,
) -> Decimal:
    comun = {"titular": titular.value, "desde": desde, "hasta": hasta}
    if titular is Titular.AGENCIA:
        # El techo de la agencia suma lo de TODOS sus clientes de desarrollo: es
        # uno solo, no uno por alta.
        sentencia = _GASTO_DE_LA_AGENCIA
        parametros = comun | {"agencia_id": inquilino.agencia_id}
    else:
        sentencia = _GASTO_DEL_CLIENTE
        parametros = comun | {"agencia_id": inquilino.agencia_id, "cliente_id": dueno}
    return Decimal((await conexion.execute(sentencia, parametros)).one().gastado)


def _avisos(
    motivo: str, *, gastado_usd: Decimal, techo_usd: Decimal, titular: Titular
) -> tuple[Aviso, ...]:
    """Un aviso por destinatario. RF-16: «al operador Y al cliente»."""
    detalle = {
        "gastado_usd": str(gastado_usd),
        "techo_usd": str(techo_usd),
        "titular": titular.value,
    }
    return tuple(
        Aviso(motivo=motivo, destinatario=destinatario, detalle=detalle)
        for destinatario in DESTINATARIOS_DEL_TECHO
    )


def _avisos_de_precio(motivo: str, detalle: Mapping[str, Any]) -> tuple[Aviso, ...]:
    """Los avisos de la rama del no van al OPERADOR y solo a el.

    # WHY: que el catalogo de precios este caducado o no tenga un pais es un
    # defecto de operacion nuestro, no una noticia del cliente. Mandarselo seria
    # ruido que el no puede arreglar — y de paso le contaria como se tarifa por
    # dentro.
    """
    return (Aviso(motivo=motivo, destinatario="operador", detalle=dict(detalle)),)


async def _alarma_si_toca(
    conexion,
    inquilino: Inquilino,
    *,
    titular: Titular,
    dueno: UUID,
    gastado_usd: Decimal,
    techo_usd: Decimal,
    umbral: Decimal,
    desde: datetime,
) -> tuple[Aviso, ...]:
    """La alarma ANTES del techo: una sola vez por mes y por techo vigente.

    # WHY (la idempotencia se apoya en la BITACORA y no en «el valor anterior
    # cruzaba o no»): la bitacora es de solo insercion y sobrevive a los
    # reintentos, y ademas es consultable — «¿avisamos a este cliente este mes?»
    # se contesta mirando el rastro, no reconstruyendo el estado. Y como todo
    # esto ocurre con el cerrojo del techo tomado, dos consumos simultaneos que
    # crucen el umbral a la vez no producen dos alarmas.
    #
    # ==WHY (la llave incluye el TECHO VIGENTE): sin el, subir el techo dejaria al
    # cliente sin alarma para el resto del mes== — justo cuando mas puede gastar,
    # porque acaba de recibir mas margen. Con el techo en la llave la alarma se
    # re-arma sola en cuanto el limite cambia, y sigue siendo una sola por mes
    # mientras el limite no cambie.
    """
    if gastado_usd < techo_usd * umbral:
        return ()

    recurso = RECURSO_TECHO_AGENCIA if titular is Titular.AGENCIA else RECURSO_TECHO_CLIENTE
    mes = desde.date().isoformat()
    ya = (
        await conexion.execute(
            _ALARMA_YA_EMITIDA,
            {
                "agencia_id": inquilino.agencia_id,
                "accion": ACCION_ALARMA,
                "recurso": recurso,
                "mes": mes,
                "techo": str(techo_usd),
            },
        )
    ).one_or_none()
    if ya is not None:
        return ()

    await apuntar(
        conexion,
        Inquilino.desde_usuario(agencia_id=inquilino.agencia_id, cliente_id=dueno),
        actor="sistema:techo",
        accion=ACCION_ALARMA,
        recurso=recurso,
        detalle={
            "mes": mes,
            "gastado_usd": str(gastado_usd),
            "techo_usd": str(techo_usd),
            "umbral": str(umbral),
            "titular": titular.value,
        },
    )
    return _avisos(
        ACCION_ALARMA, gastado_usd=gastado_usd, techo_usd=techo_usd, titular=titular
    )


async def _precio_mas_alto(conexion, pais: str, tipo: str) -> Precio:
    """El respaldo de la rama del no: el precio mas alto que se conoce."""
    fila = (await conexion.execute(_PRECIO_MAS_ALTO)).one_or_none()
    if fila is None:
        raise CatalogoDePreciosVacio(
            f"no hay precio para ({pais}, {tipo}) y tampoco hay ninguna fila de la que "
            "sacar el precio mas alto conocido: el catalogo esta vacio"
        )
    return Precio(
        pais=pais,
        tipo=tipo,
        precio_usd=Decimal(fila.precio_usd),
        vigente_desde=fila.vigente_desde,
        fuente=fila.fuente,
        es_respaldo=True,
    )


def _entrada_valida(entrada: Mapping[str, Any]) -> dict[str, Any]:
    """Una fila del catalogo, comprobada antes de que toque la base."""
    faltan = sorted({"pais", "tipo", "precio_usd", "vigente_desde"} - set(entrada))
    if faltan:
        raise ValueError(f"entrada de precio incompleta, faltan {faltan}: {dict(entrada)}")

    pais = str(entrada["pais"])
    if len(pais) != 2 or not pais.isascii() or not pais.isalpha() or pais != pais.upper():
        raise ValueError(
            f"pais {pais!r}: se espera un codigo ISO-3166-1 alfa-2 en mayusculas (dos "
            "letras). El catalogo se consulta por igualdad exacta, asi que un codigo de "
            "tres letras no coincide con nada y manda la consulta al respaldo"
        )
    _tipo_valido(str(entrada["tipo"]))

    precio = Decimal(str(entrada["precio_usd"]))
    if precio < 0:
        raise ValueError(f"precio {precio} para ({pais}, {entrada['tipo']}): es negativo")

    vigente = entrada["vigente_desde"]
    if isinstance(vigente, datetime):
        vigente = vigente.date()
    elif isinstance(vigente, str):
        vigente = date.fromisoformat(vigente)
    if not isinstance(vigente, date):
        raise ValueError(f"vigente_desde {entrada['vigente_desde']!r}: no es una fecha")

    return {
        "pais": pais,
        "tipo": str(entrada["tipo"]),
        "precio_usd": precio,
        "vigente_desde": vigente,
    }
