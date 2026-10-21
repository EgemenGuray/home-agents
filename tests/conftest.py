import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).parent))


class Phone:
    """Stands in for the app: a P-256 key like the Secure Enclave's."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    @property
    def public_pem(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, req, decision, attachment_sha256=""):
        sig = self.key.sign(req.signing_message(decision, attachment_sha256), ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(sig).decode()


@pytest.fixture
def phone():
    return Phone()


@pytest.fixture
def conn():
    from home_agents.db import connect
    return connect(":memory:")


def block(**kw):
    return SimpleNamespace(**kw)


def response(content, stop_reason="end_turn", inp=100, out=20):
    return SimpleNamespace(content=content, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


class FakeClient:
    """Returns scripted responses and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        return self.responses.pop(0)
