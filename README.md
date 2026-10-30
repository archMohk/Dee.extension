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

About, Align, Cloud, Coordination, Family, Health, Masterplan, Mode, Model, Models,
Quantities, Rooms, SheetsExport, Tools, Topography, ViewsDatums.
Each tool has a help page in `docs/pages/<ToolName>.html`.

## Before you push

```
python tools/checks/check_bundles.py
python tools/checks/check_iss.py
python tools/checks/check_self_calls.py
```
