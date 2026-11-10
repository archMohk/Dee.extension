# Dee.extension

A pyRevit extension for Revit (the **DeePack** ribbon tab), with a Supabase mirror for updates.

## Folder map

| Folder / file | What it is | Move or rename? |
|---|---|---|
| `DeePack.tab/` | The ribbon. One `*.panel` per ribbon panel, `*.stack` groups stacked buttons, `*.pushbutton` is one tool (`script.py`, `ui.xaml`, `bundle.yaml`). | **No** - pyRevit builds the ribbon from these names. |
| `lib/` | Shared Python modules the tools import by bare name (`dee_*`, `acc_*`) plus shared XAML dialogs. | **No** - 100+ scripts import from here. |
| `startup.py`, `icon.png`, `icon.dark.png` | pyRevit startup hook and extension icons. | **No** - pyRevit expects them at the root. |
| `installer/` | Inno Setup script (`Dee.extension.iss`); built setup lands in `installer/Output/`. | |
| `docs/` | Help site: `index.html` and one page per tool in `docs/pages/`. | |
| `tools/checks/` | Repo health checks (bundles, installer, self-calls, tool categories, WPF imports). Run from the repo root, e.g. `python tools/checks/check_bundles.py`. | |
| `tools/icons/` | Ribbon icon generator: `python tools/icons/dee_icons.py`. | |
| `tools/` (publish scripts) | Release pipeline to the Supabase mirror: `publish_mirror.bat`, `publish_supabase.py`, `verify_mirror.py`, `setup_mirror_bucket.py`, `link_publish_key.py`. The GitHub workflow calls these paths. | **No** - CI and the secret config depend on the location. |
| `.github/workflows/` | Mirrors every push to `master` to Supabase. | |
| `acc_config.example.json` | Template for `acc_config.json` (real credentials, never committed). | |

## Not in git (per machine)

`acc_config.json`, `.codex/`, `dee_control_panel.json`, ACC caches, `dashboard/`, `logs/`.

## Panels in `DeePack.tab`

Ribbon order (folder name -> ribbon title): Mode, Models (Session), Cloud, Coordination (Links),
Masterplan, Model, Rooms (Rooms & QS), Datums, ViewsDatums (Views & Sheets), SheetsExport (Export),
Health, Align, Tools (AI), About. Folder names are kept stable because `lib/` code and
`dee_control_panel.json` refer to them; only the `title:` in each panel's `bundle.yaml` changes.
Retired bundles live in `_archive/` (currently DeeCoord, superseded by DeeBIMview).
Each documented tool has a help page in `docs/pages/<ToolName>.html`.

## Before you push

```
python tools/checks/check_bundles.py
python tools/checks/check_iss.py
python tools/checks/check_self_calls.py
```

To prove the extension still loads in each installed Revit (headless, takes a few minutes each):

```
pyrevit run "D:\<path to>\Dee.extension\tools\checks\revit_compat_probe.py" --revit=2024
pyrevit run "D:\<path to>\Dee.extension\tools\checks\revit_compat_probe.py" --revit=2026
```

The script path must be absolute: `pyrevit run` resolves a relative path against a temp folder and silently runs nothing.
