"""企业微信回调消息加解密（WXBizMsgCrypt 的 Python 实现）。

协议说明：https://developer.work.weixin.qq.com/document/path/90968
- 签名：sha1(sort([token, timestamp, nonce, encrypt]))
- 加密：AES-256-CBC，key = base64decode(EncodingAESKey + "=")，iv = key[:16]
- 明文：random(16B) + msg_len(4B, 网络字节序) + msg + receive_id，PKCS#7 填充到 32 字节倍数
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree as ET

BLOCK_SIZE = 32


class WeComCryptoError(Exception):
    pass


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError("EncodingAESKey 长度必须是 43 位")
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]
        self.receive_id = receive_id

    # ---- 签名 ----
    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = sorted([self.token, str(timestamp), str(nonce), encrypted])
        return hashlib.sha1("".join(parts).encode()).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        expected = self.signature(timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, msg_signature or ""):
            raise WeComCryptoError("签名校验失败")

    # ---- AES ----
    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    def decrypt(self, encrypted: str) -> str:
        try:
            data = base64.b64decode(encrypted)
            dec = self._cipher().decryptor()
            plain = dec.update(data) + dec.finalize()
        except Exception as e:  # noqa: BLE001
            raise WeComCryptoError(f"解密失败: {e}") from e
        pad = plain[-1]
        if pad < 1 or pad > BLOCK_SIZE:
            raise WeComCryptoError("填充错误")
        content = plain[16:-pad]
        (length,) = struct.unpack(">I", content[:4])
        msg = content[4 : 4 + length]
        rid = content[4 + length :].decode()
        if rid != self.receive_id:
            raise WeComCryptoError("receive_id 不匹配（检查 CorpID 配置）")
        return msg.decode()

    def encrypt(self, text: str) -> str:
        msg = text.encode()
        raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + self.receive_id.encode()
        pad = BLOCK_SIZE - len(raw) % BLOCK_SIZE
        raw += bytes([pad]) * pad
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(raw) + enc.finalize()).decode()

    # ---- 回调便捷方法 ----
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """处理配置回调 URL 时的 GET 校验，返回需要原样回给企业微信的明文。"""
        self.verify(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def decrypt_callback(self, msg_signature: str, timestamp: str, nonce: str, body: bytes | str) -> str:
        """处理 POST 回调，返回解密后的 XML 明文。"""
        try:
            root = ET.fromstring(body)
        except Exception as e:  # noqa: BLE001
            raise WeComCryptoError(f"XML 解析失败: {e}") from e
        node = root.find("Encrypt")
        if node is None or not node.text:
            raise WeComCryptoError("缺少 Encrypt 字段")
        self.verify(msg_signature, timestamp, nonce, node.text)
        return self.decrypt(node.text)

    def build_callback(self, plain_xml: str, nonce: str = "nonce123") -> tuple[bytes, dict]:
        """测试/模拟用：把明文包装成企业微信推送的格式，返回 (body, query 参数)。"""
        ts = str(int(time.time()))
        enc = self.encrypt(plain_xml)
        sig = self.signature(ts, nonce, enc)
        body = f"<xml><ToUserName><![CDATA[{self.receive_id}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        return body.encode(), {"msg_signature": sig, "timestamp": ts, "nonce": nonce}


def parse_xml(xml_text: str) -> dict[str, str]:
    """把一层的企业微信 XML 消息转成 dict。"""
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}
