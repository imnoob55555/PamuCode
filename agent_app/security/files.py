"""Descriptor-relative regular-file I/O for confined built-in file tools."""
import contextlib
import os
import stat

from .policy import SecurityDenied

MAX_FILE_BYTES = 8 * 1024 * 1024


@contextlib.contextmanager
def open_regular(policy, path, *, write=False, create=False):
    # path was checked by the caller. Walk its lexical components without
    # re-resolving: re-resolution could follow a link swapped after the check.
    parts = path.relative_to(policy.workspace).parts
    if not parts:
        raise SecurityDenied('Expected a file')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(policy.workspace, flags)
    descriptor = None
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        access = os.O_RDWR if write else os.O_RDONLY
        if create:
            access |= os.O_CREAT
        descriptor = os.open(parts[-1], access | os.O_NOFOLLOW | os.O_NONBLOCK,
                             mode=0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SecurityDenied('Only single-link regular files are supported')
        if info.st_size > MAX_FILE_BYTES:
            raise SecurityDenied('File exceeds confined tool byte limit')
        with os.fdopen(descriptor, 'r+' if write else 'r', encoding='utf-8') as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def read_bounded(stream):
    text = stream.read(MAX_FILE_BYTES + 1)
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise SecurityDenied('File exceeds confined tool byte limit')
    return text


def replace_text(stream, text):
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise SecurityDenied('File exceeds confined tool byte limit')
    stream.seek(0)
    stream.write(text)
    stream.truncate()
