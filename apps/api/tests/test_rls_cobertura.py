"""T-014-bis — Cobertura de RLS DERIVADA DEL CATALOGO, no de una lista a mano.

# WHY (C-06): la bateria de aislamiento se autorizo una vez, sobre las tablas que
# existian ENTONCES. Las fases siguientes anaden ~10 tablas de inquilino y nada
# obligaba a que llevaran `FORCE` ni prueba. El CI saldria verde — porque el
# verde no dice QUE midio (`feedback_verde_no_dice_que_midio`). Aqui la lista de
# tablas se DERIVA de `pg_class` + `pg_policies`: una tabla nueva entra sola en la
# medida, y si no encaja en su clase el CI se pone en ROJO. Olvidarlo no compila.
#
# WHY (K-03): hay TRES clases, no dos, y la clase se determina por LAS COLUMNAS
# QUE LA TABLA TIENE, no por la buena voluntad de quien la creo.
#
# WHY (L-02 / CR-03): para la clase *de agencia* la politica DEBE nombrar
# literalmente `app.alcance`. Sin ese predicado, una sesion de portal de cliente
# alcanza TODAS las filas de esa agencia — la lista de clientes, los usuarios
# operadores, las credenciales del proveedor — y el CI sale verde porque la tabla
# «encaja en su clase». Es la segunda vez que ese predicado se pierde al cerrar;
# aqui es una condicion de la prueba, no una frase de un documento.
#
# WHY (I-4A-01): no basta con que la expresion NOMBRE las cosas. `A AND B OR C`
# es `(A AND B) OR C`: la rama de exposicion pierde el predicado de agencia y la
# politica queda abierta AUNQUE mencione las tres variables. Por eso se comprueba
# la ESTRUCTURA: en el primer nivel de la expresion no puede haber un `OR`.
#
# WHY (L-20): el motivo de cada excepcion vive AQUI, en un diccionario
# `TABLA -> motivo`. Un motivo en un comentario suelto no es un artefacto: no se
# puede consultar, no se puede contar y nadie se entera cuando caduca.
#
# WHY: redactado por ALLOWLIST (`feedback_denylist_por_allowlist`). Lo que no
# esta declarado, falla. Una lista de exclusiones es una foto del dia que se
# escribio.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.tenancy.politicas import (
    COLUMNA_AGENCIA,
    COLUMNA_CLIENTE,
    NOMBRE_POLITICA,
    VARIABLE_AGENCIA,
    VARIABLE_ALCANCE,
    VARIABLE_CLIENTE,
)
from app.tenancy.rol import ROL_APLICACION, sentencias_de_creacion

CLASE_CLIENTE = "de cliente"
CLASE_AGENCIA = "de agencia"
CLASE_NO_INQUILINO = "no-inquilino"
CLASE_MEDIA_CLAVE = "media-clave"

#: UNICA excepcion admitida, con su motivo ESCRITO. Anadir una entrada aqui es
#: un acto deliberado y revisable; olvidarse de anadirla pone el CI en rojo.
ALLOWLIST_NO_INQUILINO: dict[str, str] = {
    "alembic_version": (
        "Catalogo de migraciones de Alembic. No contiene ningun dato de "
        "inquilino: solo el identificador de la revision aplicada. El rol de "
        "aplicacion no tiene NINGUN privilegio sobre ella (se comprueba abajo), "
        "asi que no es alcanzable desde la aplicacion, con RLS o sin el."
    ),
}

_OR_SUELTO = re.compile(r"\bOR\b", re.IGNORECASE)

#: Atributos del rol que estas pruebas afirman, y la palabra que los fija en SQL.
#: UNA sola tabla gobierna las dos mitades: la lectura del catalogo y la
#: exigencia de que la migracion los nombre. Anadir una fila aqui obliga a las
#: dos cosas a la vez; no hay forma de afirmar un atributo que nadie fija.
ATRIBUTOS_DEL_ROL: dict[str, str] = {
    "rolsuper": "NOSUPERUSER",
    "rolbypassrls": "NOBYPASSRLS",
    "rolcreaterole": "NOCREATEROLE",
    "rolcreatedb": "NOCREATEDB",
    "rolinherit": "NOINHERIT",
}


# --------------------------------------------------------------------------
# Lectura del catalogo
# --------------------------------------------------------------------------
def _tablas(conexion) -> dict[str, dict]:
    filas = conexion.execute(
        text(
            """
            SELECT c.relname AS tabla,
                   c.relrowsecurity AS activada,
                   c.relforcerowsecurity AS forzada
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
    ).all()
    return {f.tabla: {"activada": f.activada, "forzada": f.forzada} for f in filas}


def _columnas(conexion) -> dict[str, set[str]]:
    """Las columnas de cada tabla ordinaria, leidas de `pg_catalog`.

    # WHY (`pg_catalog` y NO `information_schema`) — es la leccion de P-16 en el
    # OTRO sitio donde estaba: las vistas de `information_schema` estan FILTRADAS
    # POR PRIVILEGIO. Alli el defecto era vivo (el inventario de RNF-06 se encogia
    # en silencio tras un `REVOKE` y decia «se destruyen 1 filas»); aqui no lo es,
    # porque esta lectura la hace el rol MIGRADOR, que lo ve todo. Se cambia igual:
    # la CLASE de cada tabla —y con ella todo lo que este gate exige de ella— se
    # deriva de estas columnas, asi que el dia que esto se lea con un rol mas
    # estrecho una tabla desapareceria del universo y el gate saldria VERDE POR
    # AUSENCIA. ==Medido antes de tocarlo, contra Postgres 16 y el rol migrador:
    # las dos lecturas devuelven el mismo mapa (62 pares, 0 diferencias en las dos
    # direcciones)==, o sea que cerrarlo no arregla ningun defecto vivo. Se cierra
    # porque el arreglo vive SOLO en este guard y su salida es identica.
    """
    filas = conexion.execute(
        text(
            """
            SELECT c.relname AS tabla, a.attname AS columna
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
                                AND a.attnum > 0 AND NOT a.attisdropped
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            """
        )
    ).all()
    mapa: dict[str, set[str]] = {}
    for fila in filas:
        mapa.setdefault(fila.tabla, set()).add(fila.columna)
    return mapa


def _politicas(conexion) -> dict[str, list[dict]]:
    filas = conexion.execute(
        text(
            """
            SELECT tablename AS tabla, policyname AS nombre, permissive, roles,
                   cmd, qual, with_check
            FROM pg_policies
            WHERE schemaname = 'public'
            """
        )
    ).all()
    mapa: dict[str, list[dict]] = {}
    for fila in filas:
        mapa.setdefault(fila.tabla, []).append(
            {
                "nombre": fila.nombre,
                "permissive": fila.permissive,
                "roles": list(fila.roles),
                "cmd": fila.cmd,
                "qual": fila.qual,
                "with_check": fila.with_check,
            }
        )
    return mapa


def clase_de(columnas: set[str]) -> str:
    """La clase sale de LAS COLUMNAS. No hay declaracion que la contradiga."""
    tiene_agencia = COLUMNA_AGENCIA in columnas
    tiene_cliente = COLUMNA_CLIENTE in columnas
    if tiene_agencia and tiene_cliente:
        return CLASE_CLIENTE
    if tiene_agencia:
        return CLASE_AGENCIA
    if tiene_cliente:
        # Media clave declarada. No es una clase: es un defecto. Una tabla con
        # `cliente_id` y sin `agencia_id` obliga a mirar otra tabla para saber de
        # que agencia es, y la politica no puede hacerlo sin una subconsulta que
        # a su vez pasa por RLS (T-011: TODA tabla de inquilino lleva LAS DOS).
        return CLASE_MEDIA_CLAVE
    return CLASE_NO_INQUILINO


# --------------------------------------------------------------------------
# Estructura de la expresion
# --------------------------------------------------------------------------
def _cierra_al_final(texto: str) -> bool:
    """El primer parentesis, ¿casa con el ULTIMO caracter? Si no, no se pela."""
    profundidad = 0
    en_literal = False
    for indice, caracter in enumerate(texto):
        if caracter == "'":
            en_literal = not en_literal
            continue
        if en_literal:
            continue
        if caracter == "(":
            profundidad += 1
        elif caracter == ")":
            profundidad -= 1
            if profundidad == 0:
                return indice == len(texto) - 1
    return False


def _sin_parentesis_externos(expresion: str) -> str:
    texto = expresion.strip()
    while texto.startswith("(") and _cierra_al_final(texto):
        texto = texto[1:-1].strip()
    return texto


def hay_or_de_primer_nivel(expresion: str) -> bool:
    """El operador que domina la expresion, ¿es un `OR`?

    Si lo es, la politica esta rota aunque nombre todas las variables: hay una
    rama que se satisface SIN el predicado de agencia. Es el defecto I-4A-01,
    detectado por ESTRUCTURA y no por lectura humana.
    """
    texto = _sin_parentesis_externos(expresion)
    profundidad = 0
    en_literal = False
    visible: list[str] = []
    for caracter in texto:
        if caracter == "'":
            en_literal = not en_literal
            visible.append(" ")
            continue
        if en_literal:
            visible.append(" ")
            continue
        if caracter == "(":
            profundidad += 1
            visible.append(" ")
            continue
        if caracter == ")":
            profundidad -= 1
            visible.append(" ")
            continue
        visible.append(caracter if profundidad == 0 else " ")
    return bool(_OR_SUELTO.search("".join(visible)))


# --------------------------------------------------------------------------
# Fixtures derivadas
# --------------------------------------------------------------------------
@pytest.fixture
def catalogo(motor_admin) -> dict[str, dict]:
    with motor_admin.connect() as conexion:
        tablas = _tablas(conexion)
        columnas = _columnas(conexion)
        politicas = _politicas(conexion)
    return {
        tabla: {
            **datos,
            "columnas": columnas.get(tabla, set()),
            "clase": clase_de(columnas.get(tabla, set())),
            "politicas": politicas.get(tabla, []),
        }
        for tabla, datos in tablas.items()
    }


def _de_clase(catalogo: dict, clase: str) -> dict:
    return {t: d for t, d in catalogo.items() if d["clase"] == clase}


# --------------------------------------------------------------------------
# El gate
# --------------------------------------------------------------------------
def test_hay_tablas_que_medir(catalogo: dict) -> None:
    """Una medida que no encuentra nada que medir es ROJA, no verde."""
    assert catalogo, "el esquema public no tiene ninguna tabla: la migracion no corrio"
    assert _de_clase(catalogo, CLASE_CLIENTE), (
        "no hay ninguna tabla de la clase *de cliente*: la rama del gate que "
        "gobierna el aislamiento entre clientes no se estaria ejerciendo"
    )
    assert _de_clase(catalogo, CLASE_AGENCIA), (
        "no hay ninguna tabla de la clase *de agencia*: la rama que L-02 abrio y "
        "CR-03 cerro quedaria en verde POR AUSENCIA, que es como se perdio dos veces"
    )


def test_ninguna_tabla_lleva_media_clave(catalogo: dict) -> None:
    """`cliente_id` sin `agencia_id` no es una clase: es un defecto (T-011)."""
    rotas = sorted(_de_clase(catalogo, CLASE_MEDIA_CLAVE))
    assert not rotas, (
        f"tablas con {COLUMNA_CLIENTE} y sin {COLUMNA_AGENCIA}: {rotas}. Toda tabla "
        "de inquilino lleva LAS DOS claves; con media, la cascada no es expresable"
    )


def test_toda_tabla_de_inquilino_tiene_rls_forzada(catalogo: dict) -> None:
    """`FORCE`, no solo `ENABLE`: el dueño ignora sus propias politicas."""
    for tabla, datos in catalogo.items():
        if datos["clase"] == CLASE_NO_INQUILINO:
            continue
        assert datos["activada"], f"{tabla} ({datos['clase']}) no tiene RLS activada"
        assert datos["forzada"], (
            f"{tabla} ({datos['clase']}) tiene RLS activada pero NO forzada: el rol "
            "dueño de la tabla ignoraria las politicas y el aislamiento se saltaria "
            "en silencio"
        )


def test_cada_tabla_de_inquilino_tiene_exactamente_una_politica(catalogo: dict) -> None:
    """Las politicas PERMISIVAS se combinan con OR: dos son una union.

    # WHY: el resto de este archivo comprueba que CADA politica cumple su clase, y
    # eso no basta. Con RLS, varias politicas permisivas sobre la misma tabla se
    # unen con OR, asi que el acceso efectivo es la SUMA de todas — anadir una
    # segunda solo puede AMPLIAR lo que se ve, nunca reducirlo. Un gate que
    # aprueba cada politica por separado firmaria esa union sin haberla mirado.
    # Por eso la regla es «exactamente una, y con el nombre canonico»: asi la
    # politica efectiva de una tabla es la que este archivo ya sabe auditar.
    """
    for tabla, datos in catalogo.items():
        if datos["clase"] == CLASE_NO_INQUILINO:
            continue
        nombres = sorted(p["nombre"] for p in datos["politicas"])
        assert nombres == [NOMBRE_POLITICA], (
            f"{tabla} ({datos['clase']}) tiene las politicas {nombres} y deberia tener "
            f"exactamente ['{NOMBRE_POLITICA}']. Las politicas permisivas se combinan "
            "con OR: el acceso efectivo es la union de todas, y auditarlas una a una "
            "no dice nada sobre lo que la union deja pasar"
        )


def test_cada_politica_es_permisiva_para_todos_y_para_todo(catalogo: dict) -> None:
    """Una politica por comando, o acotada a un rol, parte el gobierno en trozos."""
    for tabla, datos in catalogo.items():
        if datos["clase"] == CLASE_NO_INQUILINO:
            continue
        assert datos["politicas"], f"{tabla} ({datos['clase']}) no tiene ninguna politica"
        for politica in datos["politicas"]:
            assert politica["cmd"] == "ALL", (
                f"{tabla}.{politica['nombre']} rige solo {politica['cmd']}: un comando "
                "sin politica queda fuera del mecanismo"
            )
            assert politica["permissive"] == "PERMISSIVE", (
                f"{tabla}.{politica['nombre']} es RESTRICTIVE: se combina con AND y no "
                "puede ser la unica regla de acceso"
            )
            assert "public" in politica["roles"], (
                f"{tabla}.{politica['nombre']} rige solo para {politica['roles']}: una "
                "fuga se podria 'arreglar' anadiendo un rol en vez de corregir la regla"
            )


def test_cada_politica_declara_using_y_with_check(catalogo: dict) -> None:
    """Las DOS clausulas (K-02): la lectura y la escritura se miden aparte."""
    for tabla, datos in catalogo.items():
        if datos["clase"] == CLASE_NO_INQUILINO:
            continue
        for politica in datos["politicas"]:
            assert politica["qual"], f"{tabla}.{politica['nombre']} no declara USING"
            assert politica["with_check"], (
                f"{tabla}.{politica['nombre']} no declara WITH CHECK. Postgres usaria "
                "la de USING por omision, pero entonces la condicion de esta prueba no "
                "seria comprobable sobre las dos clausulas y una politica partida por "
                "comando dejaria el INSERT sin gobierno"
            )


def test_la_clase_de_cliente_nombra_las_dos_claves(catalogo: dict) -> None:
    exigidos = (
        COLUMNA_AGENCIA,
        COLUMNA_CLIENTE,
        VARIABLE_AGENCIA,
        VARIABLE_CLIENTE,
        VARIABLE_ALCANCE,
    )
    for tabla, datos in _de_clase(catalogo, CLASE_CLIENTE).items():
        for politica in datos["politicas"]:
            for clausula in ("qual", "with_check"):
                expresion = politica[clausula]
                for exigido in exigidos:
                    assert exigido in expresion, (
                        f"{tabla}.{politica['nombre']}.{clausula} no nombra {exigido!r}: "
                        f"{expresion}"
                    )


def test_la_clase_de_agencia_nombra_el_alcance(catalogo: dict) -> None:
    """CR-03: sin `app.alcance` la tabla queda legible por una sesion de cliente."""
    for tabla, datos in _de_clase(catalogo, CLASE_AGENCIA).items():
        assert COLUMNA_CLIENTE not in datos["columnas"], (
            f"{tabla} se clasifico *de agencia* pero tiene {COLUMNA_CLIENTE}: "
            "estaba mal clasificada"
        )
        for politica in datos["politicas"]:
            for clausula in ("qual", "with_check"):
                expresion = politica[clausula]
                assert COLUMNA_AGENCIA in expresion, (
                    f"{tabla}.{politica['nombre']}.{clausula} no nombra {COLUMNA_AGENCIA}"
                )
                assert VARIABLE_AGENCIA in expresion, (
                    f"{tabla}.{politica['nombre']}.{clausula} no nombra {VARIABLE_AGENCIA}"
                )
                assert VARIABLE_ALCANCE in expresion, (
                    f"{tabla}.{politica['nombre']}.{clausula} NO nombra "
                    f"{VARIABLE_ALCANCE}. Sin ese predicado, una sesion de portal de "
                    "cliente alcanza todas las filas de su agencia (L-02)"
                )


def test_ninguna_politica_esta_dominada_por_un_or(catalogo: dict) -> None:
    """I-4A-01: `A AND B OR C` es `(A AND B) OR C`. Se mide la ESTRUCTURA."""
    for tabla, datos in catalogo.items():
        if datos["clase"] == CLASE_NO_INQUILINO:
            continue
        for politica in datos["politicas"]:
            for clausula in ("qual", "with_check"):
                expresion = politica[clausula]
                assert not hay_or_de_primer_nivel(expresion), (
                    f"{tabla}.{politica['nombre']}.{clausula} esta dominada por un OR: "
                    "hay una rama que se satisface SIN el predicado de agencia. "
                    f"Faltan parentesis. Expresion: {expresion}"
                )


def test_toda_tabla_sin_claves_esta_en_la_allowlist_con_motivo(catalogo: dict) -> None:
    for tabla in _de_clase(catalogo, CLASE_NO_INQUILINO):
        motivo = ALLOWLIST_NO_INQUILINO.get(tabla)
        assert motivo, (
            f"{tabla} no lleva ni {COLUMNA_AGENCIA} ni {COLUMNA_CLIENTE} y no esta en "
            "ALLOWLIST_NO_INQUILINO. O es una tabla de inquilino a la que le faltan las "
            "claves, o es una excepcion que alguien tiene que justificar POR ESCRITO"
        )
        assert len(motivo.strip()) >= 40, f"el motivo de {tabla} no explica nada: {motivo!r}"


def test_la_allowlist_no_tiene_entradas_muertas(catalogo: dict) -> None:
    """Un motivo escrito para una tabla que ya no existe es un motivo que miente."""
    sobrantes = sorted(set(ALLOWLIST_NO_INQUILINO) - set(catalogo))
    assert not sobrantes, (
        f"ALLOWLIST_NO_INQUILINO justifica tablas que no existen: {sobrantes}. "
        "Una excepcion caducada tapa la siguiente"
    )


def test_el_rol_de_aplicacion_no_alcanza_las_tablas_exentas(catalogo: dict, motor_admin) -> None:
    """La excepcion se sostiene porque la aplicacion NO llega, no porque lo diga."""
    with motor_admin.connect() as conexion:
        for tabla in _de_clase(catalogo, CLASE_NO_INQUILINO):
            for verbo in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                tiene = conexion.execute(
                    text("SELECT has_table_privilege(:rol, :tabla, :verbo)"),
                    {"rol": ROL_APLICACION, "tabla": tabla, "verbo": verbo},
                ).scalar_one()
                assert not tiene, (
                    f"{ROL_APLICACION} tiene {verbo} sobre {tabla}, que esta exenta de "
                    "RLS. La exencion solo vale si la aplicacion no la alcanza"
                )


def test_la_migracion_declara_cada_atributo_que_se_afirma() -> None:
    """Cada atributo afirmado tiene que estar NOMBRADO en el `ALTER ROLE`.

    # WHY (P-10): un rol sobrevive al borrado de las tablas, a la base entera y a
    # todas las corridas anteriores. Si la sentencia no nombra un atributo, el rol
    # conserva el valor que tuviera de antes — y la lectura del catalogo sale
    # VERDE por inercia, no porque la migracion lo haya fijado. Un `ALTER ROLE`
    # que SI lo nombra lo fija venga de donde venga, y entonces la lectura del
    # catalogo mide de verdad lo que la migracion produce.
    #
    # WHY: se mira la salida del generador y no la migracion porque
    # `test_la_redaccion_vigente_no_diverge_del_generador` ya prueba que son la
    # misma cosa. Dos lecturas del mismo hecho podrian discrepar; una no.
    """
    sql = " ".join(sentencias_de_creacion())
    for columna, palabra in ATRIBUTOS_DEL_ROL.items():
        assert palabra in sql, (
            f"las pruebas afirman {columna!r} pero la creacion del rol no nombra "
            f"{palabra!r}: el atributo quedaria con el valor que el rol trajera de "
            "una corrida anterior, y la comprobacion del catalogo saldria verde "
            "sin que la migracion lo haya fijado"
        )


def test_el_rol_de_aplicacion_no_es_superusuario_ni_puede_saltarse_rls(motor_admin) -> None:
    """Sin esto, todo lo anterior es teatro (plan §3.1 punto 1)."""
    columnas = ", ".join(sorted(ATRIBUTOS_DEL_ROL))
    with motor_admin.connect() as conexion:
        fila = conexion.execute(
            text(f"SELECT {columnas} FROM pg_roles WHERE rolname = :rol"),  # noqa: S608
            {"rol": ROL_APLICACION},
        ).one_or_none()
    assert fila is not None, f"el rol {ROL_APLICACION} no existe: la migracion no lo creo"
    for columna, palabra in sorted(ATRIBUTOS_DEL_ROL.items()):
        assert not getattr(fila, columna), (
            f"el rol de aplicacion tiene {columna} activo, y deberia estar fijado por "
            f"{palabra}. Con el puesto, el aislamiento de RLS deja de ser un mecanismo: "
            "un rol que se salta las politicas las vuelve decorativas"
        )


def test_el_rol_de_aplicacion_no_es_miembro_de_ningun_otro_rol(motor_admin) -> None:
    """`NOINHERIT` desactiva la herencia automatica; esto vigila el otro lado.

    # WHY: `NOINHERIT` impide usar los privilegios heredados SIN un `SET ROLE`,
    # pero la membresia sigue existiendo y sigue siendo una puerta. Que no haya
    # ninguna es la unica lectura que no depende de interpretar bien la semantica
    # de INHERIT — y una membresia nueva no la crea este repositorio, asi que sin
    # esta comprobacion aparecerian sin que nada se enterase.
    """
    with motor_admin.connect() as conexion:
        pertenencias = conexion.execute(
            text(
                "SELECT concedente.rolname AS rol "
                "FROM pg_auth_members m "
                "JOIN pg_roles miembro ON miembro.oid = m.member "
                "JOIN pg_roles concedente ON concedente.oid = m.roleid "
                "WHERE miembro.rolname = :rol"
            ),
            {"rol": ROL_APLICACION},
        ).scalars().all()
    assert not pertenencias, (
        f"{ROL_APLICACION} es miembro de {sorted(pertenencias)}: por ahi entran "
        "privilegios que ninguna migracion de este repositorio concedio"
    )


def test_el_rol_de_aplicacion_no_es_dueno_de_ninguna_tabla(catalogo: dict, motor_admin) -> None:
    """El dueño puede hacer `ALTER TABLE ... NO FORCE` sobre lo que lo gobierna."""
    with motor_admin.connect() as conexion:
        duenos = dict(
            conexion.execute(
                text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).all()
        )
    for tabla in catalogo:
        assert duenos.get(tabla) != ROL_APLICACION, (
            f"{ROL_APLICACION} es DUEÑO de {tabla}: podria desactivar el FORCE que lo "
            "gobierna, y entonces nada de esto seria un mecanismo"
        )


# --------------------------------------------------------------------------
# El punto ciego del gate: lo que NO es una tabla ordinaria
# --------------------------------------------------------------------------
#: Vistas y vistas materializadas admitidas, con su motivo ESCRITO. Vacio a
#: proposito: hoy no hay ninguna, y el mecanismo existe para que la primera que
#: aparezca sea una DECISION y no un descuido.
ALLOWLIST_VISTAS: dict[str, str] = {}


def test_no_hay_vistas_que_esquiven_el_mecanismo(motor_admin) -> None:
    """Una vista sobre una tabla de inquilino puede saltarse su RLS entera.

    # WHY: el resto de este archivo mide `relkind = 'r'` — tablas ordinarias. Una
    # VISTA es `'v'` y una MATERIALIZADA es `'m'`: las dos caian fuera de la
    # medida, y las dos son caminos reales a los datos.
    #   - Una vista sin `security_invoker = true` evalua los permisos y las
    #     politicas de las tablas de abajo como su DUEÑO, no como quien consulta.
    #     Nuestras migraciones las corre el rol migrador; si ese rol es
    #     superusuario —lo es en el CI y suele serlo en un despliegue— la RLS de
    #     abajo queda SALTADA y la vista es una fuga completa.
    #   - Una vista MATERIALIZADA es peor: la RLS no se le aplica en absoluto y
    #     ademas guarda una copia fisica de las filas.
    # Redactado por allowlist: la primera vista que alguien anada pone el CI en
    # rojo hasta que se declare por que es segura.
    """
    with motor_admin.connect() as conexion:
        encontradas = conexion.execute(
            text(
                """
                SELECT c.relname AS nombre, c.relkind AS tipo, c.reloptions AS opciones
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
                ORDER BY c.relname
                """
            )
        ).all()

    for vista in encontradas:
        motivo = ALLOWLIST_VISTAS.get(vista.nombre)
        assert motivo and len(motivo.strip()) >= 40, (
            f"{vista.nombre} es una vista ({vista.tipo}) sobre el esquema public y no "
            "esta en ALLOWLIST_VISTAS con motivo escrito. Una vista puede evaluar la "
            "RLS de las tablas de abajo como su dueño y saltarsela entera"
        )
        assert vista.tipo != "m", (
            f"{vista.nombre} es una vista MATERIALIZADA: la RLS no se le aplica y "
            "guarda una copia fisica de las filas. No hay motivo que la haga segura"
        )
        opciones = list(vista.opciones or [])
        assert "security_invoker=true" in opciones, (
            f"{vista.nombre} no declara `security_invoker = true` ({opciones}): "
            "evaluaria las politicas como su dueño, no como quien consulta"
        )


#: Secuencias admitidas, con su motivo ESCRITO. Vacio: el modelo usa uuid, asi
#: que hoy no hay ninguna. El mecanismo existe para que la primera sea una
#: decision y no un descuido — igual que con las vistas.
ALLOWLIST_SECUENCIAS: dict[str, str] = {}


def test_ninguna_secuencia_alcanzable_sin_declarar(motor_admin) -> None:
    """A una secuencia NO se le aplica RLS. Ninguna, aqui, es alcanzable.

    # WHY: cerrar el punto ciego de las vistas (P-08) dejo la pregunta a medias.
    # `relkind = 'S'` es el tercer tipo de objeto que el rol de aplicacion podria
    # tocar, y las politicas de fila no le aplican: quien puede leer una secuencia
    # ve su contador, que es una fuga de VOLUMEN entre inquilinos (cuantas filas
    # se crearon) aunque no de contenido. Hoy no existe ninguna —el modelo usa
    # uuid, a proposito— y por eso esta comprobacion es una PROHIBICION, no una
    # medida: si alguien introduce una columna `serial`, aparece la secuencia y el
    # CI se pone en rojo hasta que se declare por que es segura.
    """
    with motor_admin.connect() as conexion:
        secuencias = conexion.execute(
            text(
                "SELECT c.relname AS nombre FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'S' ORDER BY 1"
            )
        ).scalars().all()
        for nombre in secuencias:
            motivo = ALLOWLIST_SECUENCIAS.get(nombre)
            assert motivo and len(motivo.strip()) >= 40, (
                f"{nombre} es una secuencia del esquema public y no esta en "
                "ALLOWLIST_SECUENCIAS con motivo escrito. A una secuencia no se le "
                "aplica RLS: su contador es visible para quien tenga privilegio"
            )
            tiene = conexion.execute(
                text("SELECT has_sequence_privilege(:rol, :seq, 'USAGE')"),
                {"rol": ROL_APLICACION, "seq": nombre},
            ).scalar_one()
            assert not tiene, (
                f"{ROL_APLICACION} alcanza la secuencia {nombre}, que RLS no gobierna"
            )


def test_el_rol_de_aplicacion_no_puede_crear_en_el_esquema(motor_admin) -> None:
    """Sin CREATE no puede fabricarse una tabla fuera del gobierno del gate.

    # WHY: todo este archivo mide las tablas que EXISTEN. Un rol con `CREATE`
    # sobre `public` puede crear una tabla nueva en tiempo de ejecucion, sin
    # migracion y sin politica — y el gate solo la veria en la corrida siguiente,
    # si alguien vuelve a mirar. La migracion revoca ese privilegio; esto
    # comprueba que la revocacion surtio efecto, en vez de darla por hecha.
    """
    with motor_admin.connect() as conexion:
        puede = conexion.execute(
            text("SELECT has_schema_privilege(:rol, 'public', 'CREATE')"),
            {"rol": ROL_APLICACION},
        ).scalar_one()
        usa = conexion.execute(
            text("SELECT has_schema_privilege(:rol, 'public', 'USAGE')"),
            {"rol": ROL_APLICACION},
        ).scalar_one()
    assert not puede, (
        f"{ROL_APLICACION} tiene CREATE sobre el esquema public: podria fabricar una "
        "tabla sin migracion y sin politica, fuera de lo que este gate audita"
    )
    assert usa, (
        f"{ROL_APLICACION} no tiene USAGE sobre public: es el control de la asercion "
        "anterior — si tampoco tuviera USAGE, el 'no puede CREATE' seria trivial"
    )
