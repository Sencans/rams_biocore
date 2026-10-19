# Amadeus Móvil Android (R.A.M.S. BIO-CORE + VTuber AI Studio)

Aplicación móvil instalable en Android (PWA / WebView) que unifica todas las funciones actuales de **Amadeus** (`R.A.M.S. BIO-CORE`) y del visualizador 3D VRM **`vtuber-ai`** sin modificar ninguno de los contratos ni debilitar las reglas de seguridad de `IMPORTANTEIA.md`.

## Qué incluye

1. **Videollamada 3D en vivo (`vtuber-ai` + HUD Amadeus):**
   - Renderizado en tiempo real del modelo VRM 3D de `vtuber-ai` (`127.0.0.1:3939`) con tema procedural `amadeus`, lip-sync, parpadeo, mirada y movimiento autónomo de `corteza_motora.py`.
   - Medidor Nixie de Divergencia (`1.048596`), cronómetro de llamada, estado de presencia (`Presente` / `Ausente` / `Ambiguo`), LED de cámara (`vistazo` / `continua`), indicador de visión local (`👁`) y apertura de boca a 15 Hz.
   - Subtítulos en vivo y controles directos del HUD (`Silenciar`, `Vistazo cámara`, `Colgar 30m`) y de escritorio (`No molestar`, `Quieta`, `Pausar visión`).
   - Tarjeta emergente de **Confirmación de Permiso (`CompuertaPermisos`)** con `vista_previa` y botones **Aprobar / Rechazar**.
2. **Chat RINE (`/escritorio`):**
   - Se conecta como cliente de chat del HUD (`Origin: http://127.0.0.1:8765`), por lo que **convive con Amadeus Desktop sin cerrarlo (no provoca código 4001)**.
   - Mensajes por texto y dictado por voz en español (`es-CO`) que entran siempre como `{"tipo": "chat", "texto": ...}` para pasar por `core/intenciones.enrutar` y `CompuertaPermisos.autorizar()`.
3. **Estudio VRM 3D (`vtuber-ai`):**
   - Control táctil completo sobre `vtuber-ai`: `speak` (voz + emoción + gesto), expresiones faciales (`0–100%`), las 8 poses, los 15 gestos animados, los 12 objetivos de mirada (`look`), las 6 formas de manos (`left`/`right`/`both`), cruceta D-Pad de movimiento/giro (`move`/`turn`/`reset`), rotación de huesos VRM (`X`/`Y`/`Z`), selector de modelos `.vrm`, cámara (`bust`, `face`, `full`, `wide`), fondo y tema (`amadeus`/`none`).
4. **Conectoma LIF y Telemetría del Sistema:**
   - Barras homeostáticas en vivo de `core/sincan_connectome.py` (`arousal`, `foco`, `aburrimiento`, `fatiga`, `curiosidad`, `presión de sueño`, `energía`, `dopamina`, `serotonina`).
   - Telemetría del PC (`CPU`, `RAM`, `Temperatura`, `Batería`, proceso activo e inactividad), estado de módulos y bitácora de Salieri (`KV-0001`) y Megumin/Telegram.

## Cómo usarlo en Android

### 1. Por cable USB (`adb reverse`, recomendado — todo en `127.0.0.1`)

Conecta tu teléfono Android con depuración USB activada y ejecuta:

**Windows (PowerShell):**
```powershell
.\movil\scripts\amadeus-movil.cmd --url-hud "http://127.0.0.1:8765/?t=<TOKEN_DEL_HUD>"
```

**Arch Linux:**
```bash
./movil/scripts/amadeus-movil.sh --url-hud "http://127.0.0.1:8765/?t=<TOKEN_DEL_HUD>"
```

Abre en el navegador de tu Android la URL con token que imprime `amadeus-movil` (`http://127.0.0.1:8767/?t=...`) y pulsa **«Instalar aplicación»** en el menú de Chrome para tenerla a pantalla completa.
