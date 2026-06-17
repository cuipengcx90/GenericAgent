#!/usr/bin/env python3
"""
openssl_utils.py — 常用密码学工具集
覆盖：摘要/HMAC / Base64编解码 / 随机生成 / 对称加密(AES-GCM) / 证书解析 / RSA密钥生成 / CLI
依赖：cryptography>=3.0（pip install cryptography）；hashlib/secrets/base64 均为标准库

用法：
    from openssl_utils import (
        digest, hmac_sign, b64_encode, b64_decode,
        random_bytes, random_token,
        aes_gcm_encrypt, aes_gcm_decrypt,
        parse_cert_pem, gen_rsa_keypair,
    )
    python memory/openssl_utils.py --demo
"""

import hashlib
import hmac as _hmac
import base64
import secrets
import os
from typing import Optional, Dict, Any, Tuple


# ─────────────────────────────────────────────
# 1. 摘要 (Digest / Hash)
# ─────────────────────────────────────────────

def digest(data: bytes, algo: str = "sha256") -> str:
    """
    计算字节摘要，返回十六进制字符串。
    algo: sha256 / sha512 / sha1 / md5 / sha3_256 / blake2b 等
    """
    h = hashlib.new(algo)
    h.update(data)
    return h.hexdigest()


def digest_file(path: str, algo: str = "sha256", chunk: int = 65536) -> str:
    """流式计算文件摘要，支持大文件。"""
    h = hashlib.new(algo)
    with open(os.path.expanduser(path), "rb") as f:
        while True:
            blk = f.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


# ─────────────────────────────────────────────
# 2. HMAC
# ─────────────────────────────────────────────

def hmac_sign(key: bytes, data: bytes, algo: str = "sha256") -> str:
    """HMAC签名，返回十六进制字符串。"""
    return _hmac.new(key, data, algo).hexdigest()


def hmac_verify(key: bytes, data: bytes, expected_hex: str,
                algo: str = "sha256") -> bool:
    """恒时比较HMAC，防时序攻击。"""
    actual = hmac_sign(key, data, algo)
    return _hmac.compare_digest(actual, expected_hex.lower())


# ─────────────────────────────────────────────
# 3. Base64 编解码
# ─────────────────────────────────────────────

def b64_encode(data: bytes, urlsafe: bool = False) -> str:
    """Base64编码，返回字符串（可选URL安全模式）。"""
    fn = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return fn(data).decode()


def b64_decode(s: str, urlsafe: bool = False) -> bytes:
    """Base64解码，自动补padding。"""
    # 补齐 padding
    s = s.strip()
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    fn = base64.urlsafe_b64decode if urlsafe else base64.b64decode
    return fn(s)


# ─────────────────────────────────────────────
# 4. 随机生成
# ─────────────────────────────────────────────

def random_bytes(n: int = 32) -> bytes:
    """生成n字节密码学安全随机字节。"""
    return secrets.token_bytes(n)


def random_token(n_bytes: int = 32, urlsafe: bool = True) -> str:
    """生成随机token字符串（默认URL安全base64）。"""
    if urlsafe:
        return secrets.token_urlsafe(n_bytes)
    return secrets.token_hex(n_bytes)


def random_int(low: int, high: int) -> int:
    """生成[low, high]范围内密码学安全随机整数。"""
    return secrets.randbelow(high - low + 1) + low


# ─────────────────────────────────────────────
# 5. AES-GCM 对称加密（需 cryptography）
# ─────────────────────────────────────────────

def aes_gcm_encrypt(plaintext: bytes,
                    key: Optional[bytes] = None,
                    aad: Optional[bytes] = None
                    ) -> Dict[str, str]:
    """
    AES-256-GCM 加密。
    返回 {"key": hex, "nonce": hex, "ciphertext": hex, "tag": hex}
    若不传key则随机生成32字节密钥。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key is None:
        key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)           # 96-bit nonce (GCM标准)
    aesgcm = AESGCM(key)
    ct_with_tag = aesgcm.encrypt(nonce, plaintext, aad)
    # cryptography库将tag附在密文末尾(最后16字节)
    ct = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]
    return {
        "key":        key.hex(),
        "nonce":      nonce.hex(),
        "ciphertext": ct.hex(),
        "tag":        tag.hex(),
    }


def aes_gcm_decrypt(key_hex: str, nonce_hex: str,
                    ciphertext_hex: str, tag_hex: str,
                    aad: Optional[bytes] = None) -> bytes:
    """AES-256-GCM 解密，验证失败抛 InvalidTag。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key   = bytes.fromhex(key_hex)
    nonce = bytes.fromhex(nonce_hex)
    ct    = bytes.fromhex(ciphertext_hex)
    tag   = bytes.fromhex(tag_hex)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct + tag, aad)


# ─────────────────────────────────────────────
# 6. RSA 密钥生成 & 签名验签（需 cryptography）
# ─────────────────────────────────────────────

def gen_rsa_keypair(bits: int = 2048) -> Tuple[str, str]:
    """
    生成RSA密钥对，返回 (private_pem_str, public_pem_str)。
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    priv = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv_pem, pub_pem


def rsa_sign(priv_pem: str, data: bytes, algo: str = "sha256") -> str:
    """RSA-PSS签名，返回base64字符串。"""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes, serialization
    _HASH = {"sha256": hashes.SHA256(), "sha512": hashes.SHA512()}
    priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
    sig = priv.sign(data, padding.PSS(
        mgf=padding.MGF1(_HASH[algo]),
        salt_length=padding.PSS.MAX_LENGTH
    ), _HASH[algo])
    return b64_encode(sig)


def rsa_verify(pub_pem: str, data: bytes, sig_b64: str,
               algo: str = "sha256") -> bool:
    """RSA-PSS验签，返回True/False。"""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.exceptions import InvalidSignature
    _HASH = {"sha256": hashes.SHA256(), "sha512": hashes.SHA512()}
    pub = serialization.load_pem_public_key(pub_pem.encode())
    try:
        pub.verify(b64_decode(sig_b64), data, padding.PSS(
            mgf=padding.MGF1(_HASH[algo]),
            salt_length=padding.PSS.MAX_LENGTH
        ), _HASH[algo])
        return True
    except InvalidSignature:
        return False


# ─────────────────────────────────────────────
# 7. X.509 证书解析（需 cryptography）
# ─────────────────────────────────────────────

def parse_cert_pem(pem_str: str) -> Dict[str, Any]:
    """
    解析PEM证书，返回常用字段字典。
    支持从文件路径或PEM字符串输入。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    if pem_str.strip().startswith("/") or pem_str.strip().startswith("~"):
        pem_str = open(os.path.expanduser(pem_str.strip())).read()
    cert = x509.load_pem_x509_certificate(pem_str.encode())
    return {
        "subject":       cert.subject.rfc4514_string(),
        "issuer":        cert.issuer.rfc4514_string(),
        "serial":        str(cert.serial_number),
        "not_before":    cert.not_valid_before_utc.isoformat(),
        "not_after":     cert.not_valid_after_utc.isoformat(),
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "public_key_type":   type(cert.public_key()).__name__,
        "version":       str(cert.version),
    }


def fetch_server_cert(host: str, port: int = 443) -> str:
    """获取远端TLS服务器的PEM证书。"""
    import ssl, socket
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    pem = ssl.DER_cert_to_PEM_cert(der)
    return pem


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def _demo():
    """运行内置演示，覆盖所有主要功能。"""
    print("=" * 55)
    print("openssl_utils Demo")
    print("=" * 55)

    # 1. digest
    h = digest(b"hello world", "sha256")
    print(f"[1] digest sha256: {h[:32]}...")

    # 2. hmac
    key = b"secret-key"
    sig = hmac_sign(key, b"message")
    ok  = hmac_verify(key, b"message", sig)
    print(f"[2] HMAC-SHA256 sign+verify: {'✅' if ok else '❌'}")

    # 3. base64
    enc = b64_encode(b"\x00\xff\xde\xad\xbe\xef")
    dec = b64_decode(enc)
    print(f"[3] Base64 encode→decode: {enc} → {dec.hex()}")

    # 4. random
    tok = random_token(16)
    rb  = random_bytes(8)
    print(f"[4] token={tok[:20]}... | random_bytes={rb.hex()}")

    # 5. AES-GCM
    enc_result = aes_gcm_encrypt(b"TOP SECRET DATA", aad=b"header")
    plain = aes_gcm_decrypt(
        enc_result["key"], enc_result["nonce"],
        enc_result["ciphertext"], enc_result["tag"], aad=b"header"
    )
    print(f"[5] AES-GCM enc+dec: {'✅' if plain == b'TOP SECRET DATA' else '❌'}")

    # 6. RSA
    priv, pub = gen_rsa_keypair(2048)
    s = rsa_sign(priv, b"sign this")
    ok6 = rsa_verify(pub, b"sign this", s)
    print(f"[6] RSA-2048 PSS sign+verify: {'✅' if ok6 else '❌'}")
    print(f"    pub_key snippet: {pub[27:70]}...")

    # 7. cert (fetch from github.com)
    try:
        pem = fetch_server_cert("github.com", 443)
        info = parse_cert_pem(pem)
        print(f"[7] github.com cert subject: {info['subject'][:60]}")
        print(f"    valid until: {info['not_after']}")
    except Exception as e:
        print(f"[7] cert fetch skipped (offline?): {e}")

    print("=" * 55)
    print("✅ All demos complete")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true")
    p.add_argument("--digest", metavar="TEXT")
    p.add_argument("--algo", default="sha256")
    p.add_argument("--b64enc", metavar="HEX")
    p.add_argument("--token", type=int, metavar="N")
    args = p.parse_args()

    if args.digest:
        print(digest(args.digest.encode(), args.algo))
    elif args.b64enc:
        print(b64_encode(bytes.fromhex(args.b64enc)))
    elif args.token:
        print(random_token(args.token))
    else:
        _demo()