# Campfire

Campfire backs up and restores emulated game saves on Linux and Windows. It works directly with your saves directory, without requiring emulator-specific integration.

## Features

- Back up individual saves or entire directories, including memory cards and supporting files.
- Verify copies with SHA-256 before publishing a backup or replacing a save.
- Browse backups by name and version, with confirmation before overwriting files.
- Remove older snapshots while retaining a chosen number of recent versions.

Directory restores are staged and verified before replacing live saves, with rollback attempted if replacement fails. Automated tests cover corruption, copy failures, and recovery behavior.

## Getting started

Requires **Python 3.11 or newer**. No third-party packages are needed.

Download or clone this repository, open a terminal in its directory, and run:

```sh
python3 Campfire.py
```

On first launch, choose your existing **saves folder** and a separate **backup storage folder**. Campfire stores these settings in a local `config.json` file.

## Restoring on another machine

Copy your backup folder to the other machine (or use a shared storage device), configure the saves and backup locations on the new machine, and follow the restore menu. 

Campfire preserves the original save format; it does not convert saves between incompatible emulators.

## Protecting your saves

**Restore is the only operation that can overwrite files in your configured saves folder.** Always check the selected backup and destination before confirming a restore. Keep an independent copy of important saves.

- **Close the emulator first.** An emulator running during backup or restore may change save files or overwrite restored progress.
- **Review restore confirmations.** Restoring a directory can replace newer saves and emulator settings. Restoring a memory-card file replaces the entire card, which may contain saves for several games.
- **Review cleanup before deleting.** Cleanup deletes stored backups, not local saves. Older backups may contain files missing from newer versions. Deletion is permanent.

## Configuration

Optional settings in `config.json`:

- `"save_extensions": ["customsave"]` adds recognized file types.
- `"show_all_files": true` includes unknown files in single-file backup selection.

## Development

Run the automated tests:

```sh
python -m unittest discover -s tests -v
```
