"""Cimiento del inquilino: agencia -> cliente -> heraldo, con RLS FORCE.

Revision ID: 0001
Revises:

# WHY (T-011, RF-17): la jerarquia `agencia -> cliente -> heraldo` no es un
# diagrama: son claves reales con borrado en cascada. TODA tabla de inquilino
# lleva LAS DOS claves (`agencia_id` Y `cliente_id`) — es lo que hace posible la
# cascada de dos niveles (C-01). Una tabla que solo llevara `cliente_id` obligaria
# a mirar otra tabla para saber de que agencia es, y la politica de RLS no puede
# hacer eso sin abrir una subconsulta que a su vez pasa por RLS.
#
# WHY: la clase de cada tabla se determina por LAS COLUMNAS QUE TIENE (plan
# §3.1), asi que los nombres de columna aqui NO son cosmeticos: son la etiqueta
# que lee `test_rls_cobertura.py`.
#   - `agencias`  -> clave `agencia_id`, sin `cliente_id`  -> clase DE AGENCIA
#                    sin columna propia: INVISIBLE al alcance `cliente`. Un
#                    portal en marca blanca (RF-59) no debe poder ni nombrar a la
#                    agencia que hay detras.
#   - `clientes`  -> clave `id`, con `agencia_id`          -> clase DE AGENCIA
#                    CON columna propia (`id`): el cliente ve SU fila y ninguna
#                    otra; el operador de la agencia ve todas las de su agencia.
#   - `heraldos`  -> `agencia_id` Y `cliente_id`           -> clase DE CLIENTE
#
# WHY: la clave foranea de `heraldos` es COMPUESTA — `(agencia_id, cliente_id)`
# contra `clientes (agencia_id, id)` — y no un simple `cliente_id`. Con la simple,
# una fila podria declarar `agencia_id` de la agencia A y colgar de un cliente de
# la agencia B: la base la aceptaria y la politica de RLS la daria por buena,
# porque la politica comprueba coherencia con la SESION, no con la fila padre.
#
# WHY: el SQL esta CONGELADO LITERAL; no se le pide a `app.tenancy` al aplicar.
# Una migracion ya aplicada no puede cambiar de significado: re-aplicar esta
# revision manana tiene que hacer exactamente lo que hizo el dia que se escribio.
# Importar el generador ataba el pasado al presente — cambiar `politicas.py`
# habria reescrito, EN SILENCIO, lo que esta revision dice haber hecho.
#
# WHY: y para que «una sola redaccion VIGENTE» siga siendo verdad por MECANISMO y
# no por buena voluntad, `RECETAS_CONGELADAS` declara con que llamada se genero
# cada bloque. El guard `test_la_redaccion_vigente_no_diverge_del_generador` (en
# `test_cimiento.py`) se la vuelve a pedir al generador y exige salida IDENTICA:
# si alguien toca `politicas.py` o `rol.py` y no escribe una migracion nueva, el
# CI se pone en ROJO. Las revisiones VIEJAS se quedan congeladas a proposito —
# eso es historia, no deuda.
#
# WHY: no se usa `--autogenerate` (D-10). Las politicas, el rol y sus privilegios
# no salen de una comparacion de metadata.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

#: Tablas sobre las que el rol de aplicacion recibe los cuatro verbos. Es una
#: ALLOWLIST: lo que no este aqui, el rol no lo toca. `alembic_version` no esta.
TABLAS_DE_INQUILINO = ("agencias", "clientes", "heraldos")

#: DDL de las tablas. Literal desde el primer dia: esto nunca lo genero nadie.
TABLAS_SQL: tuple[str, ...] = (
    """CREATE TABLE agencias (
    agencia_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre      text NOT NULL,
    creada_en   timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE clientes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id  uuid NOT NULL REFERENCES agencias (agencia_id) ON DELETE CASCADE,
    nombre      text NOT NULL,
    creado_en   timestamptz NOT NULL DEFAULT now(),
    -- Destino de la clave foranea COMPUESTA de las tablas de cliente.
    CONSTRAINT clientes_agencia_id_id_key UNIQUE (agencia_id, id)
)""",
    "CREATE INDEX clientes_agencia_id_idx ON clientes (agencia_id)",
    """CREATE TABLE heraldos (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id  uuid NOT NULL,
    cliente_id  uuid NOT NULL,
    nombre      text NOT NULL,
    creado_en   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT heraldos_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE
)""",
    "CREATE INDEX heraldos_inquilino_idx ON heraldos (agencia_id, cliente_id)",
)

#: Con que llamada al generador se produjo cada bloque congelado:
#: (clave, generador, argumentos). Un generador que el guard no sepa regenerar es
#: un FALLO, no un bloque que se salta en silencio.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('politica:agencias', 'politica_de_agencia', {'tabla': 'agencias', 'columna_propia': None}),
    ('politica:clientes', 'politica_de_agencia', {'tabla': 'clientes', 'columna_propia': 'id'}),
    ('politica:heraldos', 'politica_de_cliente', {'tabla': 'heraldos'}),
    ('rol:creacion', 'rol_creacion', {}),
    ('rol:privilegios', 'rol_privilegios', {'tablas': ['agencias', 'clientes', 'heraldos']}),
)

#: El SQL tal y como se aplico. CONGELADO: no lo toques para mejorarlo — si la
#: redaccion tiene que cambiar, se escribe una migracion NUEVA.
SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'politica:agencias': (
        'ALTER TABLE agencias ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE agencias FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON agencias
    FOR ALL
    USING (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND current_setting('app.alcance') = 'agencia'
    )
    WITH CHECK (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND current_setting('app.alcance') = 'agencia'
    )""",
    ),
    'politica:clientes': (
        'ALTER TABLE clientes ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE clientes FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON clientes
    FOR ALL
    USING (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND ( current_setting('app.alcance') = 'agencia'
            OR id = current_setting('app.cliente_id')::uuid )
    )
    WITH CHECK (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND ( current_setting('app.alcance') = 'agencia'
            OR id = current_setting('app.cliente_id')::uuid )
    )""",
    ),
    'politica:heraldos': (
        'ALTER TABLE heraldos ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE heraldos FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON heraldos
    FOR ALL
    USING (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND ( current_setting('app.alcance') = 'agencia'
            OR cliente_id = current_setting('app.cliente_id')::uuid )
    )
    WITH CHECK (
      agencia_id = current_setting('app.agencia_id')::uuid
      AND ( current_setting('app.alcance') = 'agencia'
            OR cliente_id = current_setting('app.cliente_id')::uuid )
    )""",
    ),
    'rol:creacion': (
        """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heraldo_app') THEN
        CREATE ROLE heraldo_app NOLOGIN;
    END IF;
END
$$""",
        'ALTER ROLE heraldo_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION',
    ),
    'rol:privilegios': (
        'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM heraldo_app',
        'REVOKE ALL ON SCHEMA public FROM heraldo_app',
        'REVOKE CREATE ON SCHEMA public FROM PUBLIC',
        'GRANT USAGE ON SCHEMA public TO heraldo_app',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON agencias, clientes, heraldos TO heraldo_app',
    ),
}


def _ejecutar(sentencias: tuple[str, ...]) -> None:
    for sentencia in sentencias:
        op.execute(sentencia)


def upgrade() -> None:
    _ejecutar(TABLAS_SQL)
    # Politicas y rol, en el orden de las recetas: primero se gobiernan las
    # tablas, y despues nace el rol que vivira bajo ese gobierno.
    for clave, _, _ in RECETAS_CONGELADAS:
        _ejecutar(SQL_CONGELADO[clave])


def downgrade() -> None:
    # Las politicas caen con sus tablas. El ROL no se borra aqui a proposito: es
    # un objeto de CLUSTER, no de base, y puede tener privilegios en otras bases;
    # borrarlo desde una migracion de una base seria un efecto fuera de alcance.
    op.execute("DROP TABLE IF EXISTS heraldos")
    op.execute("DROP TABLE IF EXISTS clientes")
    op.execute("DROP TABLE IF EXISTS agencias")
