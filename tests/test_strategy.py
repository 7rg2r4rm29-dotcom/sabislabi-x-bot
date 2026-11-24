"""L'agent d'analyse (bot/strategy.py) : chiffres, tendances et algorithme → consignes. Claude
est simulé : l'analyste et le relecteur renvoient ce que le test leur fait dire."""
from __future__ import annotations

import pytest

from bot import strategy as smod
from bot.strategy import Strategist, directive_problem

GOOD_EDITOR = {"text": "Put breaking news that names something everybody knows first.",
               "fr": "Mettre en premier les actus chaudes qui nomment quelque chose que tout le monde connaît.",
               "basis": "numbers", "n": 12,
               "why": "First lines with breaking wording: 1.32x on 52 posts against 1.16x."}
GOOD_WRITER = {"text": "End most posts with a statement, not a question.", "basis": "numbers", "n": 32,
               "why": "Statements 1.44x on 88 posts, questions 0.98x on 32."}
ALGO_WRITER = {"text": "Write the kind of line a reader wants to answer in a few words.", "basis": "algorithm", "n": 0,
               "why": "ReplyWeight is 5.0 against 0.708 for a like."}


def proposal(editor=(GOOD_EDITOR,), writer=(GOOD_WRITER, ALGO_WRITER), test="Try a two-line post with one number.",
             **over) -> dict:
    out = {"findings": [{"what": "Les threads font nettement mieux que les posts d'actu.", "n": 7,
                         "gap": "1.69x vs 0.99x"}],
           "editor": [dict(d) for d in editor], "writer": [dict(d) for d in writer], "test": test,
           "summary_fr": "Les threads et les actus chaudes marchent le mieux. Je mets donc ces histoires en premier."}
    out.update(over)
    return out


class FakeAnalytics:
    def __init__(self, sheet: str = "OUR NUMBERS — 120 posts…", posts: int = 120) -> None:
        self.sheet, self.posts = sheet, posts

    def figures_sheet(self):
        return self.sheet, self.posts


class FakeWriter:
    def __init__(self, answer=None, verdicts="all") -> None:
        self.answer = proposal() if answer is None else answer
        self.verdicts = verdicts
        self.reviews: list[tuple] = []
        self.checks: list[tuple] = []

    def strategy_review(self, figures, algo="", trends="", previous="", limits=""):
        self.reviews.append((figures, algo, trends, previous))
        self.limits_seen = limits
        return self.answer

    def strategy_check(self, figures, algo, trends, directives):
        self.checks.append((figures, algo, trends, list(directives)))
        if self.verdicts == "all":
            return [True] * len(directives)
        return self.verdicts


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text
        self.refreshed = 0

    def refresh(self, force: bool = False):
        self.refreshed += 1
        return True

    def block(self) -> str:
        return self.text

    def changes_fr(self) -> str:
        return "X a modifié son classement cette semaine : ReplyWeight : 5.0 → 7.5."


def make(settings, state, writer=None, analytics=None, algo=True, trends=True) -> Strategist:
    return Strategist(settings, state, analytics or FakeAnalytics(), writer or FakeWriter(),
                      trends=FakeBlock("TRENDING TODAY — GTA VI") if trends else None,
                      algo=FakeBlock("X'S RANKING ALGORITHM — ReplyWeight 5.0") if algo else None)


# ---------------------------------------------------------------------------
# Les garde-fous du code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Put breaking news that names something everybody knows first.",
    "Mention the price on its own line.",
    "End with a line people want to reply to or argue with.",
    "Write posts people want to quote to a friend.",
])
def test_a_plain_directive_passes(text):
    assert directive_problem({"text": text, "basis": "numbers", "n": 9, "why": "1.6x vs 1.0x"}) is None


@pytest.mark.parametrize("text, why", [
    ("Add a link to the source at the end.", "règle"), ("Use hashtags like #AI to reach more people.", "règle"),
    ("Reply under big accounts within minutes.", "règle"), ("Quote-post viral posts from other accounts.", "règle"),
    ("Post more posts per day when a trend is hot.", "règle"), ("Tag the brand's account in the first line.", "règle"),
    ("Pick more stories for developers; they score well.", "règle"),
    ("Cover the crypto market when Bitcoin trends.", "sujet interdit"),
    ("Tell stories about the war in plain words.", "sujet interdit"),
    ("See https://example.com/tips for hooks.", "lien"), ("Short.", "vide"), ("x" * 230, "trop longue"),
    ("Today, pick the GTA VI story first if an article covers it.", "actu du jour"),
    ("Lead with whatever is trending right now on Google.", "actu du jour"),
])
def test_a_directive_that_touches_the_rules_is_thrown_away(text, why):
    assert why in directive_problem({"text": text, "basis": "numbers", "n": 9, "why": "1.6x vs 1.0x"})


def test_a_directive_needs_a_basis_and_enough_posts():
    assert "moins de 5 posts" in directive_problem(dict(GOOD_EDITOR, n=4))
    assert "moins de 5 posts" in directive_problem(dict(GOOD_EDITOR, n=True))
    assert directive_problem(dict(GOOD_EDITOR, basis="vibes")) == "sans fondement déclaré"
    assert directive_problem(dict(GOOD_EDITOR, why=" ")) == "sans justification"
    assert directive_problem(ALGO_WRITER) is None                    # l'algorithme : pas de nombre de posts
    assert directive_problem("pas un objet") == "illisible"


# ---------------------------------------------------------------------------
# Une analyse
# ---------------------------------------------------------------------------
def test_a_review_turns_figures_trends_and_algorithm_into_directives(settings, state, clock):
    clock(9, 0)
    writer = FakeWriter()
    st = make(settings, state, writer)
    memo = st.review()
    assert memo is not None and state.data["strategy"] is memo
    assert [d["text"] for d in memo["editor"]] == [GOOD_EDITOR["text"]]
    assert [d["text"] for d in memo["writer"]] == [GOOD_WRITER["text"], ALGO_WRITER["text"]]
    assert memo["posts"] == 120 and memo["dropped"] == 0 and memo["test"] == "Try a two-line post with one number."
    figures, algo, trends, previous = writer.reviews[0]
    assert figures.startswith("OUR NUMBERS") and algo.startswith("X'S RANKING ALGORITHM") and trends.startswith("TRENDING")
    assert previous == ""                                           # première analyse
    assert st.algo.refreshed == 1                                   # le code de X est relu avant chaque analyse
    shown = writer.checks[0][3]
    assert shown[0].startswith("(for choosing stories; basis: numbers, 12 posts) Put breaking news")
    assert shown[2].startswith("(for writing; basis: algorithm) Write the kind of line") and "ReplyWeight is 5.0" in shown[2]


def test_the_reviewer_has_the_last_word(settings, state, clock):
    writer = FakeWriter(verdicts=[True, False, True])
    st = make(settings, state, writer)
    memo = st.review()
    assert [d["text"] for d in memo["editor"]] == [GOOD_EDITOR["text"]]
    assert [d["text"] for d in memo["writer"]] == [ALGO_WRITER["text"]] and memo["dropped"] == 1


def test_what_the_code_refuses_never_reaches_the_reviewer(settings, state, clock):
    bad = {"text": "Add a link to the source at the end.", "basis": "numbers", "n": 30, "why": "links 2x"}
    thin = dict(GOOD_EDITOR, text="Prefer robots over everything else.", n=2)
    writer = FakeWriter(proposal(editor=(bad, thin, GOOD_EDITOR), writer=()))
    memo = make(settings, state, writer).review()
    assert len(writer.checks[0][3]) == 1 and "Put breaking news" in writer.checks[0][3][0]
    assert [d["text"] for d in memo["editor"]] == [GOOD_EDITOR["text"]] and memo["writer"] == [] and memo["dropped"] == 2


def test_no_more_than_five_directives_a_side(settings, state, clock):
    many = tuple(dict(GOOD_EDITOR, text=f"Put story kind number {i} first, when it is strong.") for i in range(9))
    memo = make(settings, state, FakeWriter(proposal(editor=many, writer=()))).review()
    assert len(memo["editor"]) == smod.MAX_DIRECTIVES


def test_a_test_idea_that_breaks_a_rule_is_dropped(settings, state, clock):
    memo = make(settings, state, FakeWriter(proposal(test="Try adding a link to the official video."))).review()
    assert memo["test"] == ""


def test_nothing_clear_in_the_figures_is_a_valid_answer(settings, state, clock):
    writer = FakeWriter(proposal(editor=(), writer=(), test="", findings=[]))
    st = make(settings, state, writer)
    memo = st.review()
    assert memo["editor"] == [] and memo["writer"] == [] and writer.checks == []     # rien à relire
    assert st.editor_block() == "" and st.writer_block() == "" and st.test_block() == ""
    assert "Rien d'assez net dans les chiffres" in st.report_fr(memo)


@pytest.mark.parametrize("case", ["few_posts", "analyst_silent", "reviewer_silent", "crash"])
def test_when_the_review_cannot_be_done_the_earlier_directives_stay(settings, state, clock, case):
    clock(9, 0, day=10)
    st = make(settings, state)
    first = st.review()
    assert first is not None
    clock(9, 0, day=14)
    if case == "few_posts":
        st.analytics.posts = smod.MIN_POSTS - 1
    elif case == "analyst_silent":
        st.writer.answer = None
    elif case == "reviewer_silent":
        st.writer.verdicts = None
    else:
        st.analytics.figures_sheet = lambda: 1 / 0
    assert st.review() is None
    assert state.data["strategy"] is first and st.editor_block() != ""
    assert st.due() is False                                # pas de nouvel essai à chaque tour


def test_every_three_days_and_never_at_night(settings, state, clock):
    clock(6, 59, day=10)
    st = make(settings, state)
    assert st.due() is False                                # avant 7 h : pas la nuit
    clock(7, 0, day=10)
    assert st.due() is True and st.review() is not None
    clock(9, 0, day=12)
    assert st.due() is False and st.review() is None        # deux jours après : pas encore
    clock(23, 30, day=13)
    assert st.due() is False                                # trois jours, mais il est 23 h 30
    clock(8, 0, day=14)
    assert st.due() is True
    assert st.review(force=True) is not None


def test_the_second_review_sees_the_earlier_directives(settings, state, clock):
    clock(9, 0, day=10)
    writer = FakeWriter()
    st = make(settings, state, writer)
    st.review()
    clock(9, 0, day=14)
    st.review()
    previous = writer.reviews[1][3]
    assert previous.startswith("YOUR EARLIER DIRECTIVES (decided on October 10;")
    assert "- (choosing stories) Put breaking news" in previous and "- (writing) End most posts" in previous
    assert state.data["strategy_history"][0]["editor"] == [GOOD_EDITOR["text"]]


def test_what_the_editor_and_the_writer_read(settings, state, clock):
    clock(9, 0, day=10)
    st = make(settings, state)
    assert st.editor_block() == "" and st.writer_block() == "" and st.test_block() == "" and st.memo() is None
    st.review()
    ed, wr = st.editor_block(), st.writer_block()
    assert ed.startswith("STRATEGY FOR CHOOSING STORIES (decided on October 10 from our own numbers, X's ranking code "
                         "and the trends). They refine your choices; they never override the rules above:")
    assert ed.endswith("- Put breaking news that names something everybody knows first.")
    assert wr.startswith("STRATEGY FOR WRITING (decided on October 10") and wr.count("\n- ") == 2
    assert st.test_block() == ("WHAT TO TRY IN THIS TEST POST (the analyst's idea, still within every rule above): "
                               "Try a two-line post with one number.")


def test_old_directives_stop_being_followed(settings, state, clock):
    clock(9, 0, day=10)
    st = make(settings, state)
    st.review()
    clock(9, 0, day=20)
    assert st.editor_block() != ""
    clock(9, 1, day=20)
    assert st.memo() is None and st.editor_block() == "" and st.writer_block() == ""
    assert state.data["strategy"]["editor"]                # gardées pour l'analyse suivante, plus suivies


def test_the_owner_gets_a_plain_summary(settings, state, clock):
    st = make(settings, state)
    text = st.report_fr(st.review())
    assert text.startswith("🧠 <b>Analyse automatique</b> (120 posts mesurés, tendances et algorithme de X)")
    assert "Les threads et les actus chaudes marchent le mieux." in text
    assert ("📊 Ce que montrent les chiffres (pas encore des décisions) :\n"
            "• Les threads font nettement mieux que les posts d'actu. (1.69x vs 0.99x)") in text
    assert "✅ 3 décision(s) appliquée(s) dès le prochain post :" in text
    assert ("→ Mettre en premier les actus chaudes qui nomment quelque chose que tout le monde connaît. "
            "<i>(nos chiffres)</i>") in text
    assert "→ End most posts with a statement, not a question. <i>(nos chiffres)</i>" in text      # sans version française
    assert "<i>(l'algorithme de X)</i>" in text
    assert "⚙️ X a modifié son classement cette semaine : ReplyWeight : 5.0 → 7.5." in text
    assert text.endswith("Prochaine analyse dans 3 jours. Rien à faire de ton côté.")
    assert st.report_fr() == text and make(settings, type(state)(state.path.with_name("vide.json"))).report_fr() == ""


def test_it_works_without_trends_or_algorithm(settings, state, clock):
    writer = FakeWriter()
    memo = make(settings, state, writer, algo=False, trends=False).review()
    assert memo is not None and writer.reviews[0][1:3] == ("", "")


def test_the_summary_says_when_the_reviewer_refused_some(settings, state, clock):
    st = make(settings, state, FakeWriter(verdicts=[True, False, True]))
    text = st.report_fr(st.review())
    assert "✅ 2 décision(s) appliquée(s) dès le prochain post (1 autre(s) refusée(s) à la relecture, pas assez solides) :" in text


# ---------------------------------------------------------------------------
# Le nombre de posts par jour (accord du propriétaire, 24/11/2026 : « il peut régler même les
# limites de post si nécessaire […] juste pour l'argent fais attention »)
# ---------------------------------------------------------------------------
def limits_for(settings, state, usd_per_day: float = 3.0):
    from datetime import timedelta

    from bot import utils
    from bot.config import parse_post_slots
    from bot.limits import PostLimits
    settings.post_slots = parse_post_slots("tip@14:30,news@19:00,news@23:00")
    settings.reactive_max_per_day = 3
    settings.claude_budget_monthly_usd = 195.0
    today = utils.now_local()
    state.data["llm_usage"] = {"days": {(today - timedelta(days=i)).strftime("%Y-%m-%d"): {"usd": usd_per_day}
                                        for i in range(1, 8)}}
    return PostLimits(settings, state)


def with_limits(settings, state, writer, **kw) -> Strategist:
    st = make(settings, state, writer)
    st.limits = limits_for(settings, state, **kw)
    return st


MORE_BREAKING = {"scheduled_per_day": 3, "reactive_per_day": 4, "why": "Breaking posts 1.38x on 49 posts vs 0.99x.",
                 "fr": "Les posts à chaud marchent mieux : un de plus par jour."}


def test_the_analyst_is_told_the_limits_and_the_money(settings, state, clock):
    clock(9, 0)
    writer = FakeWriter(proposal(limits={"scheduled_per_day": 3, "reactive_per_day": 3, "why": "", "fr": ""}))
    st = with_limits(settings, state, writer)
    memo = st.review()
    assert writer.limits_seen.startswith("POSTING LIMITS — the owner lets you change them")
    assert "careful with money" in writer.limits_seen and "for a cap of $195" in writer.limits_seen
    assert "POSTING LIMITS" in writer.checks[0][0]              # le relecteur voit les mêmes chiffres d'argent
    assert memo["limits"] is None and memo["limits_note"] == "" and "post_limits" not in state.data
    assert "Nombre de posts" not in st.report_fr(memo)          # rien ne change : rien à dire


def test_a_justified_change_is_reviewed_then_applied_tomorrow(settings, state, clock):
    clock(9, 0)
    writer = FakeWriter(proposal(limits=MORE_BREAKING))
    st = with_limits(settings, state, writer)
    memo = st.review()
    shown = writer.checks[0][3]
    assert shown[-1] == ("(posting limits) Scheduled posts per day: 3 → 3; breaking-news posts per day at most: 3 → 4 "
                         "— Breaking posts 1.38x on 49 posts vs 0.99x.")
    assert memo["limits"]["reactive"] == 4 and memo["limits"]["from"] == "2026-10-16"
    assert st.limits.reactive() == 3 and st.limits.target() == (3, 4)
    text = st.report_fr(memo)
    assert ("📈 Nombre de posts, à partir de demain : 3 prévus par jour (avant : 3), 4 « à chaud » au plus (avant : 3). "
            "Les posts à chaud marchent mieux : un de plus par jour.") in text


def test_the_reviewer_can_refuse_the_change(settings, state, clock):
    writer = FakeWriter(proposal(limits=MORE_BREAKING), verdicts=[True, True, True, False])
    st = with_limits(settings, state, writer)
    memo = st.review()
    assert memo["limits"] is None and st.limits.target() == (3, 3) and memo["dropped"] == 1
    assert len(memo["editor"]) == 1 and len(memo["writer"]) == 2          # les autres consignes restent
    assert "📈 Le relecteur a refusé de changer le nombre de posts. Le nombre de posts ne change pas." in st.report_fr(memo)


def test_money_first_a_raise_the_budget_cannot_carry_never_reaches_the_reviewer(settings, state, clock):
    writer = FakeWriter(proposal(limits=MORE_BREAKING))
    st = with_limits(settings, state, writer, usd_per_day=5.0)            # 150 $ par mois : plus de marge
    memo = st.review()
    assert all("posting limits" not in d for d in writer.checks[0][3])
    assert memo["limits"] is None and st.limits.target() == (3, 3)
    assert memo["limits_note"].startswith("changement du nombre de posts refusé : le budget ne le permet pas")
    assert "💰 Changement du nombre de posts refusé : le budget ne le permet pas" in st.report_fr(memo)


@pytest.mark.parametrize("limits, note", [
    ({"scheduled_per_day": 9, "reactive_per_day": 3, "why": "more is more", "fr": "plus"}, "hors bornes"),
    ({"scheduled_per_day": 3, "reactive_per_day": 5, "why": "two steps", "fr": "deux crans"}, "plus d'un cran"),
    ({"scheduled_per_day": 4, "reactive_per_day": 3, "why": " ", "fr": "sans raison"}, "sans justification"),
])
def test_the_code_refuses_what_is_out_of_bounds(settings, state, clock, limits, note):
    writer = FakeWriter(proposal(limits=limits))
    st = with_limits(settings, state, writer)
    memo = st.review()
    assert memo["limits"] is None and note in memo["limits_note"] and st.limits.target() == (3, 3)
    assert all("posting limits" not in d for d in writer.checks[0][3])


def test_fewer_posts_is_a_valid_decision(settings, state, clock):
    fewer = {"scheduled_per_day": 2, "reactive_per_day": 3, "why": "Late-night news posts 0.98x on 15 posts.",
             "fr": "Le post de 23 h marche moins bien : on l'enlève."}
    st = with_limits(settings, state, FakeWriter(proposal(limits=fewer)), usd_per_day=5.0)
    memo = st.review()
    assert memo["limits"]["scheduled"] == 2 and st.limits.target() == (2, 3)     # une baisse ne coûte rien


def test_a_change_alone_is_still_reviewed(settings, state, clock):
    writer = FakeWriter(proposal(editor=(), writer=(), limits=MORE_BREAKING))
    st = with_limits(settings, state, writer)
    memo = st.review()
    assert len(writer.checks[0][3]) == 1 and memo["limits"]["reactive"] == 4


def test_without_the_limits_module_the_field_is_ignored(settings, state, clock):
    writer = FakeWriter(proposal(limits=MORE_BREAKING))
    memo = make(settings, state, writer).review()
    assert memo["limits"] is None and memo["limits_note"] == "" and writer.limits_seen == ""
