"""El rol de aplicacion: sin privilegios, no dueño, y sin escotilla.

# WHY: plan §3.1 punto 1. «El rol de la aplicacion no es superusuario y no es
# dueño de las tablas. Sin esto, RLS se salta en silencio: por defecto el dueño
# de una tabla ignora sus propias politicas». Y hay una TERCERA escotilla que la
# frase no nombra y que valdria lo mismo: el atributo `BYPASSRLS`. Un rol con
# `BYPASSRLS` no es superusuario, no es dueño, y aun asi ve todas las filas.
# Por eso se le quita explicitamente y por eso el test lo comprueba.
#
# WHY (NOINHERIT): hay una CUARTA escotilla, y no se cierra con privilegios.
# Un rol `INHERIT` usa automaticamente los privilegios de todo rol del que sea
# miembro. Da igual lo estrecho que sea su GRANT propio: basta un
# `GRANT algun_rol_potente TO heraldo_app` hecho por cualquiera con permiso para
# ello para que la aplicacion herede ese poder EN SILENCIO, sin tocar este
# repositorio ni ninguna migracion. Con `NOINHERIT` esos privilegios exigen un
# `SET ROLE` explicito que la aplicacion no hace. No afecta a los GRANT directos
# de `sentencias_de_privilegios`, que son los que la aplicacion si necesita: es
# estrictamente mas estrecho.
#
# WHY: el nombre del rol vive aqui, en un solo sitio, para que la migracion que
# lo crea y el test que lo audita midan EL MISMO rol. Un test que audita un rol
# que no es el que corre la aplicacion es teatro.
#
# WHY: aqui no hay ninguna contrasena, ni un valor por defecto. El rol nace
# `NOLOGIN`; quien despliega le da `LOGIN` y contrasena por fuera, y el DSN
# viaja por entorno.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.tenancy.politicas import valida_identificador

#: El rol con el que corre la aplicacion. NO es el dueño de las tablas.
ROL_APLICACION = "heraldo_app"

#: Los cuatro verbos que la aplicacion necesita. No hay un quinto: nada de DDL,
#: nada de TRUNCATE (que no dispara politicas de fila).
VERBOS = ("SELECT", "INSERT", "UPDATE", "DELETE")

#: Los dos unicos verbos de una tabla de SOLO INSERCION (RF-10). No es una
#: convencion ni un comentario: es lo que se concede, y lo que no se concede la
#: aplicacion no lo puede hacer aunque su codigo lo intente.
VERBOS_DE_SOLO_INSERCION = ("SELECT", "INSERT")

#: ==La declaracion VIGENTE de que puede hacer la aplicacion sobre cada tabla.==
#:
#: # WHY: hasta la revision 0003 esto era una lista de tablas y un unico juego de
#: cuatro verbos para todas. Eso no puede expresar RF-10 —una bitacora que la
#: propia aplicacion no pueda reescribir— porque «solo insercion» no es un estilo
#: de programar: es un PERMISO que la base niega. Un `# no borrar` en un
#: comentario no es un mecanismo; un `UPDATE` que revienta con
#: `permission denied`, si.
#:
#: # WHY: es una ALLOWLIST. Una tabla que no este aqui no recibe ningun verbo, y
#: `test_los_privilegios_efectivos_son_exactamente_los_declarados` lo mide contra
#: el catalogo: olvidarse de declarar una tabla nueva no la deja abierta, la deja
#: inalcanzable — y olvidarse de retirar una que ya no existe tambien sale en rojo.
PRIVILEGIOS_DE_APLICACION: dict[str, tuple[str, ...]] = {
    # --- el cimiento (revision 0001) ---
    "agencias": VERBOS,
    "clientes": VERBOS,
    "heraldos": VERBOS,
    # --- la base y la cola (revision 0003) ---
    "secretos": VERBOS,
    # RF-10: la bitacora se escribe y se lee. No se corrige y no se borra.
    "bitacora": VERBOS_DE_SOLO_INSERCION,
    # La cola se reclama (UPDATE) y se archiva (DELETE despues de copiar).
    "trabajos": VERBOS,
    # El archivo recibe (INSERT), se consulta (SELECT) y se purga (DELETE). NO se
    # actualiza: un archivo que se puede reescribir no es un archivo.
    "trabajos_archivados": ("SELECT", "INSERT", "DELETE"),
    # RF-12: el registro de idempotencia se inserta y se lee. Un `UPDATE` solo
    # serviria para reescribir la historia de que llego y que no; un `DELETE`,
    # para volver a procesar un mensaje que ya se proceso una vez.
    "mensajes_entrantes": VERBOS_DE_SOLO_INSERCION,
}


def sentencias_de_creacion() -> list[str]:
    """Crea el rol si no existe y le quita las tres escotillas, siempre.

    El SQL sale sin sangria ni lineas en blanco de sobra a proposito: estas
    sentencias se CONGELAN literales dentro de cada migracion, y una sangria
    decorativa alli seria ruido que nadie puede leer ni comparar.
    """
    valida_identificador(ROL_APLICACION)
    # S608: `ROL_APLICACION` es una constante de este modulo y pasa por
    # `valida_identificador()`. Aqui no llega entrada de usuario.
    return [
        f"""DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROL_APLICACION}') THEN
        CREATE ROLE {ROL_APLICACION} NOLOGIN NOINHERIT;
    END IF;
END
$$""",  # noqa: S608
        # Idempotente y explicito: aunque el rol ya existiera con atributos
        # heredados de otro sitio, aqui se le quitan.
        f"ALTER ROLE {ROL_APLICACION} NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOBYPASSRLS NOREPLICATION NOINHERIT",
    ]


class VerboNoAdmitido(ValueError):
    """Un verbo fuera de los cuatro: no se concede lo que nadie ha pensado."""


def sentencias_de_privilegios(privilegios: Mapping[str, Sequence[str]]) -> list[str]:
    """Menor privilegio POR TABLA, redactado por ALLOWLIST.

    Se revoca todo primero y se concede solo lo declarado: una tabla nueva NO
    hereda permiso por existir, y una tabla declarada no recibe un verbo que no
    pidio. Es lo contrario de conceder `ON ALL TABLES`, que le regalaria acceso
    al catalogo de migraciones y a cualquier tabla futura que nadie haya pensado.

    # WHY (la agrupacion): se emite un `GRANT` por JUEGO DE VERBOS, no uno por
    # tabla. Con uno por tabla, anadir una tabla reescribiria una sentencia que ya
    # estaba congelada en otra revision; agrupando, la lista de tablas de cada
    # juego crece en su propia linea y el diff dice exactamente que cambio.
    #
    # # WHY (el orden): las tablas van ordenadas y los juegos tambien. El SQL se
    # CONGELA literal en la migracion, asi que dos llamadas con el mismo contenido
    # y distinto orden de diccionario produciran el mismo texto — si no, el guard
    # `test_la_redaccion_vigente_no_diverge_del_generador` se pondria en rojo por
    # el orden de un `dict`, que no es un cambio de significado.
    """
    valida_identificador(ROL_APLICACION)
    por_juego: dict[tuple[str, ...], list[str]] = {}
    for tabla, verbos in privilegios.items():
        valida_identificador(tabla)
        desconocidos = [v for v in verbos if v not in VERBOS]
        if desconocidos:
            raise VerboNoAdmitido(
                f"{tabla}: {desconocidos} no esta entre los verbos admitidos {list(VERBOS)}. "
                "La aplicacion no hace DDL y no hace TRUNCATE (que no dispara politicas)"
            )
        if not verbos:
            raise VerboNoAdmitido(
                f"{tabla} se declara con cero verbos. Una tabla que la aplicacion no "
                "toca no se declara aqui: se deja fuera del mapa y no recibe ningun GRANT"
            )
        # Se normaliza al ORDEN CANONICO de `VERBOS`, no al orden en que se
        # escribieron: `("INSERT", "SELECT")` y `("SELECT", "INSERT")` son el
        # mismo permiso y tienen que producir el mismo texto congelado.
        juego = tuple(v for v in VERBOS if v in verbos)
        por_juego.setdefault(juego, []).append(tabla)

    concesiones = [
        # S608: tablas y rol pasan por `valida_identificador()` y los verbos por
        # la comprobacion de arriba. Aqui no llega entrada de usuario.
        f"GRANT {', '.join(juego)} ON {', '.join(sorted(tablas))} TO {ROL_APLICACION}"  # noqa: S608
        for juego, tablas in sorted(por_juego.items())
    ]
    return [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROL_APLICACION}",
        f"REVOKE ALL ON SCHEMA public FROM {ROL_APLICACION}",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA public TO {ROL_APLICACION}",
        *concesiones,
    ]
