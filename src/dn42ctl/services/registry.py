"""dn42 registry parser.

Reads RPSL-style flat files under `dn42_registry_path`:
  - data/aut-num/AS<N>      -> mnt-by list
  - data/mntner/<NAME>      -> auth lines
  - data/key-cert/PGPKEY-XX -> certif: ASCII-armored PGP block

All public functions sanitize identifiers and require resolved paths to stay
inside the registry root, so a hostile ASN / mntner name cannot escape via `..`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.services.core import Dn42CtlError

_ASN_RE = re.compile(r"^[0-9]+$")
_MNTNER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_PGP_FP_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Auth schemes verified via `ssh-keygen -Y verify`.
SSH_SCHEMES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ssh-dss",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
PGP_SCHEME = "pgp-fingerprint"


class RegistryError(Dn42CtlError):
    """Recoverable registry-lookup failures (file not found, malformed)."""


class RegistryNotFoundError(RegistryError):
    """Subclass for HTTP 404 mapping in the API layer."""


@dataclass(frozen=True)
class AuthOption:
    index: int  # position among SUPPORTED auth lines (0-based)
    scheme: str  # ssh-ed25519 / ssh-rsa / pgp-fingerprint / ...
    raw: str  # full original auth line (after stripping `auth:` prefix)
    fingerprint: str | None  # PGP only: 40-hex fingerprint


def _safe_join(root: Path, *parts: str) -> Path:
    """Join parts onto root, resolve, and reject anything escaping root."""
    root_abs = root.resolve()
    candidate = root_abs.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise RegistryError(f"路径越界: {candidate}") from exc
    return candidate


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RegistryNotFoundError(f"registry 文件不存在: {path.name}") from exc
    except OSError as exc:
        raise RegistryError(f"读取 registry 失败: {path} ({exc})") from exc
    return text.splitlines()


def _iter_fields(lines: list[str]) -> list[tuple[str, str]]:
    """Yield (key, value) for non-blank, non-comment lines of form `key: value`.

    Continuation lines (starting with `+` or whitespace and preceded by a key)
    are appended to the previous value. dn42's aut-num / mntner files don't use
    continuations in practice but RPSL allows them.
    """
    pairs: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line or line.lstrip().startswith("#") or line.lstrip().startswith("%"):
            continue
        if line[:1] in (" ", "\t", "+") and pairs:
            key, value = pairs[-1]
            pairs[-1] = (key, value + "\n" + line.lstrip("+ \t"))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        pairs.append((key.strip().lower(), value.strip()))
    return pairs


def _ensure_registry_root(registry_path: str | None) -> Path:
    if not registry_path:
        raise RegistryError("dn42_registry_path 未配置")
    root = Path(registry_path)
    if not root.exists():
        raise RegistryError(f"dn42_registry_path 不存在: {root}")
    return root


def read_aut_num(registry_path: str | None, asn: int) -> list[str]:
    """Return ordered list of mnt-by names declared on `aut-num/AS<asn>`.

    Raises RegistryNotFoundError if the file does not exist; RegistryError on
    malformed input or no mnt-by lines.
    """
    if asn <= 0:
        raise RegistryError(f"ASN 必须为正整数: {asn}")
    root = _ensure_registry_root(registry_path)
    asn_str = str(asn)
    if not _ASN_RE.match(asn_str):
        raise RegistryError(f"非法 ASN: {asn}")

    file_path = _safe_join(root, "data", "aut-num", f"AS{asn_str}")
    pairs = _iter_fields(_read_lines(file_path))
    mntners: list[str] = []
    for key, value in pairs:
        if key == "mnt-by" and value and value not in mntners:
            mntners.append(value)
    if not mntners:
        raise RegistryError(f"AS{asn_str} 没有 mnt-by 字段")
    return mntners


def read_mntner_auth(registry_path: str | None, mntner: str) -> list[AuthOption]:
    """Return supported auth options for the given mntner, in file order.

    Unsupported schemes (e.g. `ed25519-pw`) are silently dropped from the list;
    callers needing the full list (for diagnostics) should use the raw parser.

    The returned `index` is the position within the SUPPORTED subset; this is
    what auto-peer challenge requests reference. Storing the raw line lets the
    verifier reconstruct the allowed_signers / key-cert file later.
    """
    if not _MNTNER_RE.match(mntner):
        raise RegistryError(f"非法 mntner 名: {mntner!r}")
    root = _ensure_registry_root(registry_path)
    file_path = _safe_join(root, "data", "mntner", mntner)
    pairs = _iter_fields(_read_lines(file_path))

    options: list[AuthOption] = []
    for key, value in pairs:
        if key != "auth" or not value:
            continue
        scheme, _, rest = value.partition(" ")
        scheme = scheme.strip()
        rest = rest.strip()
        if not scheme or not rest:
            continue
        if scheme in SSH_SCHEMES:
            options.append(
                AuthOption(
                    index=len(options),
                    scheme=scheme,
                    raw=value,
                    fingerprint=None,
                )
            )
        elif scheme == PGP_SCHEME:
            fp = rest.replace(" ", "")
            if _PGP_FP_RE.match(fp):
                options.append(
                    AuthOption(
                        index=len(options),
                        scheme=scheme,
                        raw=value,
                        fingerprint=fp.upper(),
                    )
                )
        # other schemes (ed25519-pw, ...) intentionally ignored
    return options


def read_pgp_key(registry_path: str | None, fingerprint: str) -> str:
    """Return the ASCII-armored PGP public key block referenced by `fingerprint`.

    Looks up `data/key-cert/PGPKEY-<last8 of fingerprint>` and reassembles the
    multi-line `certif:` payload. Raises RegistryNotFoundError if missing.
    """
    fp = fingerprint.replace(" ", "").upper()
    if not _PGP_FP_RE.match(fp):
        raise RegistryError(f"非法 PGP fingerprint: {fingerprint!r}")
    root = _ensure_registry_root(registry_path)
    key_id = f"PGPKEY-{fp[-8:]}"
    file_path = _safe_join(root, "data", "key-cert", key_id)
    pairs = _iter_fields(_read_lines(file_path))

    certif_lines: list[str] = []
    for key, value in pairs:
        if key == "certif":
            if value:
                certif_lines.extend(value.splitlines())
            else:
                certif_lines.append("")
    if not certif_lines:
        raise RegistryError(f"{key_id} 没有 certif 字段")

    armored = "\n".join(line for line in certif_lines).strip()
    if "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in armored:
        raise RegistryError(f"{key_id} 不是 ASCII-armored PGP 公钥")
    return armored + "\n"
