"""Мінімальний клієнт ONVIF: отримати адресу RTSP-потоку (GetProfiles → GetStreamUri).

Профіль 0 — основний потік, профіль 1 (якщо є) — додатковий. Автентифікація — WS-UsernameToken
(PasswordDigest), яку підтримують камери з профілем ONVIF S.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import urllib.request
from typing import List, Optional

MEDIA_PATH = "/onvif/media_service"
_NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
       'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
       'xmlns:tt="http://www.onvif.org/ver10/schema"')


def _security(login: str, password: str, nonce: Optional[bytes] = None, created: Optional[str] = None) -> str:
    if not login:
        return ""
    nonce = nonce or os.urandom(16)
    created = created or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
        f"<Username>{login}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
        f'#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
        f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security></s:Header>")


def envelope(body: str, login: str = "", password: str = "") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {_NS}>{_security(login, password)}<s:Body>{body}</s:Body></s:Envelope>'


def parse_profiles(xml: str) -> List[str]:
    return re.findall(r'<(?:\w+:)?Profiles[^>]*\btoken="([^"]+)"', xml)


def parse_stream_uri(xml: str) -> str:
    m = re.search(r"<(?:\w+:)?Uri>([^<]+)</(?:\w+:)?Uri>", xml)
    if not m:
        raise ValueError("камера не повернула адресу потоку")
    return m.group(1).replace("&amp;", "&").strip()


def _post(url: str, body: str, timeout: float) -> str:
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — адресу задає оператор
        return r.read().decode("utf-8", "replace")


def media_url(address: str) -> str:
    a = address.strip().rstrip("/")
    if not a.lower().startswith(("http://", "https://")):
        a = "http://" + a
    return a + MEDIA_PATH


def get_stream_uri(address: str, login: str = "", password: str = "", stream_kind: str = "main",
                   transport: str = "TCP", timeout: float = 5.0) -> str:
    url = media_url(address)
    profiles = parse_profiles(_post(url, envelope("<trt:GetProfiles/>", login, password), timeout))
    if not profiles:
        raise ValueError("камера не повідомила жодного профілю ONVIF")
    token = profiles[1] if stream_kind == "sub" and len(profiles) > 1 else profiles[0]
    proto = "RTSP" if transport == "TCP" else "UDP"
    body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
            f"<tt:Protocol>{proto}</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>")
    return parse_stream_uri(_post(url, envelope(body, login, password), timeout))
