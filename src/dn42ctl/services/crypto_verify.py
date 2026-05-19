"""Subprocess wrappers for SSH / PGP signature verification.

These wrap external CLI tools (`ssh-keygen`, `gpg`) rather than depending on a
Python crypto library, so the runtime stays slim. Both functions are designed
to NEVER raise: on any subprocess error (missing tool, bad input, timeout,
non-zero exit) they return False. The caller treats False as "verification
failed" and maps it to an HTTP 400.

A fresh `tempfile.TemporaryDirectory` is used per call so the host system
keystore / keyring is never touched.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

_SUBPROCESS_TIMEOUT_S = 5.0


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdin_input: bytes | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a subprocess with stdin/stdout/stderr captured, fixed timeout, no shell."""
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(  # noqa: S603 — argv is constructed from constants + tempfile paths
        cmd,
        check=False,
        capture_output=True,
        input=stdin_input,
        cwd=str(cwd) if cwd else None,
        timeout=_SUBPROCESS_TIMEOUT_S,
        env=env,
    )


def verify_ssh(
    *,
    message: bytes,
    signature: str,
    allowed_pubkey: str,
    namespace: str,
    identity: str,
) -> bool:
    """Verify an OpenSSH signature (ssh-keygen -Y sign output) against a known pubkey.

    Args:
        message: original signed bytes (the challenge nonce, in our case).
        signature: ASCII-armored `-----BEGIN SSH SIGNATURE-----` block pasted
            by the user.
        allowed_pubkey: the full `auth:` line value (e.g. "ssh-ed25519 AAAA... comment"),
            already pulled from the mntner file.
        namespace: must match what the user used in `ssh-keygen -Y sign -n <ns>`.
        identity: principal identity to associate with the pubkey in
            allowed_signers (e.g. "dn42@<asn>-<mntner>").

    Returns:
        True iff ssh-keygen exits 0; False on any failure.
    """
    if shutil.which("ssh-keygen") is None:
        return False
    if not signature.strip():
        return False
    if not allowed_pubkey.strip() or not namespace or not identity:
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="dn42ctl-ssh-verify-") as td:
            tmp = Path(td)
            sig_path = tmp / "msg.sig"
            allowed_path = tmp / "allowed_signers"
            sig_path.write_text(signature, encoding="utf-8")
            allowed_path.write_text(
                f"{identity} {allowed_pubkey.strip()}\n",
                encoding="utf-8",
            )
            try:
                result = _run(
                    [
                        "ssh-keygen",
                        "-Y",
                        "verify",
                        "-n",
                        namespace,
                        "-I",
                        identity,
                        "-s",
                        str(sig_path),
                        "-f",
                        str(allowed_path),
                    ],
                    stdin_input=message,
                )
            except (subprocess.TimeoutExpired, OSError):
                return False
            return result.returncode == 0
    except OSError:
        return False


def verify_pgp(*, message: bytes, signature: str, ascii_key: str) -> bool:
    """Verify a cleartext-signed PGP message against a known public key.

    `signature` is the entire `-----BEGIN PGP SIGNED MESSAGE-----` block the
    user pastes after running `gpg --clearsign`. The original `message` bytes
    must round-trip through the cleartext-signed envelope (i.e. equal the
    payload between the headers).
    """
    if shutil.which("gpg") is None:
        return False
    if not signature.strip() or not ascii_key.strip():
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="dn42ctl-pgp-verify-") as td:
            tmp = Path(td)
            home = tmp / "gnupg"
            home.mkdir(mode=0o700)
            key_path = tmp / "key.asc"
            signed_path = tmp / "signed.asc"
            output_path = tmp / "plain.txt"
            key_path.write_text(ascii_key, encoding="utf-8")
            signed_path.write_text(signature, encoding="utf-8")

            common = [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-tty",
                "--no-auto-key-locate",
                "--no-auto-key-retrieve",
                "--trust-model",
                "always",
                "--quiet",
            ]

            try:
                imp = _run(common + ["--import", str(key_path)])
                if imp.returncode != 0:
                    return False
                # --output extracts the cleartext for byte comparison; --verify alone
                # only checks the signature.
                verified = _run(
                    common
                    + [
                        "--output",
                        str(output_path),
                        "--decrypt",
                        str(signed_path),
                    ]
                )
            except (subprocess.TimeoutExpired, OSError):
                return False
            if verified.returncode != 0 or not output_path.exists():
                return False
            try:
                plain = output_path.read_bytes()
            except OSError:
                return False
            # gpg's --clearsign appends a trailing newline; strip both sides
            # before byte compare so users don't have to fight whitespace.
            return plain.rstrip(b"\r\n") == message.rstrip(b"\r\n")
    except OSError:
        return False
