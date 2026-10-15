"""Tests de CommentDesk (bot/comments.py) : aucun appel réseau."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot.comments import CommentDesk, strip_leading_mentions
from bot.x_api import XError, tweet_url
from tests.fakes import FakeX, make_tweet

ME = "42"


# ---------------------------------------------------------------------------
# Doublures locales
# ---------------------------------------------------------------------------
class StubWriter:
    def __init__(self, reply="Thanks! The trick is to paste the error message too."):
        self.reply = reply
        self.calls: list[tuple] = []

    def draft_comment_reply(self, my_post_text, commenter, comment_text):
        self.calls.append((my_post_text, commenter, comment_text))
        if isinstance(self.reply, Exception):
            raise self.reply
        if callable(self.reply):
            return self.reply(my_post_text, commenter, comment_text)
        return self.reply


class StubApprovals:
    def __init__(self, fail: bool = False):
        self.comment_items: list[dict] = []
        self.fail = fail

    def create_comment_item(self, **kw):
        if self.fail:
            raise RuntimeError("telegram down")
        self.comment_items.append(kw)
        return {"id": f"c{len(self.comment_items)}", **kw}


@pytest.fixture(autouse=True)
def _frozen(clock):
    return clock


@pytest.fixture
def setup(settings, state, fake_x):
    state.add_published("500", "tip", "Paste the full error message into Claude, not a summary.")
    state.add_published("600", "news", "Voice mode is free for everyone now.")
    writer, approvals = StubWriter(), StubApprovals()
    desk = CommentDesk(settings, state, fake_x, writer, approvals)
    return desk, writer, approvals


def reply_to_me(tid: str, text: str, author: str = "7", post: str = "500", conv: str | None = None) -> dict:
    return make_tweet(tid, text, author_id=author, in_reply_to_user_id=ME,
                      conversation_id=conv or post, referenced_reply_to=post)


def users(*pairs: tuple[str, str]) -> dict:
    return {uid: {"id": uid, "username": name} for uid, name in pairs}


# ---------------------------------------------------------------------------
def test_strip_leading_mentions():
    assert strip_leading_mentions("@sabislabi @bob merci beaucoup") == "merci beaucoup"
    assert strip_leading_mentions("  @sabislabi ok") == "ok"
    assert strip_leading_mentions("great point @bob") == "great point @bob"
    assert strip_leading_mentions("@sabislabi") == ""


def test_reply_under_my_post_creates_draft(setup, state, fake_x, clock):
    desk, writer, approvals = setup
    now = clock(10, 0)
    fake_x.mention_batches.append((
        [reply_to_me("901", "@sabislabi Does this also work with stack traces?")],
        users(("7", "alice")), "901"))
    assert desk.check() == 1
    assert writer.calls == [("Paste the full error message into Claude, not a summary.", "alice",
                             "Does this also work with stack traces?")]
    kw = approvals.comment_items[0]
    assert kw["text"] == "Thanks! The trick is to paste the error message too."
    assert kw["comment_id"] == "901"
    assert kw["comment_text"] == "Does this also work with stack traces?"
    assert kw["commenter"] == "alice"
    assert kw["my_post_text"].startswith("Paste the full error")
    assert kw["post_url"] == tweet_url("alice", "901")
    assert kw["expires_at"] == now + timedelta(hours=24)
    assert state.count("comment_drafts") == 1
    assert state.is_handled("901")
    assert state.data["mentions_since_id"] == "901"
    assert state.recent_texts("comment_reply") == [kw["text"]]
    # Rien n'est publié ici : c'est le Publisher qui répond après validation.
    assert fake_x.posts == []


def test_since_id_is_passed_and_persisted(setup, state, fake_x):
    desk, _, _ = setup
    state.data["mentions_since_id"] = "100"
    fake_x.mention_batches.append(([], {}, "150"))
    desk.check()
    assert ("mentions", "100") in fake_x.calls
    assert state.data["mentions_since_id"] == "150"


def test_conversation_match_uses_root_post_text(setup, state, fake_x):
    """Réponse au 3e tweet d'un thread : seul le tweet d'accroche est connu."""
    desk, writer, approvals = setup
    state.add_published("700", "thread", "Hook tweet of my thread about prompts.")
    tw = make_tweet("902", "@sabislabi step 3 saved me hours", author_id="8",
                    in_reply_to_user_id=ME, conversation_id="700", referenced_reply_to="703")
    fake_x.mention_batches.append(([tw], users(("8", "bob")), "902"))
    assert desk.check() == 1
    assert approvals.comment_items[0]["my_post_text"] == "Hook tweet of my thread about prompts."


@pytest.mark.parametrize("tweet,user,why", [
    # mention simple, pas une réponse
    (make_tweet("910", "hey @sabislabi look at this", author_id="7"), "alice", "pas une réponse"),
    # réponse à quelqu'un d'autre dans ma conversation
    (make_tweet("911", "@carol I disagree with you", author_id="7", in_reply_to_user_id="99",
                conversation_id="500", referenced_reply_to="555"), "alice", "réponse à un autre"),
    # réponse à moi, mais sous un post que le bot n'a pas publié
    (reply_to_me("912", "@sabislabi nice one", post="12345"), "alice", "pas mon post"),
    # moi-même
    (reply_to_me("913", "@sabislabi adding a detail here", author=ME), "sabislabi", "moi"),
    # bruit
    (reply_to_me("914", "@sabislabi giveaway! follow me and tag a friend"), "alice", "bruit"),
    # sujet bloqué
    (reply_to_me("915", "@sabislabi what about the election though"), "alice", "politique"),
    # hors ligne éditoriale
    (reply_to_me("916", "@sabislabi use it for crypto trading"), "alice", "crypto"),
    # vide après les @
    (reply_to_me("917", "@sabislabi @bob"), "alice", "vide"),
    (reply_to_me("918", "@sabislabi k"), "alice", "1 caractère"),
])
def test_rejected_mentions_never_reach_writer(setup, state, fake_x, tweet, user, why):
    desk, writer, approvals = setup
    fake_x.mention_batches.append(([tweet], users((tweet["author_id"], user)), tweet["id"]))
    assert desk.check() == 0, why
    assert writer.calls == []
    assert approvals.comment_items == []
    assert state.is_handled(tweet["id"]), why


def test_blacklisted_user_and_keyword(settings, state, fake_x):
    settings.blacklist_users = ["spammer"]
    settings.blacklist_keywords = ["dm me"]
    state.add_published("500", "tip", "tip text")
    writer, approvals = StubWriter(), StubApprovals()
    desk = CommentDesk(settings, state, fake_x, writer, approvals)
    fake_x.mention_batches.append(([
        reply_to_me("920", "@sabislabi great thread, thanks", author="7"),
        reply_to_me("921", "@sabislabi nice, DM me for a collab", author="8"),
    ], users(("7", "Spammer"), ("8", "dave")), "921"))
    assert desk.check() == 0
    assert writer.calls == []
    assert state.is_handled("920") and state.is_handled("921")


def test_already_handled_is_skipped(setup, state, fake_x):
    desk, writer, _ = setup
    state.mark_handled("930")
    fake_x.mention_batches.append(([reply_to_me("930", "@sabislabi so useful")], users(("7", "alice")), "930"))
    assert desk.check() == 0
    assert writer.calls == []


def test_one_draft_per_author_and_post_per_day(setup, state, fake_x, clock):
    desk, writer, approvals = setup
    fake_x.mention_batches.append(([
        reply_to_me("940", "@sabislabi first question about this", author="7", post="500"),
        reply_to_me("941", "@sabislabi and a second one", author="7", post="500"),
        reply_to_me("942", "@sabislabi on the other post now", author="7", post="600"),
        reply_to_me("943", "@sabislabi another person here", author="8", post="500"),
    ], users(("7", "alice"), ("8", "bob")), "943"))
    assert desk.check() == 3
    assert [k["comment_id"] for k in approvals.comment_items] == ["940", "942", "943"]
    assert state.is_handled("941")
    # Le lendemain, la même personne sous le même post : de nouveau permis.
    clock(10, 0, day=16)
    fake_x.mention_batches.append(([reply_to_me("944", "@sabislabi back again with a question")],
                                   users(("7", "alice")), "944"))
    assert desk.check() == 1


def test_writer_none_marks_handled_without_item(setup, state, fake_x):
    desk, writer, approvals = setup
    writer.reply = None
    fake_x.mention_batches.append(([reply_to_me("950", "@sabislabi cool")], users(("7", "alice")), "950"))
    assert desk.check() == 0
    assert len(writer.calls) == 1
    assert approvals.comment_items == []
    assert state.is_handled("950")
    assert state.count("comment_drafts") == 0


def test_writer_or_approvals_failure_does_not_crash(settings, state, fake_x):
    state.add_published("500", "tip", "tip text")
    writer, approvals = StubWriter(reply=RuntimeError("boom")), StubApprovals()
    desk = CommentDesk(settings, state, fake_x, writer, approvals)
    fake_x.mention_batches.append(([reply_to_me("960", "@sabislabi question here"),
                                    reply_to_me("961", "@sabislabi another one", author="8")],
                                   users(("7", "alice"), ("8", "bob")), "961"))
    assert desk.check() == 0
    assert state.is_handled("960") and state.is_handled("961")

    state.data["last_runs"].pop("comments")
    desk2 = CommentDesk(settings, state, fake_x, StubWriter(), StubApprovals(fail=True))
    fake_x.mention_batches.append(([reply_to_me("962", "@sabislabi third one", author="9")],
                                   users(("9", "carl")), "962"))
    assert desk2.check() == 0
    assert state.count("comment_drafts") == 0


class _ErrX(FakeX):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind

    def mentions(self, since_id=None, max_results=50):
        self.calls.append(("mentions", since_id))
        raise XError(self.kind, "nope")


@pytest.mark.parametrize("kind", ["rate_limited", "server", "auth"])
def test_x_errors_return_zero(settings, state, kind):
    state.add_published("500", "tip", "tip text")
    state.data["mentions_since_id"] = "77"
    x = _ErrX(kind)
    desk = CommentDesk(settings, state, x, StubWriter(), StubApprovals())
    assert desk.check() == 0
    assert state.data["mentions_since_id"] == "77"


# ---------------------------------------------------------------------------
# Déclenchement
# ---------------------------------------------------------------------------
def _one_mention(fake_x, tid="970", author="7"):
    fake_x.mention_batches.append(([reply_to_me(tid, "@sabislabi good stuff, thanks", author=author)],
                                   users((author, f"user{author}")), tid))


def test_disabled_paused_and_window(setup, settings, state, fake_x, clock):
    desk, _, _ = setup
    settings.comments_enabled = False
    _one_mention(fake_x)
    assert desk.check() == 0 and not any(c[0] == "mentions" for c in fake_x.calls)
    settings.comments_enabled = True
    state.data["paused"] = True
    assert desk.check() == 0 and not any(c[0] == "mentions" for c in fake_x.calls)
    state.data["paused"] = False
    clock(7, 30)   # hors fenêtre 08:00-23:30
    assert desk.check() == 0 and not any(c[0] == "mentions" for c in fake_x.calls)
    clock(8, 5)
    assert desk.check() == 1


def test_interval_between_checks(setup, state, fake_x, clock):
    desk, _, _ = setup
    clock(10, 0)
    _one_mention(fake_x, "971")
    assert desk.check() == 1
    _one_mention(fake_x, "972", author="8")
    clock(10, 5)
    assert desk.check() == 0          # 5 min < 10 min
    clock(10, 11)
    assert desk.check() == 1
    assert state.last_run("comments") is not None


def test_daily_cap_skips_reading(setup, settings, state, fake_x):
    desk, _, _ = setup
    settings.comments_max_drafts_per_day = 2
    state.incr("comment_drafts", 2)
    _one_mention(fake_x)
    assert desk.check() == 0
    assert not any(c[0] == "mentions" for c in fake_x.calls)


def test_cap_reached_mid_batch_holds_since_id(setup, settings, state, fake_x, clock):
    desk, writer, approvals = setup
    settings.comments_max_drafts_per_day = 1
    state.data["mentions_since_id"] = "100"
    fake_x.mention_batches.append(([
        # l'API renvoie du plus récent au plus ancien
        reply_to_me("982", "@sabislabi second comment here", author="8"),
        reply_to_me("981", "@sabislabi first comment here", author="7"),
    ], users(("7", "alice"), ("8", "bob")), "982"))
    assert desk.check() == 1
    assert approvals.comment_items[0]["comment_id"] == "981"       # le plus ancien d'abord
    assert not state.is_handled("982")
    assert state.data["mentions_since_id"] == "981"                 # 982 sera relu demain
    clock(10, 0, day=16)
    fake_x.mention_batches.append(([reply_to_me("982", "@sabislabi second comment here", author="8")],
                                   users(("8", "bob")), "982"))
    assert desk.check() == 1
    assert state.data["mentions_since_id"] == "982"


def test_no_published_posts_means_no_read(settings, state, fake_x):
    desk = CommentDesk(settings, state, fake_x, StubWriter(), StubApprovals())
    _one_mention(fake_x)
    assert desk.check() == 0
    assert not any(c[0] == "mentions" for c in fake_x.calls)


def test_writer_calls_per_check_are_bounded(setup, state, fake_x):
    from bot import comments as cmod
    desk, writer, _ = setup
    writer.reply = None
    n = cmod.MAX_WRITER_CALLS_PER_CHECK + 3
    tweets = [reply_to_me(str(2000 + i), f"@sabislabi comment number {i}", author=str(100 + i))
              for i in range(n)]
    fake_x.mention_batches.append((tweets, users(*[(str(100 + i), f"user{i}") for i in range(n)]),
                                   str(2000 + n - 1)))
    desk.check()
    assert len(writer.calls) == cmod.MAX_WRITER_CALLS_PER_CHECK
    assert state.data["mentions_since_id"] == str(2000 + cmod.MAX_WRITER_CALLS_PER_CHECK - 1)
