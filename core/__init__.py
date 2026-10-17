"""Núcleo de R.A.M.S. BIO-CORE.

Paquete con los contratos núcleo (configuración, bus de eventos, tipos compartidos y
gramática de etiquetas), el conectoma, la memoria y la lógica local sin LLM.

No importa ningún submódulo a propósito: así los imports son baratos y no hay ciclos
(``from core.config import Settings`` solo carga lo que pide).
"""

from __future__ import annotations

__version__ = "0.1.0"
NOMBRE = "R.A.M.S. BIO-CORE"
