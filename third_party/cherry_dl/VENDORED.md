# cherry_dl — copia interna (vendored)

- **Origen:** `E:\githubs\cherry-dl` (repo autónomo, sigue vivo y es la fuente canónica).
- **Commit de origen:** `befa56d` (2026-06-28) — copiado el 2026-07-02.
- **Licencia:** BSL 1.1 (mismo licenciante que Panopticon). Ver `LICENSE` en esta carpeta.

## Reglas (decisión CF2 de `CHERRY_FUSION_DESIGN.md`)

1. **NO modificar este código.** Es una copia transicional; cualquier cambio de motor se hace
   upstream en `E:\githubs\cherry-dl` y se re-copia aquí (actualizar el commit arriba).
2. Corre sobre el **pool de dependencias de Panopticon** (`requirements.txt` raíz). Deps
   añadidas por esta copia: httpx[http2], aiosqlite, typer, rich, pydantic, tenacity, qasync,
   browser-cookie3, nodriver. (`textual` NO se instala: la TUI legacy no funciona aquí.)
3. Se importa añadiendo `third_party/` a `sys.path`/`PYTHONPATH` (el paquete usa imports
   relativos; el único absoluto es `__main__.py`).
4. El estado vive en `~/.cherry-dl/` (config, index.db, sesión) — **compartido con el
   standalone**. No ejecutar ambas GUIs a la vez contra los mismos perfiles.
5. Cuando la fusión se valide con uso real, este aviso se reemplaza y el standalone se congela.
