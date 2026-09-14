#  <img src="icon.ico" alt="icon" align="top" width="36"> ZZZ Mod Housekeeper

Simple GUI that can fix some ZZZ mods and do basic mod management. 
This app is built mainly for personal use.


## Setup/Usage
1. Have Python 3.10+.
2. Download/clone this repo.
3. Run `build_exe.bat` and wait until it's done.
4. Run `ZZZModKeeper.exe`.
5. Optional: click **Update data** to enable the fix features; mod management works without data.  
*Alternatively, download data from sources below and drop into `data/`.
6. Browse to your mod folder.
7. Hopefully the rest is self-explanatory.

## Features
- Fully portable.
- Supports flat and nested folder hierarchy.
- Mod fixer features:
  - Automatic update checks from source repos.
  - Fix or revert per mod or single file.
  - Update hashes to currently known values.
  - Remap blend bone indices from data tables or auto-derived mappings.
  - Repair face texcoords for current game format (fixes scrambled faces).
  - Automatic backups under `backups/`; revert any fix, any depth.
  - Supports custom hash patches via `user.txt` (`oldhash to newhash`, one per line) and `PlayerCharacterData.json` from [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools) (drop into `data/`).
- Mod management features:
  - Install mod archives via menu or drag and drop (zip; rar/7z when 7-zip or WinRAR is installed).
  - Enable/disable mods (via the `DISABLED_` prefix).
  - Top-level folder creation.
  - Renaming and deleting folders/mods.
  - Tree filters: show enabled only, show/hide empty folders.
  - Mod preset loadout management.
  - Context menu for mod info (author, toggles).
  - Add/browse mod images in its gallery, set a preview thumbnail as hover tooltip.

## Known Issues
- Can't guarantee it'll fix every mod.
- Don't include any loose file in a category folder; category may falsely be identified as a mod.
- Drag and drop doesn't work when running app elevated; just run it normal.
- Missing various stuff that I haven't felt like adding.
- Jank.

## Credits & license

Source data is pulled from [ZZZ-Model-Hash](https://github.com/hefengchang/ZZZ-Model-Hash), [ZZZ-Model-Hash_LowVarm](https://github.com/hefengchang/ZZZ-Model-Hash_LowVarm), and [ZZZ-Model-Fix-Tool](https://github.com/hefengchang/ZZZ-Model-Fix-Tool) by [    hefengchang](https://github.com/hefengchang).  
Inspired by [ZZZ-Mod-Fixer](https://github.com/Vonksdesu/ZZZ-Mod-Fixer) and [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools).

MIT License, Copyright (c) 2026 Kilvoctu — see [LICENSE](LICENSE).
