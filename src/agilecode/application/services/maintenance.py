"""Declared migration snapshots, distinct from portable project backups.

These private local snapshots include affected global settings, so they must never
be added to portable exports. Rollback is conditional on the migration's actual
post-image and preserves subsequent edits rather than reverting a whole home.
"""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
from pathlib import Path
import tempfile

from agilecode.adapters.config_lock import atomic_json, shared_file_lock, migration_file_observer
from agilecode.application.coordination import settle
from agilecode.core.types import AgileCodeError, now, uid


@dataclass
class MigrationSnapshot:
    owner: str
    path: Path | None
    manifest: dict


class MaintenanceService:
    def __init__(self, storage: Path, global_root: Path, coordinator, *, extra_paths=()):
        self.storage, self.global_root = Path(storage).resolve(), Path(global_root).resolve()
        self.coordinator = coordinator
        self.extra_paths = tuple(Path(path).resolve() for path in extra_paths)

    def _locks(self):
        return sorted([self.global_root / 'settings.json', self.global_root / 'providers.json',
                       self.global_root / 'teams' / '.catalog', self.global_root / 'skills' / '.catalog',
                       self.global_root / 'project_manager' / 'profiles' / '.catalog'])

    def _roots(self):
        return [self.global_root / 'settings.json', self.global_root / 'providers.json',
                self.global_root / 'teams', self.global_root / 'skills',
                self.global_root / 'project_manager' / 'profiles',
                self.storage / 'project_manager', self.storage.parent / '.agile_code' / 'project_manager',
                *self.extra_paths]

    def _declared(self, path):
        path = Path(path).absolute()
        roots = self._roots()
        if ".." in path.parts:
            return False
        return path in roots or any(root in path.parents for root in roots[2:7])

    def _inventory(self):
        files, directories = [], []
        lock_files = {path.with_name(path.name + ".lock") for path in self._locks()}
        for root in self._roots():
            if root.is_symlink():
                raise AgileCodeError(f'Migration resource must not be a symlink: {root}')
            if root.is_file():
                files.append(root)
            elif root.is_dir():
                directories.append(str(root))
                for path in sorted(root.rglob('*')):
                    if path.is_symlink():
                        raise AgileCodeError(f'Migration resource must not be a symlink: {path}')
                    if path.is_dir():
                        directories.append(str(path))
                    elif path.is_file() and path not in lock_files:
                        files.append(path)
        return sorted(set(files)), sorted(set(directories))

    def _capture(self, snapshot):
        files, directories = self._inventory()
        result = {}
        blobs = snapshot / 'files'; blobs.mkdir(exist_ok=True, mode=0o700)
        for path in files:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            blob = blobs / digest
            if not blob.exists():
                with blob.open('xb') as handle:
                    os.chmod(blob, 0o600); handle.write(data); handle.flush(); os.fsync(handle.fileno())
            if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                raise AgileCodeError('Migration rollback snapshot verification failed.')
            result[str(path)] = {'sha256': digest, 'bytes': len(data), 'mode': path.stat().st_mode & 0o777}
        return result, directories

    @asynccontextmanager
    async def startup(self, *, database_backup=None):
        owner = 'migration:' + uid()
        async with self.coordinator.maintenance(owner):
            with ExitStack() as locks:
                for path in self._locks():
                    locks.enter_context(shared_file_lock(path, owner=owner))
                snapshot = self.storage / 'migration-rollbacks' / owner.split(':', 1)[1]
                snapshot.mkdir(parents=True, mode=0o700)
                before, directories = self._capture(snapshot)
                manifest = {'format': 'agilecode-migration-rollback', 'version': 1, 'owner': owner,
                            'created': now(), 'status': 'prepared', 'roots': [str(p) for p in self._roots()],
                            'database_backup': str(database_backup) if database_backup else None,
                            'before': before, 'before_directories': directories, 'after': None, 'intents': {}, 'versions': {}}
                self._plan_legacy_move(snapshot, manifest)
                atomic_json(snapshot / 'manifest.json', manifest)
                locks.enter_context(migration_file_observer(owner, lambda path, data, mode: self._record_intent(snapshot, manifest, path, data, mode)))
                token = MigrationSnapshot(owner, snapshot, manifest)
                try:
                    yield token
                except BaseException:
                    manifest['status'] = 'interrupted'
                    raise
                else:
                    manifest['status'] = 'completed'
                finally:
                    # Async cancellation cannot interrupt this short synchronous
                    # post-image capture; child physical writers must settle first.
                    after, directories = self._capture(snapshot)
                    manifest.update(after=after, after_directories=directories, finished=now())
                    manifest['unrecorded_changes'] = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name) and manifest['intents'].get(name, before.get(name)) != after.get(name)]
                    atomic_json(snapshot / 'manifest.json', manifest)
                    if (manifest['status'] == 'completed' and before == after
                            and set(manifest['before_directories']) == set(directories)
                            and not database_backup):
                        # This directory was exclusively created above for this
                        # no-op open. Never remove a prior migration's snapshot.
                        shutil.rmtree(snapshot)
                        token.path = None
                        manifest['status'] = 'no_changes'

    async def rollback(self, snapshot: Path):
        """Restore only owned file effects; database rollback stays explicit."""
        snapshot = Path(snapshot).resolve()
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        if manifest.get('format') != 'agilecode-migration-rollback' or manifest.get('version') != 1:
            raise AgileCodeError('Unsupported migration rollback snapshot.')
        if manifest.get('roots') != [str(p) for p in self._roots()]:
            raise AgileCodeError('Migration rollback snapshot belongs to different declared resources.')
        if manifest.get('after') is None:
            # Intent is durable before an atomic write. A crash before replacement
            # leaves the pre-image; a crash afterward leaves the exact post-image.
            # Neither case permits attributing a different later edit to us.
            after = dict(manifest['before'])
            for name, record in manifest.get('intents', {}).items():
                path = Path(name)
                if path.is_file() and not path.is_symlink():
                    observed = hashlib.sha256(path.read_bytes()).hexdigest()
                    for candidate in manifest.get('versions', {}).get(name, []):
                        if candidate and candidate['sha256'] == observed:
                            record = candidate
                            break
                if record is None:
                    after.pop(name, None)
                else:
                    after[name] = record
            manifest['after'] = after
            _, directories = self._inventory()
            manifest['after_directories'] = directories
        owner = 'rollback:' + uid()
        async with self.coordinator.maintenance(owner):
            with ExitStack() as locks:
                for path in self._locks():
                    locks.enter_context(shared_file_lock(path, owner=owner))
                return self._rollback(snapshot, manifest)

    def _record_intent(self, snapshot, manifest, path, data, mode):
        path = path.absolute()
        if not self._declared(path):
            return
        if path.is_symlink():
            raise AgileCodeError(f'Migration target must not be a symlink: {path}')
        digest = hashlib.sha256(data).hexdigest()
        blob = snapshot / 'files' / digest
        if not blob.exists():
            with blob.open('xb') as handle:
                os.chmod(blob, 0o600); handle.write(data); handle.flush(); os.fsync(handle.fileno())
        record = {'sha256': digest, 'bytes': len(data), 'mode': mode}
        versions = manifest.setdefault('versions', {}).setdefault(str(path), [])
        if record not in versions:
            versions.append(record)
        manifest['intents'][str(path)] = record
        atomic_json(snapshot / 'manifest.json', manifest)

    def _plan_legacy_move(self, snapshot, manifest):
        legacy = self.storage.parent / '.agile_code' / 'project_manager'
        canonical = self.storage / 'project_manager'
        if legacy.is_dir() and not canonical.exists():
            for name, record in manifest['before'].items():
                path = Path(name)
                if legacy in path.parents:
                    manifest['intents'][name] = None
                    manifest['intents'][str(canonical / path.relative_to(legacy))] = record

    def _rollback(self, snapshot, manifest):
        restored, conflicts = [], []
        before, after = manifest['before'], manifest['after']
        for name in sorted(before.keys() | after.keys()):
            old, new = before.get(name), after.get(name)
            if old == new:
                continue
            path = Path(name)
            if not self._declared(path):
                raise AgileCodeError('Rollback snapshot contains an undeclared target.')
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                conflicts.append(name); continue
            current = path.read_bytes() if path.is_file() else None
            original = _blob(snapshot, old)
            post = _blob(snapshot, new)
            if current == original:
                continue  # Idempotent rollback.
            if current != post:
                if path in {self.global_root / 'settings.json', self.global_root / 'providers.json'}:
                    merged, field_conflicts = _reverse_json(original, post, current, providers=path.name == 'providers.json')
                    conflicts.extend(f'{name}:{key}' for key in field_conflicts)
                    if merged is not None and merged != current:
                        _write_bytes(path, merged, old['mode'] if old else 0o600)
                        restored.append(name)
                else:
                    conflicts.append(name)
                continue
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes(path, original, old['mode'])
            restored.append(name)
        for name in sorted(set(manifest.get('after_directories', [])) - set(manifest.get('before_directories', [])), key=len, reverse=True):
            if not self._declared(Path(name)):
                raise AgileCodeError('Rollback snapshot contains an undeclared directory.')
            try:
                Path(name).rmdir()
            except OSError:
                pass  # Preserve subsequent user files and stable lock inodes.
        result = {'restored': restored, 'conflicts': conflicts, 'database_backup': manifest.get('database_backup')}
        atomic_json(snapshot / 'rollback-result.json', result)
        return result


def _blob(snapshot, record):
    if record is None:
        return None
    data = (snapshot / 'files' / record['sha256']).read_bytes()
    if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise AgileCodeError('Migration rollback content checksum mismatch.')
    return data


def _write_bytes(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.rollback-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _reverse_json(before, after, current, *, providers=False):
    """Reverse only the migration's unchanged fields, retaining later edits."""
    try:
        old, new, live = (json.loads(value) if value else {} for value in (before, after, current))
        if not all(isinstance(value, dict) for value in (old, new, live)):
            return None, ['modified']
    except (ValueError, TypeError):
        return None, ['modified']
    missing = object()
    conflicts = []
    def reverse(a, b, c, prefix=''):
        result = dict(c)
        for key in a.keys() | b.keys():
            prior, written, actual = a.get(key, missing), b.get(key, missing), c.get(key, missing)
            if prior == written or actual == prior:
                continue
            if actual != written:
                conflicts.append(prefix + key); continue
            if prior is missing:
                result.pop(key, None)
            else:
                result[key] = prior
        return result
    if providers:
        maps = [{item['id']: item for item in value.get('providers', [])} for value in (old, new, live)]
        entries = reverse(*maps, prefix='providers.')
        live = reverse({k: v for k, v in old.items() if k != 'providers'},
                       {k: v for k, v in new.items() if k != 'providers'},
                       {k: v for k, v in live.items() if k != 'providers'})
        live['providers'] = list(entries.values())
        if live.get('default_provider_id') not in entries:
            live['default_provider_id'] = next(iter(entries), None)
        live.setdefault('format_version', 1)
    else:
        live = reverse(old, new, live)
    return (json.dumps(live, ensure_ascii=False, indent=2) + '\n').encode(), conflicts
