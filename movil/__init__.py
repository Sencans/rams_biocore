"""Aplicación móvil Android para R.A.M.S. BIO-CORE y VTuber AI Studio.

Expone en local (``127.0.0.1:8767`` por defecto, apto para ``adb reverse`` o túnel privado)
una interfaz táctil instalable (PWA/WebView Android) que unifica:
- El visor 3D VRM en tiempo real de ``vtuber-ai`` (``127.0.0.1:3939``) y todos sus controles.
- El HUD «videollamada Amadeus» de R.A.M.S. (``127.0.0.1:8765/ws``).
- El enlace ``/escritorio`` (chat RINE, confirmaciones con botón Aprobar/Rechazar y controles).
"""

from __future__ import annotations

__all__ = ["VERSION_MOVIL"]

VERSION_MOVIL = "0.1.0"
