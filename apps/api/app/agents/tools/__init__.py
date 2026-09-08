"""Herramientas del heraldo: lo que el modelo puede PEDIR, y con que forma.

# WHY: el guard de red (`egress.red`) decide A DONDE sale una peticion; este paquete decide
# QUE LLEVA DENTRO. `schema` declara la forma de cada argumento y autoriza o rechaza la
# llamada; `texto_libre` rellena el unico argumento que no tiene forma con el texto del turno
# del usuario final, nunca con lo que el modelo proponga. Los ejecutores (HTTP, MCP) y su
# registro llegan en F3 y consumen lo que aqui sale autorizado.
"""
