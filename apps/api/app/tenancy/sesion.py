"""La UNICA forma de abrir un acceso a datos en Heraldo.

# WHY: plan §4 — «Toda sesion de base de datos nace de UNA dependencia que
# declara `(agencia, cliente, alcance)`. No existe una segunda forma de abrir
# conexion». Este modulo es esa dependencia. Si manana aparece un segundo camino,
# el aislamiento deja de ser estructural y vuelve a ser disciplina.
#
# WHY: plan §3.1 punto 2 — las tres variables se declaran con `SET LOCAL` dentro
# de UNA transaccion. El `LOCAL` es lo que lo hace seguro con un pool en modo
# transaccion: el valor muere con la transaccion y no se filtra a la siguiente
# peticion que reutilice esa conexion fisica. La casa ya se mordio con esto en
# Supabase (`feedback_supavisor_tenant`).
#
# WHY: se usa `set_config(nombre, valor, true)` y no `SET LOCAL nombre = valor`.
# `SET` no acepta parametros ligados: obligaria a interpolar el uuid en el texto
# de la sentencia. `set_config` los acepta, asi que el valor viaja como
# PARAMETRO y no hay superficie de inyeccion en el camino de aislamiento.
#
# WHY: `current_setting('x')` en las politicas — la forma que LANZA ERROR — y no
# `current_setting('x', true)`, que devolveria nulo (plan §3.1 punto 6, RF-03).
# Una consulta sin inquilino declarado ABORTA ruidosamente: cero filas
# silenciosas se confunden con «no hay datos».
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.tenancy.inquilino import Inquilino

#: DSN del rol de APLICACION — el que no es dueño ni superusuario. Se declara por
#: entorno: en el repositorio no vive ninguna credencial.
VARIABLE_DE_ENTORNO_DSN = "HERALDO_DATABASE_URL"

#: Configuracion de pool de PRODUCCION. Las pruebas de aislamiento corren contra
#: ESTA configuracion, no contra una conexion directa (mitigacion de R-01).
TAMANO_POOL = 10
DESBORDE_MAXIMO = 5
RECICLADO_SEGUNDOS = 1800

# Las tres, en una sola ida y vuelta. `true` = is_local, o sea `SET LOCAL`.
_DECLARAR_INQUILINO = text(
    "SELECT set_config('app.agencia_id', :agencia_id, true),"
    "       set_config('app.cliente_id', :cliente_id, true),"
    "       set_config('app.alcance',    :alcance,    true)"
)


class DsnNoDeclarado(RuntimeError):
    """No hay DSN de aplicacion en el entorno: se falla, no se adivina uno."""


def dsn_de_aplicacion() -> str:
    dsn = os.environ.get(VARIABLE_DE_ENTORNO_DSN)
    if not dsn:
        raise DsnNoDeclarado(
            f"falta {VARIABLE_DE_ENTORNO_DSN}: el DSN del rol de aplicacion se "
            "declara por entorno, nunca en el repositorio"
        )
    return dsn


def crear_motor(dsn: str | None = None, *, tamano_pool: int | None = None) -> AsyncEngine:
    """Motor con el pool en su configuracion de produccion.

    `tamano_pool` solo se toca para el caso PEOR del pool en las pruebas
    (una sola conexion fisica, reutilizada por inquilinos distintos).
    """
    return create_async_engine(
        dsn or dsn_de_aplicacion(),
        pool_size=TAMANO_POOL if tamano_pool is None else tamano_pool,
        max_overflow=DESBORDE_MAXIMO if tamano_pool is None else 0,
        pool_pre_ping=True,
        pool_recycle=RECICLADO_SEGUNDOS,
    )


@asynccontextmanager
async def sesion_de_inquilino(
    motor: AsyncEngine, inquilino: Inquilino
) -> AsyncIterator[AsyncConnection]:
    """Abre UNA transaccion con las tres variables ya declaradas.

    Al salir del bloque la transaccion termina y con ella mueren las tres
    variables. La conexion vuelve al pool sin rastro del inquilino anterior.
    """
    async with motor.begin() as conexion:
        await conexion.execute(
            _DECLARAR_INQUILINO,
            {
                "agencia_id": str(inquilino.agencia_id),
                "cliente_id": str(inquilino.cliente_id),
                "alcance": str(inquilino.alcance),
            },
        )
        yield conexion
