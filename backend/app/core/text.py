import re

_STRIP = re.compile(r"[\s\W_]+")


def normalize(text: str) -> str:
    """공백·특수문자를 없애고 소문자로 맞춘 비교용 문자열."""
    return _STRIP.sub("", text).lower()
