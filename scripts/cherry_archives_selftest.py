"""Pruebas Windows sin red ni herramientas del OS; todos los datos son temporales."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from third_party.cherry_dl import archives as arc, catalog as cat, organizer
from third_party.cherry_dl.file_routing import (
    COUNTER_NAMESPACES,
    LAYOUT_VERSION,
    destination as download_destination,
    prepare_selected_folders,
    relative_filename,
    route,
    routed_name,
)
from third_party.cherry_dl.layout_migration import migrate_project
from modules.cherry.logic.archive_batch import process_profiles

checks = 0


def check(ok, message):
    global checks
    assert ok, message
    checks += 1


async def register(folder, name, data):
    counter = await cat.next_counter(folder)
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    h = hashlib.sha256(data).hexdigest()
    # Archivos locales diferentes representan fuentes diferentes en este fixture.
    await cat.add_file(folder, h, name, 'https://example.test/' + name + '?sha256=' + h,
                       len(data), counter)
    return h


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        folder = base / 'Landidzu'
        await cat.init_catalog(folder)
        image_dir, image_relative = download_destination(folder, 'Landidzu_00001.png')
        check(image_dir == folder and image_relative == 'Landidzu_00001.png',
              'downloads keep images at project root')
        zip_dir, zip_relative = download_destination(folder, 'Landidzu_00001.zip')
        check(zip_dir == folder / 'Comprimidos'
              and zip_relative == 'Comprimidos/Landidzu_00001.zip',
              'downloads route archives to Comprimidos')
        check(relative_filename('Landidzu_00001.psd') == 'Proyectos/Landidzu_00001.psd',
              'catalog stores the routed relative filename')
        created = prepare_selected_folders(folder, {'.jpg', '.zip', '.psd'})
        check(created == {'Comprimidos', 'Proyectos'}, 'selected non-image folders resolved')
        check((folder / 'Comprimidos').is_dir() and (folder / 'Proyectos').is_dir(),
              'selected non-image folders created before download')
        check(routed_name('Landidzu', 7, 'image.png') == 'Landidzu_00007.png',
              'visual naming remains unchanged')
        check(routed_name('Landidzu', 1, 'pack.rar') == 'arc-Landidzu-00001.rar',
              'archive gets type prefix and category counter')
        check(route('pack.rar').key == 'archive', 'archive counter namespace')
        check(routed_name('Landidzu', 2, 'work.clip') == 'pro-Landidzu-00002.clip',
              'CLIP shares the project-file category')

        imported_source = base / 'external'
        imported_source.mkdir()
        (imported_source / 'image.jpg').write_bytes(b'external image')
        (imported_source / 'pack.zip').write_bytes(b'external archive')
        (imported_source / 'work.clip').write_bytes(b'external project')
        imported, imported_dir = await organizer.organize(
            imported_source, 'Importado', '42', 'test', base,
        )
        check(imported.moved == 3 and not imported.errors, 'external importer routes all files')
        check((imported_dir / 'Importado_00001.jpg').is_file(),
              'external image keeps legacy visual name at root')
        check((imported_dir / 'Comprimidos/arc-Importado-00001.zip').is_file(),
              'external archive gets routed name and own counter')
        check((imported_dir / 'Proyectos/pro-Importado-00001.clip').is_file(),
              'external project gets routed name and own counter')
        imported_rows = await cat.get_all_files(imported_dir)
        check(all('/' in row['filename'] for row in imported_rows
                  if not row['filename'].endswith('.jpg')),
              'external catalog stores nonvisual relative paths')
        image = b'image original exact bytes'
        image_hash = await register(folder, 'Landidzu_00001.png', image)
        await register(folder, 'Landidzu_00002.psd', b'8BPS sample')
        bundle = base / 'bundle.zip'
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('duplicated.png', image)
            z.writestr('new/imagen.png', b'new image')
            z.writestr('same/imagen.png', b'new image')
            z.writestr('../../escape.txt', b'document')
            z.writestr('work.psd', b'8BPS new')
        ah = await register(folder, 'Landidzu_00003.zip', bundle.read_bytes())
        await cat.add_content_alias(folder, service='patreon', post_id='123',
            original_filename='bundle.zip', position=0, role='attachment', file_hash=ah)
        organized = await arc.organize_project(folder)
        check(organized.organized == 2, 'organize without extraction')
        check((folder / 'Comprimidos/Landidzu_00003.zip').read_bytes() == bundle.read_bytes(),
              'organization preserves compressed bytes')
        check((folder / 'Landidzu_00001.png').read_bytes() == image, 'images stay at root')
        check((await arc.organize_project(folder)).organized == 0, 'organization idempotent')
        unnamed = folder / 'image-without-extension'
        unnamed.write_bytes(b'\xff\xd8\xff\xe1Exif')
        check(arc.file_category(unnamed) == '', 'JPEG without extension stays at root')
        unnamed.unlink()
        result = await arc.process_project(folder, 'Landidzu')
        check(not result.errors, result.errors)
        check((result.processed, result.added, result.duplicates) == (1, 3, 2), result)
        check((folder / 'Proyectos/Landidzu_00002.psd').exists(), 'organize PSD')
        check((folder / 'Landidzu_00003.png').exists(), 'incremental image compacted')
        check(not list(folder.glob('*.zip')), 'no ZIP at root')
        check(not list((folder / 'Comprimidos').glob('*.zip')), 'remove verified ZIP')
        check(not (base / 'escape.txt').exists(), 'no archive path traversal')
        check(await cat.hash_exists(folder, ah), 'retain archive hash')
        check(await cat.url_exists(folder, 'https://example.test/Landidzu_00003.zip?sha256=' + ah), 'retain URL')
        check(await cat.content_alias_hash(folder, 'patreon', '123', 'bundle.zip', 0) == ah, 'retain alias')
        receipt = json.loads((folder / arc.STATE_DIR / f'{ah}.json').read_text('utf-8'))
        check(receipt['state'] == 'completed' and len(receipt['entries']) == 5, 'manifest')
        await cat.init_catalog(folder)
        batch = await cat.ScanCatalogBatch.open(folder)
        check(ah in batch.downloaded_hashes, 'rescan still knows archive')
        await batch.close()
        second = await arc.process_project(folder, 'Landidzu')
        check(second.processed == second.added == second.organized == 0, 'idempotent')

        # La misma operación, aun sin comprimidos, hace la auditoría inicial:
        # conserva la copia de número menor y compacta cada contador v2 aparte.
        audit = base / 'Audit'
        await cat.init_catalog(audit)
        check(await cat.ensure_layout_version(
            audit, COUNTER_NAMESPACES, LAYOUT_VERSION,
        ), 'empty audit project adopts layout v2')
        duplicate = b'exact duplicate'
        duplicate_hash = hashlib.sha256(duplicate).hexdigest()
        (audit / 'Audit_00001.png').write_bytes(duplicate)
        (audit / 'Audit_00003.png').write_bytes(duplicate)
        await cat.add_file(
            audit, duplicate_hash, 'Audit_00003.png', file_size=len(duplicate), counter=3,
        )
        unique = b'unique visual'
        unique_hash = hashlib.sha256(unique).hexdigest()
        (audit / 'Audit_00005.png').write_bytes(unique)
        await cat.add_file(
            audit, unique_hash, 'Audit_00005.png', file_size=len(unique), counter=5,
        )
        projects = audit / 'Proyectos'
        projects.mkdir()
        project_a, project_b = b'project a', b'project b'
        for counter, data in ((2, project_a), (4, project_b)):
            name = f'Proyectos/pro-Audit-{counter:05d}.psd'
            (audit / name).write_bytes(data)
            await cat.add_file(
                audit, hashlib.sha256(data).hexdigest(), name,
                file_size=len(data), counter=counter,
            )
        receipt_dir = audit / arc.STATE_DIR
        receipt_dir.mkdir()
        receipt_path = receipt_dir / 'history.json'
        receipt_path.write_text(json.dumps({
            'archive': {}, 'state': 'completed',
            'entries': [{'hash': unique_hash, 'target': 'Audit_00005.png', 'counter': 5}],
        }), encoding='utf-8')
        cleaned = await arc.process_project(audit, 'Audit')
        check(not cleaned.errors and cleaned.audited == 5, cleaned)
        check(cleaned.duplicates_removed == 1 and not (audit / 'Audit_00003.png').exists(),
              'full audit removes the higher-numbered exact copy')
        check((audit / 'Audit_00001.png').read_bytes() == duplicate
              and (audit / 'Audit_00002.png').read_bytes() == unique,
              'visual counter compacts around the retained lowest copy')
        check((projects / 'pro-Audit-00001.psd').read_bytes() == project_a
              and (projects / 'pro-Audit-00002.psd').read_bytes() == project_b,
              'project counter compacts independently')
        rows = {row['hash']: row for row in await cat.get_all_files(audit)}
        check(rows[duplicate_hash]['filename'] == 'Audit_00001.png',
              'catalog follows the retained lower-numbered copy')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        check(receipt['entries'][0]['target'] == 'Audit_00002.png'
              and receipt['entries'][0]['counter'] == 2,
              'archive receipt follows compaction')
        audit_plan = json.loads(
            (audit / '.cherry-maintenance/plan.json').read_text(encoding='utf-8')
        )
        check(audit_plan['reviewed'] == 5 and audit_plan['hashed'] == 1,
              'initial audit reuses catalog hashes and reads only the untracked copy')
        repeated = await arc.process_project(audit, 'Audit')
        check(repeated.audited == repeated.duplicates_removed == repeated.compacted == 0,
              'completed initial audit is not repeated automatically')
        from third_party.cherry_dl.project_maintenance import maintain_project
        _, forced = await maintain_project(audit, 'Audit', force_audit=True, apply=False)
        check(forced['reviewed'] == forced['hashed'] == 4,
              'forced audit recalculates every remaining file')

        recovery = base / 'MaintenanceRecovery'
        await cat.init_catalog(recovery)
        await cat.ensure_layout_version(recovery, COUNTER_NAMESPACES, LAYOUT_VERSION)
        same, other = b'recovery duplicate', b'recovery unique'
        same_hash, other_hash = hashlib.sha256(same).hexdigest(), hashlib.sha256(other).hexdigest()
        for name, data in (
            ('Recovery_00001.png', same),
            ('Recovery_00003.png', same),
            ('Recovery_00005.png', other),
        ):
            (recovery / name).write_bytes(data)
        await cat.add_file(recovery, same_hash, 'Recovery_00003.png', file_size=len(same), counter=3)
        await cat.add_file(recovery, other_hash, 'Recovery_00005.png', file_size=len(other), counter=5)
        real_apply_maintenance = cat.apply_project_maintenance
        async def fail_maintenance(*_args, **_kwargs):
            raise OSError('injected catalog boundary failure')
        with patch.object(cat, 'apply_project_maintenance', fail_maintenance):
            interrupted_cleanup = await arc.process_project(recovery, 'Recovery')
        check(bool(interrupted_cleanup.errors), 'maintenance interruption surfaced')
        pending_plan = json.loads(
            (recovery / '.cherry-maintenance/plan.json').read_text(encoding='utf-8')
        )
        check(pending_plan['status'] == 'applying', 'maintenance plan remains resumable')
        with patch.object(cat, 'apply_project_maintenance', real_apply_maintenance):
            resumed_cleanup = await arc.process_project(recovery, 'Recovery')
        check(not resumed_cleanup.errors, resumed_cleanup.errors)
        check((recovery / 'Recovery_00001.png').read_bytes() == same
              and (recovery / 'Recovery_00002.png').read_bytes() == other,
              'maintenance resumes across the disk/catalog boundary')
        check(not (recovery / '.cherry-maintenance/duplicates').exists(),
              'verified duplicate quarantine is removed')
        batch_steps = []
        batch = await process_profiles([
            {'id': 1, 'display_name': 'Audit', 'folder_path': str(audit)},
            {'id': 2, 'display_name': 'Recovery', 'folder_path': str(recovery)},
        ], on_profile=lambda position, total, profile_id, name: batch_steps.append(
            (position, total, profile_id, name)))
        check(batch.profiles == 2 and not batch.errors, batch.errors)
        check(batch_steps == [(1, 2, 1, 'Audit'), (2, 2, 2, 'Recovery')],
              'batch iterates the same per-project operation in order')

        legacy = base / 'Legacy'
        await cat.init_catalog(legacy)
        await register(legacy, 'Legacy_00001.jpg', b'primary image')
        archive_hash = await register(
            legacy, 'Comprimidos/Legacy_00002.zip', b'old archive')
        await register(legacy, 'PSD/Legacy_00003.psd', b'old project file')
        await register(legacy, 'Legacy_00004.clip', b'old clip file')
        receipt_dir = legacy / arc.STATE_DIR
        receipt_dir.mkdir(exist_ok=True)
        receipt = receipt_dir / f'{archive_hash}.json'
        receipt.write_text(json.dumps({
            'archive': {'hash': archive_hash,
                        'filename': 'Comprimidos/Legacy_00002.zip'},
            'state': 'pending', 'entries': [],
        }), encoding='utf-8')
        check(not await cat.ensure_layout_version(
            legacy, COUNTER_NAMESPACES, LAYOUT_VERSION),
            'non-empty legacy catalog requires explicit migration')
        plan = await migrate_project(legacy, 'Legacy')
        check(not plan['errors'] and plan['status'] == 'planned', 'layout v2 dry run')
        migrated = await migrate_project(legacy, 'Legacy', apply=True)
        check(migrated['status'] == 'completed', 'layout v2 applied')
        check((legacy / 'Legacy_00001.jpg').is_file(), 'migration leaves image name in root')
        check((legacy / 'Comprimidos/arc-Legacy-00001.zip').is_file(),
              'migration renames archive with own sequence')
        check((legacy / 'Proyectos/pro-Legacy-00001.psd').is_file()
              and (legacy / 'Proyectos/pro-Legacy-00002.clip').is_file(),
              'PSD and CLIP share project sequence')
        rows = {row['hash']: row for row in await cat.get_all_files(legacy)}
        check(rows[archive_hash]['filename'] == 'Comprimidos/arc-Legacy-00001.zip',
              'migration updates catalog path')
        saved_receipt = json.loads(receipt.read_text(encoding='utf-8'))
        check(saved_receipt['archive']['filename'] == 'Comprimidos/arc-Legacy-00001.zip',
              'migration updates archive receipt')
        check(await cat.next_counter(legacy, 'archive') == 2,
              'archive counter resumes independently')
        check(await cat.next_counter(legacy, 'visual') == 2,
              'visual counter resumes from existing image')
        repeated = await migrate_project(legacy, 'Legacy')
        check(not repeated['errors'] and not any(
            row['old'] != row['new'] for row in repeated['updates']),
            'completed migration is idempotent')

        interrupted = base / 'Interrupted'
        await cat.init_catalog(interrupted)
        interrupted_hash = await register(
            interrupted, 'Interrupted_00001.zip', b'interrupted migration')
        async def fail_layout(*_args, **_kwargs):
            raise RuntimeError('forced interruption')
        with patch.object(cat, 'apply_layout_version', fail_layout):
            try:
                await migrate_project(interrupted, 'Interrupted', apply=True)
                raise AssertionError('forced migration interruption was ignored')
            except RuntimeError as exc:
                check(str(exc) == 'forced interruption', 'migration interruption surfaced')
        check((interrupted / 'Comprimidos/arc-Interrupted-00001.zip').is_file(),
              'interrupted migration leaves recoverable final file')
        await migrate_project(interrupted, 'Interrupted', apply=True)
        recovered_rows = {
            row['hash']: row for row in await cat.get_all_files(interrupted)
        }
        check(recovered_rows[interrupted_hash]['filename']
              == 'Comprimidos/arc-Interrupted-00001.zip',
              'migration resumes after disk/database boundary interruption')

        # Un reindexado/fusión no debe perder el recibo del ZIP ya eliminado.
        copy = base / 'merge-source'
        shutil.copytree(folder, copy)
        destination = base / 'merge-destination'
        await cat.init_catalog(destination)
        merged = await cat.migrate_unique_files(copy, destination)
        check(not merged['errors'], merged['errors'])
        check(await cat.hash_exists(destination, ah), 'merged archive hash preserved')
        check(await cat.content_alias_hash(destination, 'patreon', '123', 'bundle.zip', 0) == ah,
              'merged archive identity preserved')
        check((destination / arc.STATE_DIR / f'{ah}.json').exists(), 'merged manifest preserved')

        # Crash after move, before catalog registration; same counter on resume.
        with zipfile.ZipFile(bundle, 'w') as z:
            z.writestr('recovery.png', b'recovery image')
        recovery_hash = await register(folder, 'recover.zip', bundle.read_bytes())
        real_add = cat.add_file
        async def fail_add(*args, **kwargs):
            if kwargs.get('content_role') == 'archive_member':
                raise OSError('injected interrupted registration')
            return await real_add(*args, **kwargs)
        with patch.object(cat, 'add_file', fail_add):
            interrupted = await arc.process_project(folder, 'Landidzu')
        check(bool(interrupted.errors), 'injected failure')
        check((folder / 'Comprimidos/recover.zip').exists(), 'retain on failure')
        recovered = await arc.process_project(folder, 'Landidzu')
        check(not recovered.errors and recovered.processed == 1, 'resume succeeds')
        numbered = await cat.get_numbered_files(folder)
        check([item[0] for item in numbered] == list(range(1, len(numbered) + 1))
              and await cat.get_meta_int(folder, 'counter') == len(numbered),
              'recovery incorporates the member without leaving a counter gap')

        # A stale DB hash must not authorize discarding the only correct copy.
        with zipfile.ZipFile(bundle, 'w') as z:
            z.writestr('old.png', image)
        await register(folder, 'stale.zip', bundle.read_bytes())
        (folder / 'Landidzu_00001.png').write_bytes(b'modified locally')
        stale = await arc.process_project(folder, 'Landidzu')
        check(bool(stale.errors) and (folder / 'Comprimidos/stale.zip').exists(), 'stale hash retains archive')

        # Real compressed RAR3 and RAR5 (including solid) with no external executable.
        fixtures = ROOT / 'tests/fixtures/cherry_archives'
        for filename in ('rar3-solid.rar', 'rar5-crc.rar', 'rar5-blake.rar'):
            target = base / filename
            await cat.init_catalog(target)
            await register(target, filename, (fixtures / filename).read_bytes())
            with patch.dict(os.environ, {'PATH': ''}):
                result = await arc.process_project(target, 'RAR')
            check(not result.errors and result.processed == 1, (filename, result.errors))
            check(result.added > 0, 'RAR content incorporated')
        for filename in ('rar5-hpsw.rar', 'rar5-hlink.rar', 'rar5-dups.rar'):
            target = base / filename
            await cat.init_catalog(target)
            await register(target, filename, (fixtures / filename).read_bytes())
            result = await arc.process_project(target, 'RAR')
            check(bool(result.errors) and result.processed == 0, (filename, result))
            check((target / 'Comprimidos' / filename).exists(), 'unresolved RAR preserved')

        # Truncar datos comprimidos de un RAR real debe fallar, nunca simular EOF.
        target = base / 'truncated-rar'
        await cat.init_catalog(target)
        raw = (fixtures / 'rar5-crc.rar').read_bytes()
        await register(target, 'truncated.rar', raw[:len(raw)//2])
        result = await arc.process_project(target, 'RAR')
        check(bool(result.errors) and result.processed == 0, 'truncated RAR retained')

        # Truncation and folder collision never overwrite or delete content.
        target = base / 'bad'
        await cat.init_catalog(target)
        await register(target, 'bad.zip', b'PK\x03\x04truncated')
        bad = await arc.process_project(target, 'bad')
        check(bool(bad.errors) and (target / 'Comprimidos/bad.zip').exists(), 'corrupt ZIP kept')
        await register(target, 'collision.psd', b'new PSD')
        (target / 'Proyectos').mkdir()
        (target / 'Proyectos/collision.psd').write_bytes(b'other PSD')
        try:
            await arc.process_project(target, 'bad')
            raise AssertionError('collision was accepted')
        except FileExistsError:
            check((target / 'collision.psd').read_bytes() == b'new PSD', 'source retained')
            check((target / 'Proyectos/collision.psd').read_bytes() == b'other PSD', 'target retained')

        # Cancelar la extracción mata y espera al proceso hijo antes de limpiar temporales.
        spawned = []
        real_spawn = asyncio.create_subprocess_exec
        async def slow_spawn(*args, **kwargs):
            p = await real_spawn(sys.executable, '-c', 'import time; time.sleep(30)', **kwargs)
            spawned.append(p)
            return p
        with patch.object(asyncio, 'create_subprocess_exec', slow_spawn):
            task = asyncio.create_task(arc._extract(bundle, base))
            for _ in range(100):
                if spawned:
                    break
                await asyncio.sleep(.01)
            check(bool(spawned), 'decoder process started')
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        check(spawned[0].returncode is not None, 'cancel reaps decoder process')
    print(f'Archives: {checks} checks passed')


def ui_test():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication
    from modules.cherry.ui.view import CherryView
    app = QApplication.instance() or QApplication([])
    view = CherryView()
    view._started = True
    view._reload_detail = lambda: None
    view._announce_folder = lambda profile: notified.append(profile['folder_path'])
    notified = []

    def wait_until(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        check(predicate(), 'UI operation completed')

    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            asyncio.run(cat.init_catalog(folder))
            source = folder / 'manual.zip'
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('new.png', b'UI-image')
            asyncio.run(register(folder, source.name, source.read_bytes()))
            selected = {'id': 100, 'folder_path': tmp, 'folder_exists': True,
                        'display_name': 'Selected'}
            view._current_profile = lambda: selected
            view._update_actions()
            check(view._btn_archives.isEnabled(), 'project action available')
            check(bool(view._btn_archives.toolTip()), 'action has tooltip')
            check(bool(view._btn_archives_all.toolTip()), 'batch action has tooltip')
            view._preparing_download = True
            view._update_actions()
            check(not view._btn_archives.isEnabled(), 'blocked while preparing downloads')
            view._preparing_download = False
            view._bridge._busy = True
            view._update_actions()
            check(not view._btn_archives.isEnabled(), 'blocked while downloading')
            view._bridge._busy = False
            view._update_actions()
            view._btn_archives.click()
            check(view._maintenance_busy and not view._btn_download.isEnabled(), 'mutual exclusion')
            check(not view._btn_all.isEnabled() and not view._btn_dups.isEnabled(), 'batch and merge blocked')
            view._start([100], False)
            check(not view._preparing_download, 'download handler guard')
            # Cambiar de selección no cambia el proyecto capturado al hacer clic.
            selected = {'id': 200, 'folder_path': 'unrelated', 'folder_exists': True}
            wait_until(lambda: not view._maintenance_busy)
            check(not source.exists() and (folder / 'Selected_00001.png').exists(),
                  'manual action extracts and compacts')
            check(notified == [tmp], 'notifies original selected project')
            check(view._btn_download.isEnabled(), 'controls restored')

            selected = {'id': 100, 'folder_path': tmp, 'folder_exists': True,
                        'display_name': 'Selected'}
            awaitable_started = []
            async def pause_extraction(*args):
                awaitable_started.append(True)
                await asyncio.sleep(30)
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('another.png', b'new UI image')
            asyncio.run(register(folder, source.name, source.read_bytes()))
            with patch.object(arc, '_extract', pause_extraction):
                view._update_actions()
                view._btn_archives.click()
                wait_until(lambda: bool(awaitable_started))
                view._cancel()
                wait_until(lambda: not view._maintenance_busy)
            check((folder / 'Comprimidos/manual.zip').exists(), 'UI cancel keeps archive')
            check(view._btn_download.isEnabled(), 'cancel restores download controls')
    finally:
        view.shutdown()
        view.close()
        app.processEvents()
    print(f'Archives + UI: {checks} checks passed')


if __name__ == '__main__':
    asyncio.run(main())
    ui_test()
