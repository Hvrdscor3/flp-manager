# Scripts de FL Studio incluidos en FLP Manager

Cada carpeta es un script que aparece en la sección **Scripts** de la app
(instalar / actualizar / desinstalar).

```
fl_scripts/
  <id>/
    info.json        datos que muestra la app
    <archivo>.py     el script (o .pyscript)
```

`info.json`:

| campo    | qué es                                                                 |
|----------|------------------------------------------------------------------------|
| `name`   | nombre visible                                                         |
| `type`   | `hardware` (controlador MIDI), `pianoroll` o `audio` (Edison)          |
| `folder` | solo `hardware`: carpeta dentro de `Settings\Hardware`                 |
| `file`   | nombre del archivo del script dentro de esta carpeta                   |
| `what`   | qué hace                                                               |
| `how`    | lista de pasos para usarlo                                             |
| `limits` | límites o avisos (opcional)                                            |

Destino en FL (`Documentos\Image-Line\FL Studio\Settings`):

- `hardware`  → `Hardware\<folder>\<file>`
- `pianoroll` → `Piano roll scripts\<file>`
- `audio`     → `Audio scripts\<file>`

Desinstalar envía el script a la Papelera de reciclaje (recuperable).
