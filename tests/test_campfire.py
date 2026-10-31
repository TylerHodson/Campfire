import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import Campfire as c


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'saves'
        self.source.mkdir()
        self.storage = self.root / 'backups'
        self.target = self.root / 'other-emulator'
        self.target.mkdir()
        self.config = {'save_directory': str(self.source), 'backup_directory': str(self.storage)}

    def put(self, relative, content=b'game progress'):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def backup(self, relative):
        return c.create_backup(self.source, self.storage, relative)

    def test_recursive_discovery_duplicates_case_and_extensionless(self):
        self.put('Dolphin/User/GC/USA/Card A/game.GCI')
        self.put('a/game.sav')
        self.put('b/game.sav')
        self.put('README.txt')
        self.put('title/data/00000001')
        paths, errors = c.discover(self.source)
        self.assertEqual(len(paths), 3)
        self.assertFalse(errors)
        self.assertEqual(len(c.discover(self.source, all_files=True)[0]), 5)
        self.assertEqual(len(c.discover(self.source, extensions=['txt'])[0]), 4)
        for name in ['game.state12.auto', 'game.ds23', 'game.CUSTOM']:
            self.put(name)
        paths, _ = c.discover(self.source, extensions=['custom'])
        self.assertEqual(len(paths), 6)

    def test_excludes_backups_and_symlink_loops(self):
        self.put('game.sav')
        self.put('backups/old.sav')
        (self.source / 'loop').symlink_to(self.source, target_is_directory=True)
        self.assertEqual(c.discover(self.source, exclude=[self.source / 'backups'])[0], [Path('game.sav')])

    def test_backup_contains_only_selected_file(self):
        self.put('a/game.sav', b'one')
        neighbor = self.put('b/game.sav', b'two')
        backup = self.backup('a/game.sav')
        data, payload = c.read_backup(backup)
        self.assertEqual([e['path'] for e in data['files']], ['a/game.sav'])
        self.assertEqual((payload / 'a/game.sav').read_bytes(), b'one')
        self.assertEqual(neighbor.read_bytes(), b'two')

    def test_new_emulator_receives_file_without_old_core_tree(self):
        self.put('old/core/game.srm')
        backup = self.backup('old/core/game.srm')
        result = c.restore_backup(backup, self.target)
        self.assertEqual(result, self.target / 'game.srm')
        self.assertFalse((self.target / 'old').exists())
        with self.assertRaises(FileExistsError):
            c.restore_backup(backup, self.target)

    def test_same_root_retains_missing_nested_path(self):
        original = self.put('deep/core/game.gci')
        backup = self.backup('deep/core/game.gci')
        original.unlink()
        self.assertEqual(c.restore_backup(backup, self.source), original)

    def test_ambiguous_destinations_require_selection(self):
        self.put('game.sav')
        backup = self.backup('game.sav')
        for name in ['a', 'b']:
            (self.target / name).mkdir()
            (self.target / name / 'game.sav').write_bytes(name.encode())
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.target, overwrite=True)
        c.restore_backup(backup, self.target, target='b/game.sav', overwrite=True)
        self.assertEqual((self.target / 'a/game.sav').read_bytes(), b'a')
        self.assertEqual((self.target / 'b/game.sav').read_bytes(), b'game progress')

    def test_traversal_symlinks_and_backup_destination_rejected(self):
        self.put('game.sav')
        backup = self.backup('game.sav')
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.target, target='../escaped.sav')
        (self.target / 'nested').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.target, target='nested/game.sav')
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.storage, target='oops.sav', exclude=[self.storage])
        manifest_path = backup / c.MANIFEST
        data = json.loads(manifest_path.read_text())
        data['files'][0]['path'] = '../escaped.sav'
        manifest_path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.target)

    def test_failed_copy_preserves_existing_save(self):
        self.put('game.sav')
        backup = self.backup('game.sav')
        destination = self.target / 'game.sav'
        destination.write_bytes(b'existing')
        def corrupt_copy(src, dst):
            Path(dst).write_bytes(b'bad copy')
        with patch.object(c.shutil, 'copy2', side_effect=corrupt_copy):
            with self.assertRaises(ValueError):
                c.restore_backup(backup, self.target, overwrite=True)
        self.assertEqual(destination.read_bytes(), b'existing')
        self.assertEqual(list(self.target.iterdir()), [destination])
        (backup / 'files/game.sav').write_bytes(b'corrupt stored backup')
        with self.assertRaises(ValueError):
            c.restore_backup(backup, self.target, overwrite=True)
        self.assertEqual(destination.read_bytes(), b'existing')

    def test_failed_backup_leaves_no_snapshot(self):
        self.put('game.sav')
        with patch.object(c.shutil, 'copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.backup('game.sav')
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_interactive_restore_cancellation_changes_nothing(self):
        original = self.put('game.sav', b'old')
        self.backup('game.sav')
        original.write_bytes(b'new')
        with patch('builtins.input', side_effect=['2', '1', '1', '1', 'n']), contextlib.redirect_stdout(io.StringIO()):
            c.interactive(self.config)
        self.assertEqual(original.read_bytes(), b'new')

    def test_directory_backup_copies_unknown_hidden_and_empty_paths(self):
        self.put('Dolphin/GC/Card.raw', b'card')
        self.put('Dolphin/.config', b'config')
        self.put('Dolphin/title/extensionless', b'unknown')
        (self.source / 'Dolphin/empty').mkdir()
        backup = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        data, payload = c.read_backup(backup)
        self.assertEqual(len(data['files']), 3)
        self.assertEqual((payload / 'GC/Card.raw').read_bytes(), b'card')
        self.assertEqual((payload / '.config').read_bytes(), b'config')
        self.assertEqual((payload / 'title/extensionless').read_bytes(), b'unknown')
        self.assertTrue((payload / 'empty').is_dir())
        self.assertIn('empty', data['directories'])
        restored = c.restore_backup(backup, self.target, relative='title/extensionless')
        self.assertEqual(restored.read_bytes(), b'unknown')

    def test_directory_backup_rejects_symlinks_and_excludes_storage(self):
        self.put('game.sav')
        storage = self.source / 'backups'
        storage.mkdir()
        (storage / 'ignored.sav').write_bytes(b'old')
        backup = c.create_directory_backup(self.source, storage)
        self.assertEqual([e['path'] for e in c.read_backup(backup)[0]['files']], ['game.sav'])
        (self.source / 'link').symlink_to(self.target, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.create_directory_backup(self.source, storage)
        self.assertEqual(len(list(storage.iterdir())), 2)

    def test_menu_backs_up_selected_directory(self):
        self.put('Dolphin/unknown-file', b'data')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['1', '2', '1', 'y']), contextlib.redirect_stdout(output):
            c.interactive(self.config)
        backup = next(self.storage.iterdir())
        self.assertEqual(c.read_backup(backup)[0]['files'][0]['path'], 'unknown-file')

    def group_for(self, folder):
        return next(group for group in c.backup_groups(self.storage).values()
                    if any(item['folder'] == folder for item in group['snapshots']))

    def test_distinct_relative_paths_have_separate_backup_histories(self):
        self.put('a/game.sav')
        self.put('b/game.sav')
        first = self.backup('a/game.sav')
        self.backup('a/game.sav')
        second = self.backup('b/game.sav')
        directory = c.create_directory_backup(self.source, self.storage)
        self.assertEqual(c.read_backup(first)[0]['kind'], 'single')
        self.assertEqual(c.read_backup(directory)[0]['kind'], 'directory')
        self.assertEqual(len(c.backup_groups(self.storage)), 3)
        self.assertEqual(len(self.group_for(first)['snapshots']), 2)
        self.assertEqual(len(self.group_for(second)['snapshots']), 1)

    def test_cleanup_keeps_newest_three_and_live_files(self):
        live = self.put('game.sav')
        snapshots = [self.backup('game.sav') for _ in range(5)]
        group = self.group_for(snapshots[0])
        plan = c.plan_cleanup(group)
        self.assertEqual(len(plan['candidates']), 2)
        self.assertGreater(plan['bytes'], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            c.execute_cleanup(plan, self.storage, self.source)
        self.assertEqual(len(list(self.storage.iterdir())), 3)
        for item in group['snapshots'][:3]:
            self.assertTrue(item['folder'].exists())
        self.assertEqual(live.read_bytes(), b'game progress')
        self.assertIsNone(c.plan_cleanup(self.group_for(group['snapshots'][0]['folder'])))

    def test_cleanup_rejects_zero_keep_and_corrupt_retained_backup(self):
        self.put('game.sav')
        old = self.backup('game.sav')
        newest = self.backup('game.sav')
        group = self.group_for(old)
        with self.assertRaises(ValueError):
            c.plan_cleanup(group, keep=0)
        (newest / 'files/game.sav').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            c.plan_cleanup(group, keep=1)
        self.assertTrue(old.exists())

    def test_cleanup_cancellation_preserves_every_snapshot(self):
        self.put('game.sav')
        for _ in range(4):
            self.backup('game.sav')
        before = set(self.storage.iterdir())
        with patch('builtins.input', side_effect=['4', '1', '', 'n']), contextlib.redirect_stdout(io.StringIO()):
            c.interactive(self.config)
        self.assertEqual(set(self.storage.iterdir()), before)

    def test_cleanup_refuses_changes_since_preview(self):
        self.put('game.sav')
        old = self.backup('game.sav')
        self.backup('game.sav')
        plan = c.plan_cleanup(self.group_for(old), keep=1)
        (old / 'new-file').write_bytes(b'added after preview')
        with self.assertRaises(ValueError):
            c.execute_cleanup(plan, self.storage, self.source)
        self.assertTrue(old.exists())

    def test_cleanup_never_deletes_a_live_source(self):
        self.put('game.sav')
        old = self.backup('game.sav')
        self.backup('game.sav')
        plan = c.plan_cleanup(self.group_for(old), keep=1)
        with self.assertRaises(ValueError):
            c.execute_cleanup(plan, self.storage, old / 'files')
        self.assertTrue(old.exists())

    def test_invalid_metadata_is_excluded_from_cleanup(self):
        self.put('game.sav')
        backup = self.backup('game.sav')
        (backup / c.MANIFEST).write_text('{bad json')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.backup_groups(self.storage), {})
        self.assertTrue(backup.exists())

    def test_directory_group_stays_together_when_contents_change(self):
        self.put('old-only.txt')
        old = c.create_directory_backup(self.source, self.storage)
        (self.source / 'old-only.txt').unlink()
        self.put('new-only.sav')
        c.create_directory_backup(self.source, self.storage)
        self.assertEqual(len(self.group_for(old)['snapshots']), 2)
        plan = c.plan_cleanup(self.group_for(old), keep=1)
        self.assertEqual(plan['candidates'][0]['folder'], old)

    def test_file_restore_lists_only_file_backups(self):
        game = self.put('GC/game.sav', b'old')
        neighbor = self.put('GC/other.sav', b'old neighbor')
        c.create_directory_backup(self.source, self.storage)
        self.backup('GC/game.sav')
        game.write_bytes(b'new')
        neighbor.write_bytes(b'new neighbor')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['2', '1', '1', '1', 'y']), contextlib.redirect_stdout(output):
            c.interactive(self.config)
        self.assertEqual(game.read_bytes(), b'old')
        self.assertEqual(neighbor.read_bytes(), b'new neighbor')
        self.assertNotIn('saves/ (1 versions)', output.getvalue())
        self.assertIn('GC/game.sav (1 versions)', output.getvalue())

    def test_directory_picker_lists_only_saves_subdirectories(self):
        self.put('Dolphin/GC/game.gci')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['1']), contextlib.redirect_stdout(output):
            self.assertEqual(c.browse_directory(self.source, self.storage), self.source / 'Dolphin')
        self.assertNotIn('[Up]', output.getvalue())
        self.assertNotIn('GC/', output.getvalue())
        self.assertNotIn('Use this directory', output.getvalue())
        with patch('builtins.input', side_effect=['2']), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(c.browse_directory(self.source, self.storage))

    def test_directory_picker_hides_backup_storage_and_symlinks(self):
        storage = self.source / 'backups'
        storage.mkdir()
        (self.source / 'link').symlink_to(self.target, target_is_directory=True)
        output = io.StringIO()
        with patch('builtins.input', side_effect=['1']), contextlib.redirect_stdout(output):
            self.assertIsNone(c.browse_directory(self.source, storage))
        self.assertNotIn('backups/', output.getvalue())
        self.assertNotIn('link/', output.getvalue())

    def test_directory_boundary_rejects_root_parents_outside_and_links(self):
        for selected in (self.source, self.source.parent, self.target, self.source / '../other-emulator'):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                c.checked_directory(self.source, selected, self.storage)
        (self.source / 'link').symlink_to(self.target, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.checked_directory(self.source, self.source / 'link', self.storage)

    def test_directory_selection_rechecked_before_backup(self):
        selected = self.source / 'Dolphin'
        selected.mkdir()
        def answer(prompt):
            if prompt == 'Select operation: ':
                return '1'
            if prompt.startswith('What would'):
                return '2'
            if prompt == 'Select directory: ':
                return '1'
            selected.rmdir()
            selected.symlink_to(self.target, target_is_directory=True)
            return 'y'
        with patch('builtins.input', side_effect=answer), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                c.interactive(self.config)
        self.assertFalse(self.storage.exists())

    def test_matching_relative_paths_share_versions_across_roots(self):
        self.put('core/game.sav')
        folder = self.backup('core/game.sav')
        self.assertEqual(self.group_for(folder)['label'], 'core/game.sav')
        other = self.target / 'core/game.sav'
        other.parent.mkdir()
        other.write_bytes(b'other')
        c.create_backup(self.target, self.storage, 'core/game.sav')
        groups = list(c.backup_groups(self.storage).values())
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]['snapshots']), 2)
        self.assertEqual(groups[0]['label'], 'core/game.sav')
        directory = c.create_directory_backup(self.source, self.storage)
        self.assertEqual(self.group_for(directory)['label'], 'saves/')

    def test_directory_restore_preserves_layout_and_unrelated_files(self):
        self.put('GC/game.gci', b'backup')
        (self.source / 'empty').mkdir()
        folder = c.create_directory_backup(self.source, self.storage)
        (self.target / 'saves/GC').mkdir(parents=True)
        save = self.target / 'saves/GC/game.gci'
        save.write_bytes(b'newer')
        other = self.target / 'unrelated.sav'
        other.write_bytes(b'keep')
        inside = self.target / 'saves/unrelated.dat'
        inside.write_bytes(b'also keep')
        with self.assertRaises(FileExistsError):
            c.restore_directory(folder, self.target, self.storage)
        self.assertEqual(save.read_bytes(), b'newer')
        self.assertEqual(c.restore_directory(folder, self.target, self.storage, overwrite=True), 1)
        self.assertEqual(save.read_bytes(), b'backup')
        self.assertEqual(other.read_bytes(), b'keep')
        self.assertEqual(inside.read_bytes(), b'also keep')
        self.assertTrue((self.target / 'saves/empty').is_dir())

    def test_directory_restore_preflight_detects_corruption_before_writing(self):
        self.put('a.sav')
        self.put('b.sav')
        folder = c.create_directory_backup(self.source, self.storage)
        (folder / 'files/b.sav').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_directory_restore_blocks_backup_storage_and_symlinks(self):
        self.put('nested/game.sav')
        folder = c.create_directory_backup(self.source, self.storage)
        with self.assertRaises(ValueError):
            c.restore_directory(folder, self.storage, self.storage, overwrite=True)
        (self.target / 'saves').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.restore_directory(folder, self.target, self.storage, overwrite=True)

    def test_directory_restore_menu_requires_explicit_confirmation(self):
        save = self.put('Dolphin/game.sav', b'old')
        c.create_directory_backup(self.source / 'Dolphin', self.storage)
        save.write_bytes(b'new')
        for answer, expected in [('n', b'new'), ('y', b'old')]:
            output = io.StringIO()
            with patch('builtins.input', side_effect=['2', '2', '1', '1', answer]), contextlib.redirect_stdout(output):
                c.interactive(self.config)
            self.assertIn('1 existing files will be replaced', output.getvalue())
            self.assertEqual(save.read_bytes(), expected)

    def test_directory_restore_creates_outer_folder_on_another_machine(self):
        self.put('Dolphin/GC/card.raw', b'card')
        (self.source / 'Dolphin/empty').mkdir()
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        config = dict(self.config, save_directory=str(self.target))
        self.assertEqual(list(self.target.iterdir()), [])
        with patch('builtins.input', side_effect=['2', '2', '1', '1', 'y']), contextlib.redirect_stdout(io.StringIO()):
            c.interactive(config)
        self.assertEqual((self.target / 'Dolphin/GC/card.raw').read_bytes(), b'card')
        self.assertTrue((self.target / 'Dolphin/empty').is_dir())
        self.assertFalse((self.target / 'GC').exists())
        self.assertFalse((self.target / 'Dolphin/Dolphin').exists())

    def test_cancel_directory_restore_does_not_create_outer_folder(self):
        self.put('Dolphin/game.sav')
        c.create_directory_backup(self.source / 'Dolphin', self.storage)
        config = dict(self.config, save_directory=str(self.target))
        with patch('builtins.input', side_effect=['2', '2', '1', '1', 'n']), contextlib.redirect_stdout(io.StringIO()):
            c.interactive(config)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_restore_empty_directory_creates_outer_folder(self):
        (self.source / 'Dolphin').mkdir()
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        self.assertEqual(c.restore_directory(folder, self.target, self.storage), 0)
        self.assertTrue((self.target / 'Dolphin').is_dir())

    def test_restore_outer_folder_file_conflict_leaves_file_untouched(self):
        self.put('Dolphin/game.sav')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        conflict = self.target / 'Dolphin'
        conflict.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(conflict.read_bytes(), b'keep')

    def test_malformed_settings_report_an_error_without_traceback(self):
        config_path = self.root / 'config.json'
        invalid = [[], None, {'save_directory': 42}, {'save_extensions': 'sav'},
                   {'save_extensions': [None]}, {'show_all_files': 'false'}]
        for config in invalid:
            with self.subTest(config=config):
                config_path.write_text(json.dumps(config))
                output = io.StringIO()
                with patch.object(c, 'CONFIG_PATH', config_path), contextlib.redirect_stdout(output):
                    self.assertEqual(c.main(), 1)
                self.assertIn('Error:', output.getvalue())

    def test_invalid_manifest_records_are_rejected(self):
        self.put('game.sav')
        folder = self.backup('game.sav')
        path = folder / c.MANIFEST
        original = json.loads(path.read_text())
        for changes in ({'files': [None]}, {'files': [{'path': [], 'sha256': 'a' * 64}]},
                        {'directories': [None]}, {'directories': ['game.sav']},
                        {'directories': ['game.sav/child']}, {'kind': 'unknown'}, {'version': 999},
                        {'created': 'not a date'}, {'source': ''}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps(dict(original, **changes)))
                with self.assertRaises(ValueError):
                    c.read_backup(folder)

    def test_windows_and_linux_directory_backups_share_versions(self):
        self.put('dolphin-emu/User/Cache/GZLE01.uidcache', b'cache')
        linux = c.create_directory_backup(self.source / 'dolphin-emu', self.storage)
        windows = c.create_directory_backup(self.source / 'dolphin-emu', self.storage)
        path = windows / c.MANIFEST
        data = json.loads(path.read_text())
        data['source'] = r'D:\NOTREAL_Saves\dolphin-emu'
        for entry in data['files']:
            entry['path'] = entry['path'].replace('/', chr(92))
        data['directories'] = [name.replace('/', chr(92)) for name in data['directories']]
        path.write_text(json.dumps(data))
        groups = c.backup_groups(self.storage)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(self.group_for(linux)['snapshots']), 2)
        with patch.object(c.os, 'link', side_effect=OSError('Incorrect function')):
            c.restore_directory(windows, self.target, self.storage)
        self.assertEqual((self.target / 'dolphin-emu/User/Cache/GZLE01.uidcache').read_bytes(), b'cache')

    def test_single_restore_does_not_require_hard_links(self):
        self.put('game.sav')
        folder = self.backup('game.sav')
        with patch.object(c.os, 'link', side_effect=OSError('Incorrect function')):
            result = c.restore_backup(folder, self.target)
        self.assertEqual(result.read_bytes(), b'game progress')

    def test_staging_copy_failure_never_changes_existing_directory(self):
        self.put('Dolphin/a.sav', b'backup a')
        self.put('Dolphin/b.sav', b'backup b')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        destination = self.target / 'Dolphin'
        destination.mkdir()
        (destination / 'a.sav').write_bytes(b'newer')
        (destination / 'unrelated').write_bytes(b'preserve')
        before = c.snapshot_state(destination)
        original_copy = c.shutil.copy2
        def failing_copy(source, target, *args, **kwargs):
            if Path(source) == folder / 'files/b.sav':
                raise OSError('disk full')
            return original_copy(source, target, *args, **kwargs)
        with patch.object(c.shutil, 'copy2', side_effect=failing_copy):
            with self.assertRaises(OSError):
                c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(c.snapshot_state(destination), before)
        self.assertEqual((destination / 'a.sav').read_bytes(), b'newer')
        self.assertEqual((destination / 'unrelated').read_bytes(), b'preserve')
        self.assertEqual(list(self.target.iterdir()), [destination])

    def test_staging_verification_failure_does_not_create_destination(self):
        self.put('Dolphin/game.sav')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        with patch.object(c.shutil, 'copy2', side_effect=lambda src, dst: Path(dst).write_bytes(b'corrupt')):
            with self.assertRaises(ValueError):
                c.restore_directory(folder, self.target, self.storage)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_failed_directory_swap_rolls_back_original(self):
        self.put('Dolphin/game.sav', b'backup')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        destination = self.target / 'Dolphin'
        destination.mkdir()
        (destination / 'game.sav').write_bytes(b'original')
        before = c.snapshot_state(destination)
        rename = c.os.rename
        def fail_publish(src, dst):
            if Path(src).name == 'ready':
                raise OSError('simulated Windows rename failure')
            return rename(src, dst)
        with patch.object(c.os, 'rename', side_effect=fail_publish):
            with self.assertRaises(OSError):
                c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(c.snapshot_state(destination), before)
        self.assertEqual((destination / 'game.sav').read_bytes(), b'original')
        self.assertEqual(list(self.target.iterdir()), [destination])

    def test_failed_rollback_preserves_recovery_directory(self):
        self.put('Dolphin/game.sav', b'backup')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        destination = self.target / 'Dolphin'
        destination.mkdir()
        (destination / 'game.sav').write_bytes(b'original')
        rename = c.os.rename
        def fail_publish_and_rollback(src, dst):
            if Path(src).name in ('ready', 'previous'):
                raise OSError('device unavailable')
            return rename(src, dst)
        with patch.object(c.os, 'rename', side_effect=fail_publish_and_rollback):
            with self.assertRaisesRegex(OSError, 'Original saves preserved'):
                c.restore_directory(folder, self.target, self.storage, overwrite=True)
        recovery = next(self.target.glob('.campfire-restore-*/previous'))
        self.assertEqual((recovery / 'game.sav').read_bytes(), b'original')
        with self.assertRaisesRegex(ValueError, 'interrupted restore'):
            c.restore_directory(folder, self.target, self.storage)
        self.assertTrue(recovery.exists())

    def test_single_file_publish_failure_removes_only_new_placeholder(self):
        self.put('game.sav')
        folder = self.backup('game.sav')
        with patch.object(c.os, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                c.restore_backup(folder, self.target, target='nested/game.sav')
        self.assertEqual(list(self.target.iterdir()), [])

    def test_portable_paths_reject_windows_escape_on_every_platform(self):
        for path in (r'C:\outside.sav', r'..\outside.sav', r'\\server\share\save', 'C:relative.sav'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                c.safe_path(self.target, path)

    def test_live_changes_during_staging_abort_restore(self):
        self.put('Dolphin/game.sav', b'backup')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        destination = self.target / 'Dolphin'
        destination.mkdir()
        live = destination / 'game.sav'
        live.write_bytes(b'original')
        original_copy = c.shutil.copy2
        def concurrent_write(src, dst, *args, **kwargs):
            result = original_copy(src, dst, *args, **kwargs)
            if Path(src) == folder / 'files/game.sav':
                live.write_bytes(b'emulator changed this save')
            return result
        with patch.object(c.shutil, 'copy2', side_effect=concurrent_write):
            with self.assertRaisesRegex(ValueError, 'changed during restore'):
                c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(live.read_bytes(), b'emulator changed this save')
        self.assertEqual(list(self.target.iterdir()), [destination])

    def test_failure_to_move_original_leaves_live_tree_untouched(self):
        self.put('Dolphin/game.sav')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        destination = self.target / 'Dolphin'
        destination.mkdir()
        live = destination / 'game.sav'
        live.write_bytes(b'original')
        with patch.object(c.os, 'rename', side_effect=PermissionError('file locked on Windows')):
            with self.assertRaises(PermissionError):
                c.restore_directory(folder, self.target, self.storage, overwrite=True)
        self.assertEqual(live.read_bytes(), b'original')
        self.assertEqual(list(self.target.iterdir()), [destination])

    def test_failed_initial_directory_publish_leaves_no_live_directory(self):
        self.put('Dolphin/game.sav')
        folder = c.create_directory_backup(self.source / 'Dolphin', self.storage)
        with patch.object(c.os, 'rename', side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                c.restore_directory(folder, self.target, self.storage)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_windows_junction_is_treated_as_a_link(self):
        from types import SimpleNamespace
        import stat
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, 'lstat', return_value=info):
            self.assertTrue(c.is_link(self.source / 'junction'))


if __name__ == '__main__':
    unittest.main()
