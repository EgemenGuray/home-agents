from conftest import Phone

_other = Phone()


def other_phone_signature(req, decision):
    return _other.sign(req, decision)
