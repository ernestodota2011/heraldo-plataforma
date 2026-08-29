"""T-016 (RF-09, CE-06) — el secreto no sale. Medido, no prometido.

Las sondas de este archivo contestan a cinco preguntas distintas, y cada una
tiene su control:

1. ¿Lo que hay en la base es texto cifrado, y no el valor? · control: descifra.
2. ¿Un texto cifrado movido a otro inquilino sirve de algo? · control: en el suyo si.
3. ¿El serializador deja salir el material cifrado? · control: lo publico si sale.
4. ==¿Y un campo NUEVO que nadie declaro?== · es el control de que es ALLOWLIST.
5. ¿El barrido encuentra algo en ALGUNA respuesta? (CE-06) · sobre todas las tablas.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.tenancy import sesion_de_inquilino
from app.tenancy.secrets import (
    CAMPOS_PUBLICOS,
    VARIABLE_DE_ENTORNO_CLAVE,
    CampoDeclaradoQueNoLlega,
    ClaveInvalida,
    ClaveNoDeclarada,
    RecursoNoDeclarado,
    SecretoEnClaro,
    SecretoEnLaRespuesta,
    SecretoNoDescifrable,
    a_json,
    barrer,
    clave_de_cifrado,
    descifrar,
    genera_clave,
    guardar,
    leer,
    serializar,
)
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    CLIENTE_A2,
    NOMBRE_DEL_SECRETO_SEMBRADO,
    RAIZ,
    resembrar,
    sesion_de_agencia,
    sesion_de_cliente,
    valor_sembrado_del_secreto,
)
from test_rls_cobertura import CLASE_NO_INQUILINO, clase_de

MODULO_DE_SECRETOS = RAIZ / "apps" / "api" / "app" / "tenancy" / "secrets.py"


@pytest.fixture(autouse=True)
def escenario_intacto(motor_de_siembra) -> None:
    """Este modulo ESCRIBE secretos. Cada sonda arranca del mismo escenario."""
    resembrar(motor_de_siembra)


# --------------------------------------------------------------------------
# 1 y 2 — cifrado en reposo, atado al inquilino
# --------------------------------------------------------------------------
async def test_lo_que_queda_en_la_base_no_contiene_el_valor(motor, motor_admin) -> None:
    """RF-09: en reposo hay texto cifrado. El valor en claro NO esta en la fila."""
    valor = "clave-del-proveedor-que-no-debe-verse"
    clave = clave_de_cifrado()
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await guardar(
            conexion, inquilino, nombre="prueba", valor=SecretoEnClaro(valor), clave=clave
        )

    with motor_admin.connect() as conexion:
        crudo = conexion.execute(
            text("SELECT cifrado FROM secretos WHERE nombre = 'prueba'")
        ).scalar_one()

    assert valor.encode("utf-8") not in bytes(crudo), (
        "el valor en claro aparece TAL CUAL dentro de la columna cifrada: no se "
        "cifro nada, se guardo el secreto con otro nombre"
    )


async def test_control_el_secreto_guardado_se_puede_volver_a_leer(motor) -> None:
    """Control de la sonda anterior: un cifrado que nadie puede descifrar no sirve."""
    valor = "clave-del-proveedor-que-no-debe-verse"
    clave = clave_de_cifrado()
    inquilino = sesion_de_cliente(AGENCIA_A, CLIENTE_A1)
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        await guardar(
            conexion, inquilino, nombre="prueba", valor=SecretoEnClaro(valor), clave=clave
        )
    async with sesion_de_inquilino(motor, inquilino) as conexion:
        leido = await leer(conexion, inquilino, nombre="prueba", clave=clave)
    assert leido == SecretoEnClaro(valor)


def test_un_texto_cifrado_movido_a_otro_inquilino_no_descifra(motor_admin) -> None:
    """El aislamiento tambien lo sostiene la criptografia, no solo la politica.

    # WHY: RLS impide LLEGAR a la fila del vecino. Esto mide el escalon siguiente:
    # aunque alguien consiguiera el material —un volcado, un fallo de politica, una
    # copia de seguridad mal guardada— seguiria sin poder leerlo como si fuera suyo,
    # porque el inquilino va DENTRO del material autenticado.
    """
    clave = clave_de_cifrado()
    with motor_admin.connect() as conexion:
        material = conexion.execute(
            text("SELECT cifrado FROM secretos WHERE cliente_id = :c"), {"c": CLIENTE_A1}
        ).scalar_one()

    with pytest.raises(SecretoNoDescifrable) as capturado:
        descifrar(
            clave,
            agencia_id=AGENCIA_A,
            cliente_id=CLIENTE_A2,  # el VECINO, misma agencia
            nombre=NOMBRE_DEL_SECRETO_SEMBRADO,
            cifrado=material,
        )

    # RF-09 «ni siquiera al rechazarlo»: el error no cita ni el valor ni el material.
    mensaje = str(capturado.value)
    assert valor_sembrado_del_secreto("A1") not in mensaje
    assert bytes(material).hex()[:16] not in mensaje


def test_control_en_su_propio_inquilino_el_mismo_material_si_descifra(motor_admin) -> None:
    """Sin este control, un `descifrar` que fallara SIEMPRE pasaria la sonda de arriba."""
    clave = clave_de_cifrado()
    with motor_admin.connect() as conexion:
        material = conexion.execute(
            text("SELECT cifrado FROM secretos WHERE cliente_id = :c"), {"c": CLIENTE_A1}
        ).scalar_one()
    recuperado = descifrar(
        clave,
        agencia_id=AGENCIA_A,
        cliente_id=CLIENTE_A1,
        nombre=NOMBRE_DEL_SECRETO_SEMBRADO,
        cifrado=material,
    )
    assert recuperado.revelar() == valor_sembrado_del_secreto("A1")


def test_el_secreto_en_claro_no_se_enseña_ni_en_su_repr() -> None:
    """Un `print` accidental, un log, un depurador: los tres ven `[REDACTADO]`."""
    secreto = SecretoEnClaro("valor-muy-privado")
    assert repr(secreto) == "[REDACTADO]"
    assert str(secreto) == "[REDACTADO]"
    assert "valor-muy-privado" not in f"{secreto!r} {secreto} {[secreto]}"


def test_la_clave_de_cifrado_se_exige_por_entorno(monkeypatch) -> None:
    """Sin clave se FALLA; no se inventa una ni se guarda en claro «por ahora»."""
    monkeypatch.delenv(VARIABLE_DE_ENTORNO_CLAVE, raising=False)
    with pytest.raises(ClaveNoDeclarada):
        clave_de_cifrado()
    with pytest.raises(ClaveInvalida):
        clave_de_cifrado("Y29ydGE=")  # base64 valido, longitud equivocada
    # Control: una clave bien formada SI se acepta.
    assert len(clave_de_cifrado(genera_clave())) == 32


# --------------------------------------------------------------------------
# 3 y 4 — el serializador por allowlist, y su control
# --------------------------------------------------------------------------
def _fila_de_secreto_completa() -> dict[str, object]:
    """Una fila con TODAS sus columnas, incluida la del material cifrado."""
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "agencia_id": str(AGENCIA_A),
        "cliente_id": str(CLIENTE_A1),
        "nombre": NOMBRE_DEL_SECRETO_SEMBRADO,
        "creado_en": "2026-08-27T00:00:00Z",
        "actualizado_en": "2026-08-27T00:00:00Z",
        "cifrado": b"\x00material-cifrado",
    }


def test_el_serializador_no_deja_salir_el_material_cifrado() -> None:
    salida = serializar("secretos", _fila_de_secreto_completa())
    assert "cifrado" not in salida
    # Control: lo que SI es publico sale. Un serializador que no devolviera nada
    # pasaria la asercion de arriba y el producto no funcionaria.
    assert salida["nombre"] == NOMBRE_DEL_SECRETO_SEMBRADO
    assert set(salida) == set(CAMPOS_PUBLICOS["secretos"])


def test_un_campo_nuevo_que_nadie_declaro_tampoco_sale() -> None:
    """==Esta es la prueba de que es ALLOWLIST y no lista de exclusiones.==

    # WHY: si el serializador funcionara por denylist, el campo secreto de manana
    # —uno que hoy no existe y que nadie ha metido en ninguna lista— saldria. Aqui
    # se le pasan dos campos inventados, con pinta de credencial, y no aparecen: no
    # porque esten prohibidos, sino porque nadie los permitio.
    """
    fila = _fila_de_secreto_completa() | {
        "token_del_proveedor": "tk_vivo_1234567890",
        "cookie_de_sesion": "sess_abcdef",
    }
    salida = serializar("secretos", fila)
    texto = json.dumps(salida, default=str)
    assert "tk_vivo_1234567890" not in texto
    assert "sess_abcdef" not in texto
    assert set(salida) == set(CAMPOS_PUBLICOS["secretos"])


def test_un_recurso_no_declarado_no_se_serializa_como_venga() -> None:
    with pytest.raises(RecursoNoDeclarado):
        serializar("tabla_que_nadie_declaro", {"lo_que_sea": 1})


def test_un_campo_declarado_que_la_fila_no_trae_es_un_fallo_ruidoso() -> None:
    """Si la consulta deja de proyectarlo o la columna se renombra, ROJO."""
    fila = _fila_de_secreto_completa()
    del fila["nombre"]
    with pytest.raises(CampoDeclaradoQueNoLlega):
        serializar("secretos", fila)


def test_el_barrido_aborta_ante_bytes_y_ante_un_secreto_descifrado() -> None:
    """El barrido rechaza por TIPO: no necesita saber de que columna viene."""
    with pytest.raises(SecretoEnLaRespuesta):
        barrer({"datos": {"lista": [1, 2, b"material"]}})
    with pytest.raises(SecretoEnLaRespuesta):
        barrer({"datos": [SecretoEnClaro("valor")]})
    # Control: una respuesta limpia pasa entera y no se deforma.
    limpia = {"a": 1, "b": ["x", None], "c": {"d": 2.5}}
    assert barrer(limpia) == limpia


# --------------------------------------------------------------------------
# 5 — CE-06: el barrido AUTOMATICO sobre TODAS las respuestas
# --------------------------------------------------------------------------
async def test_ningun_secreto_almacenado_aparece_en_ninguna_respuesta(
    motor, motor_admin, catalogo_de_tablas
) -> None:
    """CE-06 medido: se recorren TODAS las tablas de inquilino, no tres elegidas.

    Por cada tabla se lee una fila real por el camino de sesion de produccion, se
    serializa por su declaracion y se comprueba que en el texto resultante no
    aparece ni el valor en claro de ningun secreto sembrado ni un solo byte de su
    material cifrado.

    # WHY (derivado del catalogo): «todas las respuestas» solo es cierto si el
    # universo lo da la base y no una lista escrita a mano. La tabla numero nueve
    # entra sola en esta medida.
    """
    with motor_admin.connect() as conexion:
        materiales = [
            bytes(f.cifrado).hex()
            for f in conexion.execute(text("SELECT cifrado FROM secretos")).all()
        ]
    valores = [valor_sembrado_del_secreto(etiqueta) for etiqueta in ("A1", "A2", "B1")]
    assert materiales and valores, "no hay secretos sembrados: esta sonda no mediria nada"

    de_inquilino = [
        tabla
        for tabla, columnas in sorted(catalogo_de_tablas.items())
        if clase_de(columnas) != CLASE_NO_INQUILINO
    ]
    assert de_inquilino, "no hay tablas de inquilino que barrer"

    operador = sesion_de_agencia(AGENCIA_A)
    tablas_con_filas = 0
    for tabla in de_inquilino:
        declarados = CAMPOS_PUBLICOS.get(tabla)
        assert declarados, (
            f"la tabla {tabla!r} no declara sus campos publicos en CAMPOS_PUBLICOS. "
            "Sin declaracion no se puede serializar, y lo que no se puede serializar "
            "acaba saliendo 'como venga'"
        )
        columnas = ", ".join(sorted(declarados))
        async with sesion_de_inquilino(motor, operador) as conexion:
            # S608: `tabla` y `columnas` salen del catalogo de Postgres y de una
            # declaracion del propio producto. Aqui no llega entrada de usuario.
            filas = (await conexion.execute(text(f"SELECT {columnas} FROM {tabla}"))).all()  # noqa: S608
        assert filas, (
            f"{tabla} esta vacia para el operador de la agencia A: barrer una tabla sin "
            "filas es un verde que no midio nada"
        )
        tablas_con_filas += 1
        for fila in filas:
            texto = a_json(serializar(tabla, dict(fila._mapping)))
            for valor in valores:
                assert valor not in texto, (
                    f"la respuesta de {tabla!r} lleva el valor EN CLARO de un secreto"
                )
            for material in materiales:
                assert material not in texto, (
                    f"la respuesta de {tabla!r} lleva el material cifrado de un secreto"
                )

    assert tablas_con_filas == len(de_inquilino)


def test_todo_recurso_del_esquema_declara_sus_campos_publicos(catalogo_de_tablas) -> None:
    """La declaracion se compara con el CATALOGO: sobrar tambien es un defecto."""
    de_inquilino = {
        tabla
        for tabla, columnas in catalogo_de_tablas.items()
        if clase_de(columnas) != CLASE_NO_INQUILINO
    }
    faltan = sorted(de_inquilino - set(CAMPOS_PUBLICOS))
    sobran = sorted(set(CAMPOS_PUBLICOS) - de_inquilino)
    assert not faltan, f"tablas de inquilino sin campos publicos declarados: {faltan}"
    assert not sobran, (
        f"CAMPOS_PUBLICOS declara recursos que no existen en el esquema: {sobran}. "
        "Una declaracion caducada tapa a la siguiente"
    )


def test_los_campos_declarados_publicos_existen_de_verdad(catalogo_de_tablas) -> None:
    """Un campo publico que no es columna de nada seria una declaracion que miente."""
    for tabla, campos in sorted(CAMPOS_PUBLICOS.items()):
        columnas = catalogo_de_tablas.get(tabla, set())
        fantasmas = sorted(set(campos) - columnas)
        assert not fantasmas, (
            f"{tabla} declara publicos campos que no tiene: {fantasmas}. O la columna "
            "se renombro, o la declaracion se copio de otra tabla"
        )


# --------------------------------------------------------------------------
# El guard estructural: el serializador NO conoce los campos de secreto
# --------------------------------------------------------------------------
_FUNCIONES_QUE_NO_PUEDEN_NOMBRAR_COLUMNAS = ("serializar", "barrer", "_valor_publico", "a_json")


def test_el_serializador_no_nombra_ninguna_columna_del_esquema(catalogo_de_tablas) -> None:
    """La propiedad «no conoce los campos de secreto», convertida en un rojo del CI.

    # WHY: el diseño se puede perder de una forma concretisima — alguien escribe
    # `if campo == "cifrado": continue` dentro del serializador y vuelve a ser una
    # denylist con otro nombre, con el mismo defecto que tenia el referente. Aqui
    # se recorre el arbol de esas funciones y se exige que NINGUNA de sus cadenas
    # literales sea el nombre de una columna del esquema. Las declaraciones de
    # campos publicos viven fuera de ellas, en un diccionario, que es justo la
    # diferencia entre «conocer los campos» y «leer una declaracion».
    """
    columnas = {columna for campos in catalogo_de_tablas.values() for columna in campos}
    assert columnas, "el catalogo salio vacio: este guard pasaria por no tener nada que mirar"

    arbol = ast.parse(MODULO_DE_SECRETOS.read_text(encoding="utf-8"))
    vistas: list[str] = []
    culpables: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if nodo.name not in _FUNCIONES_QUE_NO_PUEDEN_NOMBRAR_COLUMNAS:
            continue
        vistas.append(nodo.name)
        for hijo in ast.walk(nodo):
            if (
                isinstance(hijo, ast.Constant)
                and isinstance(hijo.value, str)
                and hijo.value in columnas
            ):
                culpables.append(f"{nodo.name}:{hijo.lineno} -> {hijo.value!r}")

    assert sorted(vistas) == sorted(_FUNCIONES_QUE_NO_PUEDEN_NOMBRAR_COLUMNAS), (
        f"el guard solo encontro {sorted(vistas)} y tenia que auditar "
        f"{sorted(_FUNCIONES_QUE_NO_PUEDEN_NOMBRAR_COLUMNAS)}. Si una funcion se "
        "renombra, este guard saldria verde sin haber mirado nada"
    )
    assert not culpables, (
        f"el serializador nombra columnas del esquema: {culpables}. En cuanto nombra "
        "una, deja de ser una allowlist derivada de una declaracion y pasa a ser una "
        "lista de casos particulares — que es una FOTO del dia que se escribio"
    )


def test_el_guard_estructural_encontraria_una_denylist_metida_a_mano(tmp_path: Path) -> None:
    """El control del guard de arriba: se le da una version saboteada y la caza.

    # WHY (`feedback_sabotaje_audita_al_test`): un guard estructural que no se
    # prueba contra el defecto que dice cazar puede estar mirando el arbol
    # equivocado y salir verde para siempre. Aqui se compila a proposito una
    # funcion `serializar` que SI nombra una columna, y se comprueba que el mismo
    # recorrido la encuentra.
    """
    saboteado = tmp_path / "saboteado.py"
    saboteado.write_text(
        "def serializar(recurso, fila):\n"
        "    return {c: v for c, v in fila.items() if c != 'cifrado'}\n",
        encoding="utf-8",
    )
    arbol = ast.parse(saboteado.read_text(encoding="utf-8"))
    encontrados = [
        hijo.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "serializar"
        for hijo in ast.walk(nodo)
        if isinstance(hijo, ast.Constant) and isinstance(hijo.value, str)
    ]
    assert "cifrado" in encontrados, (
        "el recorrido del guard no encuentra una columna nombrada dentro de "
        "`serializar` ni cuando la ponemos a proposito: estaria mirando otro arbol"
    )
