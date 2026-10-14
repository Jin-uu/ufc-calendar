import pytest

from ufc_calendar.countries import country_name, flag, to_iso2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("KOR", "KR"),  # FightMetric (IOC = ISO here)
        ("GER", "DE"),  # IOC differs from ISO
        ("SUI", "CH"),
        ("IRI", "IR"),
        ("BRN", "BH"),  # IOC Bahrain, not ISO Brunei
        ("South Korea", "KR"),  # ESPN names
        ("USA", "US"),
        ("England", "GB"),
        ("Russia", "RU"),
        ("Türkiye", "TR"),
        ("Czechia", "CZ"),
        ("Brazil", "BR"),
        ("kr", "KR"),  # ISO alpha-2
        ("North Korea", "KP"),
        ("Atlantis", None),
        ("", None),
        (None, None),
    ],
)
def test_to_iso2(value, expected):
    assert to_iso2(value) == expected


def test_names_and_flags():
    assert flag("KR") == "🇰🇷"
    assert flag("jp") == "🇯🇵"
    assert country_name("KR") == "South Korea"
    assert country_name("KR", korean=True) == "대한민국"
    assert country_name("BR", korean=True) == "브라질"
    assert country_name("RU", korean=True) == "러시아"
