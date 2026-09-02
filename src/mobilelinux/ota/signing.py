"""Release signing and verification.

Signing uses **ed25519** (small, fast, no parameter pitfalls). Private keys are
never stored in git (``keys/`` is git-ignored). There is a strict separation
between *development* keys (self-generated, for testing) and *release* keys
(kept offline / in an HSM; only the public key ships on the device).

Two backends:
  * ``cryptography`` (preferred, pure-Python verify on the phone),
  * ``openssl`` CLI fallback.

The signature covers the canonical manifest body (see manifest.canonical_body).
"""

from __future__ import annotations

import base64
from pathlib import Path

from ..core import tools, ui
from ..core.errors import MobileLinuxError


class SigningError(MobileLinuxError):
    pass


def _have_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Key management
# --------------------------------------------------------------------------
def generate_keypair(private_path: Path, public_path: Path, *, key_id: str) -> None:
    """Generate an ed25519 keypair. Refuses to overwrite an existing key."""
    if private_path.exists():
        raise SigningError(f"refusing to overwrite existing private key {private_path}")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    if _have_crypto():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        key = Ed25519PrivateKey.generate()
        priv = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_path.write_bytes(priv)
        public_path.write_bytes(pub)
    else:
        # openssl fallback
        tools.require("openssl")
        import subprocess
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519",
                        "-out", str(private_path)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(private_path),
                        "-pubout", "-out", str(public_path)], check=True)
    private_path.chmod(0o600)
    ui.success(f"generated key '{key_id}': {private_path} (KEEP PRIVATE), {public_path}")


# --------------------------------------------------------------------------
# Signing / verifying
# --------------------------------------------------------------------------
def sign(body: bytes, private_path: Path) -> str:
    """Return a base64 ed25519 signature of ``body``."""
    if _have_crypto():
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        sig = key.sign(body)
        return base64.b64encode(sig).decode("ascii")
    # openssl fallback (writes body to a temp file)
    tools.require("openssl")
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile() as bf, tempfile.NamedTemporaryFile() as sf:
        bf.write(body); bf.flush()
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_path),
                        "-rawin", "-in", bf.name, "-out", sf.name], check=True)
        return base64.b64encode(Path(sf.name).read_bytes()).decode("ascii")


def verify(body: bytes, signature_b64: str, public_path: Path) -> bool:
    """Verify a base64 ed25519 signature over ``body``. Returns True/False."""
    sig = base64.b64decode(signature_b64)
    if _have_crypto():
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
        pub = serialization.load_pem_public_key(public_path.read_bytes())
        try:
            pub.verify(sig, body)
            return True
        except InvalidSignature:
            return False
    tools.require("openssl")
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile() as bf, tempfile.NamedTemporaryFile() as sf:
        bf.write(body); bf.flush()
        sf.write(sig); sf.flush()
        r = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_path),
             "-rawin", "-in", bf.name, "-sigfile", sf.name],
            capture_output=True,
        )
        return r.returncode == 0
