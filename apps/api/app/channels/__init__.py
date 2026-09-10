"""Canales: por donde entran y salen los mensajes de un heraldo.

Viven aqui la puerta de idempotencia (T-021, RF-12: el mismo mensaje externo
produce EXACTAMENTE un procesamiento, llegue las veces que llegue) y los
destinos internos de aviso (T-118, RF-46-bis: a quien avisa cada inquilino y
por que canal, con el guard que rechaza el destino no declarado).
"""

__all__: list[str] = []
