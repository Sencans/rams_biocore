#!/bin/sh
# Lanzador de Amadeus Desktop para Linux (Arch/Hyprland).
#
# Instalación (enlace simbólico, así el script encuentra la carpeta desktop/):
#   ln -s "$HOME/proyectos/rams_biocore/desktop/scripts/amadeus-desktop.sh" "$HOME/.local/bin/amadeus-desktop"
# Si prefieres copiarlo, define AMADEUS_DESKTOP_DIR con la ruta de desktop/.
#
# Uso:  amadeus-desktop [--hidden] [--amadeus=toggle|show|hide|chat|quit|basta|vision_pausa|clics]
# Con una instancia ya abierta, la orden se le entrega a esa instancia y este proceso sale.
set -eu

if [ -n "${AMADEUS_DESKTOP_DIR:-}" ]; then
  DIR="$AMADEUS_DESKTOP_DIR"
else
  DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fi

ELECTRON="$DIR/node_modules/electron/dist/electron"
if [ ! -x "$ELECTRON" ]; then
  echo "amadeus-desktop: no encuentro Electron en $ELECTRON" >&2
  echo "Ejecuta 'npm i' y luego 'npx install-electron' (o 'npm start' una vez) en $DIR" >&2
  exit 1
fi

# Wayland sin Hyprland: no hay cursor global para el click-through → se usa X11 (XWayland).
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  set -- --ozone-platform=x11 "$@"
fi

exec "$ELECTRON" "$DIR" "$@"
