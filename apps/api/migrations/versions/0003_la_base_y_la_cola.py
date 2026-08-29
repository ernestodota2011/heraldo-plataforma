"""La base y la cola: secretos, bitacora, cola de trabajos, archivo e idempotencia.

Revision ID: 0003
Revises: 0002

# WHY (T-016, T-017, T-020, T-021): las cinco tablas de esta revision son las
# cinco tablas de inquilino que faltaban para que el producto tenga cuerpo. Todas
# llevan LAS DOS claves (`agencia_id` Y `cliente_id`) — T-011 — asi que el gate
# derivado del catalogo las clasifica solo en la clase *de cliente* y exige de
# ellas exactamente lo que a esa clase le toca. Ninguna se declara: se deducen.
#
# WHY (la cascada): las cinco cuelgan de `clientes (agencia_id, id)` con clave
# foranea COMPUESTA y `ON DELETE CASCADE`. Compuesta y no simple, por lo mismo que
# `heraldos` (revision 0001): con la simple, una fila podria declarar la agencia A
# y colgar de un cliente de la agencia B, y la politica de RLS la daria por buena
# porque comprueba coherencia con la SESION, no con la fila padre. Y con cascada,
# porque la limpieza de la suite DERIVA su universo del modelo — una tabla de
# inquilino que no cuelgue de la cascada sobrevive al borrado en silencio (P-15).
#
# WHY (el SQL CONGELADO): igual que en 0001 y 0002, aqui no se importa
# `app.tenancy`. Una migracion ya aplicada no puede cambiar de significado. Y
# `RECETAS_CONGELADAS` declara con que llamada se genero cada bloque para que
# `test_la_redaccion_vigente_no_diverge_del_generador` pueda volver a pedirsela al
# generador y exigir salida IDENTICA.
#
# WHY (`rol:privilegios` se vuelve a congelar): esta revision cambia la FORMA de
# los privilegios, no solo su contenido. Hasta aqui la aplicacion recibia los
# cuatro verbos sobre todas las tablas; ==RF-10 no es expresable asi==: una
# bitacora que la propia aplicacion no pueda reescribir necesita que el `UPDATE`
# y el `DELETE` NO ESTEN CONCEDIDOS. «Solo insercion» deja de ser una costumbre y
# pasa a ser un permiso que la base niega.
#
# WHY (no se usa `--autogenerate`, D-10): las politicas, el rol y sus privilegios
# no salen de una comparacion de metadata.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

#: DDL de las cinco tablas. Literal desde el primer dia: esto nunca lo genero nadie.
#:
#: # WHY (`gen_random_uuid` y no `serial`): a una SECUENCIA no se le aplica RLS. Su
#: contador es una fuga de VOLUMEN entre inquilinos —cuantas filas se crearon— y
#: `test_ninguna_secuencia_alcanzable_sin_declarar` lo prohibe expresamente.
TABLAS_SQL: tuple[str, ...] = (
    # ----------------------------------------------------------------- T-016
    # WHY: `cifrado` es `bytea` y no `text`. Un secreto que viaja como texto acaba
    # en un log, en un `EXPLAIN`, en un volcado de la fila; `bytea` obliga a que
    # cualquier camino que lo quiera enseñar lo convierta a proposito. Y el
    # serializador RECHAZA por TIPO todo lo que sea `bytes`, asi que la columna
    # elige justo el tipo que el barrido sabe cazar.
    #
    # WHY: NO existe una columna con el valor en claro, ni una «copia por si
    # acaso». RF-09 dice que no se devuelve en claro «por ninguna via»: la via mas
    # facil seria que estuviera guardado en claro en otra columna.
    """CREATE TABLE secretos (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id   uuid NOT NULL,
    cliente_id   uuid NOT NULL,
    nombre       text NOT NULL,
    cifrado      bytea NOT NULL,
    creado_en    timestamptz NOT NULL DEFAULT now(),
    actualizado_en timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT secretos_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT secretos_nombre_key UNIQUE (agencia_id, cliente_id, nombre)
)""",
    "CREATE INDEX secretos_inquilino_idx ON secretos (agencia_id, cliente_id)",
    # ----------------------------------------------------------------- T-017
    # WHY (RF-10): «quien, que y cuando». `actor` es quien, `accion` + `recurso`
    # es que, `ocurrido_en` es cuando. `detalle` es jsonb para lo que cada accion
    # necesite sin cambiar el esquema cada vez.
    #
    # WHY: la tabla no lleva ninguna columna de estado —nada de `corregido`,
    # `anulado` ni `visible`—. Una columna de estado en una bitacora de solo
    # insercion es la puerta trasera: no reescribe la fila, la esconde.
    """CREATE TABLE bitacora (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id  uuid NOT NULL,
    cliente_id  uuid NOT NULL,
    ocurrido_en timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,
    accion      text NOT NULL,
    recurso     text NOT NULL,
    detalle     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT bitacora_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE
)""",
    "CREATE INDEX bitacora_inquilino_idx ON bitacora (agencia_id, cliente_id, ocurrido_en)",
    # ----------------------------------------------------------------- T-020
    # WHY (D-04): la cola vive en Postgres para que ENCOLAR ocurra en la MISMA
    # transaccion que guarda el mensaje. Ese es el punto: con un broker aparte el
    # trabajo perdido es posible —se guarda el mensaje y el encolado falla—; aqui
    # no hay dos sitios que puedan discrepar.
    #
    # WHY (`estado` con CHECK y no un enum): un `CREATE TYPE ... AS ENUM` es un
    # objeto mas del esquema que el gate derivado del catalogo no mira hoy, y
    # añadirle un valor exige `ALTER TYPE`, que no se puede deshacer. El CHECK
    # nombra los cuatro estados y una migracion futura lo reescribe entero.
    #
    # WHY (`disponible_en`): la espera creciente de RF-14 no vive en el proceso.
    # Vive en la FILA: un trabajo que fallo vuelve a `pendiente` con su
    # `disponible_en` en el futuro. Si el worker muere entre medias, la espera
    # sigue siendo la misma cuando otro lo recoja — un temporizador en memoria se
    # habria perdido con el proceso, que es el defecto 6 del referente.
    """CREATE TABLE trabajos (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id    uuid NOT NULL,
    cliente_id    uuid NOT NULL,
    tipo          text NOT NULL,
    carga         jsonb NOT NULL DEFAULT '{}'::jsonb,
    estado        text NOT NULL DEFAULT 'pendiente',
    intentos      integer NOT NULL DEFAULT 0,
    maximo_intentos integer NOT NULL DEFAULT 5,
    disponible_en timestamptz NOT NULL DEFAULT now(),
    creado_en     timestamptz NOT NULL DEFAULT now(),
    actualizado_en timestamptz NOT NULL DEFAULT now(),
    terminado_en  timestamptz,
    ultimo_error  text,
    CONSTRAINT trabajos_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT trabajos_estado_check
        CHECK (estado IN ('pendiente', 'en_curso', 'hecho', 'fallido')),
    CONSTRAINT trabajos_intentos_check CHECK (intentos >= 0),
    CONSTRAINT trabajos_maximo_intentos_check CHECK (maximo_intentos >= 1)
)""",
    # WHY: el indice lleva `disponible_en` y `creado_en` porque la unica consulta
    # del camino caliente es «el pendiente disponible mas antiguo». Es PARCIAL
    # para que los trabajos terminados no engorden el indice que sostiene la
    # latencia — y esa es la mitad barata de la mitigacion de R-07.
    """CREATE INDEX trabajos_pendientes_idx
    ON trabajos (agencia_id, cliente_id, disponible_en, creado_en)
    WHERE estado = 'pendiente'""",
    # WHY: y este otro para el barrido de archivado, que busca por lo contrario.
    """CREATE INDEX trabajos_terminados_idx
    ON trabajos (agencia_id, cliente_id, terminado_en)
    WHERE estado IN ('hecho', 'fallido')""",
    # WHY (R-07, y por que existe DESDE EL DIA 1): `SKIP LOCKED` es solido, pero la
    # tabla crece. Si el archivado se deja «para cuando haga falta», para entonces
    # la tabla de cola YA es el cuello: el indice no cabe en memoria, el autovacuum
    # no alcanza, y el barrido que habria que correr es justo el que no se puede
    # correr sin bloquear el camino caliente. Se paga ahora, que es barato.
    """CREATE TABLE trabajos_archivados (
    id            uuid PRIMARY KEY,
    agencia_id    uuid NOT NULL,
    cliente_id    uuid NOT NULL,
    tipo          text NOT NULL,
    carga         jsonb NOT NULL,
    estado        text NOT NULL,
    intentos      integer NOT NULL,
    maximo_intentos integer NOT NULL,
    creado_en     timestamptz NOT NULL,
    terminado_en  timestamptz NOT NULL,
    archivado_en  timestamptz NOT NULL DEFAULT now(),
    ultimo_error  text,
    CONSTRAINT trabajos_archivados_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT trabajos_archivados_estado_check
        CHECK (estado IN ('hecho', 'fallido'))
)""",
    """CREATE INDEX trabajos_archivados_purga_idx
    ON trabajos_archivados (agencia_id, cliente_id, archivado_en)""",
    # ----------------------------------------------------------------- T-021
    # WHY (RF-12): ==la restriccion unica es la defensa que no miente.== Redis es
    # un acelerador: cuando se reinicia, su memoria desaparece y con ella la unica
    # razon para creer que un mensaje ya se vio. Esta restriccion sigue ahi.
    #
    # WHY (la clave lleva el inquilino DENTRO): `(agencia_id, cliente_id, canal,
    # id_externo)` y no `(canal, id_externo)`. Con la global, dos inquilinos que
    # recibieran el mismo identificador externo —cosa que pasa: esos
    # identificadores los numera la plataforma del canal, no nosotros— chocarian
    # entre si. Y el choque seria una FUGA: el segundo inquilino descubriria, por
    # el error, que otro ya recibio ese mensaje. Con el inquilino dentro, el
    # choque entre inquilinos distintos es imposible por construccion.
    #
    # WHY (`trabajo_id` SIN clave foranea): el trabajo se archiva y despues se
    # purga; el registro de idempotencia tiene que sobrevivirle, o el mensaje
    # volveria a entrar el dia que se purgue su trabajo. Una foranea con CASCADE
    # lo borraria y una con SET NULL le quitaria el rastro. Se guarda el
    # identificador y se documenta que puede apuntar a un trabajo ya archivado.
    """CREATE TABLE mensajes_entrantes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id  uuid NOT NULL,
    cliente_id  uuid NOT NULL,
    canal       text NOT NULL,
    id_externo  text NOT NULL,
    trabajo_id  uuid,
    recibido_en timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mensajes_entrantes_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT mensajes_entrantes_externo_key
        UNIQUE (agencia_id, cliente_id, canal, id_externo)
)""",
)

#: Con que llamada al generador se produjo cada bloque congelado:
#: (clave, generador, argumentos).
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('politica:secretos', 'politica_de_cliente', {'tabla': 'secretos'}),
    ('politica:bitacora', 'politica_de_cliente', {'tabla': 'bitacora'}),
    ('politica:trabajos', 'politica_de_cliente', {'tabla': 'trabajos'}),
    ('politica:trabajos_archivados', 'politica_de_cliente',
     {'tabla': 'trabajos_archivados'}),
    ('politica:mensajes_entrantes', 'politica_de_cliente',
     {'tabla': 'mensajes_entrantes'}),
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
        },
    }),
)

#: El SQL tal y como se aplico. CONGELADO: no lo toques para mejorarlo — si la
#: redaccion tiene que cambiar, se escribe una migracion NUEVA.
SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'politica:secretos': (
        'ALTER TABLE secretos ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE secretos FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON secretos
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
    'politica:bitacora': (
        'ALTER TABLE bitacora ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE bitacora FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON bitacora
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
    'politica:trabajos': (
        'ALTER TABLE trabajos ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE trabajos FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON trabajos
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
    'politica:trabajos_archivados': (
        'ALTER TABLE trabajos_archivados ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE trabajos_archivados FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON trabajos_archivados
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
    'politica:mensajes_entrantes': (
        'ALTER TABLE mensajes_entrantes ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE mensajes_entrantes FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON mensajes_entrantes
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
        'GRANT SELECT, INSERT ON bitacora, mensajes_entrantes TO heraldo_app',
        'GRANT SELECT, INSERT, DELETE ON trabajos_archivados TO heraldo_app',
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
    # forma anterior: la revision 0001 los vuelve a conceder si se baja hasta ella,
    # y dejar un `GRANT` a medias entre las dos seria peor que no tocarlos.
    op.execute("DROP TABLE IF EXISTS mensajes_entrantes")
    op.execute("DROP TABLE IF EXISTS trabajos_archivados")
    op.execute("DROP TABLE IF EXISTS trabajos")
    op.execute("DROP TABLE IF EXISTS bitacora")
    op.execute("DROP TABLE IF EXISTS secretos")
