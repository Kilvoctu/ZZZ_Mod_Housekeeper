# ZZZ Hash Fixer

GUI that can fix some ZZZ mods. This app is built mainly for personal use, as there are features I wanted that other mod fixers don't have.

## Requirements

- Python 3.10+

## Features

- Fixes hashes using latest ZZZ-Model-Hash data.
- Fix or revert per mod or single file.
- Automatic backups under `backups/`; revert any fix, any depth.
- Enable/disable mods via the `DISABLED_` prefix.
- Supports custom hash patches via `data/user.txt` (`oldhash to newhash`, one per line).
- Supports `PlayerCharacterData.json` from [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools) (drop into `data/`).
- Fully portable.

## Usage

1. Run `build_exe.bat` and wait.
2. Run `ZZZHashFix.exe`.
3. Click **Update hashes** (needs internet for the first download).  
*Alternatively, download hash data from sources below and drop into `data/`.
4. Browse to your mods folder.
5. Select a mod / subfolder / `.ini`, hit **Fix** (or **Revert** to undo).

## Credits & license

Source hash data is pulled from [ZZZ-Model-Hash](https://github.com/hefengchang/ZZZ-Model-Hash)
and [ZZZ-Model-Hash_LowVarm](https://github.com/hefengchang/ZZZ-Model-Hash_LowVarm).  
Inspired by [ZZZ-Mod-Fixer](https://github.com/Vonksdesu/ZZZ-Mod-Fixer) and [ZZMI_tools](https://github.com/Satan1c/ZZMI_tools).

MIT License, Copyright (c) 2026 Kilvoctu — see [LICENSE](LICENSE).
