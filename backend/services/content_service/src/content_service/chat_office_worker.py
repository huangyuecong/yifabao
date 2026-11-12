"""Internal Linux-only exec helper. No application configuration is loaded."""

import ctypes
import errno
import os
import resource
import socket
import sys
from pathlib import Path


def main():
    directory, executable, conversion_filter, source = sys.argv[1:]
    root = Path(directory).resolve(strict=True)
    if not root.name.startswith("content-office-") or Path(source).parent != root:
        raise ValueError("Invalid private conversion directory")
    resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024**2, 100 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if os.geteuid() == 0:
        for path in [root, *root.rglob("*")]:
            os.chown(path, 65534, 65534, follow_symlinks=False)
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    os.umask(0o077)
    os.chdir(root)
    # Converters must not fetch external links from uploaded Office documents.
    # Local Unix sockets used by Writer remain available for its own IPC.
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_int),
            ("datum_a", ctypes.c_uint64),
            ("datum_b", ctypes.c_uint64),
        ]

    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(Comparison),
    ]
    seccomp.seccomp_rule_add_array.restype = ctypes.c_int
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot initialize conversion network isolation")
    try:
        syscall = seccomp.seccomp_syscall_resolve_name(b"socket")
        for family in (socket.AF_INET, socket.AF_INET6, socket.AF_PACKET):
            comparison = Comparison(0, 4, family, 0)  # SCMP_CMP_EQ
            if (
                seccomp.seccomp_rule_add_array(
                    context, 0x00050000 | errno.EPERM, syscall, 1, ctypes.byref(comparison)
                )
                != 0
            ):
                raise RuntimeError("Cannot configure conversion network isolation")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Cannot enforce conversion network isolation")
    finally:
        seccomp.seccomp_release(context)
    command = [
        executable,
        "-env:UserInstallation=" + (root / "profile").as_uri(),
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        "--convert-to",
        conversion_filter,
        "--outdir",
        str(root / "output"),
        source,
    ]
    os.execv(executable, command)


if __name__ == "__main__":
    main()
