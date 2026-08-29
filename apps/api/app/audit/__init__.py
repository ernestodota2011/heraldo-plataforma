"""T-017 (RF-10) — la bitacora de solo insercion.

«Que la propia aplicacion no pueda reescribir» no es una forma de programar con
cuidado: es un PERMISO que la base de datos niega. El rol con el que corre la
aplicacion no tiene `UPDATE` ni `DELETE` sobre `bitacora` (revision 0003), asi
que un `UPDATE` escrito por descuido —o por alguien que quiera tapar algo—
revienta con `permission denied`, no con un comentario que nadie lee.
"""

from app.audit.bitacora import Apunte, apuntar, leer_apuntes

__all__ = ["Apunte", "apuntar", "leer_apuntes"]
