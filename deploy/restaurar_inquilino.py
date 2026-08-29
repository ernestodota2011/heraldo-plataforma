"""T-026 (RF-53) — restaurar UN inquilino de un respaldo completo, sin tocar a los demas.

Implementa EXACTO el procedimiento del plan (Sec.4, "Exportar / borrar / restaurar
un solo inquilino"): respaldo -> base TEMPORAL -> extraccion de las filas de ese
inquilino -> reinsercion por el rol de APLICACION con su alcance declarado.
==JAMAS un `pg_restore` sobre la base viva.==

Universo de lo que se restaura (plan Sec.4): la clase "de cliente" completa
(agencia_id + cliente_id en la tabla) MAS la FILA PROPIA de ese cliente en cada
tabla de la clase "de agencia" que la tenga (aqui, `clientes`, columna_propia=id).

Uso:
    python deploy/restaurar_inquilino.py \
        --admin-dsn-tmp-base postgresql://USUARIO:CLAVE@HOST:5432/postgres \
        --app-dsn            postgresql+psycopg://heraldo_app:CLAVE@HOST:5432/heraldo \
        --dump               /ruta/al/respaldo.dump \
        --agencia            <uuid> \
        --cliente            <uuid>

La reinsercion pasa por `app.tenancy.sesion.sesion_de_inquilino` — el MISMO
codigo que usa la API en produccion (plan Sec.4: "no existe una segunda forma
de abrir conexion"). Si una fila no pertenece al inquilino declarado, la
politica RLS la rechaza aqui igual que la rechazaria en la API: este script no
tiene un camino mas privilegiado que el producto.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.tenancy.inquilino import Inquilino  # noqa: E402
from app.tenancy.sesion import crear_motor, sesion_de_inquilino  # noqa: E402

TABLAS_DE_CLIENTE: tuple[str, ...] = (
    "heraldos",
    "secretos",
    "bitacora",
    "trabajos",
    "trabajos_archivados",
    "mensajes_entrantes",
)

@dataclass(frozen=True, slots=True)
class FilaExtraida:
    tabla: str
    columnas: tuple[str, ...]
    valores: tuple


def _dsn_para_psycopg(dsn: str) -> str:
    """Normaliza un DSN estilo SQLAlchemy (`postgresql+psycopg://...`) a la
    forma que psycopg/libpq entienden (`postgresql://...`).

    # WHY (por que esto existe): `HERALDO_DATABASE_URL_ADMIN` se documenta en
    # TODO el repositorio en forma SQLAlchemy (env.py, conftest.py, README).
    # Pasarla tal cual a `psycopg.connect()` no falla con un mensaje claro:
    # falla con "missing '=' after ..." y el texto de la excepcion INCLUYE el
    # DSN completo, credencial adentro — psycopg no redacta sus propios
    # mensajes de error. Ese fallo se midio de verdad corriendo este mismo
    # script (T-026) y la contrasena filtrada al log se roto de inmediato. La
    # normalizacion vive aqui, en UN sitio, para que nadie mas vuelva a pasar
    # el DSN equivocado al driver equivocado.
    """
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


class EjecutableNoEncontrado(RuntimeError):
    """El binario que este script necesita no esta en el PATH del proceso."""


def _ruta_de(ejecutable: str) -> str:
    """Resuelve `ejecutable` a una ruta ABSOLUTA con `shutil.which()`, o falla
    nombrando que falta.

    # WHY: este script no siempre corre en la terminal de quien lo escribio.
    # Un cron, un contenedor efimero o un systemd service NO heredan el PATH
    # interactivo de una shell de login -- es exactamente la clase de cosa que
    # funciona perfecto en la terminal y falla el dia del desastre, que es el
    # peor momento posible para descubrirlo. Resolver la ruta ANTES de invocar
    # `subprocess` (en vez de dejar que el shell/exec busque `pg_restore` en su
    # propio PATH) hace el fallo EXPLICITO y con el nombre del binario que
    # falta, en vez de un "No such file or directory" generico o -segun la
    # plataforma y el PATH heredado- ejecutar sin querer un binario distinto
    # con el mismo nombre que quedo antes en el PATH. No es para aplacar al
    # linter (S607, "partial executable path"): es la misma familia de defecto
    # que ya midio T-026 con el DSN -- lo que corre bien en el sitio donde se
    # escribe no es lo que corre el dia que hace falta.
    """
    ruta = shutil.which(ejecutable)
    if ruta is None:
        raise EjecutableNoEncontrado(
            f"{ejecutable!r} no esta en el PATH de este proceso. Instala el "
            f"paquete cliente de Postgres (postgresql-client) en el entorno "
            f"que corre este script -- no se sigue adelante adivinando una "
            f"ruta"
        )
    return ruta


def restaurar_dump_en_temporal(dsn_admin_tmp_base: str, nombre_bd_tmp: str, dump: Path) -> None:
    """Crea la base temporal y le aplica el dump. Nunca toca la base viva."""
    dsn_psycopg = _dsn_para_psycopg(dsn_admin_tmp_base)
    with psycopg.connect(dsn_psycopg, autocommit=True) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{nombre_bd_tmp}"')  # noqa: S608
            cursor.execute(f'CREATE DATABASE "{nombre_bd_tmp}"')  # noqa: S608
    print(f"[restaurar] base temporal {nombre_bd_tmp!r} creada")

    # `pg_restore` no entiende una URL: se le pasan host/puerto/usuario/clave
    # como VARIABLES DE ENTORNO libpq (PGHOST/PGPORT/PGUSER/PGPASSWORD), nunca
    # como texto en el argv (un argv con la clave queda en `ps aux` de
    # cualquiera con acceso al host — `feedback_secreto_por_argv`).
    partes = conninfo_to_dict(dsn_psycopg)
    entorno = os.environ.copy()
    if "host" in partes:
        entorno["PGHOST"] = str(partes["host"])
    if "port" in partes:
        entorno["PGPORT"] = str(partes["port"])
    if "user" in partes:
        entorno["PGUSER"] = str(partes["user"])
    if "password" in partes:
        entorno["PGPASSWORD"] = str(partes["password"])

    resultado = subprocess.run(  # noqa: S603 (argv fijo + ruta absoluta resuelta arriba)
        [
            _ruta_de("pg_restore"),
            "--no-owner",
            "--no-privileges",
            "-d",
            nombre_bd_tmp,
            str(dump),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=entorno,
    )
    if resultado.returncode not in (0, 1):  # pg_restore devuelve 1 por avisos no fatales
        # WHY: se reporta SOLO el codigo de salida, nunca `resultado.stderr` completo
        # sin inspeccionar — un error de conexion de pg_restore puede ecoar el DSN
        # o el host/usuario que se le paso. El detalle queda disponible para quien
        # depure con el propio `resultado`, no se imprime aqui por defecto.
        raise RuntimeError(f"pg_restore fallo con codigo {resultado.returncode}")
    print(f"[restaurar] dump aplicado a {nombre_bd_tmp!r} (rc={resultado.returncode})")


def extraer_filas_del_inquilino(
    dsn_admin_tmp_base: str, nombre_bd_tmp: str, *, agencia_id: UUID, cliente_id: UUID
) -> tuple[FilaExtraida | None, list[FilaExtraida]]:
    """Lee de la base TEMPORAL, con el rol MIGRADOR (bypassa RLS a proposito:
    es una base desechable, no la viva). Devuelve la fila propia de clientes
    (o None si tambien se perdio) y las filas de las tablas de cliente.
    """
    dsn_base_psycopg = _dsn_para_psycopg(dsn_admin_tmp_base)
    dsn_tmp = f"{dsn_base_psycopg.rsplit('/', 1)[0]}/{nombre_bd_tmp}"
    filas: list[FilaExtraida] = []
    with psycopg.connect(dsn_tmp, row_factory=dict_row) as conexion, conexion.cursor() as cursor:
        # WHY (SELECT * y no una lista de columnas a mano): una lista fija se
        # queda atras cada vez que una migracion le agrega una columna a
        # `clientes` -- paso exactamente eso con la revision 0004
        # (`sector`, `sector_verificado_en`, RNF-04): una lista escrita antes
        # de esa migracion habria restaurado el cliente SIN su clasificacion
        # persistida, silenciosamente. Las columnas se derivan de
        # `cursor.description`, igual que ya se hace abajo para las tablas de
        # cliente -- la misma disciplina que exige el gate de RLS al derivar
        # su universo del catalogo en vez de una lista escrita a mano.
        cursor.execute(
            "SELECT * FROM clientes WHERE agencia_id = %s AND id = %s",
            (agencia_id, cliente_id),
        )
        columnas_cliente = tuple(d.name for d in cursor.description)
        fila_cliente_cruda = cursor.fetchone()
        fila_cliente = (
            FilaExtraida(
                tabla="clientes",
                columnas=columnas_cliente,
                valores=tuple(fila_cliente_cruda[c] for c in columnas_cliente),
            )
            if fila_cliente_cruda
            else None
        )

        for tabla in TABLAS_DE_CLIENTE:
            cursor.execute(
                f"SELECT * FROM {tabla} "  # noqa: S608 (tabla viene de la allowlist de arriba)
                "WHERE agencia_id = %s AND cliente_id = %s ORDER BY 1",
                (agencia_id, cliente_id),
            )
            columnas = tuple(d.name for d in cursor.description)
            for cruda in cursor.fetchall():
                filas.append(
                    FilaExtraida(
                        tabla=tabla,
                        columnas=columnas,
                        valores=tuple(cruda[c] for c in columnas),
                    )
                )
    return fila_cliente, filas


def _sentencia_insert(fila: FilaExtraida) -> tuple[str, dict]:
    """Arma el INSERT + sus parametros, con `CAST(:col AS jsonb)` para las
    columnas que la extraccion trajo como `dict`/`list` (jsonb en Postgres:
    psycopg las devuelve ya deserializadas). Sin el cast explicito, psycopg
    async no sabe adaptar un `dict` de Python al placeholder — se midio
    corriendo este mismo drill (T-026) contra `bitacora.detalle`.
    """
    columnas_sql = []
    parametros: dict = {}
    for columna, valor in zip(fila.columnas, fila.valores, strict=True):
        if isinstance(valor, (dict, list)):
            columnas_sql.append(f"CAST(:{columna} AS jsonb)")
            parametros[columna] = json.dumps(valor)
        else:
            columnas_sql.append(f":{columna}")
            parametros[columna] = valor
    sentencia = (
        f"INSERT INTO {fila.tabla} ({', '.join(fila.columnas)}) "  # noqa: S608
        f"VALUES ({', '.join(columnas_sql)}) ON CONFLICT DO NOTHING"
    )
    return sentencia, parametros


async def reinsertar_por_el_rol_de_aplicacion(
    dsn_app: str,
    *,
    agencia_id: UUID,
    cliente_id: UUID,
    fila_cliente: FilaExtraida | None,
    filas_de_cliente: list[FilaExtraida],
) -> None:
    """Reinserta EXCLUSIVAMENTE via sesion_de_inquilino — el mismo camino que
    usa la API. Dos transacciones separadas a proposito: alcance AGENCIA para
    reponer la fila propia en clientes (una sesion de alcance CLIENTE no
    puede insertar la fila que la hace existir), y alcance CLIENTE para las
    tablas de cliente.
    """
    motor = crear_motor(dsn_app, tamano_pool=1)
    try:
        if fila_cliente is not None:
            agencia = Inquilino.desde_usuario(agencia_id=agencia_id, cliente_id=None)
            async with sesion_de_inquilino(motor, agencia) as conexion:
                sentencia, parametros = _sentencia_insert(fila_cliente)
                await conexion.execute(text(sentencia), parametros)
            print("[restaurar] fila propia de clientes repuesta (alcance agencia)")

        cliente = Inquilino.desde_usuario(agencia_id=agencia_id, cliente_id=cliente_id)
        async with sesion_de_inquilino(motor, cliente) as conexion:
            for fila in filas_de_cliente:
                sentencia, parametros = _sentencia_insert(fila)
                await conexion.execute(text(sentencia), parametros)
        print(
            f"[restaurar] {len(filas_de_cliente)} filas reinsertadas, alcance={cliente.alcance}, "
            "rol=heraldo_app"
        )
    finally:
        await motor.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--admin-dsn-tmp-base",
        required=True,
        help="DSN admin apuntando a una base existente (para poder crear/soltar la temporal)",
    )
    ap.add_argument("--nombre-bd-tmp", default="heraldo_restore_tmp")
    ap.add_argument(
        "--app-dsn", required=True, help="DSN del rol de aplicacion, forma sqlalchemy+psycopg"
    )
    ap.add_argument("--dump", required=True, type=Path)
    ap.add_argument("--agencia", required=True, type=UUID)
    ap.add_argument("--cliente", required=True, type=UUID)
    args = ap.parse_args()

    restaurar_dump_en_temporal(args.admin_dsn_tmp_base, args.nombre_bd_tmp, args.dump)
    fila_cliente, filas = extraer_filas_del_inquilino(
        args.admin_dsn_tmp_base,
        args.nombre_bd_tmp,
        agencia_id=args.agencia,
        cliente_id=args.cliente,
    )
    print(
        f"[restaurar] extraidas de la temporal: clientes={1 if fila_cliente else 0}, "
        f"{len(filas)} filas de tablas de cliente"
    )
    asyncio.run(
        reinsertar_por_el_rol_de_aplicacion(
            args.app_dsn,
            agencia_id=args.agencia,
            cliente_id=args.cliente,
            fila_cliente=fila_cliente,
            filas_de_cliente=filas,
        )
    )

    with psycopg.connect(_dsn_para_psycopg(args.admin_dsn_tmp_base), autocommit=True) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{args.nombre_bd_tmp}"')  # noqa: S608
    print(f"[restaurar] base temporal {args.nombre_bd_tmp!r} eliminada")


if __name__ == "__main__":
    main()
