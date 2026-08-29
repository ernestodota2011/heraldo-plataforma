"""Endurecer el rol de aplicacion con NOINHERIT.

Revision ID: 0002
Revises: 0001

# WHY: la 0001 creo el rol sin superusuario, sin BYPASSRLS y sin propiedad de las
# tablas. Faltaba una cuarta escotilla, que no es un privilegio sino una FORMA DE
# ADQUIRIRLOS: un rol `INHERIT` usa automaticamente los privilegios de todo rol
# del que sea miembro. Un `GRANT algun_rol_potente TO heraldo_app` hecho fuera de
# este repositorio le habria dado ese poder a la aplicacion sin que ninguna
# migracion ni ninguna prueba se enterara. Con `NOINHERIT` esos privilegios
# exigen un `SET ROLE` explicito que la aplicacion nunca hace.
#
# WHY: esto es una migracion NUEVA y no una edicion de la 0001, aunque la 0001
# todavia no se haya desplegado en ningun sitio. La regla no admite excepciones
# convenientes: una revision ya escrita se queda como esta. Y no depende de la
# disciplina de nadie — al cambiar `rol.py`, el guard
# `test_la_redaccion_vigente_no_diverge_del_generador` se puso en ROJO, y este
# archivo es la unica forma correcta de devolverlo a verde. La 0001 conserva su
# redaccion como historia; la VIGENTE es esta.
#
# WHY: las sentencias son idempotentes (el `DO $$` no recrea un rol que ya
# existe y el `ALTER ROLE` puede repetirse), asi que aplicar esta revision sobre
# una base que ya paso por la 0001 no rompe nada.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

#: Solo se vuelve a congelar la receta que CAMBIO. `rol:privilegios` y las tres
#: politicas siguen vigentes desde la 0001: su redaccion no se ha tocado.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('rol:creacion', 'rol_creacion', {}),
)

SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'rol:creacion': (
        """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heraldo_app') THEN
        CREATE ROLE heraldo_app NOLOGIN NOINHERIT;
    END IF;
END
$$""",
        'ALTER ROLE heraldo_app NOSUPERUSER NOCREATEDB NOCREATEROLE '
        'NOBYPASSRLS NOREPLICATION NOINHERIT',
    ),
}


def upgrade() -> None:
    for clave, _, _ in RECETAS_CONGELADAS:
        for sentencia in SQL_CONGELADO[clave]:
            op.execute(sentencia)


def downgrade() -> None:
    # Se devuelve el atributo, no el rol: borrarlo aqui seria un efecto de
    # cluster disparado desde la migracion de una sola base.
    op.execute("ALTER ROLE heraldo_app INHERIT")
