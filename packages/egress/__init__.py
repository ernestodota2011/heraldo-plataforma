"""Punto unico de salida de Heraldo.

# WHY: paquete de PRIMER NIVEL, no un submodulo de `apps/api`. `apps/api` y
# `apps/worker` corren como servicios separados en Compose; si el egress viviera
# dentro de uno de ellos, el otro acabaria COPIANDOLO, y una copia es el segundo
# camino de salida que todo el diseno prohibe (plan D-17, T-119-bis).
# Se declara como DEPENDENCIA desde los dos, jamas se copia.

Este modulo es el cimiento: los modulos reales (`red.py`, `mensajes.py`,
`cupo.py`, `rampa.py`, `corte.py`) los crean sus tareas duenas.
"""

__all__: list[str] = []
