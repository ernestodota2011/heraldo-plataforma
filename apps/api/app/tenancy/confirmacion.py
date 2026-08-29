"""Nada irreversible se ejecuta sin que alguien haya visto QUE y CUANTO (T-025·bis).

RNF-06: «Toda operacion destructiva o irreversible sobre datos de un cliente
exige confirmacion explicita».

# WHY: «¿seguro?» NO cumple RNF-06. Un dialogo que pregunta si estas seguro solo
# comprueba que sabes pulsar el boton otra vez; no te dice que estas a punto de
# destruir. La confirmacion de este modulo se DERIVA del inventario: para poder
# confirmar hay que devolver una huella que solo existe si antes se ha calculado
# —y por tanto mostrado— **el objeto y el recuento por tabla**. No hay forma de
# confirmar a ciegas porque no hay ningun valor constante que sirva de «si».
#
# WHY (protege ademas de la carrera): la huella cubre los recuentos. Si entre la
# vista previa y la confirmacion el cliente ha pasado de 412 conversaciones a
# 900, la huella cambia y la confirmacion se rechaza. Confirmaste 412; no se
# destruyen 900 en tu nombre.
#
# WHY (FAIL-CLOSED, la regla que manda): si no se puede CONTAR, no se confirma.
# Ni se destruye «por si acaso», ni se ensena un inventario incompleto que el
# operador leeria como completo. Todos los caminos por los que el recuento puede
# quedar a medias —una tabla que no se deja contar, un universo vacio, un cliente
# que no existe dentro del alcance de quien pide— terminan en la MISMA excepcion,
# `NoSePuedeContar`, y esa excepcion nunca produce un inventario. Un inventario
# solo existe si esta completo: no hay un `Recuento` con `filas = None`.
#
# WHY (el universo se DERIVA del catalogo, no se escribe a mano): es la leccion
# que este repositorio ya aprendio tres veces (P-10, P-11, P-12) y la que L-03
# convirtio en critica — una lista de tablas escrita a mano se queda corta el dia
# que una migracion anade la cuarta, y el inventario diria «se destruyen 3 filas»
# mientras la cascada se lleva 3.000. Aqui las tablas de la clase *de cliente*
# salen de `information_schema`, asi que una tabla futura entra SOLA.
#
# WHY (por que el registro de clientes SI se nombra): `TABLA_REGISTRO_DE_CLIENTES`
# es una constante, no una lista. Es la forma del modelo —igual que
# `COLUMNA_AGENCIA` y `COLUMNA_CLIENTE` en `politicas.py`—, no un inventario que
# haya que mantener al dia: es UNA tabla, es singular, y si dejara de existir el
# inventario falla cerrado en vez de contar de menos.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.tenancy.politicas import COLUMNA_AGENCIA, COLUMNA_CLIENTE, valida_identificador

#: La tabla donde vive la ficha del cliente. Ver el WHY de la cabecera.
TABLA_REGISTRO_DE_CLIENTES = "clientes"
#: Su columna de identidad (la que la politica de RLS expone al propio cliente).
COLUMNA_IDENTIDAD_DEL_CLIENTE = "id"
#: La columna con el nombre legible. Sin ella el inventario no puede NOMBRAR el
#: objeto, y RNF-06 exige nombrarlo: por eso su ausencia tambien falla cerrado.
COLUMNA_NOMBRE_DEL_CLIENTE = "nombre"

#: Nombre de la operacion. Se mete en la huella: una confirmacion emitida para
#: una operacion no sirve para otra aunque los recuentos coincidan.
OPERACION_BAJA_DE_CLIENTE = "baja-de-cliente"

#: Longitud de la huella en caracteres hexadecimales. 16 = 64 bits: de sobra para
#: que no se acierte por casualidad, y corta para que quepa en un aviso legible.
LONGITUD_DE_HUELLA = 16


class NoSePuedeContar(RuntimeError):
    """No se pudo inventariar. Fail-closed: sin inventario no hay confirmacion."""


class ConfirmacionRequerida(Exception):
    """Falta la confirmacion. Lleva el inventario para que quien pregunta lo VEA."""

    def __init__(self, inventario: Inventario) -> None:
        super().__init__(inventario.frase())
        self.inventario = inventario


class ConfirmacionInvalida(Exception):
    """La confirmacion no corresponde a ESTE inventario: no se ejecuta nada."""

    def __init__(self, inventario: Inventario) -> None:
        super().__init__(
            "la confirmacion no corresponde a lo que se va a destruir ahora mismo "
            "(o el recuento cambio desde que la pediste): vuelve a pedirla"
        )
        self.inventario = inventario


@dataclass(frozen=True, slots=True)
class Recuento:
    """Cuantas filas caen de UNA tabla. `filas` es un entero: nunca «no se»."""

    tabla: str
    filas: int


@dataclass(frozen=True, slots=True)
class Inventario:
    """Que se destruye y cuanto. Es lo que RNF-06 exige que se nombre.

    Solo lo construye `inventariar_baja_de_cliente`, y solo cuando ha podido
    contar TODO. Un inventario que existe es un inventario completo.
    """

    operacion: str
    objeto: str
    identificador: str
    recuentos: tuple[Recuento, ...]

    @property
    def total(self) -> int:
        return sum(recuento.filas for recuento in self.recuentos)

    def huella(self) -> str:
        """Deriva la confirmacion del contenido: no hay un «si» constante.

        # WHY: sha256 y no un contador ni la marca de tiempo. Lo que tiene que ser
        # imposible es acertar la confirmacion SIN haber visto el inventario; con
        # un valor predecible, un cliente automatizado la generaria sola y el
        # mecanismo volveria a ser un «¿seguro?».
        """
        canonico = "|".join(
            [
                self.operacion,
                self.identificador,
                *(f"{r.tabla}={r.filas}" for r in self.recuentos),
            ]
        )
        return sha256(canonico.encode("utf-8")).hexdigest()[:LONGITUD_DE_HUELLA]

    def frase(self) -> str:
        """El aviso en lenguaje llano: nombra el objeto y el recuento (RNF-02)."""
        desglose = ", ".join(f"{r.tabla}: {r.filas}" for r in self.recuentos)
        return (
            f"Vas a ejecutar «{self.operacion}» sobre {self.objeto}. "
            f"Se destruyen {self.total} filas en total — {desglose}. "
            "Esto no se puede deshacer."
        )

    def como_dict(self) -> dict:
        """Forma serializable para el cuerpo de la respuesta."""
        return {
            "operacion": self.operacion,
            "objeto": self.objeto,
            "identificador": self.identificador,
            "total": self.total,
            "recuentos": {r.tabla: r.filas for r in self.recuentos},
            "confirmacion": self.huella(),
            "aviso": self.frase(),
        }


#: Columnas del esquema `public`, leidas de `pg_catalog`.
#:
#: # WHY (`pg_catalog` y NO `information_schema`) — MEDIDO, no supuesto: las
#: vistas de `information_schema` estan FILTRADAS POR PRIVILEGIO. Comprobado
#: contra Postgres 16 con el rol de la aplicacion: tras un
#: `REVOKE ALL ON heraldos FROM heraldo_app`, `information_schema.columns` deja
#: de listar `heraldos` mientras `pg_class` la sigue viendo. Derivando de
#: `information_schema`, quitarle un privilegio al rol haria que la tabla
#: DESAPARECIERA del inventario: el aviso diria «se destruyen 1 filas» y la
#: cascada se llevaria las demas. Es decir, el universo se encogeria en silencio
#: en vez de fallar cerrado — justo lo contrario de lo que este modulo promete.
#: Con `pg_catalog` la tabla sigue en el universo, el recuento revienta y el
#: resultado es `NoSePuedeContar`. Registrado como P-16.
_COLUMNAS_DEL_ESQUEMA = text(
    """
    SELECT c.relname AS tabla, a.attname AS columna
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    """
)


async def _columnas_por_tabla(conexion: AsyncConnection) -> dict[str, set[str]]:
    mapa: dict[str, set[str]] = {}
    for fila in (await conexion.execute(_COLUMNAS_DEL_ESQUEMA)).all():
        mapa.setdefault(fila.tabla, set()).add(fila.columna)
    return mapa


def _tablas_de_la_clase_de_cliente(columnas: dict[str, set[str]]) -> tuple[str, ...]:
    """Las que llevan LAS DOS claves de la cascada.

    Es la misma clasificacion por columnas que usa `test_rls_cobertura.py`, y la
    misma razon: la clase de una tabla la determinan las columnas que tiene, no
    la buena voluntad de quien la creo.
    """
    return tuple(
        sorted(
            tabla
            for tabla, suyas in columnas.items()
            if COLUMNA_AGENCIA in suyas and COLUMNA_CLIENTE in suyas
        )
    )


def _registro_de_clientes_existe(columnas: dict[str, set[str]]) -> bool:
    """El registro tiene que existir Y traer sus tres columnas obligadas."""
    return {
        COLUMNA_IDENTIDAD_DEL_CLIENTE,
        COLUMNA_NOMBRE_DEL_CLIENTE,
        COLUMNA_AGENCIA,
    } <= columnas.get(TABLA_REGISTRO_DE_CLIENTES, set())


async def inventariar_baja_de_cliente(
    conexion: AsyncConnection, *, cliente_id: UUID
) -> Inventario:
    """Cuenta lo que se llevaria por delante la baja de un cliente.

    Cuenta **por el mismo camino de sesion que despues destruye**: la conexion
    llega con las tres variables de inquilino ya declaradas, asi que los
    recuentos ya vienen recortados por RLS. Contar con un rol de mas privilegio
    ensenaria un numero que la operacion no puede alcanzar — y contar con uno de
    menos, un numero que se queda corto.

    Levanta `NoSePuedeContar` en cuanto algo impide un recuento COMPLETO.
    """
    try:
        columnas = await _columnas_por_tabla(conexion)
        if not _registro_de_clientes_existe(columnas):
            raise NoSePuedeContar(
                f"no existe la tabla {TABLA_REGISTRO_DE_CLIENTES!r} con sus columnas "
                "obligadas: sin ella no se puede nombrar ni contar lo que se destruye"
            )

        tablas = _tablas_de_la_clase_de_cliente(columnas)
        if not tablas:
            # WHY: un universo vacio produciria el inventario «0 filas», que un
            # operador leeria como «esto no destruye nada» justo antes de que la
            # cascada se lleve todo lo que el catalogo no supo enumerar.
            raise NoSePuedeContar(
                "el catalogo no declara ninguna tabla de la clase de cliente: un "
                "inventario vacio no es un inventario de cero, es un inventario que "
                "no se pudo hacer"
            )

        ficha = (
            await conexion.execute(
                text(
                    f"SELECT {COLUMNA_NOMBRE_DEL_CLIENTE} AS nombre "  # noqa: S608
                    f"FROM {TABLA_REGISTRO_DE_CLIENTES} "
                    f"WHERE {COLUMNA_IDENTIDAD_DEL_CLIENTE} = :cliente"
                ),
                {"cliente": cliente_id},
            )
        ).first()
        if ficha is None:
            # WHY: tambien es fail-closed, y ademas es aislamiento: pedir el
            # inventario del cliente de OTRA agencia entra por aqui, porque RLS
            # deja la fila fuera del alcance de esta sesion. La respuesta es la
            # misma que para un identificador inventado — no se distingue «existe
            # y no es tuyo» de «no existe», que seria un oraculo de enumeracion.
            raise NoSePuedeContar(
                "no hay ningun cliente con ese identificador dentro de tu alcance: "
                "no se puede contar lo que no se ve, y no se destruye a ciegas"
            )

        recuentos: list[Recuento] = [
            Recuento(tabla=TABLA_REGISTRO_DE_CLIENTES, filas=1),
        ]
        for tabla in tablas:
            valida_identificador(tabla)
            filas = (
                await conexion.execute(
                    text(
                        f"SELECT count(*) FROM {tabla} "  # noqa: S608
                        f"WHERE {COLUMNA_CLIENTE} = :cliente"
                    ),
                    {"cliente": cliente_id},
                )
            ).scalar_one()
            recuentos.append(Recuento(tabla=tabla, filas=int(filas)))
    except NoSePuedeContar:
        raise
    except Exception as fallo:  # noqa: BLE001 - cualquier fallo = no se cuenta
        # WHY: ancho a proposito, y es lo contrario de tragarse el error. Aqui
        # «no se» y «no» son la misma respuesta: no se confirma. Una denylist de
        # excepciones esperadas dejaria pasar la que faltara como un 500 — y un
        # 500 en este camino es una destruccion que nadie inventario.
        raise NoSePuedeContar(
            f"el recuento no pudo completarse ({type(fallo).__name__}): "
            "sin inventario completo no hay confirmacion posible"
        ) from fallo

    return Inventario(
        operacion=OPERACION_BAJA_DE_CLIENTE,
        objeto=f"el cliente «{ficha.nombre}»",
        identificador=str(cliente_id),
        recuentos=tuple(recuentos),
    )


def exigir_confirmacion(inventario: Inventario, confirmacion: str | None) -> None:
    """La compuerta. Devuelve `None` solo si la confirmacion es la de ESTE inventario.

    No acepta booleanos, ni «si», ni «true»: el unico valor valido es la huella,
    que no se puede escribir sin haber recibido antes el recuento.
    """
    if confirmacion is None or not confirmacion.strip():
        raise ConfirmacionRequerida(inventario)
    if confirmacion.strip().lower() != inventario.huella():
        raise ConfirmacionInvalida(inventario)
