"""Build the exact tested RNNoise revision on the current Linux computer/Pi.

Usage: python build_rnnoise.py
Needs Python 3.9+, GCC and internet access to the official GitHub/Xiph servers.
No sudo, system installation, or Python ML framework is needed. Rebuild on each
CPU: -march=native makes the output specific to the computer doing the build.
An existing official checkout with the model files can be supplied with --source.
"""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

COMMIT = '70f1d256acd4b34a572f999a05c87bf00b67730d'
MODEL_SHA = '0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37'
MODEL_C_SHA = 'd6021b7697677c4d2274c912975e143765552b0e6f25500aa660fdb4a9849be5'
SOURCES = ['denoise.c', 'rnn.c', 'pitch.c', 'kiss_fft.c', 'celt_lpc.c', 'nnet.c',
           'nnet_default.c', 'parse_lpcnet_weights.c', 'rnnoise_data.c', 'rnnoise_tables.c']


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, target):
    print('Downloading', target.name, flush=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'RNNoise-reproducible-build/1.0'})
    with urllib.request.urlopen(request, timeout=120) as response, open(target, 'wb') as stream:
        shutil.copyfileobj(response, stream)


def extract_regular_files(archive, destination, selected=None):
    # Do not follow archive links or permit traversal outside the private build folder.
    with tarfile.open(archive, 'r:gz') as bundle:
        for member in bundle:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('Unsafe archive path.')
            if selected is not None and member.name not in selected:
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise RuntimeError('Unexpected link or special file in source archive.')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, open(target, 'wb') as out:
                shutil.copyfileobj(source, out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Existing exact official git checkout, including downloaded model')
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('librnnoise.so'))
    parser.add_argument('--compiler', default='gcc')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise SystemExit('This build script targets Linux, including Raspberry Pi OS.')
    if not shutil.which(args.compiler):
        raise SystemExit('GCC was not found. Install the distribution C build tools, then rerun.')
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rnnoise-build-') as temp:
        temp = Path(temp)
        if args.source:
            source = args.source.resolve()
            revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            if revision != COMMIT:
                raise SystemExit('The local checkout is not the pinned source commit.')
            # Reject tracked local edits: these would invalidate a reproducible build.
            if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True).strip():
                raise SystemExit('The local source checkout has tracked modifications.')
        else:
            archive = temp / 'rnnoise-source.tar.gz'
            download(f'https://github.com/xiph/rnnoise/archive/{COMMIT}.tar.gz', archive)
            extract_regular_files(archive, temp)
            source = temp / f'rnnoise-{COMMIT}'
            model = temp / 'rnnoise-model.tar.gz'
            download(f'https://media.xiph.org/rnnoise/models/rnnoise_data-{MODEL_SHA}.tar.gz', model)
            if sha256(model) != MODEL_SHA:
                raise SystemExit('Official model archive checksum mismatch; stopping.')
            extract_regular_files(model, source, {'src/rnnoise_data.c', 'src/rnnoise_data.h'})
        if (source / 'model_version').read_text().strip() != MODEL_SHA:
            raise SystemExit('The source expects a different model archive.')
        if not (source / 'src/rnnoise_data.c').is_file() or sha256(source / 'src/rnnoise_data.c') != MODEL_C_SHA:
            raise SystemExit('Missing or changed model C source; use the pinned official model.')
        command = [args.compiler, '-O3', '-march=native', '-fPIC', '-shared', '-Iinclude', '-Isrc']
        command += [f'src/{name}' for name in SOURCES]
        command += ['-lm', '-o', str(output)]
        print('Compiling for this CPU...', flush=True)
        subprocess.run(command, cwd=source, check=True)
        metadata = {'source_commit': COMMIT, 'model_archive_sha256': MODEL_SHA,
                    'model_c_sha256': MODEL_C_SHA, 'library_sha256': sha256(output),
                    'frame_samples': 480, 'delay_samples': 960, 'sample_rate_hz': 48000,
                    'architecture': platform.machine(), 'system': platform.platform(),
                    'compiler': subprocess.check_output([args.compiler, '--version'], text=True).splitlines()[0],
                    'compiler_command': command, 'native_cpu_build': True}
        output.with_suffix('.json').write_text(json.dumps(metadata, indent=2) + '\n')
        # Retain the upstream license beside a locally built library.
        shutil.copyfile(source / 'COPYING', output.with_name('RNNOISE_COPYING.txt'))
    print('Built', output)
    print('Keep its matching .json beside it. Rebuild before moving to a different CPU.')


if __name__ == '__main__':
    main()
