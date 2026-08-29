"""T-025 (RF-52) — la salud de la cola, y la alarma que suena por ANTIGUEDAD.

RF-52 pide tres numeros —profundidad, antiguedad del trabajo mas viejo y tasa de
fallo— y una alarma que suene por ANTIGUEDAD, no por «el proceso esta vivo».

# WHY (R-03, y por que la diferencia lo es todo): el worker es el punto unico de
# fallo del producto. Cuando muere, la cola crece Y NO PASA NADA MAS: no hay
# excepcion, no hay error, no hay peticion que devuelva 500. Todo esta «bien»
# salvo que nada se genera. Una alarma de latido —«el proceso responde»— tiene dos
# fallos que la hacen inutil justo aqui:
#
#   1. **Falso verde.** Un worker vivo que se quedo pillado en una llamada de red
#      sin plazo, o que consume una cola que no es esta, o que arranco con el
#      inquilino equivocado, LATE perfectamente mientras la cola se hunde.
#   2. **Falsa alarma.** Un worker que se reinicia porque no hay nada que hacer
#      —o durante un despliegue— deja de latir sin que exista ningun problema.
#
# La antiguedad del trabajo mas viejo DISPONIBLE no tiene ninguno de los dos: si
# hay algo esperando desde hace mas de lo tolerable, el producto esta roto, y da
# igual cuantos procesos esten vivos y cuantos latidos hayan llegado.
#
# **La pregunta de control de este modulo:** ¿que resultado lo pondria en rojo?
# Respuesta: UN trabajo, uno solo, esperando desde hace mucho. Por eso la sonda
# `test_la_alarma_suena_por_antiguedad_aunque_la_cola_sea_minuscula` usa
# profundidad 1 —por debajo de cualquier umbral de profundidad— y exige que la
# alarma suene igual. Si alguien sustituyera la antiguedad por la profundidad, o
# por un latido, esa sonda se pondria roja.
#
# WHY (aqui no hay ningun concepto de proceso): este modulo no sabe que es un
# `pid`, ni un latido, ni un despliegue. Solo lee la cola. Lo que no sabe no lo
# puede confundir con salud.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import text

from worker.cola import PLAZO_DE_ABANDONO

#: Cuanto puede esperar el trabajo mas viejo DISPONIBLE antes de que suene la
#: alarma. No es «cuanto tarda un trabajo»: es cuanto puede llevar uno esperando
#: a que ALGUIEN lo coja.
ANTIGUEDAD_TOLERADA = timedelta(minutes=5)

#: Ventana sobre la que se mide la tasa de fallo, y el umbral a partir del cual
#: avisa. Una tasa sobre «toda la historia» se diluye y no avisa nunca.
VENTANA_DE_TASA = timedelta(hours=1)
TASA_DE_FALLO_TOLERADA = 0.20

#: Profundidad a partir de la cual se avisa. Es un aviso SECUNDARIO: la senal que
#: importa es la antiguedad. Una cola profunda que se vacia deprisa esta sana; una
#: cola de un solo trabajo parado desde ayer, no.
PROFUNDIDAD_TOLERADA = 1000


class Alarma(StrEnum):
    """Lo que puede estar mal. La primera es la de R-03."""

    COLA_ESTANCADA = "cola estancada: el trabajo mas viejo lleva demasiado esperando"
    TRABAJOS_ABANDONADOS = "hay trabajos reclamados que nadie termino"
    TASA_DE_FALLO_ALTA = "demasiados trabajos acaban en fallido"
    COLA_PROFUNDA = "la cola acumula mas trabajos de los tolerados"


@dataclass(frozen=True, slots=True)
class Salud:
    """Los numeros de RF-52, tal y como estan en la base ahora mismo."""

    profundidad: int
    antiguedad_del_mas_viejo: timedelta | None
    en_curso: int
    abandonados: int
    hechos_en_la_ventana: int
    fallidos_en_la_ventana: int
    fallidos_visibles: int

    @property
    def tasa_de_fallo(self) -> float:
        """Fallidos sobre terminados en la ventana. Sin terminados, es 0.0.

        # WHY (cero y no `None`): una ventana sin trabajos no es una ventana con
        # problemas. Devolver `None` obligaria a cada consumidor a decidir que hacer
        # con el hueco, y alguno decidiria mal.
        """
        terminados = self.hechos_en_la_ventana + self.fallidos_en_la_ventana
        return 0.0 if terminados == 0 else self.fallidos_en_la_ventana / terminados


_MEDIR = text(
    """
    SELECT
      count(*) FILTER (WHERE estado = 'pendiente')                     AS profundidad,
      min(disponible_en) FILTER (
          WHERE estado = 'pendiente' AND disponible_en <= :ahora)      AS mas_viejo,
      count(*) FILTER (WHERE estado = 'en_curso')                      AS en_curso,
      count(*) FILTER (
          WHERE estado = 'en_curso' AND actualizado_en <= :limite)     AS abandonados,
      count(*) FILTER (
          WHERE estado = 'hecho'   AND terminado_en >= :desde)         AS hechos,
      count(*) FILTER (
          WHERE estado = 'fallido' AND terminado_en >= :desde)         AS fallidos_ventana,
      count(*) FILTER (WHERE estado = 'fallido')                       AS fallidos_visibles
    FROM trabajos
    """
)


async def medir(
    conexion,
    *,
    ahora: datetime,
    ventana: timedelta = VENTANA_DE_TASA,
    plazo_de_abandono: timedelta = PLAZO_DE_ABANDONO,
) -> Salud:
    """Lee la salud de la cola ALCANZABLE en esta sesion.

    # WHY (una sola consulta): los siete numeros salen del mismo recorrido. Con
    # siete consultas, cada una veria un instante distinto y la tasa de fallo podria
    # salir de dividir dos numeros que nunca coexistieron.
    """
    fila = (
        await conexion.execute(
            _MEDIR,
            {"ahora": ahora, "limite": ahora - plazo_de_abandono, "desde": ahora - ventana},
        )
    ).one()
    mas_viejo = fila.mas_viejo
    return Salud(
        profundidad=fila.profundidad,
        antiguedad_del_mas_viejo=None if mas_viejo is None else ahora - mas_viejo,
        en_curso=fila.en_curso,
        abandonados=fila.abandonados,
        hechos_en_la_ventana=fila.hechos,
        fallidos_en_la_ventana=fila.fallidos_ventana,
        fallidos_visibles=fila.fallidos_visibles,
    )


def alarmas(
    salud: Salud,
    *,
    antiguedad_tolerada: timedelta = ANTIGUEDAD_TOLERADA,
    tasa_tolerada: float = TASA_DE_FALLO_TOLERADA,
    profundidad_tolerada: int = PROFUNDIDAD_TOLERADA,
) -> list[Alarma]:
    """Que esta mal, a partir de los numeros. No pregunta por ningun proceso.

    # WHY (la primera importa mucho): `COLA_ESTANCADA` se evalua sobre la
    # ANTIGUEDAD y nada mas. No mira la profundidad, no mira si hay workers, no mira
    # si alguien latio. Un solo trabajo viejo la dispara.
    """
    encontradas: list[Alarma] = []
    antiguedad = salud.antiguedad_del_mas_viejo
    if antiguedad is not None and antiguedad > antiguedad_tolerada:
        encontradas.append(Alarma.COLA_ESTANCADA)
    if salud.abandonados:
        encontradas.append(Alarma.TRABAJOS_ABANDONADOS)
    if salud.tasa_de_fallo > tasa_tolerada:
        encontradas.append(Alarma.TASA_DE_FALLO_ALTA)
    if salud.profundidad > profundidad_tolerada:
        encontradas.append(Alarma.COLA_PROFUNDA)
    return encontradas


def informe(salud: Salud, alarmas_encontradas: list[Alarma]) -> dict[str, object]:
    """Los tres numeros de RF-52 y sus alarmas, listos para exponer.

    # WHY: no lleva ningun dato del inquilino ni la carga de los trabajos — solo
    # recuentos. Un informe de salud que arrastrara la carga de un trabajo seria una
    # via de salida de datos de cliente por la puerta de la observabilidad.
    """
    antiguedad = salud.antiguedad_del_mas_viejo
    return {
        "profundidad": salud.profundidad,
        "antiguedad_del_mas_viejo_s": None if antiguedad is None else antiguedad.total_seconds(),
        "tasa_de_fallo": round(salud.tasa_de_fallo, 4),
        "en_curso": salud.en_curso,
        "abandonados": salud.abandonados,
        "fallidos_visibles": salud.fallidos_visibles,
        "alarmas": [a.name for a in alarmas_encontradas],
    }
