"""Constants and pure helpers shared by the native resource fixture tools.

Standard library only. The fixture is a generated native Go plugin: nothing here
contacts a network or touches a credential.
"""
import hashlib
import io
import json
from pathlib import Path
import zipfile

PLUGIN_ID = 'hermes-resource-fixture'
BASE = '/v0/resource/plugins/' + PLUGIN_ID
MENU = 'Hermes Resource Fixture'
ENTRY = BASE + '/panel'
GOOS, GOARCH = 'linux', 'amd64'
# (version, generation): same routes, different native code and instance.
VARIANTS = (('1.0.0', '1'), ('2.0.0', '2'))
MANIFEST_NAME = 'fixture-manifest.json'
SOURCE_DIR = Path(__file__).resolve().parent
# <repo or /opt/q>/cache/native-resource-fixture, next to the runtime archive.
ARTIFACT_DIR = SOURCE_DIR.parents[1] / 'cache' / 'native-resource-fixture'
EFFECT_LOG = 'hermes-resource-fixture-effects.jsonl'


class FixtureError(Exception):
    """Only fixed, locally authored codes may be supplied."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_files(source=SOURCE_DIR):
    """The exact Go inputs of the build: go.mod, every .go file and assets/*."""
    source = Path(source)
    files = [source / 'go.mod', *sorted(source.glob('*.go')), *sorted((source / 'assets').iterdir())]
    if not all(path.is_file() and not path.is_symlink() for path in files):
        raise FixtureError('fixture_source_invalid')
    return files


def source_digest(source=SOURCE_DIR):
    source = Path(source)
    digest = hashlib.sha256()
    for path in source_files(source):
        digest.update(path.relative_to(source).as_posix().encode() + b'\0')
        digest.update(sha256(path.read_bytes()).encode() + b'\n')
    return digest.hexdigest()


def library_name(version):
    return f'{PLUGIN_ID}_{version}_{GOOS}_{GOARCH}.so'


def archive_name(version):
    return f'{PLUGIN_ID}_{version}_{GOOS}_{GOARCH}.zip'


def make_archive(library, version):
    """Deterministic plugin store archive with the library at the zip root.

    The store requires <id>.so or <id>-v<version>.so; it installs the file as
    plugins/linux/amd64/<id>-v<version>.so.
    """
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        info = zipfile.ZipInfo(PLUGIN_ID + '.so', date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100755 << 16
        bundle.writestr(info, library)
    return stream.getvalue()


def registry_document(version, artifact_url, archive):
    """Plugin store registry (schema 2, direct install) advertising one version."""
    return {'schema_version': 2, 'plugins': [{
        'id': PLUGIN_ID, 'name': 'Hermes native resource fixture',
        'description': 'Generated native resource plugin for Hermes qualification.',
        'author': 'hermes-qualification', 'version': version,
        'install': {'type': 'direct', 'artifacts': [{
            'goos': GOOS, 'goarch': GOARCH, 'url': artifact_url,
            'sha256': sha256(archive), 'size': len(archive)}]}}]}


def load_manifest(directory=ARTIFACT_DIR, source=SOURCE_DIR):
    """Verified build output: every library matches the manifest and the source."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_bytes())
    except (OSError, ValueError):
        raise FixtureError('fixture_manifest_missing') from None
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1 or
            manifest.get('plugin_id') != PLUGIN_ID or manifest.get('cgo') is not True or
            manifest.get('trimpath') is not True or manifest.get('reproducible') is not True or
            not isinstance(manifest.get('variants'), list)):
        raise FixtureError('fixture_manifest_invalid')
    if source is not None and manifest.get('source_sha256') != source_digest(source):
        raise FixtureError('fixture_source_changed')
    variants = {}
    for row in manifest['variants']:
        if not isinstance(row, dict) or not isinstance(row.get('version'), str):
            raise FixtureError('fixture_manifest_invalid')
        try:
            library = (directory / row['file']).read_bytes()
        except (OSError, KeyError, TypeError):
            raise FixtureError('fixture_library_missing') from None
        if len(library) != row.get('size') or sha256(library) != row.get('sha256'):
            raise FixtureError('fixture_library_mismatch')
        variants[row['version']] = {'generation': row['generation'], 'library': library,
                                    'sha256': row['sha256'], 'archive': make_archive(library, row['version'])}
    if set(variants) != {version for version, _ in VARIANTS}:
        raise FixtureError('fixture_variants_incomplete')
    return {'manifest': manifest, 'variants': variants}
