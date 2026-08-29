"""La UNICA redaccion de las politicas de RLS de Heraldo.

# WHY: plan §3.1 punto 4 — «una sola, para que no haya dos redacciones que
# diverjan». Aqui vive esa redaccion. Las migraciones la piden; nadie escribe la
# expresion a mano en dos sitios.
#
# WHY: hay TRES clases de tabla, no dos (K-03), y la clase se determina por LAS
# COLUMNAS QUE LA TABLA TIENE, no por la buena voluntad de quien la creo:
#   - de cliente    -> `agencia_id` Y `cliente_id`  -> `expresion_de_cliente()`
#   - de agencia    -> `agencia_id`, sin `cliente_id` -> `expresion_de_agencia()`
#   - no-inquilino  -> ninguna de las dos           -> allowlist con motivo
#
# WHY (L-02 / I-4A-01): la expresion de la clase *de agencia* DEBE nombrar
# `app.alcance`. Sin ese predicado, una sesion de portal de cliente alcanza
# TODAS las filas de esa agencia — la lista completa de clientes, los usuarios
# operadores y las credenciales del proveedor — y el CI sale verde porque la
# tabla «encaja en su clase». Y cuando la tabla expone la fila propia del
# cliente, la rama va PARENTIZADA: sin parentesis, `A AND B OR C` es
# `(A AND B) OR C` y la rama de exposicion pierde el predicado de agencia.
#
# WHY: se emite `WITH CHECK` explicito aunque Postgres documente que al omitirlo
# en una politica `FOR ALL` se usa la de `USING` tambien para las filas nuevas
# (K-02). Se escribe igual por tres razones: hace explicita la intencion,
# protege si manana alguien parte la politica en una por comando, y hace que la
# condicion del test sea comprobable sobre LAS DOS clausulas.
"""

from __future__ import annotations

import re

#: Columna que marca el primer nivel de la cascada.
COLUMNA_AGENCIA = "agencia_id"
#: Columna que marca el segundo nivel de la cascada.
COLUMNA_CLIENTE = "cliente_id"

#: Las tres variables de sesion. Se declaran con `SET LOCAL`, siempre las tres.
VARIABLE_AGENCIA = "app.agencia_id"
VARIABLE_CLIENTE = "app.cliente_id"
VARIABLE_ALCANCE = "app.alcance"

#: El alcance que ve mas de un cliente. El otro (`cliente`) no necesita nombre
#: aqui: la expresion lo trata como «todo lo que no es agencia».
ALCANCE_AGENCIA = "agencia"

#: Nombre uniforme de la politica en toda tabla de inquilino.
NOMBRE_POLITICA = "inquilino"

# Identificadores permitidos. Nada que no case con esto entra en una sentencia:
# es lo que hace que las f-strings de abajo no sean una superficie de inyeccion.
_IDENTIFICADOR = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class IdentificadorInvalido(ValueError):
    """Un nombre de tabla o columna que no se puede interpolar con seguridad."""


def valida_identificador(nombre: str) -> str:
    if not _IDENTIFICADOR.match(nombre):
        raise IdentificadorInvalido(
            f"{nombre!r} no es un identificador SQL admitido "
            "(minusculas, digitos y guion bajo, hasta 63 caracteres)"
        )
    return nombre


def _predicado_de_agencia() -> str:
    """El predicado que NUNCA se puede perder: la fila es de mi agencia."""
    return f"{COLUMNA_AGENCIA} = current_setting('{VARIABLE_AGENCIA}')::uuid"


def expresion_de_cliente() -> str:
    """Politica de una tabla de la clase *de cliente* (plan §3.1 punto 4)."""
    return (
        f"{_predicado_de_agencia()}\n"
        f"      AND ( current_setting('{VARIABLE_ALCANCE}') = '{ALCANCE_AGENCIA}'\n"
        f"            OR {COLUMNA_CLIENTE} = current_setting('{VARIABLE_CLIENTE}')::uuid )"
    )


def expresion_de_agencia(columna_propia: str | None = None) -> str:
    """Politica de una tabla de la clase *de agencia*.

    Sin `columna_propia`, la tabla es INVISIBLE al alcance `cliente` — que es el
    comportamiento por defecto: «la exposicion se declara, la ocultacion es el
    defecto» (L-02). Con `columna_propia`, la tabla expone al cliente su propia
    fila, y la rama va parentizada.
    """
    if columna_propia is None:
        return (
            f"{_predicado_de_agencia()}\n"
            f"      AND current_setting('{VARIABLE_ALCANCE}') = '{ALCANCE_AGENCIA}'"
        )
    valida_identificador(columna_propia)
    return (
        f"{_predicado_de_agencia()}\n"
        f"      AND ( current_setting('{VARIABLE_ALCANCE}') = '{ALCANCE_AGENCIA}'\n"
        f"            OR {columna_propia} = current_setting('{VARIABLE_CLIENTE}')::uuid )"
    )


def sentencias_de_politica(tabla: str, expresion: str) -> list[str]:
    """Las tres sentencias que ponen una tabla bajo el mecanismo, en orden.

    `FORCE`, no solo `ENABLE`: por defecto el dueño de una tabla ignora sus
    propias politicas, y sin FORCE el aislamiento se salta en silencio en cuanto
    algo corra con el rol dueño (plan §3.1 punto 1).

    Sin clausula `TO`: la politica rige para PUBLIC. Un gobierno por rol dejaria
    la puerta a «arreglar» una fuga anadiendo un rol a la politica en vez de
    corregir la expresion.
    """
    valida_identificador(tabla)
    # S608: `tabla` pasa por `valida_identificador()` y la expresion la construye este mismo
    # modulo a partir de constantes. Aqui no llega entrada de usuario.
    return [
        f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY",  # noqa: S608
        f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY",  # noqa: S608
        (
            f"CREATE POLICY {NOMBRE_POLITICA} ON {tabla}\n"  # noqa: S608
            "    FOR ALL\n"
            f"    USING (\n      {expresion}\n    )\n"
            f"    WITH CHECK (\n      {expresion}\n    )"
        ),
    ]
