-- Amadeus Desktop en Hyprland 0.55+ (configuración en Lua, ~/.config/hypr/hyprland.lua).
-- Copia lo que necesites. Para Hyprland 0.53–0.54 (hyprlang) usa hyprland.ejemplo.conf.
--
-- Las reglas buscan por TÍTULO: Amadeus Desktop fija "Amadeus Overlay" y "Amadeus Chat" y no deja
-- que la página los cambie. Requiere el lanzador en el PATH:
--   ln -s ~/proyectos/rams_biocore/desktop/scripts/amadeus-desktop.sh ~/.local/bin/amadeus-desktop

-- Overlay transparente: flotante, en todos los workspaces, cubriendo el monitor y SIN robar el foco.
hl.window_rule({
  name  = "amadeus-overlay",
  match = { title = "^(Amadeus Overlay)$" },
  float = true,
  pin   = true,                                   -- todos los workspaces (solo flotantes)
  move  = { 0, 0 },                               -- coordenadas del monitor
  size  = { "(monitor_w)", "(monitor_h)" },
  no_initial_focus  = true,
  no_follow_mouse   = true,                       -- pasar el ratón por encima no te quita el foco
  focus_on_activate = false,
  no_blur = true, no_shadow = true, no_dim = true, no_anim = true,
  border_size = 0, rounding = 0, decorate = false,
  -- no_focus = true,   -- prueba opcional: si impide arrastrar o clicar al avatar, déjalo comentado
})

-- Chat: flotante, abajo a la derecha. R.A.M.S. puede abrirlo sin foco (no_initial_focus); cuando
-- lo abres tú (atajo, bandeja, clic derecho sobre ella) haces clic y escribes.
hl.window_rule({
  name  = "amadeus-chat",
  match = { title = "^(Amadeus Chat)$" },
  float = true,
  pin   = true,
  size  = { 380, 560 },
  move  = { "(monitor_w-window_w-24)", "(monitor_h-window_h-24)" },
  no_initial_focus  = true,
  no_follow_mouse   = true,
  focus_on_activate = false,
  no_shadow = true, border_size = 0,
})

-- Atajos → CLI de la 2.ª instancia (en Wayland Amadeus Desktop no registra atajos globales).
hl.bind("SUPER + A",              hl.dsp.exec_cmd("amadeus-desktop --amadeus=toggle"))       -- llamar / esconder
hl.bind("CTRL + ALT + A",         hl.dsp.exec_cmd("amadeus-desktop --amadeus=toggle"))
hl.bind("CTRL + ALT + SPACE",     hl.dsp.exec_cmd("amadeus-desktop --amadeus=chat"))         -- chat
-- Quieta: CTRL+ALT+SHIFT+Q, igual que en Windows (allí Ctrl+Alt+Q es AltGr+Q = «@» y no se puede usar).
hl.bind("CTRL + ALT + SHIFT + Q", hl.dsp.exec_cmd("amadeus-desktop --amadeus=basta"))        -- quieta + esconderse
hl.bind("CTRL + ALT + V",         hl.dsp.exec_cmd("amadeus-desktop --amadeus=vision_pausa")) -- pausar visión
hl.bind("CTRL + ALT + SHIFT + A", hl.dsp.exec_cmd("amadeus-desktop --amadeus=clics"))        -- emergencia: dejar pasar clics

-- Arranque oculto en la bandeja (necesita una barra con bandeja SNI, p. ej. el módulo tray de waybar).
hl.on("hyprland.start", function() hl.exec_cmd("amadeus-desktop --hidden") end)
