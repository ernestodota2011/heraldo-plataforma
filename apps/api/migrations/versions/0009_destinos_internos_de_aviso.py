"""Destinos internos de aviso: la lista declarada que el contrato de entrega exige.

Revision ID: 0009
Revises: 0005

# WHY (RF-46-bis, L-05): el plan (S4.1) fija que `entregar()` para un destinatario
# INTERNO comprueba, como PRIMER paso, «esta en la lista de destinos declarados del
# inquilino?». Esa lista no vivia en ningun requisito, entidad, ruta ni casilla —
# la comprobacion tenia dueno y la cosa contra la que comprueba no tenia ninguno.
# Con la lista vacia, TODOS los avisos de RF-16/39/45/46 se rechazarian en el
# primer paso. Esta revision crea esa lista.
#
# WHY (clase *de cliente*, LAS DOS claves): cada inquilino declara SUS propios
# destinos — a quien avisa y por que canal — y nadie mas puede leerlos ni usarlos.
# `agencia_id` Y `cliente_id` es lo que el gate derivado del catalogo exige para
# clasificarla asi (plan S3.1, K-03): con las dos, hereda la politica de dos
# niveles sin que nadie la escriba dos veces.
#
# WHY (la cascada es COMPUESTA, igual que en la revision 0003): con una foranea
# simple sobre `cliente_id`, una fila podria declarar la agencia A y colgar de un
# cliente de la agencia B — la politica de RLS no lo veria porque comprueba
# coherencia con la SESION, no con la fila padre de `clientes`. Compuesta contra
# `clientes (agencia_id, id)` cierra ese hueco a nivel de esquema, no de disciplina.
# Y con `ON DELETE CASCADE`: si el cliente se borra, sus destinos se van con el —
# la limpieza de la suite DERIVA su universo del modelo (P-12) y una tabla que no
# cuelgue de la cascada sobreviviria al borrado en silencio.
#
# WHY (`canal` es `text` con un CHECK de valores, no un `CREATE TYPE ... ENUM`):
# la misma razon que `trabajos.estado` en la revision 0003 — un tipo enum de
# Postgres es un objeto mas del esquema que el gate derivado del catalogo no mira
# hoy, y anadirle un valor exige `ALTER TYPE`, que no se puede deshacer. El CHECK
# nombra los tres canales declarables (correo, webhook_interno, mensajeria) y una
# migracion futura lo reescribe entero si hace falta un cuarto. `app.channels.
# destinos_internos.Canal` es la redaccion Python de los MISMOS tres valores — se
# mantienen sincronizados a mano porque una migracion aplicada no importa
# `app.*` (ver el WHY de mas abajo), igual que `trabajos.estado` nunca tuvo su
# StrEnum en la aplicacion.
#
# WHY (unicidad sobre las CUATRO columnas, no solo destino): el mismo destino de
# correo puede ser valido para el canal `correo` de un cliente Y, en teoria, para
# `mensajeria` del mismo cliente si algun dia una plantilla usara un identificador
# parecido — la identidad de un destino declarado es (para que canal, hacia
# donde), no solo hacia donde. Y la unicidad es POR INQUILINO: dos clientes
# distintos pueden declarar exactamente el mismo correo o el mismo webhook sin
# chocar entre si, porque `agencia_id`/`cliente_id` van dentro de la clave.
#
# WHY (no hay columna de «retirado_en» ni «retirado_por»): retirar un destino NO
# lo borra — `activo = false` — y el CUANDO y el QUIEN de ese retiro los apunta la
# bitacora (RF-10), no una columna nueva en esta tabla. Anadir esa columna
# duplicaria un dato que la bitacora ya sostiene mejor: la bitacora es de solo
# insercion (nadie la reescribe) y esta tabla, en cambio, SI se actualiza al
# reactivar un destino retirado — su `declarado_en`/`declarado_por` reflejan la
# declaracion VIGENTE, no el historial completo. El historial completo es la
# bitacora.
#
# WHY (`declarado_por` es `text` suelto, no una clave foranea a un usuario): la
# tabla de usuarios de la agencia (`usuarios_agencia`, RF-01-bis) todavia no
# existe — nace con T-101, en la ola siguiente. Una foranea hacia una tabla que no
# existe no se puede escribir hoy, y esperar a que exista bloquearia esta casilla
# por una dependencia que el propio troceo (Heraldo-12) declaro que NO tenia
# (T-118 no depende de T-101). El identificador que declaro se guarda como texto
# libre — igual que `bitacora.actor` antes de T-017-bis — y se resuelve a una
# persona el dia que esa tabla exista, sin migrar el dato.
#
# WHY (SQL CONGELADO, y por que esta migracion vuelve a congelar `rol:privilegios`
# ENTERO): igual que en 0001 y 0003, aqui no se importa `app.tenancy` — una
# migracion ya aplicada no puede cambiar de significado si alguien edita
# `politicas.py` o `rol.py` manana. `test_la_redaccion_vigente_no_diverge_del_
# generador` toma, POR CLAVE, la revision MAS RECIENTE que declara cada bloque
# congelado — asi que la receta `rol:privilegios` de esta revision tiene que
# llevar el mapa COMPLETO de privilegios (las ocho tablas anteriores mas esta),
# no solo la fila nueva: `rol.sentencias_de_privilegios` REVOCA todo y concede
# solo lo declarado (es una allowlist), asi que su salida siempre describe el
# universo entero, y la receta congelada tiene que describir ese mismo universo o
# el guard de divergencia sale rojo contra su propio generador.
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0005"
branch_labels = None
depends_on = None

#: DDL literal, como en el resto de revisiones: esto nunca lo genero nadie.
TABLAS_SQL: tuple[str, ...] = (
    # WHY (`etiqueta` y `declarado_por` son NOT NULL sin default): un destino
    # declarado sin decir A QUIEN corresponde (`etiqueta`) o QUIEN lo declaro
    # (`declarado_por`) no cumple RF-46-bis — el requisito pide «a quien, por que
    # canal», no solo un canal y una direccion sueltos.
    """CREATE TABLE destinos_de_aviso (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agencia_id    uuid NOT NULL,
    cliente_id    uuid NOT NULL,
    canal         text NOT NULL,
    destino       text NOT NULL,
    etiqueta      text NOT NULL,
    activo        boolean NOT NULL DEFAULT true,
    declarado_en  timestamptz NOT NULL DEFAULT now(),
    declarado_por text NOT NULL,
    CONSTRAINT destinos_de_aviso_cliente_fkey
        FOREIGN KEY (agencia_id, cliente_id)
        REFERENCES clientes (agencia_id, id) ON DELETE CASCADE,
    CONSTRAINT destinos_de_aviso_canal_check
        CHECK (canal IN ('correo', 'webhook_interno', 'mensajeria')),
    CONSTRAINT destinos_de_aviso_destino_key
        UNIQUE (agencia_id, cliente_id, canal, destino)
)""",
)

#: Con que llamada al generador se produjo cada bloque congelado:
#: (clave, generador, argumentos).
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = (
    ('politica:destinos_de_aviso', 'politica_de_cliente', {'tabla': 'destinos_de_aviso'}),
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
            'destinos_de_aviso': ['SELECT', 'INSERT', 'UPDATE'],
        },
    }),
)

#: El SQL tal y como se aplico. CONGELADO: no lo toques para mejorarlo — si la
#: redaccion tiene que cambiar, se escribe una migracion NUEVA.
SQL_CONGELADO: dict[str, tuple[str, ...]] = {
    'politica:destinos_de_aviso': (
        'ALTER TABLE destinos_de_aviso ENABLE ROW LEVEL SECURITY',
        'ALTER TABLE destinos_de_aviso FORCE ROW LEVEL SECURITY',
        """CREATE POLICY inquilino ON destinos_de_aviso
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
        'GRANT SELECT, INSERT, UPDATE ON destinos_de_aviso TO heraldo_app',
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
    # WHY (por que este DROP es la direccion segura, igual que en 0003/0004/0005):
    # al irse la tabla se van los destinos declarados por TODOS los inquilinos —
    # es perdida de datos, y es la perdida CORRECTA para deshacer esta revision:
    # sin la tabla, `exigir_destino_declarado` no tiene contra que comprobar y
    # RF-46-bis vuelve a estar sin mecanismo, que es exactamente el estado previo
    # a esta migracion. No hay una version parcial mas segura que conservar.
    #
    # Los privilegios NO se devuelven aqui a su forma anterior: la revision que
    # se baste (0005 y las que la preceden) los vuelve a conceder si se baja hasta
    # ahi, y dejar un `GRANT` a medias entre dos revisiones seria peor que no
    # tocarlos — la misma regla que ya declaro 0003.
    op.execute("DROP TABLE IF EXISTS destinos_de_aviso")
