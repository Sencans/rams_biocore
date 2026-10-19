# Instrucciones para agentes de IA (rams_biocore)

**Guía obligatoria: `IMPORTANTEIA.md`, en esta misma carpeta. Léela entera antes de cambiar nada.**

Reglas de oro (resumen; el detalle está en `IMPORTANTEIA.md`):
1. Nunca leas, imprimas ni subas a git `.env`, `data/`, `chats/`, `ctl_token`, tokens ni `vtuber-ai/models/*.vrm`.
2. Nunca hagas commits sin pedido ni uses `git checkout`, `restore`, `reset`, `stash`, `clean`, `rm`, `add -f` ni `commit -a`: hay mucho trabajo sin commit.
3. Nunca arranques ni toques los servicios en vivo (:3939, :8765, :8766). Compruébalo sin conectarte (`IMPORTANTEIA.md` §7.1).
4. Nunca debilites la seguridad: el LLM no crea ni ejecuta acciones, toda `AccionPC` pasa una vez por `CompuertaPermisos.autorizar()`, todo escucha en `127.0.0.1` y nada de `shell=True`.
5. Nunca cambies un contrato por un solo lado (Settings, bus, `contracts.py`, etiquetas, `/escritorio`, protocolo del visor, `X-Source`). Prueba lo que toques sin datos reales ni APIs de pago.
