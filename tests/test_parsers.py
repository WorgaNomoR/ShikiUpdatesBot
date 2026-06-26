from main import (
    _strip_html,
    classify_event,
    extract_score,
    extract_score_change,
)

# ==========================================================
# _strip_html
# ==========================================================

def test_strip_html_bold():
    assert _strip_html("оценено на <b>7</b>") == "оценено на 7"


def test_strip_html_strong():
    assert _strip_html("оценено на <strong>8</strong>") == "оценено на 8"


def test_strip_html_multiple_tags():
    assert (
        _strip_html("изменена оценка с <b>5</b> на <i>9</i>")
        == "изменена оценка с 5 на 9"
    )


# ==========================================================
# extract_score
# ==========================================================

def test_extract_score_ru():
    assert extract_score("оценено на 9") == 9


def test_extract_score_alt_ru_male():
    assert extract_score("выставил оценку 8") == 8


def test_extract_score_alt_ru_female():
    assert extract_score("выставила оценку 6") == 6


def test_extract_score_rated():
    assert extract_score("rated 7") == 7


def test_extract_score_scored():
    assert extract_score("scored 10") == 10


def test_extract_score_html_bold():
    assert extract_score("оценено на <b>7</b>") == 7


def test_extract_score_html_strong():
    assert extract_score("оценено на <strong>8</strong>") == 8


def test_extract_score_invalid():
    assert extract_score("какой-то текст") is None


def test_extract_score_empty():
    assert extract_score("") is None


# ==========================================================
# extract_score_change
# ==========================================================

def test_extract_score_change_ru():
    assert extract_score_change(
        "изменена оценка с 5 на 9"
    ) == (5, 9)


def test_extract_score_change_html():
    assert extract_score_change(
        "изменена оценка с <b>5</b> на <b>9</b>"
    ) == (5, 9)


def test_extract_score_change_invalid():
    assert extract_score_change(
        "изменена оценка"
    ) is None


# ==========================================================
# classify_event
# ==========================================================

def test_classify_score_changed():
    assert classify_event(
        "изменена оценка с 5 на 8"
    ) == "score_changed"


def test_classify_watching_smotryu():
    assert classify_event("смотрю") == "watching"


def test_classify_watching_smotrit():
    assert classify_event("смотрит") == "watching"


def test_classify_watching_chitayu():
    assert classify_event("читаю") == "watching"


def test_classify_watching_reading():
    assert classify_event("reading") == "watching"


def test_classify_rewatching_ru():
    assert classify_event("пересматриваю") == "rewatching"


def test_classify_rereading_ru():
    assert classify_event("перечитываю") == "rewatching"


def test_classify_rewatching_en():
    assert classify_event("rewatching") == "rewatching"


def test_classify_planned():
    assert classify_event("добавлено в список") == "planned"


def test_classify_planned_english():
    assert classify_event("planned") == "planned"


def test_classify_dropped():
    assert classify_event("брошено") == "dropped"


def test_classify_completed_fallback():
    assert classify_event("просмотрено") == "completed"


def test_classify_completed_with_score():
    assert classify_event("оценено на 8") == "completed"
