#  <img src="icon.ico" alt="icon" align="top" width="36"> ZZZ Mod Housekeeper

Simple GUI that can fix some ZZZ mods and do basic mod management. 
This app is built mainly for personal use, as I wanted per-mod operations.

## Requirements

- Python 3.10+

## Features

- Fully portable.
- Supports flat and nested folder hierarchy.
- Mod fixer features:
  - Automatic update checks from source repos.
  - Fix or revert per mod or single file.
  - Automatic backups under `backups/`; revert any fix, any depth.
  - Supports custom hash patches via `data/user.txt` (`oldhash to newhash`, one per line).
  - Supports `PlayerCharacterData.json` from [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools) (drop into `data/`).
- Mod management features:
  - Install mod archives via menu or drag and drop (zip; rar/7z when 7-zip or WinRAR is installed).
  - Enable/disable mods (via the `DISABLED_` prefix).
  - Mod preset loadout management.
  - Context menu for mod info (author, toggles) and images, if any.

## Usage

1. Run `build_exe.bat` and wait.
2. Run `ZZZModKeeper.exe`.
3. Click **Update hashes** (needs internet for the first download).  
*Alternatively, download hash data from sources below and drop into `data/`.
4. Browse to your mod folder.
5. Hopefully the rest is self-explanatory.

## Known Issues

- Can't guarantee it'll fix every mod.
- No rename, delete, or similar file operations.
- Other things I haven't added.
- Jank.

## Credits & license

Source hash data is pulled from [ZZZ-Model-Hash](https://github.com/hefengchang/ZZZ-Model-Hash)
and [ZZZ-Model-Hash_LowVarm](https://github.com/hefengchang/ZZZ-Model-Hash_LowVarm).  
Inspired and based on work by [ZZZ-Mod-Fixer](https://github.com/Vonksdesu/ZZZ-Mod-Fixer) and [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools).

MIT License, Copyright (c) 2026 Kilvoctu — see [LICENSE](LICENSE).
