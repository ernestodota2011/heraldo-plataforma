"""T-023 (RNF-07) — el paso de un solo tiro que un despliegue corre antes de la API.

Hace dos cosas, en este orden, y las dos con el rol MIGRADOR (dueno de las
tablas, nunca el de la aplicacion — plan Sec.3.1 punto 1):

1. `alembic upgrade head` — igual que hace `apps/api/tests/conftest.py`. Deja el
   esquema, las politicas RLS y el rol `heraldo_app` en su estado vigente. El
   rol nace `NOLOGIN` (asi lo crea la migracion 0001): sin login no hay forma de
   que la API se conecte con el.
2. `ALTER ROLE heraldo_app LOGIN PASSWORD '<valor de HERALDO_APP_DB_PASSWORD>'` —
   el paso que la suite hace por su cuenta en cada corrida (con una clave que
   genera y mata) y que en un despliegue real hace ESTE script, con la clave
   que declaro quien despliega. Es idempotente: correrlo dos veces con la
   MISMA clave no cambia nada observable; con una clave DISTINTA, rota la
   contrasena del rol de aplicacion sin tocar ninguna fila.

WHY (por que no vive esto en una migracion de Alembic): una migracion queda
CONGELADA para siempre (ver el WHY de la revision 0001) y una contrasena no es
historia versionable — es un secreto que cambia por su cuenta. Ponerla en una
migracion la dejaria en texto plano en el historial de Git para siempre.

WHY (por que no hay valor por defecto para la clave): un valor por defecto
comodo para "probar" es exactamente la clave que sobrevive a produccion. Se
declara por entorno o el script se niega a correr — el mismo patron que ya usa
todo el repositorio (`app/main.py`, `app/tenancy/sesion.py`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.tenancy.rol import ROL_APLICACION  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
INI_ALEMBIC = RAIZ / "apps" / "api" / "migrations" / "alembic.ini"

VARIABLE_DSN_ADMIN = "HERALDO_DATABASE_URL_ADMIN"
#: NOMBRE de la variable de entorno que lleva la clave -- no la clave misma.
#: WHY (noqa S105): el analizador estatico ve un literal que contiene
#: "PASSWORD" asignado a una constante y lo marca como posible contrasena
#: hardcodeada. Es un falso positivo verificado: el valor de esta linea es
#: el NOMBRE de una variable de entorno ("HERALDO_APP_DB_PASSWORD"), la
#: misma convencion que exige toda la casa (referenciar credenciales por
#: nombre, nunca por valor -- ninguna clave real vive en este archivo ni en
#: el repositorio). `_exigir()` lee el VALOR desde `os.environ` en tiempo de
#: ejecucion; aqui no hay ningun secreto que rotar.
VARIABLE_PASSWORD = "HERALDO_APP_DB_PASSWORD"  # noqa: S105


class VariableNoDeclarada(RuntimeError):
    """Sin la variable no se corre nada: se falla, no se adivina un valor."""


def _exigir(variable: str) -> str:
    valor = os.environ.get(variable)
    if not valor:
        raise VariableNoDeclarada(f"falta {variable}: se declara por entorno, nunca aqui")
    return valor


def main() -> None:
    dsn_admin = _exigir(VARIABLE_DSN_ADMIN)
    password = _exigir(VARIABLE_PASSWORD)
    if "'" in password:
        # ALTER ROLE no admite parametros ligados (mismo WHY que conftest.py):
        # la clave se interpola literal, asi que una comilla en la clave
        # romperia la sentencia por una razon distinta de la que queremos medir.
        raise ValueError("la clave no puede contener una comilla simple")

    print(f"[migrar] aplicando migraciones contra {INI_ALEMBIC} ...")
    command.upgrade(Config(str(INI_ALEMBIC)), "head")
    print("[migrar] migraciones aplicadas")

    motor = create_engine(dsn_admin, future=True, isolation_level="AUTOCOMMIT")
    try:
        with motor.connect() as conexion:
            conexion.execute(
                text(f"ALTER ROLE {ROL_APLICACION} LOGIN PASSWORD '{password}'")  # noqa: S608
            )
        print(f"[migrar] rol {ROL_APLICACION} activado con LOGIN")
    finally:
        motor.dispose()


if __name__ == "__main__":
    main()
