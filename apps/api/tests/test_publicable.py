"""El gate de publicabilidad, medido — incluido que no filtre lo que vigila.

# WHY: este repositorio es publico. `scripts/publicable.py` es lo unico que impide
# que vuelva a entrar el nombre de un cliente, el de un servidor propio, una
# direccion de la red interna o una credencial (P-40). Un gate asi tiene dos
# formas de fallar y las dos son mudas: no ver lo que deberia, y —peor— DECIR en
# voz alta lo que encontro, publicando en el log de CI justo lo que protege.
# Las dos se miden aqui.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[3]
GUION = RAIZ / "scripts" / "publicable.py"


def _gate():
    especificacion = importlib.util.spec_from_file_location("publicable", GUION)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


#: Un termino inventado SOLO para esta bateria. No nombra a nadie: lo que se
#: comprueba es el mecanismo de las huellas, no cual es la lista real.
_TERMINO_DE_PRUEBA = "clienteficticiodeprueba"


def test_el_guion_existe_donde_se_cree() -> None:
    """El control de todo lo demas: sin el archivo, cada prueba pasaria vacia."""
    assert GUION.is_file(), f"no existe {GUION}: el gate no esta donde se cree"


def test_el_arbol_publicado_esta_limpio() -> None:
    """La prueba que de verdad importa: HOY, este repositorio es publicable."""
    faltas = _gate().revisar_arbol()
    assert faltas == [], (
        "el arbol versionado tiene material que no puede vivir en un repositorio "
        "publico:\n  " + "\n  ".join(faltas)
    )


def test_la_normalizacion_ignora_mayusculas_y_acentos() -> None:
    """«Policlínico», «policlinico» y «POLICLINICO» son la misma palabra."""
    gate = _gate()
    assert gate.huella("Policlínico") == gate.huella("policlinico")
    assert gate.huella("POLICLINICO") == gate.huella("policlinico")


def test_un_termino_de_la_lista_se_detecta_por_su_huella(monkeypatch) -> None:
    """El mecanismo de huellas funciona sin que la lista real aparezca aqui."""
    gate = _gate()
    monkeypatch.setitem(
        gate.TERMINOS_PROHIBIDOS, gate.huella(_TERMINO_DE_PRUEBA), "clase de prueba"
    )
    faltas = gate.revisar_texto(f"# desplegado para {_TERMINO_DE_PRUEBA.title()}", "x.md")
    assert len(faltas) == 1 and "clase de prueba" in faltas[0]


def test_el_aviso_nunca_repite_el_termino_que_encontro(monkeypatch) -> None:
    """==La propiedad que hace util a este gate en un repositorio publico.==

    # WHY: su salida acaba en un log de CI que cualquiera puede leer. Un gate que
    # dijera «encontre "X" en la linea 4» seria el que publica X. Dice donde y de
    # que clase; la palabra la ve quien tiene el diff delante.
    """
    gate = _gate()
    monkeypatch.setitem(
        gate.TERMINOS_PROHIBIDOS, gate.huella(_TERMINO_DE_PRUEBA), "clase de prueba"
    )
    faltas = gate.revisar_texto(f"cliente: {_TERMINO_DE_PRUEBA}", "x.md")
    assert faltas, "el control fallo: no encontro nada que reportar"
    for falta in faltas:
        assert _TERMINO_DE_PRUEBA not in falta.lower(), (
            "el aviso del gate repite el termino encontrado: en un repositorio "
            "publico, el propio aviso seria la filtracion"
        )


@pytest.mark.parametrize(
    ("caso", "linea"),
    [
        ("IP privada 192.168/16", "conecta a 192.168.1.40 por el proxy"),
        ("IP privada 10/8", "el host interno es 10.20.1.41"),
        ("IP privada 172.16/12", "y el otro 172.20.0.5"),
        ("clave PEM", "-----BEGIN RSA PRIVATE KEY-----"),
        ("ficha de GitHub", "GH=ghp_" + "A" * 36),
        ("DSN con credencial", "postgresql://admin:unaclavereal@db:5432/x"),
        ("DSN con driver", "postgresql+psycopg://app:otraclave@db:5432/x"),
    ],
)
def test_cada_forma_prohibida_se_detecta(caso: str, linea: str) -> None:
    assert _gate().revisar_texto(linea, "x.md"), f"no detecto: {caso}"


@pytest.mark.parametrize(
    ("caso", "linea"),
    [
        # Un marcador de documentacion no es una credencial, y el gate no puede
        # ponerse rojo sobre el ejemplo de uso: es la via mas rapida a que alguien
        # lo apague.
        ("marcador en mayusculas", "postgresql://USUARIO:CLAVE@HOST:5432/postgres"),
        # Una referencia de entorno es lo CONTRARIO de una credencial escrita.
        ("variable de entorno", 'URL: "postgresql+psycopg://app:${CLAVE_APP}@db:5432/h"'),
        ("variable simple", "postgres://app:$CLAVE@db:5432/h"),
        # Una IP publica no es la red interna.
        ("IP publica", "resuelve a 93.184.216.34"),
    ],
)
def test_lo_que_no_debe_disparar(caso: str, linea: str) -> None:
    """Sin esto el gate seria ruido, y un gate ruidoso se acaba apagando."""
    assert _gate().revisar_texto(linea, "x.md") == [], f"falso positivo en: {caso}"


def test_ninguna_exencion_esta_muerta() -> None:
    """Una exencion que ya no apunta a nada parece cobertura y no la da.

    # WHY: es la leccion del chequeo 5c del gate documental — una entrada muerta
    # con motivo escrito es peor que ninguna, porque nadie vuelve a mirarla.
    """
    gate = _gate()
    for ruta, motivo in gate.EXENTAS.items():
        assert (RAIZ / ruta).is_file(), (
            f"la exencion {ruta!r} no apunta a ningun archivo: o se movio, o sobra"
        )
        assert motivo.strip(), f"la exencion {ruta!r} no tiene motivo escrito"


def test_el_gate_encuentra_archivos_que_revisar() -> None:
    """El control del control: con la lista vacia, todo lo anterior pasaria vacio."""
    assert len(_gate()._archivos_versionados()) >= 20


# --------------------------------------------------------------------------
# Unicode oculto en el arbol (P-49): el codigo se lee como se ejecuta
# --------------------------------------------------------------------------
#
# WHY (por que los caracteres se construyen con `chr()` y no se escriben): este archivo
# es parte del arbol que el gate revisa. Un literal invisible aqui lo detectaria el propio
# gate en `test_el_arbol_publicado_esta_limpio`, que es exactamente lo que queremos que
# pase con cualquier otro archivo.


def test_un_caracter_invisible_se_detecta_y_se_nombra_por_codepoint() -> None:
    texto = "x = 1\nif usuario " + chr(0x202E) + "== 'admin':\n"
    assert _gate().revisar_texto(texto, "a.py") == [
        "a.py:2: unicode oculto U+202E RIGHT-TO-LEFT OVERRIDE"
    ]


@pytest.mark.parametrize("codepoint", [0x200B, 0x200D, 0xFEFF, 0xE0041, 0x00AD, 0x1B])
def test_cada_familia_de_oculto_se_detecta(codepoint: int) -> None:
    faltas = _gate().revisar_texto("a" + chr(codepoint) + "b\n", "a.py")
    assert len(faltas) == 1
    assert f"unicode oculto U+{codepoint:04X}" in faltas[0]


def test_el_texto_limpio_con_acentos_y_emoji_no_dispara() -> None:
    # VARIATION SELECTOR-16 (el de los emoji) es categoria Mn, no Cf: no es invisible
    # en el sentido que importa aqui, y los docs lo usan.
    texto = "caf" + chr(0xE9) + " " + chr(0x26A0) + chr(0xFE0F) + " " + chr(0x1F600) + "\n"
    assert [f for f in _gate().revisar_texto(texto, "docs/x.md") if "oculto" in f] == []


def test_la_exencion_de_terminos_no_exime_del_unicode_oculto() -> None:
    texto = "fixture" + chr(0x200B) + "\n"
    assert _gate().revisar_texto(texto, "a.py", exento=True) != []


def test_el_control_del_arbol_mide_unicode_oculto_de_verdad(tmp_path, monkeypatch) -> None:
    # El sabotaje en vivo: un archivo con un caracter invisible dentro del arbol que el
    # gate revisa tiene que ponerlo en rojo. Sin esto, `test_el_arbol_publicado_esta_limpio`
    # podria estar verde porque la regla no mira, no porque el arbol este limpio.
    gate = _gate()
    (tmp_path / "sucio.py").write_text("x = 1" + chr(0x202E) + "\n", encoding="utf-8")
    monkeypatch.setattr(gate, "RAIZ", tmp_path)
    monkeypatch.setattr(gate, "_archivos_versionados", lambda: ["sucio.py"])
    assert any("unicode oculto U+202E" in f for f in gate.revisar_arbol())
