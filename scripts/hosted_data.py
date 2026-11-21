"""Pack/restore fingerprinted runtime data without changing source datasets."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ('data/prepared/',) + tuple('src/cole/features/' + name + '/data/' for name in
    ('hazards', 'industry_employment', 'poverty_education', 'age_demographics', 'greenspace'))


def fingerprint(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def file_fingerprint(path):
    with path.open('rb') as stream:
        return {'bytes': path.stat().st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def pack(root=ROOT):
    archive = root / 'deploy/runtime-data.zip'
    manifest = root / 'deploy/runtime-data.json'
    if archive.exists() or manifest.exists():
        raise RuntimeError('Refusing to overwrite a versioned runtime artifact.')
    files = {}
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for prefix in PREFIXES:
            directory = root / prefix
            if not directory.is_dir():
                raise RuntimeError('Missing runtime data directory: ' + prefix)
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError('Unexpected runtime data entry: ' + str(path))
                name = path.relative_to(root).as_posix()
                raw = path.read_bytes()
                files[name] = fingerprint(raw)
                output.writestr(name, raw)
    if archive.stat().st_size >= 100 * 1024 * 1024:
        raise RuntimeError('Runtime artifact exceeds the ordinary GitHub file limit.')
    manifest.write_text(json.dumps({'schema': 1, 'archive': file_fingerprint(archive), 'files': files}, indent=2) + '\n', encoding='utf-8')
    return {'files': len(files), 'archive_bytes': archive.stat().st_size, 'expanded_bytes': sum(v['bytes'] for v in files.values())}


def restore(root=ROOT):
    manifest = json.loads((root / 'deploy/runtime-data.json').read_text(encoding='utf-8'))
    archive = root / 'deploy/runtime-data.zip'
    if manifest.get('schema') != 1 or file_fingerprint(archive) != manifest['archive']:
        raise RuntimeError('Runtime archive fingerprint mismatch.')
    files = manifest['files']
    if not files or len(files) > 1000 or sum(v['bytes'] for v in files.values()) > 350 * 1024 * 1024:
        raise RuntimeError('Unexpected runtime artifact size.')
    with zipfile.ZipFile(archive) as source:
        if len(source.infolist()) != len(files) or set(source.namelist()) != set(files):
            raise RuntimeError('Runtime artifact membership mismatch.')
        # Validate the complete archive and existing destinations before any write.
        for name, expected in files.items():
            parts = PurePosixPath(name).parts
            prefix = next((p for p in PREFIXES if name.startswith(p)), None)
            if (not prefix or '/' in name[len(prefix):] or '..' in parts or '\\' in name
                    or PurePosixPath(name).is_absolute() or not (root / name).resolve().is_relative_to(root.resolve())):
                raise RuntimeError('Unsafe runtime member.')
            info = source.getinfo(name)
            if info.file_size != expected['bytes'] or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Runtime entry size/type mismatch.')
            with source.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected['sha256']:
                    raise RuntimeError('Runtime member fingerprint mismatch: ' + name)
            target = root / name
            if target.exists() and (not target.is_file() or file_fingerprint(target) != expected):
                raise RuntimeError('Existing data differs; build from a clean deployment checkout: ' + name)
        for name in files:
            target = root / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(name) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    return {'verified_files': len(files), 'status': 'passed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('pack', 'restore'))
    args = parser.parse_args()
    print(json.dumps(pack() if args.operation == 'pack' else restore()))
