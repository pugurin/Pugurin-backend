import pytest

from app.core.text import normalize, search_key


@pytest.mark.parametrize(
    ("english", "korean"),
    [
        ("해운대 I PARK", "해운대아이파크"),
        ("해운대 I-PARK", "해운대 아이파크"),
        ("SK VIEW", "SK뷰"),
        ("SKVIEW", "에스케이뷰"),
        ("THE SHARP", "더샵"),
        ("센텀 XI", "센텀자이"),
        ("HILLSTATE", "힐스테이트"),
        ("Lotte Castle", "롯데캐슬"),
        ("e편한세상", "이편한세상"),
        ("LG메트로시티", "엘지메트로시티"),
        ("A동", "에이동"),
        ("THE RAUM", "더라움"),
    ],
)
def test_english_and_korean_spellings_share_one_key(english, korean):
    assert search_key(english) == search_key(korean)


def test_longer_brand_words_win_over_their_parts():
    assert search_key("HILLSTATE") == "힐스테이트"
    assert search_key("HILL") == "힐"
    assert search_key("LP VILLE") == "엘피빌"


def test_unknown_letters_are_read_one_by_one():
    assert search_key("KCC") == "케이씨씨"
    assert search_key("LH") == "엘에이치"


def test_korean_and_digits_are_left_as_normalize_leaves_them():
    assert search_key("해운대 두산위브 101동") == normalize("해운대 두산위브 101동")


def test_empty_text():
    assert search_key("") == ""
