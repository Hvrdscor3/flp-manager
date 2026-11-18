# FLP Manager

Gestor de proyectos de FL Studio para Windows.

- Escanea tus unidades y encuentra todos tus `.flp` (excluye los de muestra de FL Studio).
- Agrupa versiones y autoguardados y marca cuál es el más reciente.
- Lee cada proyecto: versión de FL, tempo, fechas, horas trabajadas y **samples faltantes**.
- Marca proyectos como Útil o Descartar, añade notas, ábrelos en FL o envíalos a la Papelera.
- Organiza todo en `Destino / Año / Mes / Proyecto`, con opción de deshacer.
- Se **actualiza solo** desde los Releases de este repositorio.

## Descargar
Descarga `FLP-Manager.exe` desde la sección **Releases**. Tus datos se guardan en `%APPDATA%\FLP Manager`.

## Publicar una versión nueva
```bash
git tag v1.0.1
git push origin v1.0.1
```
GitHub Actions compila el `.exe` y crea el Release. Las copias instaladas lo detectan al abrirse.

## Otras herramientas incluidas
- `device_AutoOrganizer.py`: script de FL Studio que colorea y nombra las automatizaciones.
- `flp_ordenar.py` / `Ordenar proyecto FL.bat`: junta cada automatización en su propia pista (edita una copia del `.flp`).

## Compilar en local
```bash
python -m pip install pyinstaller pywebview
build_exe.bat
```
