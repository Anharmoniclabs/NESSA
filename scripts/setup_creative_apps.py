#!/usr/bin/env python3
"""Expose CLI engines bundled in the user's already-installed Craft AppImages.
No downloads, GUI launch, shell evaluation, or overwriting existing launchers.
"""
from pathlib import Path
import subprocess


def main():
    base = Path.home()/'.local/opt/artcraft-suite'
    launchers = Path.home()/'.local/bin'
    launchers.mkdir(parents=True,exist_ok=True)
    for app in ('photocraft','vectorcraft','filmcraft','lightcraft','printcraft','effectcraft','designcraft'):
        artifacts = sorted(base.glob(app+'-*.AppImage'))
        if len(artifacts) != 1:
            raise RuntimeError(f'Expected one installed {app} AppImage; found {len(artifacts)}. Select the installed version first.')
        dest = base/'automation'/app
        dest.mkdir(parents=True,exist_ok=True)
        subprocess.run([str(artifacts[0]),'--appimage-extract','usr/bin/*'],cwd=dest,
                       check=True,capture_output=True,timeout=60)
        binaries = list((dest/'squashfs-root/usr/bin').glob('*cli*'))
        if not binaries:
            raise RuntimeError(f'{app}: no bundled CLI found')
        for binary in binaries:
            target = launchers/binary.name
            if target.is_symlink() and target.resolve() == binary.resolve():
                pass
            elif target.exists() or target.is_symlink():
                raise RuntimeError(f'Existing launcher preserved: {target}')
            else:
                target.symlink_to(binary)
            print(f'{app}: {target}')


if __name__ == '__main__':
    main()
