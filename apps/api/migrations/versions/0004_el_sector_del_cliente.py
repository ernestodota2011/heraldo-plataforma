"""El sector del cliente se PERSISTE, para que el guard pueda volver a mirarlo.

Revision ID: 0004
Revises: 0003

# WHY (T-021·ter, RNF-04): hasta aqui el guard de BAA media el ALTA y nada mas. El
# sector declarado se evaluaba, decidia, y se TIRABA. La consecuencia la escribio
# el propio modulo el dia que se construyo: «si manana un cliente de comercio se
# convierte en clinica, nada lo vuelve a mirar». Un requisito que solo se comprueba
# en el instante del alta deja de cumplirse al dia siguiente **sin que nada se ponga
# rojo** — que es la peor forma de incumplir, porque hay una casilla marcada. Para
# volver a mirarlo hay que saber que se declaro: eso es esta columna.
#
# WHY (una COLUMNA en `clientes` y NO una tabla nueva de inquilino) — ==MEDIDO,
# no supuesto==: el encargo pedia una tabla de inquilino con LAS DOS claves para
# que el gate derivado del catalogo la clasificara sola. Es incompatible con la
# bateria de aislamiento de este mismo repositorio, y la contradiccion se midio
# contra este Postgres 16 antes de decidir:
#
#   - `sectores_de_cliente(agencia_id, cliente_id, sector)` con
#     `UNIQUE (agencia_id, cliente_id)` —la unica forma de que «el sector del
#     cliente» sea UNO— obliga a sembrar una fila por inquilino, porque la matriz
#     canonica de la clase *de cliente* exige que la celda
#     `sesion de cliente -> dato mio -> insercion` salga **PERMITIDO**.
#   - Con esa fila sembrada, esa misma celda devuelve
#     `ERROR: duplicate key value violates unique constraint` (SQLSTATE 23505),
#     que `_medir` no clasifica y RE-LANZA: la bateria no sale roja, sale ROTA.
#   - Las dos salidas eran peores que el problema: quitar la unicidad deja «el
#     sector vigente» ambiguo (dos filas, ningun criterio), y declarar la tabla en
#     `SIN_MATRIZ_PROPIA` la saca de la bateria que funda el producto — cambiar
#     una medida por una excepcion escrita.
#
# La columna consigue lo mismo con menos: la unicidad la impone la FORMA (una fila
# de cliente tiene un sector), no una restriccion que haya que defender; `clientes`
# ya esta gobernada por su politica de la revision 0001 y ya cuelga de la cascada;
# y el `GRANT` es de TABLA, asi que las columnas nuevas entran solas sin tocar los
# privilegios. Queda declarado como contradiccion al encargo, no como omision.
#
# WHY (`sector` es NULLABLE, y eso es fail-closed y no un descuido): los clientes
# anteriores a esta revision no tienen sector, y no hay ningun valor honesto que
# inventarles — un relleno por defecto seria una clasificacion que nadie hizo,
# escrita por una migracion, e indistinguible de una verificada. `NULL` significa
# **sector indeterminado**, y quien lo lee lo trata como lo trata el alta: se
# rechaza. La unica forma de salir de `NULL` es la misma que la de cambiarlo:
# pasar por el guard (`reverificar_sector`).
#
# WHY (`sector_verificado_en`): es CUANDO se miro por ultima vez. Sin esa fecha,
# «a quien hay que volver a mirar» no se puede contestar sobre ninguna fila y la
# re-evaluacion periodica seguiria siendo una intencion. La columna no la programa:
# la hace posible.
#
# WHY (aqui no se congela ninguna receta): esta revision no cambia ningun bloque
# GENERADO. La politica de `clientes` (revision 0001) rige la tabla entera,
# columnas nuevas incluidas, y los privilegios se concedieron a nivel de TABLA.
# `RECETAS_CONGELADAS` va vacia a proposito y declarado: un bloque que no cambio no
# se vuelve a congelar — la misma regla que aplico la 0002, que solo recongelo
# `rol:creacion`.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

#: DDL literal, igual que en el resto de revisiones: esto nunca lo genero nadie.
COLUMNAS_SQL: tuple[str, ...] = (
    "ALTER TABLE clientes ADD COLUMN sector text",
    "ALTER TABLE clientes ADD COLUMN sector_verificado_en timestamptz",
)

#: Ninguna receta generada cambia en esta revision. Ver el WHY de la cabecera.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = ()

SQL_CONGELADO: dict[str, tuple[str, ...]] = {}


def upgrade() -> None:
    for sentencia in COLUMNAS_SQL:
        op.execute(sentencia)


def downgrade() -> None:
    # Se van las dos columnas y con ellas la clasificacion persistida. No se
    # guarda una copia «por si acaso»: una clasificacion sanitaria que sobrevive a
    # su propia columna es un dato de cliente sin dueno ni gobierno.
    op.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS sector_verificado_en")
    op.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS sector")
