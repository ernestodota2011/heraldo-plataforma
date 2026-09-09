"""La marca `desarrollo` del cliente: la UNICA puerta a la clave de agencia (B4).

Revision ID: 0005
Revises: 0004

# WHY (T-100, RF-18, decision B4): BYOK dice que el costo del modelo lo paga siempre
# el dueño de la credencial. La unica excepcion declarada es el DESARROLLO: un alta
# operada por la propia agencia —sin version publicada, sin datos de personas
# reales, sin numero real de canal— puede consumir la clave de agencia que ya
# existe en el manojo, y ese consumo cuenta contra el techo DE LA AGENCIA, no
# contra el de ningun cliente. Para que «ningun heraldo de un cliente real consume
# la clave de agencia» sea un mecanismo y no una frase, la marca tiene que estar
# EN LA FILA del cliente: es lo que `resolver_credencial` mira antes de decidir.
#
# WHY (una COLUMNA en `clientes`, igual que `sector` en la 0004): la marca es UNA
# por cliente, la unicidad la impone la forma, `clientes` ya cuelga de la cascada
# y su politica de la 0001 gobierna la tabla entera; el `GRANT` es de tabla, asi
# que la columna nueva entra sin tocar privilegios. La 0004 midio por que una
# tabla aparte rompe la bateria de aislamiento; aqui no se repite la medida, se
# hereda la decision.
#
# WHY (`NOT NULL DEFAULT false`, y esto SI es fail-closed): a diferencia del
# sector, aqui el valor por defecto es honesto. Un cliente del que nadie dijo
# «es de desarrollo» es un cliente real, y un cliente real no toca la clave de
# agencia. Que la ausencia de la marca signifique «no» es exactamente la direccion
# en la que el defecto no cuesta dinero de nadie: si alguien olvida marcar un alta
# de desarrollo, el heraldo se queda sin credencial y lo dice; si la marca
# defaulteara a `true`, un alta real olvidada gastaria la clave de la agencia en
# silencio. La rama peligrosa es la que no se ve, y la que no se ve es la segunda.
#
# WHY (aqui no se congela ninguna receta): la revision no cambia ningun bloque
# GENERADO. `RECETAS_CONGELADAS` va vacia y declarado, como en la 0004.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

#: DDL literal, como en el resto de revisiones: esto nunca lo genero nadie.
COLUMNAS_SQL: tuple[str, ...] = (
    "ALTER TABLE clientes ADD COLUMN desarrollo boolean NOT NULL DEFAULT false",
)

#: Ninguna receta generada cambia en esta revision. Ver el WHY de la cabecera.
RECETAS_CONGELADAS: tuple[tuple[str, str, dict], ...] = ()

SQL_CONGELADO: dict[str, tuple[str, ...]] = {}


def upgrade() -> None:
    for sentencia in COLUMNAS_SQL:
        op.execute(sentencia)


def downgrade() -> None:
    # Al irse la columna se va la excepcion: todo cliente vuelve a ser real y
    # ninguno puede consumir la clave de agencia. Es la direccion segura.
    op.execute("ALTER TABLE clientes DROP COLUMN IF EXISTS desarrollo")
