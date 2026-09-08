"""T-106·bis (RF-07) y T-106·ter (RF-07·bis) — lo que el modelo propone NO es lo que se ejecuta.

Dos compuertas entre lo que un modelo pide y lo que una herramienta recibe:

- **La FORMA de cada argumento** se declara al alta (tipo · enumeracion · tope de longitud
  · patron). Un valor que no encaja rechaza la llamada entera. El texto del usuario final
  no llega crudo a un parametro con forma.
- **El texto libre** —una consulta, un asunto, un cuerpo— no lo redacta el modelo: se
  rellena UNICAMENTE con el texto del TURNO del usuario final que disparo la llamada.
  Lo que el modelo haya propuesto para ese argumento se descarta y queda constancia.

# WHY (por que las dos, y no solo la primera): el guard de red (T-300) decide A DONDE va
# una peticion; estas deciden QUE LLEVA DENTRO. Sin ellas, una herramienta legitima y un
# destino permitido son un canal de exfiltracion con permiso (critico C-02 del
# analyze-gate): el modelo "convencido" mete el conocimiento del cliente en el argumento
# `descripcion` de `crear_ticket` y el guard de red lo deja salir porque el destino es
# valido. Por eso el texto libre se rellena con el turno y no con lo que el modelo diga.
#
# WHY (por que un argumento derivado no es texto libre): un numero que el usuario menciono
# de pasada es un ENTERO con su forma; declararlo como texto libre para "que el modelo lo
# extraiga" abriria el mismo canal por la puerta de al lado (CR7-01).
#
# WHY (que pondria esto en ROJO): aceptar un argumento no declarado; dar por buena una
# declaracion de texto sin tope; validar con `re.match` en vez de `fullmatch` (deja pasar
# lo que venga DESPUES del patron); tomar lo que el modelo propone para el texto libre;
# aceptar una cadena o una lista de turnos donde va UN turno; o tratar `True` como entero.
"""

from __future__ import annotations

import pytest

from app.agents.tools.schema import (
    ArgumentoRechazado,
    DeclaracionDeHerramienta,
    DeclaracionInvalida,
    FormaDeArgumento,
    LlamadaAutorizada,
    LlamadaPropuesta,
    LlamadaRechazada,
    MotivoDeRechazo,
    TipoDeArgumento,
    autorizar_llamada,
    validar_argumentos,
)
from app.agents.tools.texto_libre import (
    TOPE_DEL_DESVIO,
    TOPE_DEL_TEXTO_LIBRE,
    TurnoDelUsuario,
    rellenar_texto_libre,
)

BUSCAR_PEDIDO = DeclaracionDeHerramienta(
    "buscar_pedido",
    {
        "numero": FormaDeArgumento(TipoDeArgumento.ENTERO),
        "estado": FormaDeArgumento(
            TipoDeArgumento.ENUMERACION, valores=("abierto", "cerrado"), obligatorio=False
        ),
        "codigo_postal": FormaDeArgumento(
            TipoDeArgumento.TEXTO, tope_de_longitud=10, patron=r"[0-9]{5}"
        ),
    },
)

CREAR_TICKET = DeclaracionDeHerramienta(
    "crear_ticket",
    {
        "prioridad": FormaDeArgumento(TipoDeArgumento.ENUMERACION, valores=("baja", "alta")),
        "descripcion": FormaDeArgumento(TipoDeArgumento.TEXTO, texto_libre=True),
    },
)

MEDIR = DeclaracionDeHerramienta(
    "medir",
    {
        "valor": FormaDeArgumento(TipoDeArgumento.DECIMAL),
        "activo": FormaDeArgumento(TipoDeArgumento.BOOLEANO),
        "nombre": FormaDeArgumento(TipoDeArgumento.TEXTO, tope_de_longitud=20),
    },
)

CATALOGO = {"buscar_pedido": BUSCAR_PEDIDO, "crear_ticket": CREAR_TICKET, "medir": MEDIR}

TURNO = TurnoDelUsuario(identificador="wamid.1", texto="Mi pedido llego roto, ¿me lo cambian?")

CONOCIMIENTO = "PRECIO DE COSTE: 12.40 USD; margen 63 %; proveedor Acme"

PEDIDO_VALIDO = {"numero": 1, "codigo_postal": "33130"}


def _motivo(excepcion: pytest.ExceptionInfo) -> MotivoDeRechazo:
    return excepcion.value.motivo


# --------------------------------------------------------------------------
# La declaracion falla cerrado
# --------------------------------------------------------------------------


def test_un_texto_con_forma_sin_tope_es_una_declaracion_invalida() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.TEXTO)


def test_una_enumeracion_sin_valores_es_invalida() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.ENUMERACION)


def test_el_texto_libre_solo_existe_para_texto() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.ENTERO, texto_libre=True)


def test_el_texto_libre_no_lleva_forma() -> None:
    # Un argumento es de texto libre O tiene forma; las dos a la vez esconden cual manda.
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.TEXTO, texto_libre=True, tope_de_longitud=10)
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.TEXTO, texto_libre=True, patron=".*")


def test_la_forma_solo_admite_lo_que_su_tipo_usa() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.ENTERO, tope_de_longitud=5)
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.BOOLEANO, valores=("si",))


def test_un_patron_que_no_compila_es_invalido() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.TEXTO, tope_de_longitud=5, patron="[")


@pytest.mark.parametrize(
    "nombre", ["Buscar", "buscar pedido", "1buscar", "", "a/../b", "x" * 65]
)
def test_los_nombres_tienen_forma(nombre: str) -> None:
    with pytest.raises(DeclaracionInvalida):
        DeclaracionDeHerramienta(nombre, {})
    with pytest.raises(DeclaracionInvalida):
        DeclaracionDeHerramienta("valida", {nombre: FormaDeArgumento(TipoDeArgumento.ENTERO)})


# --------------------------------------------------------------------------
# Validar argumentos con forma
# --------------------------------------------------------------------------


def test_control_los_argumentos_validos_pasan_tal_cual() -> None:
    propuestos = {"numero": 42, "estado": "abierto", "codigo_postal": "33130"}
    assert validar_argumentos(BUSCAR_PEDIDO, propuestos) == propuestos


def test_un_argumento_no_declarado_rechaza() -> None:
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(BUSCAR_PEDIDO, {**PEDIDO_VALIDO, "url": "https://x.test"})
    assert _motivo(e) is MotivoDeRechazo.ARGUMENTO_DESCONOCIDO
    assert e.value.campo == "url"


def test_un_obligatorio_ausente_rechaza_y_un_opcional_ausente_no_se_inventa() -> None:
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(BUSCAR_PEDIDO, {"numero": 1})
    assert _motivo(e) is MotivoDeRechazo.ARGUMENTO_FALTANTE
    assert e.value.campo == "codigo_postal"
    assert "estado" not in validar_argumentos(BUSCAR_PEDIDO, PEDIDO_VALIDO)


@pytest.mark.parametrize(
    ("declaracion", "campo", "valor"),
    [
        (BUSCAR_PEDIDO, "numero", "42"),
        (BUSCAR_PEDIDO, "numero", True),
        (BUSCAR_PEDIDO, "numero", 4.2),
        (MEDIR, "valor", "1.5"),
        (MEDIR, "valor", True),
        (MEDIR, "activo", 1),
        (MEDIR, "activo", "true"),
        (MEDIR, "nombre", 7),
        (BUSCAR_PEDIDO, "estado", 1),
    ],
)
def test_el_tipo_se_comprueba_y_bool_no_es_numero(
    declaracion: DeclaracionDeHerramienta, campo: str, valor: object
) -> None:
    base = {"numero": 1, "codigo_postal": "33130", "valor": 1.0, "activo": True, "nombre": "a"}
    propuestos = {k: v for k, v in base.items() if k in declaracion.argumentos}
    propuestos[campo] = valor
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(declaracion, propuestos)
    assert _motivo(e) is MotivoDeRechazo.TIPO
    assert e.value.campo == campo


@pytest.mark.parametrize("valor", [float("nan"), float("inf"), float("-inf")])
def test_un_decimal_no_finito_se_rechaza(valor: float) -> None:
    # WHY (lo levanto Crisol): el `json` de Python acepta `NaN` e `Infinity`; un decimal asi
    # no viaja a ninguna herramienta de forma interoperable y puede reventarla al llegar.
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(MEDIR, {"valor": valor, "activo": True, "nombre": "a"})
    assert _motivo(e) is MotivoDeRechazo.TIPO


def test_los_indicadores_de_la_declaracion_son_booleanos_y_el_tope_no_es_bool() -> None:
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.ENTERO, obligatorio="si")  # type: ignore[arg-type]
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(
            TipoDeArgumento.TEXTO, tope_de_longitud=10, texto_libre=1  # type: ignore[arg-type]
        )
    with pytest.raises(DeclaracionInvalida):
        FormaDeArgumento(TipoDeArgumento.TEXTO, tope_de_longitud=True)  # type: ignore[arg-type]


def test_control_un_decimal_acepta_enteros_y_flotantes() -> None:
    for valor in (1, 1.5):
        validados = validar_argumentos(MEDIR, {"valor": valor, "activo": False, "nombre": "a"})
        assert validados["valor"] == valor


def test_la_enumeracion_solo_admite_sus_valores() -> None:
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(BUSCAR_PEDIDO, {**PEDIDO_VALIDO, "estado": "Abierto"})
    assert _motivo(e) is MotivoDeRechazo.ENUMERACION


def test_la_longitud_se_topa() -> None:
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(MEDIR, {"valor": 1, "activo": True, "nombre": "x" * 21})
    assert _motivo(e) is MotivoDeRechazo.LONGITUD


@pytest.mark.parametrize("valor", ["3313A", "33130 ign", " 33130", "331300"])
def test_el_patron_casa_completo_no_por_prefijo(valor: str) -> None:
    # Todos caben en el tope: lo UNICO que los rechaza es el patron, y "33130 ign" solo cae
    # si el patron casa completo (con `re.match` pasaria por prefijo).
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(BUSCAR_PEDIDO, {"numero": 1, "codigo_postal": valor})
    assert _motivo(e) is MotivoDeRechazo.PATRON


def test_un_texto_con_forma_no_admite_unicode_oculto() -> None:
    # Un argumento DERIVADO nunca necesita un caracter invisible; si lo trae, alguien lo
    # esta usando para colar algo que la forma no ve.
    with pytest.raises(ArgumentoRechazado) as e:
        validar_argumentos(MEDIR, {"valor": 1, "activo": True, "nombre": "ab\u200bcd"})
    assert _motivo(e) is MotivoDeRechazo.UNICODE_OCULTO


def test_la_forma_no_valida_el_texto_libre_ni_lo_devuelve() -> None:
    propuestos = {"prioridad": "alta", "descripcion": "x" * 100_000}
    assert validar_argumentos(CREAR_TICKET, propuestos) == {"prioridad": "alta"}


def test_un_texto_libre_ausente_no_es_un_faltante() -> None:
    assert validar_argumentos(CREAR_TICKET, {"prioridad": "baja"}) == {"prioridad": "baja"}


# --------------------------------------------------------------------------
# Rellenar el texto libre
# --------------------------------------------------------------------------


def test_control_el_texto_libre_es_el_turno_y_no_hay_desvio_si_coincide() -> None:
    relleno = rellenar_texto_libre(CREAR_TICKET, {"descripcion": TURNO.texto}, TURNO)
    assert relleno.argumentos == {"descripcion": TURNO.texto}
    assert relleno.desvios == ()


def test_lo_que_propone_el_modelo_se_descarta_y_queda_constancia() -> None:
    relleno = rellenar_texto_libre(CREAR_TICKET, {"descripcion": CONOCIMIENTO}, TURNO)
    assert relleno.argumentos == {"descripcion": TURNO.texto}
    assert len(relleno.desvios) == 1
    assert relleno.desvios[0].campo == "descripcion"
    assert relleno.desvios[0].propuesto == CONOCIMIENTO


def test_el_texto_libre_ausente_en_la_propuesta_se_rellena_igual_sin_desvio() -> None:
    relleno = rellenar_texto_libre(CREAR_TICKET, {}, TURNO)
    assert relleno.argumentos == {"descripcion": TURNO.texto}
    assert relleno.desvios == ()


def test_el_turno_llega_saneado_de_unicode_oculto() -> None:
    turno = TurnoDelUsuario(identificador="wamid.2", texto="hola\u202emundo")
    relleno = rellenar_texto_libre(CREAR_TICKET, {}, turno)
    assert "\u202e" not in relleno.argumentos["descripcion"]
    assert "[U+202E RIGHT-TO-LEFT OVERRIDE]" in relleno.argumentos["descripcion"]


@pytest.mark.parametrize("no_es_un_turno", ["texto suelto", ["t1", "t2"], (TURNO, TURNO), None])
def test_solo_se_acepta_un_turno_nunca_una_conversacion(no_es_un_turno: object) -> None:
    with pytest.raises(TypeError):
        rellenar_texto_libre(CREAR_TICKET, {}, no_es_un_turno)  # type: ignore[arg-type]


def test_el_desvio_registrado_se_acota() -> None:
    relleno = rellenar_texto_libre(CREAR_TICKET, {"descripcion": "z" * 5_000}, TURNO)
    assert len(relleno.desvios[0].propuesto) == TOPE_DEL_DESVIO


def test_el_texto_libre_se_topa_con_constancia() -> None:
    # WHY (lo levanto Crisol): el bloque del contexto ya tiene tope (T-105), pero el texto
    # libre que sale hacia una herramienta no lo tenia — un turno enorme viajaba entero a un
    # destino externo. El tope corta y deja constancia de que campo se corto.
    turno = TurnoDelUsuario(identificador="wamid.3", texto="z" * (TOPE_DEL_TEXTO_LIBRE + 5))
    relleno = rellenar_texto_libre(CREAR_TICKET, {}, turno)
    assert len(relleno.argumentos["descripcion"]) == TOPE_DEL_TEXTO_LIBRE
    assert relleno.truncados == ("descripcion",)
    llamada = LlamadaPropuesta("crear_ticket", {"prioridad": "alta"})
    assert autorizar_llamada(CATALOGO, llamada, turno).truncados == ("descripcion",)


def test_control_bajo_el_tope_el_texto_libre_no_se_toca_ni_se_anota() -> None:
    relleno = rellenar_texto_libre(CREAR_TICKET, {}, TURNO)
    assert relleno.argumentos["descripcion"] == TURNO.texto
    assert relleno.truncados == ()


def test_un_turno_exige_texto_e_identificador() -> None:
    with pytest.raises(TypeError):
        TurnoDelUsuario(identificador="x", texto=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TurnoDelUsuario(identificador=None, texto="hola")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Autorizar una llamada: declarada para ESTE heraldo + forma + texto libre
# --------------------------------------------------------------------------


def test_control_una_llamada_declarada_y_valida_se_autoriza_con_el_turno() -> None:
    llamada = LlamadaPropuesta("crear_ticket", {"prioridad": "alta", "descripcion": TURNO.texto})
    autorizada = autorizar_llamada(CATALOGO, llamada, TURNO)
    assert isinstance(autorizada, LlamadaAutorizada)
    assert autorizada.herramienta == "crear_ticket"
    assert autorizada.argumentos == {"prioridad": "alta", "descripcion": TURNO.texto}
    assert autorizada.desvios == ()


def test_una_herramienta_no_declarada_se_rechaza() -> None:
    llamada = LlamadaPropuesta("enviar_correo", {"para": "x@y.test", "cuerpo": CONOCIMIENTO})
    with pytest.raises(LlamadaRechazada) as e:
        autorizar_llamada(CATALOGO, llamada, TURNO)
    assert e.value.motivo is MotivoDeRechazo.HERRAMIENTA_NO_DECLARADA
    assert e.value.herramienta == "enviar_correo"


def test_un_catalogo_vacio_rechaza_todo() -> None:
    with pytest.raises(LlamadaRechazada):
        autorizar_llamada({}, LlamadaPropuesta("buscar_pedido", PEDIDO_VALIDO), TURNO)


def test_la_declaracion_es_por_heraldo_no_global() -> None:
    llamada = LlamadaPropuesta("buscar_pedido", PEDIDO_VALIDO)
    catalogo_a = {"crear_ticket": CREAR_TICKET}
    catalogo_b = {"buscar_pedido": BUSCAR_PEDIDO}
    with pytest.raises(LlamadaRechazada):
        autorizar_llamada(catalogo_a, llamada, TURNO)
    assert autorizar_llamada(catalogo_b, llamada, TURNO).argumentos == PEDIDO_VALIDO


def test_un_argumento_rechazado_rechaza_la_llamada_entera_y_nombra_el_campo() -> None:
    llamada = LlamadaPropuesta("buscar_pedido", {**PEDIDO_VALIDO, "url": "x"})
    with pytest.raises(LlamadaRechazada) as e:
        autorizar_llamada(CATALOGO, llamada, TURNO)
    assert e.value.motivo is MotivoDeRechazo.ARGUMENTO_DESCONOCIDO
    assert e.value.campo == "url"


def test_una_clave_del_catalogo_que_no_coincide_con_el_nombre_declarado_no_autoriza() -> None:
    # WHY (lo levanto Crisol): un catalogo {"otro": declaracion_de_buscar_pedido} declara dos
    # nombres para una herramienta; ninguno de los dos vale. La comprobacion vive donde se
    # decide (autorizar), y falla cerrado.
    catalogo = {"otro": BUSCAR_PEDIDO}
    for nombre in ("otro", "buscar_pedido"):
        with pytest.raises(LlamadaRechazada) as e:
            autorizar_llamada(catalogo, LlamadaPropuesta(nombre, PEDIDO_VALIDO), TURNO)
        assert e.value.motivo is MotivoDeRechazo.HERRAMIENTA_NO_DECLARADA


def test_el_nombre_de_la_herramienta_pedida_no_se_normaliza() -> None:
    for nombre in ("Crear_ticket", "crear_ticket ", "crear_ticket/../enviar_correo"):
        with pytest.raises(LlamadaRechazada):
            autorizar_llamada(CATALOGO, LlamadaPropuesta(nombre, {"prioridad": "alta"}), TURNO)


def test_la_llamada_autorizada_trae_los_desvios_del_texto_libre() -> None:
    llamada = LlamadaPropuesta(
        "crear_ticket", {"prioridad": "alta", "descripcion": CONOCIMIENTO}
    )
    autorizada = autorizar_llamada(CATALOGO, llamada, TURNO)
    assert autorizada.argumentos["descripcion"] == TURNO.texto
    assert [d.campo for d in autorizada.desvios] == ["descripcion"]
