"""La maquina del heraldo (F1): lo que entra al modelo y lo que el modelo puede pedir.

# WHY: aqui vive la frontera entre TEXTO y ORDEN. Todo lo que escribe un usuario final entra
# como dato marcado (`untrusted`), toda herramienta que el modelo pide pasa por su
# declaracion y por la forma de cada argumento (`tools`), y el contexto se arma en un solo
# sitio (`prompt`). Una inyeccion puede convencer a un modelo; no puede convencer a un `if`.
"""
