"""T-014·ter — el ALCANCE se lee de la fila del usuario, JAMAS de la peticion.

RF-01 / RF-03, plan §3.1 punto 5: «El alcance lo fija la identidad autenticada,
jamas la peticion. Una sesion de portal de cliente no puede pedir
`alcance = agencia`: el valor se lee de la fila del usuario, no de una cabecera
ni de un cuerpo. Es la diferencia entre un permiso y una sugerencia.»

Esa frase es una promesa; este archivo la convierte en una medida. Tiene tres
capas, porque la escalada se puede intentar en tres sitios distintos:

1. **En la peticion** — no hay parametro donde escribir el alcance. Se comprueba
   por FIRMA (una peticion que lo trae recibe un `TypeError`, no un permiso) y
   POR EFECTO (la sesion resultante sigue viendo solo lo del cliente).
2. **En el codigo de la aplicacion** — alguien podria saltarse la derivacion
   construyendo el inquilino a mano. Un guard estructural sobre el arbol lo
   impide: NINGUN modulo de la aplicacion llama a `Inquilino(...)` directamente.
3. **En la conexion** — alguien podria declarar `app.alcance` por su cuenta. Otro
   guard estructural: solo `app/tenancy/sesion.py` declara las variables de
   inquilino. «No existe una segunda forma de abrir conexion» deja de ser una
   frase del plan y pasa a ser algo que el CI comprueba.

# WHY (los guards estructurales, y por que no bastan las buenas intenciones): el
# docstring de `inquilino.py` afirma que «no tiene ningun constructor publico que
# acepte un alcance». La afirmacion es mas fuerte que el mecanismo: `Inquilino` es
# un dataclass y su `__init__` SI acepta `alcance` — de hecho la suite lo usa
# para probar `AlcanceInvalido`. Lo que de verdad sostiene la promesa es que
# ningun camino de la APLICACION lo use, y eso, hasta ahora, era disciplina.
# Aqui pasa a ser mecanismo: si alguien escribe ese camino, el CI se pone en rojo.
# Registrado como P-13.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.tenancy import Alcance, Inquilino, sesion_de_inquilino
from app.tenancy.inquilino import CENTINELA_SIN_CLIENTE
from app.tenancy.politicas import VARIABLE_AGENCIA, VARIABLE_ALCANCE, VARIABLE_CLIENTE
from conftest import (
    AGENCIA_A,
    CLIENTE_A1,
    RAIZ,
    sesion_de_agencia,
    sesion_de_cliente,
)

#: La peticion, tal y como llegaria por la red: texto de un tercero. Trae el
#: alcance que le da la gana. Es un DATO, nunca una instruccion.
PETICION_QUE_PIDE_MAS: dict[str, object] = {
    "alcance": "agencia",
    "cliente_id": None,
    "agencia_id": str(AGENCIA_A),
}

#: La fila del usuario autenticado: un usuario de PORTAL DE CLIENTE.
FILA_DEL_USUARIO_DE_PORTAL = {"agencia_id": AGENCIA_A, "cliente_id": CLIENTE_A1}

#: Y la del operador de la agencia. `cliente_id is None` es lo unico que produce
#: el alcance `agencia`, y esa columna la escribe el alta, no la peticion.
FILA_DEL_USUARIO_OPERADOR = {"agencia_id": AGENCIA_A, "cliente_id": None}

#: El unico modulo autorizado a declarar el inquilino en la conexion.
MODULO_DE_SESION = Path("apps") / "api" / "app" / "tenancy" / "sesion.py"

#: Arboles donde vive el codigo de la APLICACION. Los tests quedan fuera a
#: proposito: ellos SI construyen inquilinos a mano, que es como se prueba que
#: un par incoherente se rechaza.
ARBOLES_DE_APLICACION = (Path("apps"), Path("packages"))
CARPETAS_QUE_NO_SON_APLICACION = ("tests", "__pycache__", ".venv", "node_modules")


def _fuentes_de_la_aplicacion() -> list[Path]:
    encontradas: list[Path] = []
    for arbol in ARBOLES_DE_APLICACION:
        for ruta in sorted((RAIZ / arbol).rglob("*.py")):
            if any(parte in CARPETAS_QUE_NO_SON_APLICACION for parte in ruta.parts):
                continue
            encontradas.append(ruta)
    return encontradas


def test_el_guard_estructural_encuentra_fuentes_que_auditar() -> None:
    """Un guard que no mira ningun archivo pasa siempre. Es el control de los dos de abajo."""
    fuentes = _fuentes_de_la_aplicacion()
    assert len(fuentes) >= 5, (
        f"el guard estructural solo encontro {len(fuentes)} archivos de aplicacion: "
        "si la ruta se rompiera, los dos guards de abajo saldrian verdes sin haber "
        "auditado nada"
    )
    rutas = {ruta.relative_to(RAIZ).as_posix() for ruta in fuentes}
    assert MODULO_DE_SESION.as_posix() in rutas, (
        f"el guard no esta viendo {MODULO_DE_SESION.as_posix()}, que es justo el "
        "modulo cuyo monopolio se quiere comprobar"
    )
    assert "apps/api/app/tenancy/inquilino.py" in rutas


# --------------------------------------------------------------------------
# Capa 1 — la peticion no tiene donde aterrizar
# --------------------------------------------------------------------------
def test_la_derivacion_no_acepta_un_alcance_por_parametro() -> None:
    """`desde_usuario` no tiene parametro `alcance`: no hay hueco que rellenar."""
    parametros = set(inspect.signature(Inquilino.desde_usuario).parameters)
    assert parametros == {"agencia_id", "cliente_id"}, (
        f"la unica puerta de construccion acepta {sorted(parametros)}. En cuanto "
        "aparezca ahi un parametro de alcance, una peticion podra pedirlo y el "
        "permiso se convertira en una sugerencia (plan §3.1 punto 5)"
    )


def test_una_peticion_de_portal_que_pide_alcance_agencia_es_rechazada() -> None:
    """La peticion trae `alcance=agencia`; pasarla NO da un permiso, da un error."""
    assert PETICION_QUE_PIDE_MAS["alcance"] == "agencia"
    with pytest.raises(TypeError) as capturado:
        Inquilino.desde_usuario(
            agencia_id=FILA_DEL_USUARIO_DE_PORTAL["agencia_id"],
            cliente_id=FILA_DEL_USUARIO_DE_PORTAL["cliente_id"],
            alcance=PETICION_QUE_PIDE_MAS["alcance"],
        )
    assert "alcance" in str(capturado.value), (
        "el rechazo tiene que nombrar el parametro que sobra; si fallara por otra "
        f"razon estariamos midiendo otra cosa: {capturado.value}"
    )


async def test_el_alcance_derivado_gana_a_lo_que_pide_la_peticion(motor) -> None:
    """Y POR EFECTO: la sesion que sale de la fila del portal sigue viendo solo lo suyo.

    # WHY: que la peticion reciba un `TypeError` es la mitad. La otra mitad es que
    # el camino correcto —derivar de la fila— produzca de verdad el alcance
    # estrecho, y que ese alcance se note en las filas. Sin esta sonda, una
    # derivacion que devolviera `agencia` por error pasaria el test de la firma.
    """
    inquilino = Inquilino.desde_usuario(**FILA_DEL_USUARIO_DE_PORTAL)
    assert inquilino.alcance is Alcance.CLIENTE, (
        "la fila declara un cliente y aun asi salio alcance de agencia: la escalada "
        "ocurrio en la derivacion misma"
    )
    assert inquilino.cliente_id == CLIENTE_A1

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        heraldos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
        clientes = (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()
    assert (heraldos, clientes) == (1, 1), (
        f"el portal que pidio alcance de agencia acabo viendo {heraldos} heraldos y "
        f"{clientes} clientes; con su alcance real tiene que ver 1 y 1"
    )


async def test_control_el_usuario_de_agencia_si_obtiene_el_alcance_agencia(motor) -> None:
    """El control: si NADIE pudiera tener alcance de agencia, el panel no existiria.

    Una defensa que rechaza a todo el mundo no es una defensa, es un producto
    roto. Este es el par PERMITIDO de T-014·ter.
    """
    inquilino = Inquilino.desde_usuario(**FILA_DEL_USUARIO_OPERADOR)
    assert inquilino.alcance is Alcance.AGENCIA
    assert inquilino.cliente_id == CENTINELA_SIN_CLIENTE

    async with sesion_de_inquilino(motor, inquilino) as conexion:
        heraldos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
        clientes = (await conexion.execute(text("SELECT count(*) FROM clientes"))).scalar_one()
    assert (heraldos, clientes) == (2, 2), (
        f"el operador de la agencia A ve {heraldos} heraldos y {clientes} clientes; "
        "tiene que ver los dos de cada. Si sale menos, el panel esta roto (K-04)"
    )


# --------------------------------------------------------------------------
# Capa 2 — el codigo de la aplicacion no puede saltarse la derivacion
# --------------------------------------------------------------------------
def test_ningun_modulo_de_la_aplicacion_construye_un_inquilino_a_mano() -> None:
    """`Inquilino(...)` directo esquiva `desde_usuario` y con el, la derivacion.

    # WHY: `Inquilino` es un dataclass y su constructor acepta `alcance`. Un modulo
    # que escribiera `Inquilino(agencia_id=..., cliente_id=CENTINELA, alcance=AGENCIA)`
    # obtendria alcance de agencia para un usuario de portal, y el par seria
    # internamente coherente: `AlcanceInvalido` NO saltaria. La unica defensa
    # posible es que ese camino no exista en el codigo de la aplicacion — y eso se
    # comprueba, no se confia (P-13).
    """
    culpables: list[str] = []
    for ruta in _fuentes_de_la_aplicacion():
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "Inquilino"
            ):
                culpables.append(f"{ruta.relative_to(RAIZ).as_posix()}:{nodo.lineno}")
    assert not culpables, (
        f"estos sitios construyen un Inquilino directamente: {culpables}. El unico "
        "camino admitido es `Inquilino.desde_usuario(...)`, que DERIVA el alcance de "
        "la fila del usuario; el constructor lo acepta como parametro y por ahi entra "
        "la escalada"
    )


# --------------------------------------------------------------------------
# Capa 3 — nadie declara el inquilino por su cuenta
# --------------------------------------------------------------------------
def test_solo_el_modulo_de_sesion_declara_las_variables_de_inquilino() -> None:
    """«No existe una segunda forma de abrir conexion» (plan §4), comprobado.

    # WHY: el rol de aplicacion puede llamar a `set_config` — es una funcion
    # ordinaria. Un modulo despistado podria declarar `app.alcance = 'agencia'` en
    # su propia transaccion y escalar sin tocar `Inquilino` para nada. Que solo el
    # modulo de sesion lo haga es lo que hace que el aislamiento sea estructural.
    """
    autorizado = (RAIZ / MODULO_DE_SESION).resolve()
    culpables = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _fuentes_de_la_aplicacion()
        if ruta.resolve() != autorizado and "set_config" in ruta.read_text(encoding="utf-8")
    ]
    assert not culpables, (
        f"estos modulos declaran variables de sesion por su cuenta: {culpables}. La "
        f"unica forma de declarar el inquilino es {MODULO_DE_SESION.as_posix()}; una "
        "segunda devuelve el aislamiento al terreno de la disciplina"
    )


def test_el_modulo_de_sesion_declara_exactamente_las_tres_variables_del_gobierno() -> None:
    """Las que DECLARA la sesion y las que EXIGE la politica tienen que ser las mismas.

    # WHY: `sesion.py` escribe los nombres literales y `politicas.py` los guarda en
    # constantes. Son dos redacciones del mismo hecho. Si divergieran, la politica
    # leeria una variable que nadie declara y TODO abortaria — ruidoso, no
    # silencioso, pero el producto entero dejaria de funcionar. Este guard lo
    # convierte en un rojo del CI en vez de en una caida en produccion.
    """
    fuente = (RAIZ / MODULO_DE_SESION).read_text(encoding="utf-8")
    for variable in (VARIABLE_AGENCIA, VARIABLE_CLIENTE, VARIABLE_ALCANCE):
        assert f"'{variable}'" in fuente, (
            f"{MODULO_DE_SESION.as_posix()} no declara {variable!r}, que es lo que las "
            "politicas leen. Las politicas quedarian pidiendo una variable que nadie "
            "pone y toda consulta abortaria"
        )


# --------------------------------------------------------------------------
# RF-03 — media declaracion tampoco vale
# --------------------------------------------------------------------------
async def test_declarar_solo_parte_del_par_aborta_la_consulta(motor) -> None:
    """RF-03: «o con solo la mitad del par declarada» -> rechazar, no devolver todo.

    Se declaran DOS de las tres variables a proposito, saltandose la dependencia.
    Es lo que haria un modulo que copiara medio `set_config` de otro sitio.
    """
    declarar_a_medias = text(
        "SELECT set_config('app.agencia_id', :agencia, true),"
        "       set_config('app.cliente_id', :cliente, true)"
    )
    with pytest.raises(DBAPIError) as capturado:
        async with motor.begin() as conexion:
            await conexion.execute(
                declarar_a_medias, {"agencia": str(AGENCIA_A), "cliente": str(CLIENTE_A1)}
            )
            await conexion.execute(text("SELECT count(*) FROM heraldos"))
    mensaje = str(capturado.value).lower()
    assert "unrecognized configuration parameter" in mensaje, (
        "la consulta con el par a medias fallo por una razon inesperada, y la razon "
        f"importa: tiene que abortar por la variable que falta, no por otra: {mensaje}"
    )


async def test_control_declarar_las_tres_variables_si_deja_consultar(motor) -> None:
    """El control del anterior: con las tres declaradas, la consulta funciona.

    Sin esto, una base que rechazara TODO pasaria el test de arriba con nota.
    """
    async with sesion_de_inquilino(motor, sesion_de_cliente(AGENCIA_A, CLIENTE_A1)) as conexion:
        cuantos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
    assert cuantos == 1

    async with sesion_de_inquilino(motor, sesion_de_agencia(AGENCIA_A)) as conexion:
        cuantos = (await conexion.execute(text("SELECT count(*) FROM heraldos"))).scalar_one()
    assert cuantos == 2
