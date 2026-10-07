#!/usr/bin/env python3
"""Export a new, allowlisted service checkout, without private Git history or plugins."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from publisher.runtime import package_files, verify_package

DOCS = {
    'docs/standalone-agents.md': 'AGENTS.md',
    'docs/standalone-readme.md': 'README.md',
    'docs/standalone.md': 'docs/setup.md',
    'docs/standalone-operations.md': 'docs/operations.md',
    'docs/standalone-verification.md': 'docs/verification.md',
}


def build(output, root=ROOT):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError('Export must be outside the source checkout.')
    # Never reset a previous checkout or overwrite user files.
    output.mkdir(parents=True, exist_ok=False)
    selected = []
    for directory, suffix in (('publisher', '.py'), ('tests', '.py'), ('vendor', '.whl')):
        selected.extend((p.relative_to(root), p.relative_to(root))
                        for p in sorted((root / directory).glob('*' + suffix)) if p.is_file())
    for name in ('install.sh', 'bin/publisher', 'scripts/bootstrap-runtime.sh',
                 'scripts/build_standalone.py', 'assets.json', 'requirements.lock', 'pyproject.toml'):
        selected.append((Path(name), Path(name)))
    for source, target in DOCS.items():
        # The clean public checkout can also reproduce itself.
        selected.append((Path(source if (root / source).exists() else target), Path(target)))
    for source, target in selected:
        path = root / source
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Invalid release input: ' + str(source))
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    hashes = {str(p): hashlib.sha256((output / p).read_bytes()).hexdigest()
              for p in sorted(package_files(output))}
    (output / 'PACKAGE.sha256.json').write_text(json.dumps(hashes, sort_keys=True, indent=2) + '\n')
    verify_package(output)
    return {'directory': str(output), 'files': len(hashes), 'plugin_required': False,
            'live_acceptance_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    print(json.dumps(build(parser.parse_args().output_dir), indent=2))
