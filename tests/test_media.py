from main import (
    get_media_info,
    is_relevant,
)

# ==========================================================
# get_media_info
# ==========================================================

def test_get_media_info_anime_tv():
    entry = {
        "target": {
            "type": "Anime",
            "kind": "tv",
        }
    }

    assert get_media_info(entry) == ("anime", "tv")


def test_get_media_info_anime_movie():
    entry = {
        "target": {
            "type": "Anime",
            "kind": "movie",
        }
    }

    assert get_media_info(entry) == ("anime", "movie")


def test_get_media_info_manga_by_type():
    entry = {
        "target": {
            "type": "Manga",
            "kind": "manga",
        }
    }

    assert get_media_info(entry) == ("manga", "manga")


def test_get_media_info_manhwa():
    entry = {
        "target": {
            "type": "Manga",
            "kind": "manhwa",
        }
    }

    assert get_media_info(entry) == ("manga", "manhwa")


def test_get_media_info_ranobe_by_kind():
    entry = {
        "target": {
            "kind": "ranobe",
        }
    }

    assert get_media_info(entry) == ("manga", "ranobe")


def test_get_media_info_novel_by_kind():
    entry = {
        "target": {
            "kind": "novel",
        }
    }

    assert get_media_info(entry) == ("manga", "novel")


def test_get_media_info_fallback_to_anime():
    entry = {
        "target": {}
    }

    assert get_media_info(entry) == ("anime", "")


# ==========================================================
# is_relevant - anime
# ==========================================================

def test_relevant_anime_tv():
    assert is_relevant("anime", "tv") is True


def test_relevant_anime_movie():
    assert is_relevant("anime", "movie") is True


def test_relevant_anime_ova():
    assert is_relevant("anime", "ova") is True


def test_relevant_anime_ona():
    assert is_relevant("anime", "ona") is True


def test_irrelevant_anime_special():
    assert is_relevant("anime", "special") is False


def test_irrelevant_anime_tv_special():
    assert is_relevant("anime", "tv_special") is False


def test_irrelevant_anime_music():
    assert is_relevant("anime", "music") is False


def test_irrelevant_anime_pv():
    assert is_relevant("anime", "pv") is False


def test_irrelevant_anime_cm():
    assert is_relevant("anime", "cm") is False


# ==========================================================
# is_relevant - manga
# ==========================================================

def test_relevant_manga():
    assert is_relevant("manga", "manga") is True


def test_relevant_manhwa():
    assert is_relevant("manga", "manhwa") is True


def test_relevant_ranobe():
    assert is_relevant("manga", "ranobe") is True


def test_relevant_novel():
    assert is_relevant("manga", "novel") is True


def test_irrelevant_one_shot():
    assert is_relevant("manga", "one_shot") is False


def test_irrelevant_doujin():
    assert is_relevant("manga", "doujin") is False


# ==========================================================
# edge cases
# ==========================================================

def test_irrelevant_empty_kind_anime():
    assert is_relevant("anime", "") is False


def test_irrelevant_empty_kind_manga():
    assert is_relevant("manga", "") is False


def test_irrelevant_unknown_media():
    assert is_relevant("unknown", "tv") is False


# ==========================================================
# regression tests
# ==========================================================

def test_regression_manga_detected_by_kind():
    """
    Исторический баг:
    манга может определяться через kind,
    даже если type отсутствует.
    """
    entry = {
        "target": {
            "kind": "ranobe",
        }
    }

    media_type, kind = get_media_info(entry)

    assert media_type == "manga"
    assert kind == "ranobe"


def test_regression_manga_status_uses_watching():
    """
    Исторический баг /status.

    Shikimori использует watching/rewatching
    и для аниме, и для манги.

    Поэтому главное:
    манга должна определиться как manga.
    """
    entry = {
        "target": {
            "type": "Manga",
            "kind": "manga",
        }
    }

    media_type, kind = get_media_info(entry)

    assert media_type == "manga"
    assert kind == "manga"
