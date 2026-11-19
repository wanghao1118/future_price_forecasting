"""Load a small owner-only AIHub configuration without executing shell code."""

import os
from pathlib import Path
import re
import stat


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "futures-newsdesk" / "aihub.env"
ALLOWED_VARIABLES = frozenset(("AIHUB_API_KEY", "AIHUB_BASE_URL"))
MAX_CONFIG_BYTES = 65536


class PrivateConfigError(ValueError):
    """Safe diagnostics never include configuration lines or secret values."""


def _verify_private_metadata(metadata, *, directory=False):
    expected_mode = 0o700 if directory else 0o600
    kind_matches = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    label = "目录" if directory else "文件"
    if not kind_matches:
        raise PrivateConfigError("AIHub 私有配置{}必须是普通{}，不允许符号链接。".format(label, label))
    if metadata.st_uid != os.geteuid():
        raise PrivateConfigError("AIHub 私有配置{}必须属于当前用户。".format(label))
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PrivateConfigError("AIHub 私有配置{}权限必须为 {:04o}。".format(label, expected_mode))


def _read_private_file(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise PrivateConfigError("无法检查 AIHub 私有配置文件。") from None
    _verify_private_metadata(metadata)
    directory_fd = None
    file_fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(str(path.parent), flags | getattr(os, "O_DIRECTORY", 0))
        _verify_private_metadata(os.fstat(directory_fd), directory=True)
        file_fd = os.open(path.name, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        _verify_private_metadata(os.fstat(file_fd))
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise PrivateConfigError("AIHub 私有配置文件过大。")
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        raise PrivateConfigError("无法安全读取 AIHub 私有配置，请检查文件类型、所有者和权限。") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _parse_private_config(content):
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match or match.group(1) not in ALLOWED_VARIABLES:
            raise PrivateConfigError("AIHub 私有配置仅允许 AIHUB_API_KEY 和 AIHUB_BASE_URL 赋值。")
        name, value = match.groups()
        if name in values:
            raise PrivateConfigError("AIHub 私有配置包含重复变量。")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise PrivateConfigError("AIHub 私有配置的引号不完整。")
            value = value[1:-1]
        # Values are literal: no shell expansion, interpolation or escape decoding.
        if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
            raise PrivateConfigError("AIHub 私有配置包含空值或无效空白/控制字符。")
        values[name] = value
    return values


def load_private_aihub_config(path=None, environ=None):
    """Fill missing process variables; return variable names, never their values.

    An existing process key is an explicit override and needs no private file.
    Missing files preserve the existing environment-only behavior.
    """
    environment = os.environ if environ is None else environ
    if environment.get("AIHUB_API_KEY"):
        return ()
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    content = _read_private_file(config_path)
    if content is None:
        return ()
    values = _parse_private_config(content)
    loaded = []
    for name, value in values.items():
        if not environment.get(name):
            environment[name] = value
            loaded.append(name)
    return tuple(loaded)
