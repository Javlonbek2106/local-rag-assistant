"""Where the connector keeps its secrets and its security log, and proof that nobody else can read them (decision D-5).

A secret directory is acceptable only if it is outside the project, owned by the current user, and readable by nobody
else. If that cannot be shown, the answer is a refusal (fail closed), never a guess:

- Windows: the security descriptor is read through the Windows API and written as SDDL, a text form that does not
  depend on the language of the system (the output of `icacls` does, so it is never parsed). Only the current user,
  SYSTEM and Administrators may appear as allowed trustees.
- POSIX: the directory is mode 0700, the files 0600, the owner is the current user, and the file system must be one
  whose permissions mean something (a Windows drive mounted into WSL2, like /mnt/c, does not qualify).

A SecretError carries only a fixed word (`code`): never a path and never a value.
"""
import os
import re
import subprocess
from pathlib import Path

from rag import config

NOT_PRIVATE_FS = frozenset({"drvfs", "9p", "ntfs", "ntfs3", "fuseblk", "vfat", "exfat", "cifs", "smb3", "nfs", "nfs4",
                            "fuse.sshfs"})
SYSTEM_SID, ADMINS_SID = "S-1-5-18", "S-1-5-32-544"
# A secret file name is letters, digits, dot, underscore and hyphen, starting with a letter or digit: the same on every
# system (a backslash is a separator on Windows and an ordinary character on Linux; both must be refused).
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# SDDL writes some well-known trustees as two letters. These may appear in an ACL of a private location.
ALIASES_ALLOWED = {"SY": SYSTEM_SID, "BA": ADMINS_SID}
ALIASES_OF_OWNER = frozenset({"OW", "CO"})  # "owner rights" and "creator owner": they stand for the owner


class SecretError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------- Windows ----------

def _windows_api():
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return ctypes, wintypes, advapi, kernel


def _windows_current_sid():
    ctypes, wintypes, advapi, kernel = _windows_api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):  # TOKEN_QUERY
        raise SecretError("unsupported_fs")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser; fails, but sets the size
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise SecretError("unsupported_fs")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER begins with the pointer to the SID
        text = ctypes.c_void_p()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise SecretError("unsupported_fs")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _windows_sddl(path):
    """The owner and the DACL of `path` as SDDL text."""
    ctypes, _wintypes, advapi, kernel = _windows_api()
    descriptor = ctypes.c_void_p()
    owner_and_dacl = 1 | 4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    if advapi.GetNamedSecurityInfoW(str(path), 1, owner_and_dacl, None, None, None, None,
                                    ctypes.byref(descriptor)) != 0:  # 1: a file object
        raise SecretError("not_private")
    try:
        text = ctypes.c_void_p()
        to_text = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        if not to_text(descriptor, 1, owner_and_dacl, ctypes.byref(text), None):  # 1: SDDL revision 1
            raise SecretError("not_private")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(descriptor)


def sddl_is_private(sddl, current_sid):
    """True when `sddl` (owner + DACL) lets nobody but the current user, SYSTEM and Administrators in."""
    allowed = {current_sid, SYSTEM_SID, ADMINS_SID}
    owner = re.match(r"O:(S-[0-9-]+|[A-Z]{2})", sddl)
    if not owner or ALIASES_ALLOWED.get(owner.group(1), owner.group(1)) not in allowed:
        return False
    dacl = re.search(r"D:(?:P|AI|AR|N)*(?=\()", sddl)
    if not dacl:  # no DACL at all means everybody has full access
        return False
    for ace in re.findall(r"\(([^)]*)\)", sddl[dacl.end():]):
        fields = ace.split(";")
        if len(fields) < 6 or fields[0] not in ("A", "OA"):
            continue  # only "allow" entries open a door
        trustee = fields[5]
        if trustee in ALIASES_OF_OWNER:
            continue
        if ALIASES_ALLOWED.get(trustee, trustee) not in allowed:
            return False
    return True


def _check_windows(path):
    if not sddl_is_private(_windows_sddl(path), _windows_current_sid()):
        raise SecretError("not_private")


def _secure_windows(path, directory):
    """Remove inherited access and grant it to the current user, SYSTEM and Administrators only (SIDs, not names)."""
    suffix = "(OI)(CI)F" if directory else "F"
    grants = [f"*{sid}:{suffix}" for sid in (_windows_current_sid(), SYSTEM_SID, ADMINS_SID)]
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", *grants], capture_output=True)
    if result.returncode != 0:
        raise SecretError("not_private")


# ---------- POSIX ----------

def _fs_type(path):
    """The file system type of the mount that holds `path`; "" when it cannot be told."""
    real, best = os.path.realpath(path), ("", "")
    try:
        with open("/proc/mounts", encoding="utf-8") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                inside = real == mount or real.startswith(mount.rstrip("/") + "/")
                if inside and len(mount) >= len(best[0]):
                    best = (mount, parts[2])
    except OSError:
        return ""
    return best[1]


def _check_posix(path):
    kind = _fs_type(path)
    if not kind or kind in NOT_PRIVATE_FS:
        raise SecretError("unsupported_fs")
    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise SecretError("not_owner")
    if info.st_mode & 0o077:
        raise SecretError("not_private")


# ---------- the public functions ----------

def _outside_project(path):
    resolved, root = Path(path).resolve(), Path(config.ROOT).resolve()
    if resolved == root or root in resolved.parents:
        raise SecretError("inside_project")


def _is_link(path):
    """A symbolic link or (Windows) a junction: where it leads is not what was checked, so it is never accepted."""
    path = Path(path)
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def verify_private(path):
    """Raise SecretError unless `path` (a directory or a file) is outside the project and private to this user. A link
    in its place is refused: what it points to may change, and only the link would have been looked at."""
    if not Path(path).exists():
        raise SecretError("missing")
    if _is_link(path):
        raise SecretError("symlink")
    _outside_project(path)
    (_check_windows if os.name == "nt" else _check_posix)(path)


def ensure_private_dir(path):
    """Create `path` (and the directories above it that are missing) private to this user, then verify it."""
    target = Path(path)
    _outside_project(target)
    missing = []
    for part in [target, *target.parents]:
        if part.exists():
            break
        missing.append(part)
    for part in reversed(missing):
        part.mkdir(mode=0o700)
        if os.name == "nt":
            _secure_windows(part, directory=True)
        else:
            os.chmod(part, 0o700)
    verify_private(target)


def _secret_path(name, directory):
    if not SAFE_NAME.fullmatch(name or ""):
        raise SecretError("bad_name")
    return Path(directory or config.SECRETS_DIR) / name


def read_secret(name, directory=None):
    """The text of the secret file `name` (surrounding line breaks removed); the directory and the file are verified."""
    file = _secret_path(name, directory)
    verify_private(file.parent)
    verify_private(file)
    try:
        value = file.read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeDecodeError):  # unreadable or not text: a fixed word, never the reason
        raise SecretError("unreadable") from None
    if not value:
        raise SecretError("empty")
    return value


def write_secret(name, value, directory=None):
    """Write a secret file atomically, private from the first byte on (it is created inside a private directory)."""
    file = _secret_path(name, directory)
    ensure_private_dir(file.parent)
    temporary = file.with_name(file.name + ".tmp")
    temporary.unlink(missing_ok=True)  # a leftover (or a link put there) is removed, never followed
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # made by us, private from the start
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as out:
        out.write(value)
    if os.name == "nt":
        _secure_windows(temporary, directory=False)
    else:
        os.chmod(temporary, 0o600)
    os.replace(temporary, file)
    verify_private(file)
