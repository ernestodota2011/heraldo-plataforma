"""T-119-bis (RF-38) -- prueba de ARQUITECTURA del punto UNICO de salida de MENSAJES.

# WHY: RF-38 exige que la baja de un contacto se aplique "en codigo antes de que
# el mensaje llegue al modelo -- nunca como instruccion del prompt". El plan
# (Heraldo-02 S:4.1) resuelve eso con una sola funcion, `entregar()`, que ordena
# seis comprobaciones (corte, baja, consentimiento, ventana, plantilla, techo,
# cupo, rampa, calidad) antes de tocar el canal. Esa promesa vale cero si un
# segundo camino puede llamar al canal saltandoselas -- y ese segundo camino no
# hace falta que sea deliberado: basta una "llamada rapida" en el worker durante
# un apuro (`feedback_mecanismo_cableado_a_uno`).
#
# T-300 (`test_egreso_red.py`) YA vigila que nada fuera de `packages/egress/`
# importe un cliente de red crudo (httpx, socket, ...). Eso NO basta aqui: el
# propio plan lo dice -- un `httpx.post("https://graph.facebook.com/...")` no
# IMPORTA nada (usa `egress.red.pedir`, que es publico y legitimo en cualquier
# modulo) y ATRAVIESA el guard de SSRF sin problema, porque ese destino es
# PUBLICO. Por eso esta prueba mide DOS cosas, no una:
#
#   (a) que ningun modulo fuera de `packages/egress/` importe el cliente del
#       canal DE MENSAJERIA (no la libreria de red generica de T-300);
#   (b) que ningun archivo fuera de `packages/egress/` NOMBRE un dominio del
#       canal, aunque sea en una cadena o un comentario -- porque ese nombre es
#       lo unico que un `pedir()` legitimo pero mal ubicado dejaria a la vista.
#
# Ni `mensajes.py` ni el subpaquete del canal existen todavia -- T-119 los
# construye despues (S3 de la meta, corriente arriba de esta). La regla se
# escribe por RUTA CANONICA y por NOMBRE DE MODULO, no contra un import que hoy
# no se puede escribir, y se demuestra con un arbol de FIXTURE que finge que ya
# nacieron.
#
# # WHY (`RAICES_DEL_MONOREPO` no incluye a `packages/`): el universo declarado
# para esta casilla es el monorepo ENTERO, y el propio tracker lo define en tres
# raices -- api, worker y web. `packages/egress/` nunca aparece en ese barrido,
# y por eso es la ruta canonica: no hace falta "saltarsela", ni siquiera esta en
# la lista de sitios que se visitan.
#
# # WHY (C-4A-06, "por ruta, no por nombre de carpeta"): la primera redaccion de
# esta regla, en el plan, decia "cualquier modulo fuera de `egress/`" a secas, y
# una copia en `apps/worker/egress/mensajes.py` -- que SI esta "dentro de un
# `egress/`", solo que del que no cuenta -- la satisfacia igual. Este barrido no
# tiene ninguna logica que se salte una carpeta por su nombre: recorre TODO lo
# que cuelga de sus tres raices, incluida cualquier subcarpeta que se llame como
# quiera. El sabotaje de mas abajo construye exactamente esa copia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------------
# El universo: ALLOWLIST de raices y de extensiones (K-07). `packages/egress/`
# es la ruta canonica -- no es una de las tres, y por eso nunca se visita desde
# aqui (ver el WHY de arriba).
# --------------------------------------------------------------------------
RAICES_DEL_MONOREPO: tuple[str, ...] = ("apps/api", "apps/worker", "apps/web")
RUTA_CANONICA = "packages/egress"
EXTENSIONES_BARRIDAS: tuple[str, ...] = (".py", ".ts", ".tsx", ".js", ".mjs")

#: Lo que NO es codigo del monorepo: dependencias de terceros vendorizadas o
#: artefactos de build. Cada exencion con su motivo -- una sin motivo es un
#: agujero con permiso (mismo principio que `EXENTAS` en `scripts/publicable.py`).
CARPETAS_EXENTAS: dict[str, str] = {
    "node_modules": "dependencias de terceros instaladas por npm, no codigo propio",
    "__pycache__": "bytecode compilado, no codigo fuente",
    ".venv": "entorno virtual de Python, no codigo propio",
    "venv": "entorno virtual de Python, no codigo propio",
    ".next": "artefacto de build de Next.js, no codigo fuente",
    "dist": "artefacto de build, no codigo fuente",
    "build": "artefacto de build, no codigo fuente",
}

#: (a) "El cliente del canal de mensajeria", por NOMBRE DE MODULO. `mensajes.py`
#: es el unico nombre ya fijado (T-119 lo nombra en su propia casilla del
#: tracker); si ademas nace un subpaquete propio del canal, vive TAMBIEN bajo
#: `packages/egress/` y se anade aqui, por su nombre real, en cuanto exista.
MODULOS_DEL_CLIENTE_DEL_CANAL: tuple[str, ...] = ("egress.mensajes",)

# Anclado al INICIO de linea (con sangria) para no confundir un import real con
# uno comentado -- mismo patron que `_IMPORTA` en `test_egreso_red.py` (T-300).
# A diferencia de la regla (b), un import DENTRO de un comentario no es un
# segundo camino de salida: es prosa.
_IMPORTA_EL_CLIENTE = re.compile(
    r"^[ \t]*(?:import|from)[ \t]+("
    + "|".join(re.escape(modulo) for modulo in MODULOS_DEL_CLIENTE_DEL_CANAL)
    + r")\b",
    re.MULTILINE,
)

#: (b) Dominios del canal, cada uno con su motivo escrito.
DOMINIOS_DEL_CANAL: dict[str, str] = {
    # Graph API de Meta: transporte de WhatsApp Business Cloud API (canal oficial, S-03).
    "graph.facebook.com": "Graph API de Meta -- WhatsApp Business Cloud API",
    # Dominio de API/enlaces de WhatsApp Business.
    "api.whatsapp.com": "dominio de API y enlaces de WhatsApp Business",
    # Protocolo de WhatsApp Web: el puente NO oficial (S-03, riesgo de expulsion).
    "whatsapp.net": "protocolo de WhatsApp Web -- puente NO oficial (S-03)",
}

# Se busca en TODO el texto -- cadenas y comentarios incluidos -- porque el
# guard de red (T-300) no mira el destino: `graph.facebook.com` es publico y lo
# atraviesa sin problema. Sin ancla de inicio de linea a proposito: (b) no
# distingue codigo vivo de comentario, (a) si.
_NOMBRA_UN_DOMINIO = re.compile(
    "(" + "|".join(re.escape(dominio) for dominio in DOMINIOS_DEL_CANAL) + ")",
    re.IGNORECASE,
)

#: Este mismo archivo declara, EN CLARO, los nombres que busca -- y construye las
#: cadenas de sabotaje/control de sus propias pruebas -- asi que coincidiria
#: consigo mismo. Misma logica que el auto-exento de `scripts/publicable.py`
#: ("este archivo lleva escritos los patrones estructurales que busca; casarian
#: consigo mismos"): documentar el nombre de lo prohibido no es cometer la
#: violacion, y sin esta exencion serian indistinguibles.
ARCHIVOS_EXENTOS_DEL_BARRIDO: dict[str, str] = {
    "apps/api/tests/test_un_solo_emisor.py": (
        "declara MODULOS_DEL_CLIENTE_DEL_CANAL y DOMINIOS_DEL_CANAL, y escribe las "
        "cadenas de sabotaje/control de sus propias pruebas: coincidiria consigo "
        "mismo"
    ),
}


@dataclass(frozen=True)
class Violacion:
    """Un hallazgo del barrido: que archivo, que regla, por que."""

    ruta: str
    regla: str
    detalle: str


def _raices_ausentes(raiz: Path) -> list[str]:
    return [nombre for nombre in RAICES_DEL_MONOREPO if not (raiz / nombre).is_dir()]


def _archivos_del_universo(raiz: Path) -> list[Path]:
    """Recorre las tres raices sin saltar ninguna subcarpeta por su nombre (C-4A-06).

    Una subcarpeta llamada `egress` DENTRO de una de las tres raices (por
    ejemplo `apps/worker/egress/`) no es la ruta canonica -- solo
    `packages/egress/`, medida desde la raiz del repositorio, lo es, y
    `packages/` no es una de las tres raices que este barrido visita. Por eso
    esta funcion no tiene ningun `if "egress" in partes: continue`: si lo
    tuviera, el sabotaje de mas abajo pasaria en verde.
    """
    encontrados: list[Path] = []
    for raiz_relativa in RAICES_DEL_MONOREPO:
        base = raiz / raiz_relativa
        if not base.is_dir():
            continue
        for ruta in sorted(base.rglob("*")):
            if not ruta.is_file() or ruta.suffix not in EXTENSIONES_BARRIDAS:
                continue
            relativa = ruta.relative_to(raiz)
            if any(parte in CARPETAS_EXENTAS for parte in relativa.parts):
                continue
            encontrados.append(ruta)
    return encontrados


def buscar_violaciones(raiz: Path) -> list[Violacion]:
    """Funcion PURA: recibe la raiz por parametro.

    Por eso el fixture (`tmp_path`) y el repositorio real corren exactamente el
    mismo codigo (contrato de la meta S1 para T-119-bis: "funciones puras que
    reciben la raiz por parametro").
    """
    violaciones: list[Violacion] = [
        Violacion(
            ruta=ausente,
            regla="raiz-ausente",
            detalle=(
                f"falta el directorio {ausente!r}: el universo declarado es el "
                "monorepo ENTERO (api + worker + web); una raiz que falta no es "
                "un universo mas chico, es un universo que no se pudo medir"
            ),
        )
        for ausente in _raices_ausentes(raiz)
    ]

    for archivo in _archivos_del_universo(raiz):
        relativa = archivo.relative_to(raiz).as_posix()
        if relativa in ARCHIVOS_EXENTOS_DEL_BARRIDO:
            continue
        # `errors="replace"` (hallazgo de Crisol): un archivo con bytes que no son
        # UTF-8 valido no debe hacer CAER el barrido entero con una traza -- debe
        # seguir barriendo el resto y, si de verdad lleva un dominio o un import
        # del cliente en la parte legible, seguir encontrandolo.
        contenido = archivo.read_text(encoding="utf-8", errors="replace")

        if archivo.suffix == ".py":
            for encontrado in _IMPORTA_EL_CLIENTE.finditer(contenido):
                violaciones.append(
                    Violacion(
                        ruta=relativa,
                        regla="importa-el-cliente",
                        detalle=(
                            f"importa {encontrado.group(1)!r} fuera de "
                            f"{RUTA_CANONICA}/: es el cliente del canal de "
                            "mensajeria (RF-38)"
                        ),
                    )
                )

        for encontrado in _NOMBRA_UN_DOMINIO.finditer(contenido):
            dominio = encontrado.group(0).lower()
            violaciones.append(
                Violacion(
                    ruta=relativa,
                    regla="nombra-un-dominio",
                    detalle=(
                        f"nombra {encontrado.group(0)!r} fuera de {RUTA_CANONICA}/: "
                        f"{DOMINIOS_DEL_CANAL[dominio]}"
                    ),
                )
            )
    return violaciones


# ==========================================================================
# El barrido en si, con su propio control (`feedback_toda_sonda_lleva_control`)
# ==========================================================================
def test_control_el_barrido_real_encuentra_algo_que_mirar() -> None:
    """Sin este control, un barrido vacio pasaria las pruebas de abajo sin medir nada."""
    # Las TRES raices existen hoy en el repositorio real -- si faltara una, la
    # medida de mas abajo (>= 10 archivos en solo dos raices) podria seguir
    # pasando sin que nadie se enterara de que la tercera desaparecio.
    assert _raices_ausentes(RAIZ) == []

    archivos = _archivos_del_universo(RAIZ)
    assert len(archivos) >= 10, f"el barrido solo encontro {len(archivos)} archivos"
    relativas = {a.relative_to(RAIZ).as_posix() for a in archivos}
    # Control de que dos de las tres raices ya aportan archivos de verdad hoy.
    # `apps/web` todavia no tiene contenido de las extensiones barridas (solo
    # lleva un README) -- exigirlo aqui falsificaria el control, no lo
    # reforzaria: lo cubren `test_sabotaje_sin_apps_web_...` (existencia) y
    # `test_una_url_del_canal_en_un_archivo_del_panel_web_...` (contenido, con
    # fixture) por separado.
    assert any(r.startswith("apps/api/") for r in relativas)
    assert any(r.startswith("apps/worker/") for r in relativas)


def test_hoy_el_repositorio_real_no_tiene_un_segundo_camino_de_salida() -> None:
    """La medida que importa: T-119 aun no existe, y esto sigue en verde cuando nazca.

    Si algun dia deja de estarlo, alguien escribio el segundo camino que esta
    prueba existe para prohibir.
    """
    violaciones = buscar_violaciones(RAIZ)
    resumen = "; ".join(f"{v.ruta} ({v.regla}: {v.detalle})" for v in violaciones)
    assert violaciones == [], (
        f"el repositorio real tiene un segundo camino de salida de mensajes fuera "
        f"de {RUTA_CANONICA}/: {resumen}"
    )


# ==========================================================================
# Fixture: el arbol falso con las tres raices (contrato de la casilla)
# ==========================================================================
def _sembrar_arbol_valido(raiz: Path) -> None:
    """Las tres raices, cada una con contenido real -- nunca vacias a proposito.

    Un directorio vacio no demuestra que el barrido sepa mirar dentro de el.
    """
    (raiz / "apps/api/app").mkdir(parents=True)
    (raiz / "apps/api/app/main.py").write_text(
        "import os\n\n\ndef arranca() -> None:\n    print(os.getpid())\n",
        encoding="utf-8",
    )
    (raiz / "apps/worker").mkdir(parents=True)
    (raiz / "apps/worker/bucle.py").write_text(
        "async def procesa_uno() -> None:\n    ...\n", encoding="utf-8"
    )
    (raiz / "apps/web/app").mkdir(parents=True)
    (raiz / "apps/web/app/pagina.tsx").write_text(
        "export default function Pagina() {\n  return <div>hola</div>;\n}\n",
        encoding="utf-8",
    )


def test_control_un_arbol_limpio_no_tiene_violaciones(tmp_path: Path) -> None:
    """El control positivo (3): un monorepo de verdad, con sus tres raices, sale VERDE."""
    _sembrar_arbol_valido(tmp_path)
    assert buscar_violaciones(tmp_path) == []


def test_sabotaje_una_copia_en_worker_egress_que_importa_el_cliente(tmp_path: Path) -> None:
    """(1) C-4A-06: `apps/worker/egress/` no es la ruta canonica, solo lo PARECE.

    Una copia ahi que IMPORTA el cliente del canal es exactamente el segundo
    camino que esta prueba existe para prohibir.
    """
    _sembrar_arbol_valido(tmp_path)
    copia = tmp_path / "apps/worker/egress/mensajes.py"
    copia.parent.mkdir(parents=True)
    copia.write_text(
        "from egress.mensajes import entregar\n\n\n"
        "async def atajo(destinatario, contenido) -> None:\n"
        "    await entregar(destinatario, contenido)\n",
        encoding="utf-8",
    )

    violaciones = buscar_violaciones(tmp_path)

    assert len(violaciones) == 1, violaciones
    assert violaciones[0].ruta == "apps/worker/egress/mensajes.py"
    assert violaciones[0].regla == "importa-el-cliente"


def test_sabotaje_una_url_del_canal_en_una_cadena(tmp_path: Path) -> None:
    """(2): un `httpx.post(url_cruda)` no IMPORTA nada y pasa el guard de red.

    Pasa porque el destino es publico -- lo unico que lo delata es el nombre
    del dominio, dentro de una cadena.
    """
    _sembrar_arbol_valido(tmp_path)
    rogue = tmp_path / "apps/api/app/x.py"
    rogue.write_text(
        'ENDPOINT = "https://graph.facebook.com/v20.0/PHONE_ID/messages"\n',
        encoding="utf-8",
    )

    violaciones = buscar_violaciones(tmp_path)

    assert len(violaciones) == 1, violaciones
    assert violaciones[0].ruta == "apps/api/app/x.py"
    assert violaciones[0].regla == "nombra-un-dominio"


def test_sabotaje_sin_apps_web_el_universo_incompleto_es_rojo(tmp_path: Path) -> None:
    """(4): el universo declarado es el monorepo ENTERO.

    Faltar una de las tres raices no es un universo mas chico que sale verde:
    es un universo que no se pudo medir.
    """
    (tmp_path / "apps/api/app").mkdir(parents=True)
    (tmp_path / "apps/api/app/main.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "apps/worker").mkdir(parents=True)
    (tmp_path / "apps/worker/bucle.py").write_text("...\n", encoding="utf-8")
    # apps/web NO se crea.

    violaciones = buscar_violaciones(tmp_path)

    assert any(v.regla == "raiz-ausente" and v.ruta == "apps/web" for v in violaciones), (
        f"faltar apps/web deberia poner esto en rojo, y salio: {violaciones}"
    )


def test_sabotaje_sin_ninguna_raiz_las_tres_faltan(tmp_path: Path) -> None:
    """Control del control: un tmp_path recien creado no tiene NINGUNA de las tres."""
    violaciones = buscar_violaciones(tmp_path)
    ausentes = {v.ruta for v in violaciones if v.regla == "raiz-ausente"}
    assert ausentes == set(RAICES_DEL_MONOREPO)


# ==========================================================================
# Cobertura completa de la regla (b): cadena, comentario, y las 5 extensiones
# ==========================================================================
def test_una_mencion_en_un_comentario_tambien_cuenta_para_el_dominio(tmp_path: Path) -> None:
    """(b) dice "incluidas cadenas y comentarios": un dominio dejado en un
    comentario -- codigo comentado "para despues" -- es la misma fuga que en
    una cadena viva.
    """
    _sembrar_arbol_valido(tmp_path)
    archivo = tmp_path / "apps/worker/nota.py"
    archivo.write_text(
        "# TODO: probar contra api.whatsapp.com cuando este el mock\n",
        encoding="utf-8",
    )

    violaciones = buscar_violaciones(tmp_path)

    assert any(
        v.ruta == "apps/worker/nota.py" and v.regla == "nombra-un-dominio"
        for v in violaciones
    )


def test_una_url_del_canal_en_un_archivo_del_panel_web_tambien_se_detecta(
    tmp_path: Path,
) -> None:
    """El universo cubre CINCO extensiones, no solo `.py`: `apps/web` es TypeScript."""
    _sembrar_arbol_valido(tmp_path)
    archivo = tmp_path / "apps/web/app/enlace.ts"
    archivo.write_text('export const URL = "https://whatsapp.net/algo";\n', encoding="utf-8")

    violaciones = buscar_violaciones(tmp_path)

    assert any(
        v.ruta == "apps/web/app/enlace.ts" and v.regla == "nombra-un-dominio"
        for v in violaciones
    )


def test_un_import_dentro_de_un_comentario_no_es_un_segundo_camino(tmp_path: Path) -> None:
    """(a) es lo contrario de (b) en esto: un import COMENTADO no abre ningun camino.

    Es prosa -- mismo criterio que `_IMPORTA` en `test_egreso_red.py` (T-300).
    """
    _sembrar_arbol_valido(tmp_path)
    archivo = tmp_path / "apps/api/app/nota.py"
    archivo.write_text(
        "# from egress.mensajes import entregar (ejemplo viejo)\n", encoding="utf-8"
    )

    assert buscar_violaciones(tmp_path) == []


def test_una_carpeta_exenta_no_se_barre(tmp_path: Path) -> None:
    """`node_modules` es codigo de terceros vendorizado, no del monorepo.

    Barrerlo haria que instalar una dependencia de npm pusiera el CI en rojo
    por casualidad, sin que nadie del equipo hubiera escrito nada.
    """
    _sembrar_arbol_valido(tmp_path)
    vendida = tmp_path / "apps/web/node_modules/algun-paquete/index.js"
    vendida.parent.mkdir(parents=True)
    vendida.write_text('module.exports = "https://graph.facebook.com";\n', encoding="utf-8")

    assert buscar_violaciones(tmp_path) == []


def test_la_exencion_de_este_archivo_no_cubre_a_otros_archivos_de_test(
    tmp_path: Path,
) -> None:
    """La exencion es por RUTA EXACTA, no por vivir en una carpeta de tests.

    Si cubriera la carpeta entera, una prueba futura cualquiera podria colar un
    dominio del canal sin que nada lo note -- la excepcion se acota a este
    archivo, nunca a `apps/api/tests/` completo.
    """
    _sembrar_arbol_valido(tmp_path)
    otro = tmp_path / "apps/api/tests/test_otra_cosa.py"
    otro.parent.mkdir(parents=True, exist_ok=True)
    otro.write_text('URL = "https://graph.facebook.com"\n', encoding="utf-8")

    violaciones = buscar_violaciones(tmp_path)

    assert any(v.ruta == "apps/api/tests/test_otra_cosa.py" for v in violaciones), (
        "la exencion de test_un_solo_emisor.py se filtro a otro archivo de la misma "
        f"carpeta: {violaciones}"
    )


# ==========================================================================
# El sabotaje de la deteccion en si (`feedback_sabotaje_audita_al_test`)
# ==========================================================================
def test_la_deteccion_de_import_distingue_el_import_real_del_comentado() -> None:
    """El sabotaje de (a): sin este control, un patron roto pasaria vacio siempre."""
    assert _IMPORTA_EL_CLIENTE.search("from egress.mensajes import entregar\n")
    assert _IMPORTA_EL_CLIENTE.search("import egress.mensajes\n")
    assert _IMPORTA_EL_CLIENTE.search("    from egress.mensajes import entregar\n")
    # Control: lo que NO debe cazar.
    assert not _IMPORTA_EL_CLIENTE.search("# from egress.mensajes import entregar\n")
    assert not _IMPORTA_EL_CLIENTE.search("from egress.red import pedir\n")


def test_la_deteccion_de_dominio_encuentra_los_tres_documentados() -> None:
    """El sabotaje de (b): sin este control, la lista podria vaciarse sin que nada avise."""
    assert len(DOMINIOS_DEL_CANAL) == 3
    for dominio in DOMINIOS_DEL_CANAL:
        assert _NOMBRA_UN_DOMINIO.search(f"algo con {dominio} en medio"), dominio
    # Control: un dominio que no esta en la lista no dispara nada.
    assert not _NOMBRA_UN_DOMINIO.search("https://example.com/webhook")
