"""Interactive, verified backups for emulator save files and directories."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
import uuid

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
# Extensions are candidates, not proof of a particular console or encoding.
SAVE_EXTENSIONS = frozenset("""
.sav .srm .dsv .gci .gcs .gsv .raw .mci .mcr .mcd .mc .mc2 .ps2
.psv .vmp .vmc .vmu .vms .vmi .bin .dat .eep .eeprom .fla .fsh .sra
.fram .nv .nvm .nvram .ram .sram .brm .bcr .bup .crm .rtc .mpk .smp
.mem .memory .save .sgm .sg1 .sg2 .sg3 .sg4 .sg5 .sg6 .sg7 .sg8 .sg9
.state .st0 .st1 .st2 .st3 .st4 .st5 .st6 .st7 .st8 .st9
.ss0 .ss1 .ss2 .ss3 .ss4 .ss5 .ss6 .ss7 .ss8 .ss9 .dst .ds0 .ds1
.zst .zs1 .zs2 .zs3 .zs4 .zs5 .zs6 .zs7 .zs8 .zs9 .pj .pj0 .pj1
.p2s .ppst .rts .sta .freeze
""".split())
MANIFEST = "manifest.json"


def operation_notice(operation):
    print(f"{operation} files, do not interrupt or disconnect storage media", flush=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_link(path):
    """Treat Windows junctions/reparse points as links, including on Python 3.11."""
    try:
        info = Path(path).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def inside(path, root):
    return path == root or root in path.parents


def is_save_file(path, extensions=()):
    known = SAVE_EXTENSIONS | {"." + e.lower().lstrip(".") for e in extensions}
    path = Path(path)
    return path.suffix.lower() in known or bool(re.search(
        r"\.(?:state\d*|ds\d+|zs\d+|ss\d+)(?:\.auto)?$", path.name, re.I))


def discover(root, *, exclude=(), extensions=(), all_files=False):
    """Walk every real directory; report unreadable paths, never follow symlinks."""
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    excluded = [Path(p).expanduser().resolve() for p in exclude]
    files, errors = [], []
    for directory, dirs, names in os.walk(root, onerror=lambda e: errors.append(str(e))):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".campfire-restore-") and not is_link(base / d)
                         and not any(inside(base / d, p) for p in excluded))
        for name in sorted(names):
            path = base / name
            if is_link(path) or any(inside(path, p) for p in excluded):
                continue
            if all_files or is_save_file(path, extensions):
                if path.is_file():
                    files.append(path.relative_to(root))
    return sorted(files), errors


def portable_relative(value):
    """Manifests use slash-separated relative paths on every operating system."""
    text = str(value).replace("\\", "/")
    windows = PureWindowsPath(text)
    path = PurePosixPath(text)
    if windows.drive or windows.root or path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe relative path: {value}")
    return path.as_posix()


def source_name(source):
    return PurePosixPath(source.replace("\\", "/")).name


def safe_path(root, relative):
    """Reject traversal and symlink components, including in imported manifests."""
    root = Path(root).resolve()
    rel = Path(portable_relative(relative))
    if rel.is_absolute() or not rel.parts or any(p in ("..", ".") for p in rel.parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    current = root
    for part in rel.parts:
        current /= part
        if is_link(current):
            raise ValueError(f"Symlink is not a save destination: {current}")
    if not inside(current.resolve(), root):
        raise ValueError(f"Path escapes directory: {relative}")
    return current


def create_backup(source, storage, relative):
    return create_snapshot(source, storage, [str(Path(relative))], kind="single")


def create_snapshot(source, storage, paths, *, directory_paths=(), kind="single"):
    source, storage = Path(source).resolve(), Path(storage).resolve()
    if inside(source, storage):
        raise ValueError("The save directory cannot be inside the backup directory.")
    if kind not in ("single", "directory") or (kind == "single" and len(paths) != 1):
        raise ValueError("Choose one file or a directory backup.")
    if not paths and kind != "directory":
        raise ValueError("No saves found.")
    storage.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=storage))
    try:
        (staging / "files").mkdir()
        records = []
        for relative in paths:
            relative = portable_relative(relative)
            original = safe_path(source, relative)
            if inside(original, storage):
                raise ValueError("Cannot back up the backup directory.")
            copied = safe_path(staging / "files", relative)
            copied.parent.mkdir(parents=True, exist_ok=True)
            before = digest(original)
            shutil.copy2(original, copied)
            if digest(copied) != before or digest(original) != before:
                raise ValueError(f"Save changed or verification failed: {relative}")
            records.append({"path": relative, "sha256": before})
        for relative in directory_paths:
            safe_path(staging / "files", relative).mkdir(parents=True, exist_ok=True)
        manifest = {"version": 1, "created": datetime.now(timezone.utc).isoformat(),
                    "source": str(source), "kind": kind, "files": records,
                    "directories": [portable_relative(path) for path in directory_paths]}
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        target = storage / (datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
                            + "-" + uuid.uuid4().hex[:12])
        staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def create_directory_backup(source, storage):
    """Copy the complete regular-file tree, including hidden and empty directories."""
    source, storage = Path(source).expanduser().resolve(strict=True), Path(storage).resolve()
    if not source.is_dir() or inside(source, storage):
        raise ValueError("Choose a directory outside the backup directory.")
    paths, directory_paths = [], []
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(source, onerror=fail):
        base = Path(directory)
        for name in list(dirs) + files:
            path = base / name
            if inside(path, storage):
                if name in dirs:
                    dirs.remove(name)
                continue
            if is_link(path) or not (path.is_dir() or path.is_file()):
                raise ValueError(f"Directory backup cannot copy symlinks or special files: {path}")
            relative = path.relative_to(source)
            if path.is_dir():
                directory_paths.append(relative)
            else:
                paths.append(relative)
    return create_snapshot(source, storage, sorted(paths),
                           directory_paths=sorted(directory_paths), kind="directory")


def read_backup(folder):
    folder = Path(folder).resolve(strict=True)
    manifest_path = folder / MANIFEST
    if is_link(manifest_path) or is_link(folder / "files"):
        raise ValueError("Backup metadata and payload must not be symlinks.")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("files"), list)):
        raise ValueError("Unsupported backup manifest.")
    if data.get("kind") not in ("single", "directory") or not isinstance(data.get("directories"), list):
        raise ValueError("Backup must use the current single-file or directory format.")
    if (not isinstance(data.get("source"), str) or not data["source"]
            or not isinstance(data.get("created"), str)):
        raise ValueError("Backup is missing source or creation time.")
    try:
        datetime.fromisoformat(data["created"])
    except ValueError:
        raise ValueError("Invalid backup creation time.") from None
    if not (folder / "files").is_dir():
        raise ValueError("Backup payload directory is missing.")
    if data.get("kind") == "single" and len(data["files"]) != 1:
        raise ValueError("Single-file backup must contain exactly one file.")
    seen = set()
    for entry in data["files"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("Invalid backup file record.")
        entry["path"] = portable_relative(entry["path"])
        path = safe_path(folder / "files", entry["path"])
        if path in seen:
            raise ValueError("Backup contains duplicate file paths.")
        seen.add(path)
    directories_seen = set()
    for relative in data["directories"]:
        if not isinstance(relative, str):
            raise ValueError("Invalid backup directory path.")
        path = safe_path(folder / "files", relative)
        if path in seen or path in directories_seen:
            raise ValueError("Backup contains duplicate or conflicting paths.")
        directories_seen.add(path)
    if any(parent in seen for path in seen | directories_seen for parent in path.parents):
        raise ValueError("Backup contains conflicting file and directory paths.")
    data["directories"] = [portable_relative(path) for path in data["directories"]]
    return data, folder / "files"


def copy_verified(source, destination, expected):
    if digest(source) != expected:
        raise ValueError(f"Backup checksum mismatch: {source}")
    shutil.copy2(source, destination)
    if digest(destination) != expected or digest(source) != expected:
        raise ValueError("Copy verification failed or source changed.")


def verified_write(source, destination, expected, *, overwrite=False):
    """Verify a staged file, then publish without depending on hard-link support."""
    source, destination = Path(source), Path(destination)
    if is_link(destination):
        raise ValueError(f"Refusing symlink destination: {destination}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Destination already exists: {destination}")
    parent = destination.parent
    missing = []
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    fd, name = tempfile.mkstemp(prefix=".campfire-", dir=parent)
    os.close(fd)
    staged = Path(name)
    reserved = False
    created = []
    try:
        copy_verified(source, staged, expected)
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
        if not overwrite:
            # Exclusive creation prevents silently overwriting a concurrent new save.
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            reserved = True
        os.replace(staged, destination)
        reserved = False
    except BaseException:
        if reserved:
            destination.unlink()
        for directory in reversed(created):
            directory.rmdir()
        raise
    finally:
        staged.unlink(missing_ok=True)


def selected_entry(folder, relative=None):
    manifest, payload = read_backup(folder)
    entries = manifest["files"]
    if relative is None:
        if len(entries) != 1:
            raise ValueError("Select one file from this directory backup.")
        entry = entries[0]
    else:
        entry = next((e for e in entries if e["path"] == portable_relative(relative)), None)
        if entry is None:
            raise ValueError(f"File is not in this backup: {relative}")
    return manifest, payload, entry


def restore_targets(folder, destination, relative=None, *, exclude=()):
    """Locate the selected filename in the current emulator's directory tree."""
    manifest, _, entry = selected_entry(folder, relative)
    destination = Path(destination).expanduser().resolve(strict=True)
    paths, errors = discover(destination, exclude=exclude, all_files=True)
    if errors:
        raise ValueError("Cannot safely locate the destination: " + "; ".join(errors))
    matches = [p for p in paths if p.name == Path(entry["path"]).name]
    if matches:
        return matches
    # Retain the original layout when restoring to the same configured root.
    # A different emulator root receives the file directly, not the old core tree.
    if str(destination) == manifest.get("source"):
        return [Path(entry["path"])]
    return [Path(entry["path"]).name]


def restore_backup(folder, destination, *, relative=None, target=None, overwrite=False,
                   exclude=()):
    _, payload, entry = selected_entry(folder, relative)
    if target is None:
        candidates = restore_targets(folder, destination, relative, exclude=exclude)
        if len(candidates) != 1:
            raise ValueError("Multiple destination files match; select a destination: "
                             + ", ".join(map(str, candidates)))
        target = candidates[0]
    source = safe_path(payload, entry["path"])
    destination = safe_path(destination, target)
    if inside(destination, Path(folder).resolve()) or any(
            inside(destination, Path(p).expanduser().resolve()) for p in exclude):
        raise ValueError("Cannot restore into the backup directory.")
    verified_write(source, destination, entry["sha256"], overwrite=overwrite)
    return destination


def directory_restore_destination(folder, saves_root, storage):
    data, _ = read_backup(folder)
    if data["kind"] != "directory":
        raise ValueError("Select a directory backup.")
    saves_root = Path(saves_root).resolve(strict=True)
    if not saves_root.is_dir():
        raise ValueError("Configured saves folder must be a directory.")
    destination = safe_path(saves_root, source_name(data["source"]))
    if (inside(Path(storage).resolve(), destination)
            or any(inside(destination, root) for root in (Path(storage).resolve(), Path(folder).resolve()))):
        raise ValueError("Cannot restore into backup storage.")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Destination is not a directory: {destination}")
    return destination


def directory_restore_plan(folder, saves_root, storage):
    """Validate the complete tree before offering or executing a directory restore."""
    data, payload = read_backup(folder)
    if data["kind"] != "directory":
        raise ValueError("Select a directory backup.")
    destination = directory_restore_destination(folder, saves_root, storage)
    storage = Path(storage).resolve()
    protected = [storage, Path(folder).resolve()]
    plan, directories_to_create = [], [destination]
    for entry in data["files"]:
        source = safe_path(payload, entry["path"])
        if digest(source) != entry["sha256"]:
            raise ValueError(f"Backup checksum mismatch: {entry['path']}")
        target = safe_path(destination, entry["path"])
        if target.exists() and not target.is_file():
            raise ValueError(f"Destination is not a file: {target}")
        plan.append((source, target, entry["sha256"]))
    for relative in data["directories"]:
        if not safe_path(payload, relative).is_dir():
            raise ValueError(f"Backup is missing directory: {relative}")
        target = safe_path(destination, relative)
        if target.exists() and not target.is_dir():
            raise ValueError(f"Destination is not a directory: {target}")
        directories_to_create.append(target)
    files = {target for _, target, _ in plan}
    for target in files | set(directories_to_create) | {destination}:
        if any(inside(target, root) for root in protected):
            raise ValueError("Cannot restore into backup storage.")
        if any(parent in files or (parent.exists() and not parent.is_dir()) for parent in target.parents):
            raise ValueError(f"Conflicting parent directory: {target}")
    if files & set(directories_to_create):
        raise ValueError("Backup contains conflicting file and directory paths.")
    return plan, directories_to_create


def restore_directory(folder, saves_root, storage, *, overwrite=False):
    """Verify a complete merged tree before swapping it into the live directory."""
    plan, directory_paths = directory_restore_plan(folder, saves_root, storage)
    destination = directory_paths[0]
    if not overwrite and any(target.exists() for _, target, _ in plan):
        raise FileExistsError("Directory restore would replace existing files.")
    for pending in destination.parent.glob(".campfire-restore-*/previous"):
        raise ValueError(f"An interrupted restore has original saves at {pending}. Recover those files before retrying.")
    original_state = snapshot_state(destination) if destination.exists() else None
    work = Path(tempfile.mkdtemp(prefix=".campfire-restore-", dir=destination.parent))
    staged, previous = work / "ready", work / "previous"
    preserve_recovery = False
    committed = False
    expected_files = {}
    try:
        staged.mkdir()
        # Include unrelated live files in the replacement tree and verify every copy.
        if original_state is not None:
            def fail(error):
                raise error
            for directory, dirs, names in os.walk(destination, onerror=fail):
                base = Path(directory)
                for name in dirs + names:
                    source = safe_path(destination, (base / name).relative_to(destination))
                    target = safe_path(staged, source.relative_to(destination))
                    if source.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif source.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        checksum = digest(source)
                        copy_verified(source, target, checksum)
                        expected_files[target] = checksum
                    else:
                        raise ValueError(f"Cannot preserve special file: {source}")
        for directory in directory_paths[1:]:
            safe_path(staged, directory.relative_to(destination)).mkdir(parents=True, exist_ok=True)
        for source, target, checksum in plan:
            target = safe_path(staged, target.relative_to(destination))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Stage an overlay separately so read-only metadata cannot truncate a copy.
            verified_write(source, target, checksum, overwrite=True)
            expected_files[target] = checksum
        for target, checksum in expected_files.items():
            if digest(target) != checksum:
                raise ValueError(f"Staged restore verification failed: {target}")
        # Neither incomplete staging nor a changed live tree may reach the commit step.
        current_state = snapshot_state(destination) if destination.exists() else None
        if current_state != original_state:
            raise ValueError("Save directory changed during restore. Close the emulator and retry.")
        if is_link(destination):
            raise ValueError("Restore destination became a symlink.")
        if original_state is not None:
            os.rename(destination, previous)
        try:
            os.rename(staged, destination)
            committed = True
        except BaseException:
            if previous.exists():
                try:
                    os.rename(previous, destination)
                except BaseException as rollback_error:
                    preserve_recovery = True
                    raise OSError(f"Restore and rollback failed. Original saves preserved at {previous}; "
                                  "do not delete this recovery directory.") from rollback_error
            raise
        return len(plan)
    finally:
        # Never discard the original if interrupted between the two renames.
        if previous.exists() and not committed:
            preserve_recovery = True
        if not preserve_recovery:
            try:
                shutil.rmtree(work)
            except OSError as error:
                print(f"Could not remove temporary restore files at {work}: {error}")


def choose(items, prompt, labels=None):
    if not items:
        raise ValueError("No saves or backups found.")
    for i, label in enumerate(labels if labels is not None else items, 1):
        print(f"{i}: {label}")
    while True:
        try:
            index = int(input(prompt + ": ").strip()) - 1
            if 0 <= index < len(items):
                return items[index]
        except ValueError:
            pass
        print("Please select one valid number from the list.")


def configure(config):
    source = input(f"Save directory [{config.get('save_directory', '')}]: ").strip()
    storage = input(f"Backup directory [{config.get('backup_directory', '')}]: ").strip()
    source_text = source or config.get("save_directory")
    storage_text = storage or config.get("backup_directory")
    if not source_text or not storage_text:
        raise ValueError("Choose a save directory and a backup directory.")
    source = Path(source_text).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Choose a valid save directory and a backup directory.")
    storage = Path(storage_text).expanduser().resolve()
    if inside(source, storage):
        raise ValueError("The save directory cannot be inside the backup directory.")
    config.update(save_directory=str(source), backup_directory=str(storage))
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Directories saved.")


def directories(config):
    if not config.get("save_directory") or not config.get("backup_directory"):
        raise ValueError("Set your save and backup directories first.")
    source = Path(config["save_directory"]).expanduser().resolve(strict=True)
    storage = Path(config["backup_directory"]).expanduser().resolve()
    if not source.is_dir() or inside(source, storage):
        raise ValueError("Save directory must be a directory outside the backup directory.")
    return source, storage


def backup_groups(storage):
    """One row per source save or directory."""
    groups = {}
    if not Path(storage).is_dir():
        return groups
    for folder in sorted(Path(storage).iterdir()):
        if not folder.is_dir() or is_link(folder) or folder.name.startswith("."):
            continue
        try:
            data, _ = read_backup(folder)
            kind, source = data.get("kind"), data["source"]
            if kind == "directory":
                key = (kind, source_name(source))
                label = source_name(source) + "/"
            elif kind == "single":
                relative = data["files"][0]["path"]
                key = (kind, relative)
                label = relative
            try:
                created = datetime.fromisoformat(data["created"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError("Invalid backup creation time.")
            group = groups.setdefault(key, {"label": label, "snapshots": []})
            group["snapshots"].append({"folder": folder, "data": data, "created": created})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            print(f"Skipping invalid backup {folder.name}: {error}")
    for group in groups.values():
        group["snapshots"].sort(key=lambda item: (item["created"], item["folder"].name), reverse=True)
    labels = [group["label"] for group in groups.values()]
    for group in groups.values():
        if labels.count(group["label"]) > 1:
            source = group["snapshots"][0]["data"]["source"]
            group["label"] += f" | {source}"
    return groups


def choose_group(storage, kind=None):
    operation_notice("Loading")
    groups = [group for group in backup_groups(storage).values()
              if kind is None or group["snapshots"][0]["data"]["kind"] == kind]
    labels = [f"{g['label']} ({len(g['snapshots'])} versions)" for g in groups]
    return choose(groups, "Select backed-up save or directory", labels=labels)


def choose_snapshot(snapshots):
    labels = [f"{item['data']['created']} | {item['folder'].name}" for item in snapshots]
    return choose(snapshots, "Select backup version", labels=labels)


def verify_snapshot(folder):
    data, payload = read_backup(folder)
    for entry in data["files"]:
        if digest(safe_path(payload, entry["path"])) != entry["sha256"]:
            raise ValueError(f"Retained backup failed verification: {folder}")
    for relative in data.get("directories", []):
        if not safe_path(payload, relative).is_dir():
            raise ValueError(f"Retained backup is missing a directory: {relative}")
    if not payload.is_dir():
        raise ValueError("Retained backup payload is missing.")


def snapshot_state(folder):
    """Capture a preview fingerprint and size without following links."""
    if is_link(folder) or not folder.is_dir():
        raise ValueError("Backup directory changed or is a symlink.")
    state, size = [], 0
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(folder, onerror=fail):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            if is_link(path) or not (path.is_dir() or path.is_file()):
                raise ValueError(f"Cannot clean up a backup with links or special files: {path}")
            stat = path.stat()
            state.append((str(path.relative_to(folder)), stat.st_ino, stat.st_size, stat.st_mtime_ns))
            if path.is_file():
                size += stat.st_size
    return sorted(state), size


def plan_cleanup(group, *, keep=3):
    snapshots = group["snapshots"]
    if keep < 1:
        raise ValueError("Keep at least one backup version.")
    candidates, retained = snapshots[keep:], snapshots[:keep]
    if not retained:
        raise ValueError("Keep at least one backup version.")
    if not candidates:
        return None
    newest = retained[0]["folder"]
    verify_snapshot(newest)
    states = {item["folder"]: snapshot_state(item["folder"]) for item in candidates}
    return {"candidates": candidates, "newest": newest, "retained_state": snapshot_state(newest),
            "states": states, "bytes": sum(size for _, size in states.values())}


def execute_cleanup(plan, storage, live_source):
    """Recheck the reviewed plan before deleting any complete snapshot."""
    storage, live_source = Path(storage).resolve(), Path(live_source).resolve()
    for folder in list(plan["states"]) + [plan["newest"]]:
        if is_link(folder) or folder.resolve().parent != storage or inside(live_source, folder.resolve()):
            raise ValueError("Cleanup must target backup snapshots, never live saves.")
        data, _ = read_backup(folder)
        if inside(Path(data["source"]).resolve(), folder.resolve()):
            raise ValueError("Snapshot contains its recorded live source; refusing deletion.")
    if snapshot_state(plan["newest"]) != plan["retained_state"]:
        raise ValueError("Retained backup changed since the preview; run cleanup again.")
    verify_snapshot(plan["newest"])
    for folder, state in plan["states"].items():
        if snapshot_state(folder) != state:
            raise ValueError("Backup changed since the preview; run cleanup again.")
    for folder in plan["states"]:
        shutil.rmtree(folder)
        print(f"Deleted backup: {folder.name}")


def cleanup_menu(storage, live_source):
    group = choose_group(storage)
    keep = int(input("Number of newest versions to keep [3]: ").strip() or "3")
    operation_notice("Verifying")
    plan = plan_cleanup(group, keep=keep)
    if plan is None:
        print("No older backups to remove.")
        return
    print("Delete these complete backup snapshots:")
    for item in plan["candidates"]:
        print(f"{item['data']['created']} | {item['folder'].name}")
    print(f"Estimated file data removed: {plan['bytes'] / (1024 * 1024):.2f} MiB")
    print(f"Newest retained backup verified: {plan['newest'].name}")
    print("Older snapshots may contain files absent from newer versions. Deletion is permanent.")
    if input("Delete these backups? [y/N]: ").strip().lower() in ("y", "yes"):
        operation_notice("Deleting")
        execute_cleanup(plan, storage, live_source)
    else:
        print("Cancelled.")


def checked_directory(root, selected, storage):
    """Only accept real immediate subdirectories of the configured saves root."""
    root, selected, storage = Path(root).resolve(), Path(selected), Path(storage).resolve()
    if selected.parent != root:
        raise ValueError("Choose a directory listed inside the saves folder.")
    result = safe_path(root, selected.name)
    if not result.is_dir() or inside(result.resolve(), storage):
        raise ValueError("Choose a save directory outside backup storage.")
    return result


def browse_directory(start, storage):
    """List only immediate subdirectories; no path entry or upward navigation."""
    root, storage = Path(start).resolve(), Path(storage).resolve()
    children = sorted((p for p in root.iterdir()
                       if not p.name.startswith(".campfire-restore-") and not is_link(p) and p.is_dir() and not inside(p, storage)),
                      key=lambda p: p.name.casefold())
    choices = children + [None]
    labels = [p.name + "/" for p in children] + ["[Cancel]"]
    selected = choose(choices, "Select directory", labels=labels)
    if selected is None:
        return None
    return checked_directory(root, selected, storage)


def interactive(config):
    if not config.get("save_directory") or not config.get("backup_directory"):
        configure(config)
    print("1: Back up\n2: Restore\n3: Change directories\n4: Delete outdated backups")
    operation = input("Select operation: ").strip()
    if operation == "3":
        configure(config)
        return
    source, storage = directories(config)
    if operation == "1":
        mode = choose(["File", "Directory", "Cancel"], "What would you like to back up?")
        if mode == "Cancel":
            return
        if mode == "File":
            operation_notice("Scanning")
            paths, errors = discover(source, exclude=[storage],
                                     extensions=config.get("save_extensions", []),
                                     all_files=config.get("show_all_files", False))
            for error in errors:
                print(f"Scan warning: {error}")
            selected = choose(paths, "Select one save to back up")
            operation_notice("Backing up")
            print(f"Backup successful: {create_backup(source, storage, selected)}")
        else:
            directory = browse_directory(source, storage)
            if directory is None:
                return
            print(f"Copy all files and subdirectories from {directory} into {storage}.")
            print("The backup directory itself is excluded.")
            if input("Back up this directory? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
            directory = checked_directory(source, directory, storage)
            operation_notice("Backing up")
            print(f"Directory backup successful: {create_directory_backup(directory, storage)}")
    elif operation == "2":
        mode = choose(["File", "Directory", "Cancel"], "What would you like to restore?")
        if mode == "Cancel":
            return
        if mode == "Directory":
            group = choose_group(storage, kind="directory")
            snapshot = choose_snapshot(group["snapshots"])
            folder = snapshot["folder"]
            destination = directory_restore_destination(folder, source, storage)
            operation_notice("Verifying")
            plan, directory_paths = directory_restore_plan(folder, source, storage)
            replaced = sum(target.exists() for _, target, _ in plan)
            print(f"Restore contents of {snapshot['data']['source']} to {destination}")
            print(f"{len(plan)} files, {len(directory_paths)} folders; {replaced} existing files will be replaced.")
            print("This can replace newer saves, entire memory cards, and emulator settings.")
            print("Unrelated files stay in place. Close the emulator before restoring.")
            if input(f"Restore this directory and replace {replaced} existing files? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
            operation_notice("Restoring")
            count = restore_directory(folder, source, storage, overwrite=bool(replaced))
            print(f"Restored {count} files to {destination}.")
            return
        group = choose_group(storage, kind="single")
        snapshot = choose_snapshot(group["snapshots"])
        folder, data = snapshot["folder"], snapshot["data"]
        relative = data["files"][0]["path"]
        candidates = restore_targets(folder, source, relative, exclude=[storage])
        target = candidates[0] if len(candidates) == 1 else choose(candidates, "Select destination save")
        destination = safe_path(source, target)
        print(f"Restore {relative} to {destination}")
        overwrite = destination.exists()
        if overwrite and input("Replace this save? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return
        operation_notice("Restoring")
        print(f"Restored: {restore_backup(folder, source, relative=relative, target=target, overwrite=overwrite, exclude=[storage])}")
    elif operation == "4":
        cleanup_menu(storage, source)
    else:
        raise ValueError("Unknown operation.")


def load_config():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object.")
    for field in ("save_directory", "backup_directory"):
        if field in config and not isinstance(config[field], str):
            raise ValueError(f"{field} in config.json must be a path string.")
    extensions = config.get("save_extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(e, str) or not e.strip(". ") for e in extensions):
        raise ValueError("save_extensions in config.json must be a list of nonempty strings.")
    if not isinstance(config.get("show_all_files", False), bool):
        raise ValueError("show_all_files in config.json must be true or false.")
    return config


def main():
    try:
        interactive(load_config())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Error: {error}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
