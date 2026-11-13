"""Garde-fous de sortie ajoutés après la relecture du code : numérotation des threads,
hashtags et guillemets, sujets interdits venus du texte de l'article, chiffres
comparés par valeur, ordre des posts récents, post du matin en mode automatique."""
from __future__ import annotations

import pytest

from bot import filters, sources
from bot import writer as wmod
from bot.filters import blocked_topic, clean_output, publish_problem, strip_thread_numbering
from bot.models import NewsItem
from bot.writer import Writer
from tests.fakes import FakeLLM


def _item(text: str) -> NewsItem:
    return NewsItem(id="1", title="A title", summary=text, url="https://example.com/a", source="Example",
                    published=None)


# ---------------------------------------------------------------------------
# Numérotation des tweets : retirée, jamais un chiffre du texte
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, position, expected", [
    ("1/ The hook.", 1, "The hook."),
    ("2/7 Next one.", 2, "Next one."),
    ("3. Third.", 3, "Third."),
    ("4) Fourth.", 4, "Fourth."),
    ("5/7: Fifth.", 5, "Fifth."),
    ("  3/  ", None, ""),
    ("2.5 million homes already have one.", 2, "2.5 million homes already have one."),
    ("24/7, it never sleeps.", 1, "24/7, it never sleeps."),
    ("2/3 of people use one.", 2, "2/3 of people use one."),
    ("3/7 Out of order.", 2, "3/7 Out of order."),           # n/m qui n'est pas son rang : une fraction ?
    ("10 robots were sold.", 3, "10 robots were sold."),
    ("1. Hook", None, "Hook"),
])
def test_strip_thread_numbering(raw, position, expected):
    assert strip_thread_numbering(raw, position) == expected


# ---------------------------------------------------------------------------
# clean_output : « #1 » n'est pas un hashtag ; deux citations ne sont pas « entourées »
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("ChatGPT is now the #1 app in 40 countries.", "ChatGPT is now the #1 app in 40 countries."),
    ("Big #AI news today.", "Big news today."),
    ('"It can cook," says the engineer, "and it never burns rice."',
     '"It can cook," says the engineer, "and it never burns rice."'),
    ('"The whole post was quoted."', "The whole post was quoted."),
    ("“Curly quotes around everything.”", "Curly quotes around everything."),
])
def test_clean_output_keeps_rankings_and_inner_quotes(raw, expected):
    assert clean_output(raw) == expected


# ---------------------------------------------------------------------------
# Sujets interdits dans NOS textes (le texte de l'article peut en parler)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, term", [
    ("The army has already ordered 100 of them.", "The army"),
    ("Some are fitted with a rifle for the military.", "rifle"),
    ("The military already uses them.", "military"),
    ("Last year one killed a worker.", "killed"),
    ("It spots cancer earlier than doctors.", "cancer"),
    ("Amazon now faces a lawsuit over it.", "lawsuit"),
])
def test_blocked_topic_in_posts(text, term):
    assert blocked_topic(text) == term
    assert publish_problem(text, plain=True) == f"hors ligne éditoriale ({term})"


@pytest.mark.parametrize("text", [
    "Vote below: would you let it fly in your home?",
    "A robot dog that opens doors by itself.",
    "It remembers every face it sees.",
])
def test_blocked_topic_lets_everyday_posts_through(text):
    assert blocked_topic(text) is None
    assert publish_problem(text, plain=True) is None


def test_military_list_is_shared_with_the_news_filter():
    assert sources._MILITARY_RE is filters.MILITARY_RE


def test_off_limits_prompt_names_the_military():
    assert "Weapons, armies, the military, the police" in wmod.OFF_LIMITS


# ---------------------------------------------------------------------------
# Chiffres : comparés par valeur (3 billion = 3bn = 3,000,000,000 = three billion)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, hay, expected", [
    ("It raised $5m.", "It raised $5 million.", None),
    ("It is 5m tall.", "", None),                                 # des mètres : petit nombre, laissé au relecteur
    ("Open 24/7.", "", None),
    ("20 robots.", "20 million people", "20"),
    ("It costs $1.2B.", "a $1.2 billion deal", None),
    ("It costs $1.2B.", "a $1.2 million deal", "$1.2B"),
    ("$300K a year.", "a 300k salary", None),
    ("3 billion users.", "3bn users", None),
    ("3 billion users.", "three billion users", None),
    ("3 billion users.", "3,000,000,000 users", None),
    ("It is 2.5x faster.", "2.5 times faster", None),
    ("It is 10x faster.", "ten times faster", None),
    ("100 robots.", "hundreds of robots", "100"),
    ("12 eggs.", "a dozen eggs", None),
    ("25 robots.", "twenty five robots", None),
    ("40 percent.", "40%", None),
    ("1.5 million.", "1.5m", None),
    ("It weighs 300k grams.", "It weighs 300,000 grams.", None),
    ("About 1.2 million people.", "About 1,200,000 people.", None),
    ("It is now worth $40 billion.", "It has 40 cars.", "$40 billion"),
])
def test_ungrounded_number_compares_values(text, hay, expected):
    assert wmod.ungrounded_number(text, [_item(hay)]) == expected


# ---------------------------------------------------------------------------
# Posts récents : dans l'ordre où ils ont été publiés, tous types confondus
# ---------------------------------------------------------------------------
def test_recent_all_is_in_publication_order(settings, state):
    for i in range(12):
        state.add_published(f"n{i}", "news", f"news post number {i}")
        state.remember_text("news", f"news post number {i}")
    state.add_published("t1", "tip", "today's tip about a vacuum that knows faces")
    state.remember_text("tip", "today's tip about a vacuum that knows faces")
    w = Writer(settings, FakeLLM(), state, read_article=lambda url: "")
    recent = w.recent_all()
    assert recent[-1] == "today's tip about a vacuum that knows faces"
    assert recent[-11:-1] == [f"news post number {i}" for i in range(2, 12)]
    assert len(recent) == len(set(recent))


def test_recent_all_keeps_old_remembered_texts(settings, state):
    state.remember_text("tip", "an old tip with no publication record")
    state.add_published("n1", "news", "a news post")
    w = Writer(settings, FakeLLM(), state, read_article=lambda url: "")
    assert w.recent_all() == ["an old tip with no publication record", "a news post"]


def test_editor_sees_the_latest_post_of_another_kind(settings, state):
    for i in range(12):
        state.add_published(f"n{i}", "news", f"news post number {i}")
    state.add_published("t1", "tip", "today's tip about a vacuum that knows faces")
    llm = FakeLLM({"pick-": {"picks": [], "reason": "rien"}})
    w = Writer(settings, llm, state, read_article=lambda url: "")
    w._pick("news", [_item("A robot that folds laundry.")], 1)
    assert "today's tip about a vacuum that knows faces" in llm.calls[0]["user"]


# ---------------------------------------------------------------------------
# Post du matin : jamais publié sans vérification en mode automatique
# ---------------------------------------------------------------------------
GM = "gm. what is one chore you would hand to a robot first?"


def _gm_writer(settings, state):
    llm = FakeLLM({"post-gm": {"text": GM, "used_note_ids": [], "poll_options": [], "reason": "idée"}})
    return Writer(settings, llm, state, read_article=lambda url: ""), llm


def test_gm_without_factcheck_is_dropped_in_auto_mode(settings, state):
    settings.approval_mode = "auto"
    w, llm = _gm_writer(settings, state)
    assert w.draft_post("gm", [], [], "") is None
    assert llm.labels() == ["post-gm", "factcheck"]


def test_gm_without_factcheck_is_flagged_in_telegram_mode(settings, state):
    settings.approval_mode = "telegram"
    w, _ = _gm_writer(settings, state)
    d = w.draft_post("gm", [], [], "")
    assert d is not None and "vérification des faits indisponible" in d.reason
