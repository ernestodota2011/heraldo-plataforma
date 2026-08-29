"""Entorno de Alembic.

# WHY: el DSN se lee del entorno y, si no esta, se ABORTA. No hay valor por
# defecto: un valor por defecto es la forma mas comoda de migrar la base
# equivocada, y aqui la base equivocada puede ser la de un inquilino.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

#: DSN del rol MIGRADOR (dueño de las tablas), nunca el de la aplicacion.
VARIABLE_DSN = "HERALDO_DATABASE_URL_ADMIN"

# Escritas a mano: no hay metadata de la que autogenerar (D-10).
target_metadata = None


class DsnDeMigracionNoDeclarado(RuntimeError):
    """Sin DSN no se migra nada: se falla, no se adivina."""


def _dsn() -> str:
    dsn = os.environ.get(VARIABLE_DSN) or context.config.get_main_option("sqlalchemy.url")
    if not dsn:
        raise DsnDeMigracionNoDeclarado(
            f"falta {VARIABLE_DSN}: el DSN del rol migrador se declara por entorno"
        )
    return dsn


def migrar_sin_conexion() -> None:
    context.configure(url=_dsn(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def migrar_con_conexion() -> None:
    motor = create_engine(_dsn(), future=True)
    with motor.connect() as conexion:
        context.configure(connection=conexion)
        with context.begin_transaction():
            context.run_migrations()
    motor.dispose()


if context.is_offline_mode():
    migrar_sin_conexion()
else:
    migrar_con_conexion()
