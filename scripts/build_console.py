#!/usr/bin/env python3
"""Build the paired CPAMC guest and record the exact shipped source provenance.

The build also publishes THIRD_PARTY_NOTICES.md: the hand-written text above its generated
marker, then the CPAMC license and the complete license text of every npm package Vite reports
as bundled into the asset. The build fails, publishing nothing, when a bundled package has no
license text, an unreviewed license identifier, or an empty inventory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / 'upstream-console'
PACKAGE = ROOT / 'hermes-cpa'
SUPPLEMENTS = Path(__file__).resolve().parent / 'console_license_supplements'
EXCLUDED = {'node_modules', 'dist', 'dist-hermes', 'dist-reference', '.git', 'coverage'}
# The installer and qualification read each notice file with this bound.
NOTICE_LIMIT = 1024 * 1024
NOTICE_MARKER = ('<!-- BEGIN GENERATED: scripts/build_console.py replaces everything below this '
                 'line on every build; edit only above it. -->')
# vite.config.ts leaves exactly these files: the single-file document and Vite's license inventory.
DIST_FILES = {'index.html', '.vite/license.json'}
# Identifiers reviewed for the shipped bundle; any other bundled license needs a deliberate review.
PERMITTED_LICENSES = {'0BSD', 'ISC', 'MIT'}
# Packages the shipped console certainly bundles; their absence means the inventory is wrong.
REQUIRED_BUNDLED = ('axios', 'i18next', 'react', 'react-dom', 'react-i18next', 'react-router')
# braces (GHSA-vfj7-8cjw-p6xm, stack exhaustion on deeply nested patterns, no fixed release) is reachable
# only through build tooling (vite-plugin-singlefile and sass's optional watcher via micromatch). That
# exception holds only while neither is bundled into the shipped document.
BUILD_ONLY = ('braces', 'micromatch')
LICENSE_FILE = re.compile(r'^(?:licen[sc]e|copying|notice|patents|copyright)', re.IGNORECASE)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return text.replace('\r\n', '\n').strip()


def read_text(path):
    return normalized(path.read_bytes().decode('utf-8-sig'))


def fenced(text):
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}text\n{text}\n{fence}'


def package_names(key):
    """Split a bun.lock package key such as '@scope/outer/inner' into its nested package names."""
    names, parts = [], key.split('/')
    while parts:
        names.append(parts.pop(0) + '/' + parts.pop(0) if parts[0].startswith('@') else parts.pop(0))
    return names


def require_install_matches_lock():
    """Refuse to build from node_modules that drifted from bun.lock, e.g. after a lock update."""
    lock = (CONSOLE / 'bun.lock').read_text(encoding='utf-8')
    stale = []
    for key, version in re.findall(r'^\s+"([^"]+)": \["(?:@[^/"]+/)?[^@"]+@([^"]+)"', lock, re.MULTILINE):
        names = package_names(key)
        directory = CONSOLE / 'node_modules'
        for name in names[:-1]:
            directory = directory / name / 'node_modules'
        manifest = directory / names[-1] / 'package.json'
        if manifest.is_file():
            installed = json.loads(manifest.read_text(encoding='utf-8')).get('version')
            if installed != version:
                stale.append(f'{key}: installed {installed}, locked {version}')
    if stale:
        raise RuntimeError('upstream-console/node_modules does not match bun.lock; run '
                           'bun install --frozen-lockfile before building:\n  ' + '\n  '.join(stale[:10]))


def require_single_file(dist):
    found = {path.relative_to(dist).as_posix() for path in dist.rglob('*') if path.is_file()}
    if found != DIST_FILES:
        raise RuntimeError(f'Console output must be exactly {sorted(DIST_FILES)}, found {sorted(found)}; '
                           'no asset published.')


def bundled_packages(dist):
    """Vite's own inventory of the npm packages inlined into the asset, validated and sorted."""
    entries = json.loads((dist / '.vite/license.json').read_text(encoding='utf-8'))
    if not isinstance(entries, list):
        raise RuntimeError('Vite license inventory is not a list; no asset published.')
    packages = {}
    for entry in entries:
        if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) and entry[key] for key in ('name', 'version')):
            raise RuntimeError(f'Malformed Vite license inventory entry {entry!r}; no asset published.')
        if any(key in entry and not isinstance(entry[key], str) for key in ('identifier', 'text')):
            raise RuntimeError(f"Malformed Vite license inventory entry for {entry['name']}; no asset published.")
        packages[(entry['name'], entry['version'])] = entry
    missing = [name for name in REQUIRED_BUNDLED if not any(key[0] == name for key in packages)]
    if missing:
        raise RuntimeError('Vite license inventory lacks bundled packages ' + ', '.join(missing) +
                           '; the inventory is incomplete and no asset is published.')
    shipped = [name for name in BUILD_ONLY if any(key[0] == name for key in packages)]
    if shipped:
        raise RuntimeError(', '.join(shipped) + ' is bundled into the console; the unfixed braces advisory '
                           'GHSA-vfj7-8cjw-p6xm would ship, so no asset is published.')
    return [packages[key] for key in sorted(packages)]


def installed_packages():
    """Map (name, version) to a package directory for every package under node_modules."""
    found = {}

    def scan(modules):
        for entry in sorted(os.scandir(modules), key=lambda item: item.name):
            if entry.name.startswith('.') or not entry.is_dir():
                continue
            directory = Path(entry.path)
            if entry.name.startswith('@'):
                scan(directory)
                continue
            manifest = directory / 'package.json'
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding='utf-8'))
                if isinstance(data.get('name'), str) and isinstance(data.get('version'), str):
                    found.setdefault((data['name'], data['version']), directory)
            nested = directory / 'node_modules'
            if nested.is_dir():
                scan(nested)

    modules = CONSOLE / 'node_modules'
    if not modules.is_dir():
        raise RuntimeError('upstream-console/node_modules is missing; run bun install --frozen-lockfile.')
    scan(modules)
    return found


def load_supplements():
    """Reviewed license texts for bundled packages whose tarballs ship none, keyed by name@version."""
    index = json.loads((SUPPLEMENTS / 'index.json').read_text(encoding='utf-8'))
    supplements = {}
    for item in index['supplements']:
        data = (SUPPLEMENTS / item['file']).read_bytes()
        if sha256(data) != item['sha256']:
            raise RuntimeError(f"License supplement {item['file']} does not match its recorded digest.")
        key = (item['package'], item['version'])
        if key in supplements:
            raise RuntimeError('Duplicate license supplement for %s@%s.' % key)
        supplements[key] = {**item, 'text': normalized(data.decode('utf-8-sig'))}
    return supplements


def collect_notices(inventory):
    """Complete license and notice texts per bundled package; raise listing every gap."""
    installed = installed_packages()
    supplements = load_supplements()
    used, notices, problems = set(), [], []
    for entry in inventory:
        key = (entry['name'], entry['version'])
        label = '%s@%s' % key
        identifier = entry.get('identifier', '').strip()
        directory = installed.get(key)
        if directory is None:
            problems.append(f'{label} is bundled but not installed under upstream-console/node_modules')
            continue
        if identifier not in PERMITTED_LICENSES:
            problems.append(f"{label} declares license {identifier or 'none'}, outside the reviewed set "
                            f'{sorted(PERMITTED_LICENSES)}')
            continue
        files = [(path.name, read_text(path)) for path in sorted(directory.iterdir())
                 if path.is_file() and LICENSE_FILE.match(path.name)]
        files = [(name, text) for name, text in files if text]
        bundled = normalized(entry.get('text', ''))
        supplement = None
        if bundled:
            match = next(((name, text) for name, text in files if text == bundled), None)
            if match is None:
                problems.append(f'{label}: the bundler license text matches no license file in node_modules')
                continue
            texts = [match]
        else:
            supplement = supplements.get(key)
            if supplement is None:
                problems.append(f'{label} ships no license text and has no reviewed supplement in '
                                'scripts/console_license_supplements')
                continue
            if supplement['identifier'] != identifier:
                problems.append(f"{label} declares {identifier} but its supplement states {supplement['identifier']}")
                continue
            used.add(key)
            texts = [(supplement['file'], supplement['text'])]
        for name, text in files:
            if text not in {seen for _, seen in texts}:
                texts.append((name, text))
        notices.append({'label': label, 'identifier': identifier, 'texts': texts, 'supplement': supplement})
    problems.extend('Stale license supplement for %s@%s: the bundler reports no such license-less package'
                    % key for key in sorted(set(supplements) - used))
    if problems:
        raise RuntimeError('Bundled third-party license inputs are incomplete; no asset published:\n  ' +
                           '\n  '.join(problems))
    return notices


def render_notices(head, root_license, notices):
    parts = [
        head.rstrip('\n') + '\n\n' + NOTICE_MARKER,
        '## Cli-Proxy-API-Management-Center license text\n\n' + fenced(root_license),
        '## Bundled npm packages\n\n'
        f'`assets/cpamc.html` bundles the {len(notices)} npm packages below. The inventory is the one Vite '
        'emits (`build.license`) from the modules of the actual bundle. Each license text is the file '
        'published in the package, unless its entry says otherwise.\n\n' +
        '\n'.join(f"- {notice['label']}: {notice['identifier']}" for notice in notices),
    ]
    for notice in notices:
        lines = [f"### {notice['label']} ({notice['identifier']})"]
        supplement = notice['supplement']
        if supplement:
            lines.append(f"The npm package ships no license file. The text below is from {supplement['source']}. "
                         f"{supplement['note']}")
        for name, text in notice['texts']:
            lines.append(f'`{name}`:\n\n{fenced(text)}')
        parts.append('\n\n'.join(lines))
    return ('\n\n'.join(parts) + '\n').encode('utf-8')


def notice_inputs():
    """The hand-written notice head and the CPAMC license text; raises before anything is written."""
    head, marker, _ = (PACKAGE / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8').partition(NOTICE_MARKER)
    if not marker:
        raise RuntimeError('THIRD_PARTY_NOTICES.md lost its generated-section marker; no asset published.')
    root_license = read_text(CONSOLE / 'LICENSE')
    if not root_license:
        raise RuntimeError('upstream-console/LICENSE is empty; no asset published.')
    return head, root_license


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', default=os.environ.get('BUN') or shutil.which('bun'))
    args = parser.parse_args()
    if not args.bun:
        parser.error('Bun is required; supply --bun or set BUN.')
    env = {**os.environ, 'VERSION': '1.25.6'}
    require_install_matches_lock()
    subprocess.run([args.bun, 'run', 'build'], cwd=CONSOLE, env=env, check=True)
    dist = CONSOLE / 'dist'
    require_single_file(dist)
    content = (dist / 'index.html').read_bytes()
    if len(content) > 8 * 1024 * 1024:
        raise RuntimeError('Console exceeds the verified-document 8 MiB limit; no asset published.')
    digest = hashlib.sha256(content).hexdigest()
    head, root_license = notice_inputs()
    notices = collect_notices(bundled_packages(dist))
    notice_bytes = render_notices(head, root_license, notices)
    license_bytes = (PACKAGE / 'LICENSE').read_bytes()
    if max(len(notice_bytes), len(license_bytes)) > NOTICE_LIMIT:
        raise RuntimeError('A notice file exceeds the 1 MiB installer bound; no asset published.')
    notice_hashes = {'LICENSE': sha256(license_bytes), 'THIRD_PARTY_NOTICES.md': sha256(notice_bytes)}
    sources = {}
    published = subprocess.check_output([
        'git', '-C', str(ROOT), 'ls-files', '--cached', '--others',
        '--exclude-standard', '-z', '--', 'upstream-console',
    ]).decode().split('\0')
    for name in sorted(set(published) - {''}):
        path = ROOT / name
        relative = path.relative_to(CONSOLE)
        if not path.is_file() or path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
            continue
        sources[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    asset_manifest_path = PACKAGE / 'assets/cpamc.manifest.json'
    manifest = json.loads(asset_manifest_path.read_text())
    manifest.update(
        adaptation_version='hermes.6-full-console', sha256=digest,
        size_bytes=len(content), source_hashes=sources,
        build=f"Bun {subprocess.check_output([args.bun, '--version'], text=True).strip()}; "
              'VERSION=1.25.6 bun run build; verification recorded separately',
        scope='Full pinned CPAMC via privileged host-mediated management in Agent Dashboard and Desktop; verification recorded separately',
    )
    compatibility_path = PACKAGE / 'compatibility.json'
    compatibility = json.loads(compatibility_path.read_text())
    pair = next(pair for pair in compatibility['supported_pairs']
                if pair['cpa_version'] == '8.0.21' and pair['console_version'] == '1.25.6'
                and pair['platform'] == 'linux_amd64')
    pair['console_sha256'] = digest
    if set(compatibility.get('notice_sha256', {})) != set(notice_hashes):
        raise RuntimeError('compatibility.json notice_sha256 must name exactly LICENSE and THIRD_PARTY_NOTICES.md.')
    compatibility['notice_sha256'].update(notice_hashes)
    (PACKAGE / 'assets/cpamc.html').write_bytes(content)
    (PACKAGE / 'THIRD_PARTY_NOTICES.md').write_bytes(notice_bytes)
    asset_manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    compatibility_path.write_text(json.dumps(compatibility, indent=2) + '\n')
    print(json.dumps({'asset_sha256': digest, 'size_bytes': len(content),
                      'source_files': len(sources), 'bundled_packages': len(notices),
                      'supplemented_packages': [n['label'] for n in notices if n['supplement']],
                      'notice_sha256': notice_hashes, 'release_flags_unchanged': True}))


if __name__ == '__main__':
    main()
