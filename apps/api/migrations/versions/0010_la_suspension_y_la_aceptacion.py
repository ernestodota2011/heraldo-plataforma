"""La suspension del cliente y su aceptacion contractual (RF-66).

Revision ID: 0010
Revises: 0005

Tres tablas, dos clases distintas y una decision de modelo en cada una.

# WHY (`suspensiones` es un HISTORIAL, no un interruptor): RF-66 define un estado
# —el heraldo no envia ni responde, el portal en solo lectura, el export encendido,
# nada borrado— que se levanta al subsanar. La forma comoda de escribirlo es una
# columna `suspendido` en `clientes`; la correcta es esta tabla, porque el estado
# vigente **se deriva** de ella («existe una fila sin `levantada_en`») y asi solo
# hay UN sitio que lo diga. Con dos, el que se queda viejo manda sobre el producto
# sin que nadie lo mire — y aqui «viejo» significa un cliente apagado que el panel
# ve encendido, o al reves.
#
# # WHY (el INDICE unico parcial, y no un `if` en el modulo): dos suspensiones a la
# vez —el panel de un operador y el barrido de re-aceptacion— leen las dos «no hay
# ninguna vigente» y escriben las dos. Con dos filas vigentes, «levantar» deja de
# tener una respuesta unica y el historial afirma dos cortes donde hubo uno. El
# indice lo hace imposible aunque el codigo lo intente, que es la unica forma de
# que no dependa de que nadie escriba un segundo camino.
#
# # WHY (`versiones_publicadas` NO lleva inquilino, y eso es deliberado): es un
# **catalogo de plataforma** (plan §3.0). Una version del contrato no es de ningun
# cliente: es la misma para todos, y por eso no cuelga de la cascada ni la gobierna
# RLS. Su excepcion vive declarada, con motivo, en `test_rls_cobertura.py` — y ahi
# ademas se mide que la aplicacion la alcance con EXACTAMENTE los verbos declarados
# (`SELECT`, `INSERT`) y con ninguno mas: una version ya aceptada que se pudiera
# reescribir convertiria las aceptaciones que la nombran en firmas sobre otro texto.
#
# # WHY (`hash_del_texto`): «version 2.0» es un nombre, no un documento. Dos textos
# distintos publicados con el mismo nombre son dos filas distintas y la aceptacion
# apunta a UNA. Sin la huella, «que acepto este cliente» no tendria respuesta.
#
# # WHY (`aceptaciones_contractuales` es de solo insercion): una aceptacion es un
# hecho fechado. Reescribirla cambiaria QUE acepto el cliente y CUANDO; borrarla
# dejaria un alta sin la aceptacion que la autorizo, que es exactamente lo que
# RF-66 existe para impedir. Igual que la bitacora, el mecanismo es el PERMISO.
#
# # WHY (la clave foranea de la aceptacion apunta a un catalogo sin RLS): la
# comprobacion de la foranea la hace el motor con los privilegios de la tabla, no
# los de quien escribe, asi que no abre ninguna via de lectura nueva. Y no revela
# nada que la aplicacion no pueda ya consultar: el catalogo es alcanzable para ella
# por diseno — es el texto al que se adhiere todo el mundo.
#
# # WHY (aqui NO se puebla el catalogo): la fila real —la v2 del contrato y su
# anexo— la publica **T-030·quater**. Sembrar aqui una version «de ejemplo» seria
# publicar un contrato que nadie reviso, y ademas dejaria clientes aceptando un
# texto inventado por una migracion. Mientras el catalogo este vacio no hay altas
# con datos de personas reales: solo altas de desarrollo, que crean su propia
# version marcada como tal.
#
# # WHY (SQL congelado, D-10): igual que en 0001..0005, aqui no se importa
# `app.tenancy`. Una migracion ya aplicada no puede cambiar de significado.
# `RECETAS_CONGELADAS` declara con que llamada se genero cada bloque para que
# `test_la_redaccion_vigente_no_diverge_del_generador` pueda volver a pedirsela al
# generador y exigir salida IDENTICA.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0005"
branch_labels = None
depends_on = None

#: DDL de las tres tablas. Literal, como en el resto de revisiones.
#:
#: # WHY (`gen_random_uuid` y no `serial`): a una SECUENCIA no se le aplica RLS. Su
#: contador es una fuga de VOLUMEN entre inquilinos y
#: `test_ninguna_secuencia_alcanzable_sin_declarar` lo prohibe expresamente.
TABLAS_SQL: tuple[str, ...] = (
    # ------------------------------------------------------- RF-66 · suspension
    """CREATE TABLE suspensiones (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id     uuid NOT NULL,
    cliente_id     uuid NOT NULL,
    motivo         text NOT NULL,
    suspendida_en  timestamptz NOT NULL DEFAULT now(),
    suspendida_por text NOT NULL,
    levantada_en   timestamptz,
    levantada_por  text,
    CONSTRAINT suspensiones_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT suspensiones_motivo_check CHECK (btrim(motivo) <> ''),
    CONSTRAINT suspensiones_cierre_check
        CHECK ((levantada_en IS NULL) = (levantada_por IS NULL))
)""",
    # WHY: PARCIAL y UNICO. Parcial, porque un cliente puede haber estado suspendido
    # muchas veces y todas esas filas conviven; unico, porque VIGENTE solo puede
    # haber una. Es la restriccion que hace que «levantar» tenga respuesta unica.
    """CREATE UNIQUE INDEX suspensiones_vigente_idx
    ON suspensiones (agencia_id, cliente_id)
    WHERE levantada_en IS NULL""",
    """CREATE INDEX suspensiones_inquilino_idx
    ON suspensiones (agencia_id, cliente_id, suspendida_en)""",
    # ------------------------------------------- RF-66 · catalogo de plataforma
    # WHY (`documento` con CHECK y no un enum): mismo motivo que `trabajos.estado`
    # en la 0003 — un `CREATE TYPE ... AS ENUM` es un objeto mas del esquema que el
    # gate derivado del catalogo no mira, y anadirle un valor exige `ALTER TYPE`,
    # que no se puede deshacer.
    """CREATE TABLE versiones_publicadas (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    documento    text NOT NULL,
    version      text NOT NULL,
    publicada_en timestamptz NOT NULL DEFAULT now(),
    declara_instruccion_de_derechos boolean NOT NULL,
    es_desarrollo boolean NOT NULL,
    hash_del_texto text NOT NULL,
    CONSTRAINT versiones_publicadas_documento_check
        CHECK (documento IN ('contrato', 'anexo_tratamiento')),
    CONSTRAINT versiones_publicadas_version_key UNIQUE (documento, version),
    CONSTRAINT versiones_publicadas_hash_check CHECK (btrim(hash_del_texto) <> '')
)""",
    """CREATE INDEX versiones_publicadas_documento_idx
    ON versiones_publicadas (documento, publicada_en DESC)""",
    # -------------------------------------------------------- RF-66 · aceptacion
    # WHY (`aceptada_por` es texto y NO una foranea a ninguna tabla de personas):
    # RF-10 exige que el «quien» sea rol + identificador OPACO. Una foranea a una
    # tabla de usuarios volveria a atar el asiento a una identidad resoluble, que es
    # justo lo que el identificador opaco evita.
    """CREATE TABLE aceptaciones_contractuales (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id   uuid NOT NULL,
    cliente_id   uuid NOT NULL,
    version_id   uuid NOT NULL,
    aceptada_por text NOT NULL,
    aceptada_en  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aceptaciones_contractuales_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT aceptaciones_contractuales_version_fkey
        FOREIGN KEY (version_id) REFERENCES versiones_publicadas (id),
    CONSTRAINT aceptaciones_contractuales_una_por_version
        UNIQUE (agencia_id, cliente_id, version_id)
)""",
    """CREATE INDEX aceptaciones_contractuales_inquilino_idx
    ON aceptaciones_contractuales (agencia_id, cliente_id, aceptada_en)""",
)

#: Con que llamada al generador se produjo cada bloque congelado:
#: (clave, generador, argumentos).
#:
#: # WHY (`versiones_publicadas` no tiene politica y no aparece aqui): no lleva
#: `agencia_id` ni `cliente_id`, asi que no es una tabla de inquilino y no hay
#: expresion que escribirle. Su gobierno es el PRIVILEGIO, y ese si esta congelado
#: abajo, en `rol:privilegios`.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('politica:suspensiones', 'politica_de_cliente', {'tabla': 'suspensiones'}),
    ('politica:aceptaciones_contractuales', 'politica_de_cliente',
     {'tabla': 'aceptaciones_contractuales'}),
    ('rol:privilegios', 'rol_privilegios', {
        'privilegios': {
            'agencias': ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
            'clientes': ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
            'heraldos': ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
            'secretos': ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
            'bitacora': ['SELECT', 'INSERT'],
            'trabajos': ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
            'trabajos_archivados': ['SELECT', 'INSERT', 'DELETE'],
            'mensajes_entrantes': ['SELECT', 'INSERT'],
            'suspensiones': ['SELECT', 'INSERT', 'UPDATE'],
            'aceptaciones_contractuales': ['SELECT', 'INSERT'],
            'versiones_publicadas': ['SELECT', 'INSERT'],
        },
    }),
)

#: El SQL tal y como se aplico. CONGELADO: no lo toques para mejorarlo — si la
#: redaccion tiene que cambiar, se escribe una migracion NUEVA.
SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'politica:suspensiones': (
        'ALTER TABLE suspensiones ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE suspensiones FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON suspensiones
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
    'politica:aceptaciones_contractuales': (
        'ALTER TABLE aceptaciones_contractuales ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE aceptaciones_contractuales FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON aceptaciones_contractuales
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
    'rol:privilegios': (
        'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM heraldo_app',
        'REVOKE ALL ON SCHEMA public FROM heraldo_app',
        'REVOKE CREATE ON SCHEMA public FROM PUBLIC',
        'GRANT USAGE ON SCHEMA public TO heraldo_app',
        'GRANT SELECT, INSERT ON aceptaciones_contractuales, bitacora, '
        'mensajes_entrantes, versiones_publicadas TO heraldo_app',
        'GRANT SELECT, INSERT, DELETE ON trabajos_archivados TO heraldo_app',
        'GRANT SELECT, INSERT, UPDATE ON suspensiones TO heraldo_app',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON agencias, clientes, heraldos, '
        'secretos, trabajos TO heraldo_app',
    ),
}


def _ejecutar(sentencias: tuple[str, ...]) -> None:
    for sentencia in sentencias:
        op.execute(sentencia)


def upgrade() -> None:
    _ejecutar(TABLAS_SQL)
    for clave, _, _ in RECETAS_CONGELADAS:
        _ejecutar(SQL_CONGELADO[clave])


def downgrade() -> None:
    # Las politicas caen con sus tablas. Los privilegios NO se devuelven aqui a su
    # forma anterior: la revision 0003 los vuelve a conceder si se baja hasta ella,
    # y dejar un `GRANT` a medias entre las dos seria peor que no tocarlos.
    #
    # El orden importa: la aceptacion apunta al catalogo, asi que se va primero.
    op.execute("DROP TABLE IF EXISTS aceptaciones_contractuales")
    op.execute("DROP TABLE IF EXISTS versiones_publicadas")
    op.execute("DROP TABLE IF EXISTS suspensiones")
