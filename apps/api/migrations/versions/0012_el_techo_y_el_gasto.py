"""El techo de gasto, el registro de consumo y el catalogo de precios (RF-16).

Revision ID: 0012
Revises: 0005

# ==WHY (`down_revision = "0005"` salta seis numeros, y es DELIBERADO): las
# revisiones 0006-0011 no existen todavia en esta rama.== Se estan escribiendo a
# la vez en otros carriles de la misma ola —conocimiento, usos, destinos de
# aviso—, cada uno en su worktree, y ninguno ve a los demas. Cada carril reserva
# su numero y cuelga de la ultima revision que SI existe en la rama principal,
# que es la 0005; al fusionar, quien integra re-encadena las que hayan aterrizado
# antes. La alternativa —adivinar el numero del vecino— produciria dos revisiones
# con el mismo `down_revision`, que es exactamente la historia RAMIFICADA que
# D-10 prohibe. Lo levanto la revision cruzada dos veces: queda escrito aqui para
# que no haya que volver a preguntarlo.

# WHY (T-111, RF-16, CS-01): el techo es RUTA DE DINERO. Su fuente de verdad
# tiene que ser Postgres y no Redis ni la memoria de un proceso (RNF-03): con el
# estado en memoria el techo se multiplica por el numero de procesos, y con el
# estado en Redis un reinicio borra el gasto del mes. Aqui viven las tres cosas
# que el techo necesita para cortar de verdad: DONDE esta el limite, QUE se ha
# gastado, y CUANTO cuesta un mensaje segun el pais del destinatario.
#
# ==WHY (el techo es una COLUMNA de `clientes` / `agencias` y NO una tabla
# `techos`) — el encargo pedia una tabla aparte, y es incompatible con la
# bateria de aislamiento de este repositorio. La contradiccion NO se supone: la
# midio la revision 0004 contra este mismo Postgres y su cabecera la deja
# escrita.== Se repite aqui porque el encargo la volvio a pedir:
#
#   - `techos(agencia_id, cliente_id, ...)` con `UNIQUE (agencia_id, cliente_id)`
#     —la unica forma de que «el techo del cliente» sea UNO— obliga a sembrar una
#     fila por inquilino, porque la matriz canonica de la clase *de cliente* exige
#     que la celda `sesion de cliente -> dato mio -> insercion` salga PERMITIDO.
#   - Con esa fila sembrada, esa celda devuelve `duplicate key value violates
#     unique constraint` (SQLSTATE 23505), que `_medir` no clasifica y RE-LANZA:
#     la bateria no sale roja, sale ROTA.
#   - Las dos salidas eran peores que el problema: quitar la unicidad deja «el
#     techo vigente» ambiguo (dos filas, ningun criterio), y declarar la tabla en
#     `SIN_MATRIZ_PROPIA` la saca de la bateria que funda el producto.
#
# Y ademas la columna aporta algo que la tabla aparte NO tiene y que este
# requisito necesita: ==la fila del techo EXISTE SIEMPRE==. El corte atomico se
# apoya en `SELECT ... FOR UPDATE` sobre la fila del techo; con una tabla aparte,
# el techo de un cliente recien dado de alta no tendria fila que bloquear, y N
# consumos concurrentes sobre «todavia no hay fila» es exactamente el hueco por
# el que el techo deja de cortar. Con la columna, la fila del cliente ES la fila
# del techo. Queda declarado como contradiccion al encargo, no como omision.
#
# WHY (los valores por defecto salen de la spec §9 · B4, no de aqui): «techo de
# gasto por defecto de un cliente, conservador y que el cliente puede subir (la
# plataforma nunca lo baja sola)» = **20 USD/mes**; y «lo que consume la clave de
# agencia cuenta contra un techo de la agencia con la misma alarma de RF-16,
# nunca sin limite» = **100 USD/mes** para toda la actividad de desarrollo de la
# agencia (16ª I-16-04). Los dos son `NOT NULL DEFAULT`: un cliente sin techo
# seria el unico gasto sin limite del sistema, y un `NULL` que el codigo tuviera
# que interpretar acabaria interpretandose como «sin techo» el dia que alguien
# escriba el `COALESCE` al reves.
#
# WHY (`umbral_de_alarma` tambien es columna y tambien tiene defecto): la alarma
# «antes del techo» de RF-16 no se puede disparar sin una cifra. 0.80 = avisar al
# 80 %. Va junto al techo porque se lee en la MISMA lectura bloqueada: separarlos
# obligaria a dos consultas dentro del cerrojo para decidir una sola cosa.
#
# WHY (`consumos` es de SOLO INSERCION): el `GRANT` de esta revision le da a la
# aplicacion `SELECT, INSERT` y nada mas. Un registro de gasto que la aplicacion
# pueda actualizar o borrar no es un registro: es un saldo editable, y entonces
# el techo se levanta borrando filas en vez de subiendolo — que ademas dejaria
# sin rastro el acto de levantarlo. Es RF-10 aplicado al dinero.
#
# WHY (`precios_por_pais` NO lleva claves de inquilino, a proposito): el precio
# por pais lo produce LA PLATAFORMA y es el mismo para todos los inquilinos
# (plan §3.0: «Clase: no-inquilino (catalogo de plataforma)»). Darle un
# `agencia_id` seria copiar el mismo catalogo una vez por agencia y abrir la
# puerta a que dos agencias tarifen distinto el mismo mensaje. Como no tiene
# claves, el gate derivado del catalogo la clasifica *no-inquilino* y exige que
# este declarada con su motivo escrito, ademas de que la aplicacion solo pueda
# LEERLA (`test_rls_cobertura.py`).
#
# WHY (la tarificacion es por MENSAJE/PLANTILLA y no por conversacion): la
# tarificacion por conversacion esta DEPRECADA desde el 1-jul-2025 (T-004·pre,
# eje 3). Por eso la llave del catalogo es `(pais, tipo, vigente_desde)` con
# `tipo` en {utilidad, marketing, autenticacion, servicio}, y no una duracion.
#
# WHY (no se usa `--autogenerate`, D-10): las politicas, el rol y sus
# privilegios no salen de una comparacion de metadata.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0005"
branch_labels = None
depends_on = None

#: La clave unica que `heraldos` necesita para ser destino de la foranea
#: compuesta de `consumos`. La 0001 le dio `UNIQUE (agencia_id, id)` a
#: `clientes` por la misma razon; aqui hace falta el trio porque la atribucion
#: del gasto es a un heraldo DE ESE cliente, no a un heraldo cualquiera.
CLAVE_DE_HERALDOS_SQL = (
    "ALTER TABLE heraldos ADD CONSTRAINT heraldos_inquilino_id_key "
    "UNIQUE (agencia_id, cliente_id, id)"
)

#: DDL literal, como en el resto de revisiones: esto nunca lo genero nadie.
#:
#: # WHY (`gen_random_uuid` y no `serial`): a una SECUENCIA no se le aplica RLS.
#: Su contador es una fuga de VOLUMEN entre inquilinos.
TABLAS_SQL: tuple[str, ...] = (
    # ------------------------------------------------------------ el techo
    "ALTER TABLE clientes ADD COLUMN techo_usd_mes numeric(12,2) NOT NULL DEFAULT 20",
    "ALTER TABLE clientes ADD COLUMN umbral_de_alarma numeric(4,3) NOT NULL DEFAULT 0.80",
    "ALTER TABLE clientes ADD COLUMN techo_actualizado_en timestamptz",
    """ALTER TABLE clientes
    ADD CONSTRAINT clientes_techo_check CHECK (techo_usd_mes >= 0)""",
    # WHY (el umbral es una fraccion ESTRICTAMENTE mayor que 0 y como mucho 1):
    # un umbral de 0 avisaria en el primer centavo —y entonces la alarma no dice
    # nada— y uno mayor que 1 no se cruza nunca, que es una alarma apagada con
    # aspecto de encendida. Las dos formas de romperla son mudas: se cierran en
    # la base, donde ningun `if` las puede saltar.
    """ALTER TABLE clientes
    ADD CONSTRAINT clientes_umbral_check
    CHECK (umbral_de_alarma > 0 AND umbral_de_alarma <= 1)""",
    "ALTER TABLE agencias ADD COLUMN techo_usd_mes numeric(12,2) NOT NULL DEFAULT 100",
    "ALTER TABLE agencias ADD COLUMN umbral_de_alarma numeric(4,3) NOT NULL DEFAULT 0.80",
    "ALTER TABLE agencias ADD COLUMN techo_actualizado_en timestamptz",
    """ALTER TABLE agencias
    ADD CONSTRAINT agencias_techo_check CHECK (techo_usd_mes >= 0)""",
    """ALTER TABLE agencias
    ADD CONSTRAINT agencias_umbral_check
    CHECK (umbral_de_alarma > 0 AND umbral_de_alarma <= 1)""",
    # ------------------------------------------------------------ el gasto
    # WHY (`titular` es una COLUMNA y no se deduce): B4 dice que lo que consume
    # la clave de agencia cuenta contra el techo DE LA AGENCIA. Sin esta columna,
    # «cuanto lleva gastado la agencia» habria que deducirlo mirando la marca
    # `desarrollo` del cliente EN EL MOMENTO DE CONSULTAR — y esa marca puede
    # cambiar despues del gasto, con lo que la suma de un mes ya cerrado
    # cambiaria de valor a posteriori. `titular` congela quien pago, que es lo
    # que `resolver_credencial` (T-100) ya devuelve.
    #
    # WHY (`monto_usd` es `numeric(12,6)` y no `double precision`): el coste de
    # un token y el de un mensaje se cuentan en millonesimas de dolar, y una suma
    # de miles de flotantes deriva. En dinero, el tipo exacto no es un lujo.
    #
    # WHY (`heraldo_id` cuelga con clave foranea COMPUESTA y `SET NULL` de una
    # sola columna): sin la foranea, un consumo podria atribuirse al heraldo de
    # OTRO cliente y RLS no lo veria — su `WITH CHECK` gobierna `agencia_id` y
    # `cliente_id`, no la atribucion. Y `ON DELETE SET NULL (heraldo_id)` porque
    # borrar el heraldo no deshace el gasto: el dinero se gasto igual, lo unico
    # que se pierde es a quien atribuirselo. Con `CASCADE` se borraria dinero ya
    # gastado; con `SET NULL` sin lista de columnas, Postgres intentaria anular
    # tambien `agencia_id`/`cliente_id`, que son `NOT NULL`.
    """CREATE TABLE consumos (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id    uuid NOT NULL,
    cliente_id    uuid NOT NULL,
    heraldo_id    uuid,
    titular       text NOT NULL,
    concepto      text NOT NULL,
    monto_usd     numeric(12,6) NOT NULL,
    detalle       jsonb NOT NULL DEFAULT '{}'::jsonb,
    registrado_en timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT consumos_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT consumos_heraldo_fkey
        FOREIGN KEY (agencia_id, cliente_id, heraldo_id)
        REFERENCES heraldos (agencia_id, cliente_id, id)
        ON DELETE SET NULL (heraldo_id),
    CONSTRAINT consumos_titular_check CHECK (titular IN ('cliente', 'agencia')),
    CONSTRAINT consumos_concepto_check CHECK (concepto IN ('modelo', 'mensajeria')),
    CONSTRAINT consumos_monto_check CHECK (monto_usd > 0)
)""",
    # La lectura del camino caliente es «cuanto lleva ESTE cliente en ESTA
    # ventana»: los tres campos, en ese orden.
    """CREATE INDEX consumos_ventana_idx
    ON consumos (agencia_id, cliente_id, registrado_en)""",
    # Y la del techo de la agencia es «cuanto lleva la agencia entera»: sin este
    # indice esa suma recorre los consumos de todos sus clientes.
    """CREATE INDEX consumos_titular_idx
    ON consumos (agencia_id, titular, registrado_en)""",
    # ---------------------------------------------------------- los precios
    # WHY (`cargada_en` y `fuente` son obligatorias): son las dos columnas que
    # hacen posible la RAMA DEL NO de RF-16. Sin `cargada_en` no se puede
    # contestar «¿tiene mas de 30 dias?», y un catalogo que no caduca es un
    # catalogo que miente en silencio; sin `fuente` no se puede auditar de donde
    # salio una cifra que gobierna dinero (P-03: una cifra sin fuente repetida
    # cuatro veces no son cuatro confirmaciones).
    #
    # WHY (`vigente_desde` ademas de `cargada_en`): son dos fechas distintas.
    # `cargada_en` es cuando NOSOTROS lo supimos —y es la que caduca—;
    # `vigente_desde` es desde cuando la plataforma lo cobra. Un precio anunciado
    # hoy que entra en vigor el mes que viene no se puede cobrar hoy.
    """CREATE TABLE precios_por_pais (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pais          text NOT NULL,
    tipo          text NOT NULL,
    precio_usd    numeric(12,6) NOT NULL,
    vigente_desde date NOT NULL,
    fuente        text NOT NULL,
    cargada_en    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT precios_por_pais_pais_check CHECK (pais ~ '^[A-Z]{2}$'),
    CONSTRAINT precios_por_pais_tipo_check
        CHECK (tipo IN ('utilidad', 'marketing', 'autenticacion', 'servicio')),
    CONSTRAINT precios_por_pais_precio_check CHECK (precio_usd >= 0),
    CONSTRAINT precios_por_pais_vigencia_key UNIQUE (pais, tipo, vigente_desde)
)""",
    """CREATE INDEX precios_por_pais_busqueda_idx
    ON precios_por_pais (pais, tipo, vigente_desde)""",
)

#: Con que llamada al generador se produjo cada bloque congelado:
#: (clave, generador, argumentos). Un generador que el guard no sepa regenerar es
#: un FALLO, no un bloque que se salta en silencio.
#:
#: # WHY (`precios_por_pais` no tiene receta de politica): no es una tabla de
#: inquilino. No se le pone RLS porque no hay nada que filtrar — todos los
#: inquilinos ven las mismas filas, que es lo que significa «catalogo de
#: plataforma». Lo que la acota no es una politica: es que la aplicacion solo
#: tenga `SELECT`.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('politica:consumos', 'politica_de_cliente', {'tabla': 'consumos'}),
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
            'consumos': ['SELECT', 'INSERT'],
            'precios_por_pais': ['SELECT'],
        },
    }),
)

#: El SQL tal y como se aplico. CONGELADO: no lo toques para mejorarlo — si la
#: redaccion tiene que cambiar, se escribe una migracion NUEVA.
SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'politica:consumos': (
        'ALTER TABLE consumos ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE consumos FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON consumos
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
        'GRANT SELECT ON precios_por_pais TO heraldo_app',
        'GRANT SELECT, INSERT ON bitacora, consumos, mensajes_entrantes TO heraldo_app',
        'GRANT SELECT, INSERT, DELETE ON trabajos_archivados TO heraldo_app',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON agencias, clientes, heraldos, '
        'secretos, trabajos TO heraldo_app',
    ),
}


#: Lo que deshace esta revision, LITERAL y en orden. Se escribe como lista de
#: sentencias y no como un bucle con f-strings: aqui no hay ningun valor que
#: derivar, y una sentencia destructiva se lee mejor entera que interpolada — la
#: revision cruzada lo senalo, y ademas quita el `noqa` que tapaba el aviso.
DESHACER_SQL: tuple[str, ...] = (
    "DROP TABLE IF EXISTS consumos",
    "DROP TABLE IF EXISTS precios_por_pais",
    "ALTER TABLE heraldos DROP CONSTRAINT IF EXISTS heraldos_inquilino_id_key",
    "ALTER TABLE clientes DROP COLUMN IF EXISTS techo_actualizado_en",
    "ALTER TABLE clientes DROP COLUMN IF EXISTS umbral_de_alarma",
    "ALTER TABLE clientes DROP COLUMN IF EXISTS techo_usd_mes",
    "ALTER TABLE agencias DROP COLUMN IF EXISTS techo_actualizado_en",
    "ALTER TABLE agencias DROP COLUMN IF EXISTS umbral_de_alarma",
    "ALTER TABLE agencias DROP COLUMN IF EXISTS techo_usd_mes",
)


def upgrade() -> None:
    op.execute(CLAVE_DE_HERALDOS_SQL)
    for sentencia in TABLAS_SQL:
        op.execute(sentencia)
    for clave in ('politica:consumos', 'rol:privilegios'):
        for sentencia in SQL_CONGELADO[clave]:
            op.execute(sentencia)


def downgrade() -> None:
    """Se va el techo y se va el registro del gasto. Es la direccion segura.

    # WHY (cada sentencia destructiva, declarada con su motivo, como hizo la 0005
    # con su `DROP COLUMN`):
    #
    #   - `DROP TABLE consumos` — el registro de gasto solo existe desde esta
    #     revision; deshacerla es volver a un esquema donde ese gasto no se
    #     contaba. Guardarlo «por si acaso» dejaria dinero de inquilinos en una
    #     tabla que ya ninguna politica de RLS gobierna.
    #   - `DROP TABLE precios_por_pais` — el catalogo lo produce la plataforma y
    #     se vuelve a cargar del archivo versionado en el repositorio. No hay
    #     ningun dato que solo viva ahi.
    #   - `DROP COLUMN techo_usd_mes / umbral_de_alarma / techo_actualizado_en` en
    #     `clientes` y `agencias` — al irse las columnas se va el techo, y sin
    #     techo `registrar_consumo` no existe: no queda ningun camino que gaste
    #     sin limite, porque tampoco queda el camino que gasta.
    #   - `DROP CONSTRAINT heraldos_inquilino_id_key` — la clave unica se anadio
    #     SOLO para ser destino de la foranea de `consumos`, que acaba de irse.
    #
    # El orden importa: las tablas primero, porque `consumos` cuelga de las dos
    # claves que se sueltan despues.
    """
    for sentencia in DESHACER_SQL:
        op.execute(sentencia)
