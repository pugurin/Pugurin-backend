import re

_STRIP = re.compile(r"[\s\W_]+")
_LATIN = re.compile(r"[a-z]+")

# 단지 이름에 쓰이는 영문 브랜드·낱말의 한글 표기. 긴 낱말부터 맞춘다(hillstate가 hill보다 먼저)
_WORDS = {
    "hillstate": "힐스테이트",
    "raemian": "래미안",
    "central": "센트럴",
    "prugio": "푸르지오",
    "castle": "캐슬",
    "centum": "센텀",
    "golden": "골든",
    "palace": "팰리스",
    "studio": "스튜디오",
    "forest": "포레스트",
    "sharp": "샵",
    "lotte": "롯데",
    "smart": "스마트",
    "prime": "프라임",
    "crown": "크라운",
    "tower": "타워",
    "value": "밸류",
    "bless": "블레스",
    "smile": "스마일",
    "patio": "파티오",
    "ipark": "아이파크",
    "ville": "빌",
    "park": "파크",
    "view": "뷰",
    "city": "시티",
    "vill": "빌",
    "hill": "힐",
    "raum": "라움",
    "zoom": "줌",
    "weve": "위브",
    "sky": "스카이",
    "hub": "허브",
    "the": "더",
    "xi": "자이",
}
_WORD_ORDER = sorted(_WORDS, key=len, reverse=True)

# 사전에 없는 영문은 한 글자씩 읽는다(KCC → 케이씨씨, e편한세상 → 이편한세상)
_LETTERS = dict(
    zip(
        "abcdefghijklmnopqrstuvwxyz",
        "에이 비 씨 디 이 에프 지 에이치 아이 제이 케이 엘 엠 엔 "
        "오 피 큐 알 에스 티 유 브이 더블유 엑스 와이 제트".split(),
        strict=True,
    )
)


def normalize(text: str) -> str:
    """공백·특수문자를 없애고 소문자로 맞춘 비교용 문자열."""
    return _STRIP.sub("", text).lower()


def _hangul(run: str) -> str:
    out, i = [], 0
    while i < len(run):
        word = next((w for w in _WORD_ORDER if run.startswith(w, i)), None)
        if word:
            out.append(_WORDS[word])
            i += len(word)
        else:
            out.append(_LETTERS[run[i]])
            i += 1
    return "".join(out)


def search_key(text: str) -> str:
    """검색 비교용 키. normalize한 뒤 영문을 한글 표기로 바꿔, '해운대 I PARK'와 '해운대아이파크'가 같은 키가 된다.

    단지 이름과 검색어 양쪽에 똑같이 적용한다. 거래와 단지를 잇는 이름 키(normalize)에는 쓰지 않는다.
    """
    return _LATIN.sub(lambda m: _hangul(m.group()), normalize(text))
