#!/usr/bin/env python3
"""Install an invisible cursor theme used only by the touchscreen kiosk."""
from pathlib import Path
import struct


def install(destination=Path('/usr/local/share/loxicom/cursors')):
    theme = destination / 'default'
    cursors = theme / 'cursors'
    cursors.mkdir(parents=True, exist_ok=True)
    (theme / 'index.theme').write_text(
        '[Icon Theme]\nName=Loxicom touchscreen\n'
        'Comment=Invisible pointer for the dedicated kiosk session\n')

    # Xcursor header, image TOC, then one fully transparent ARGB pixel.
    # The nominal size is 24; its actual image is 1x1 with hotspot (0, 0).
    image_type = 0xFFFD0002
    image = struct.pack('<4s3I', b'Xcur', 16, 0x10000, 1)
    image += struct.pack('<3I', image_type, 24, 28)
    image += struct.pack('<10I', 36, image_type, 24, 1, 1, 1, 0, 0, 0, 0)
    (cursors / 'left_ptr').write_bytes(image)

    # Include standard Wayland/CSS names and every alias installed by the OS.
    # This also covers text fields, link hands, busy and resize pointers.
    names = set('''default arrow top_left_arrow left_ptr pointer hand hand1 hand2
        text xterm vertical-text vertical_text cross crosshair watch wait progress
        left_ptr_watch help question_arrow context-menu copy alias link move
        all-scroll size_all grab grabbing openhand closedhand no-drop not-allowed
        forbidden crossed_circle col-resize row-resize ew-resize ns-resize
        nesw-resize nwse-resize n-resize s-resize e-resize w-resize ne-resize
        nw-resize se-resize sw-resize zoom-in zoom-out dnd-copy dnd-link dnd-move
        dnd-none dnd-ask'''.split())
    for directory in Path('/usr/share/icons').glob('*/cursors'):
        names.update(path.name for path in directory.iterdir())
    for name in sorted(names - {'left_ptr'}):
        alias = cursors / name
        if not alias.is_symlink() or alias.readlink() != Path('left_ptr'):
            if alias.exists() or alias.is_symlink():
                alias.unlink()
            alias.symlink_to('left_ptr')


if __name__ == '__main__':
    install()
