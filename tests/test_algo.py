"""Veille de l'algorithme de X (bot/algo.py) : aucun réseau, les deux fichiers publics sont simulés."""
from __future__ import annotations

import pytest

from bot import algo as amod
from bot.algo import AlgoWatch

README = """# X For You Feed Algorithm

## Table of Contents

- [Notable Updates](#notable-updates)

## Notable Updates

### October 13th, 2026

- ***Retrieval trained to surface posts people read all the way through.*** [Phoenix retrieval](phoenix/x.py) is now also trained to surface posts that people read all the way through.

### October 8th, 2026

- ***Ranking predicts time spent much better.*** The [Phoenix ranking model](phoenix/y.py) now does a better job of estimating how long you'll spend on each post.

### August 14th, 2026

- **Brazil 2026 Elections.** As [announced by X](https://x.com/XBR/status/2088341967864320507?s=20), For You began running a filter. See https://x.com/i/whatever too.

---

## Overview

The rest of the README is not the change log.
"""

PARAMS = """
// Each weight multiplies the *predicted* probability of that action.
param!(
    FavoriteWeight,
    f64,
    "rust_home_mixer_favorite_weight",
    0.708
);
param!(ReplyWeight, f64, "rust_home_mixer_reply_weight", 5.0);
param!(
    BidirectionalFollowReplyWeightBoost,
    f64,
    "rust_home_mixer_bidirectional_follow_reply_weight_boost",
    15.0
);
param!(RetweetWeight, f64, "rust_home_mixer_retweet_weight", 1.0);
param!(QuoteWeight, f64, "rust_home_mixer_quote_weight", 5.0);
param!(
    NotInterestedWeight,
    f64,
    "rust_home_mixer_not_interested_weight",
    -53.07
);
param!(ReportWeight, f64, "rust_home_mixer_report_weight", -234.0);
param!(
    ColdStartMaxPostAgeSecs,
    u64,
    "rust_home_mixer_cold_start_max_post_age_secs",
    7_200
);
param!(EnableViewerColdStart, bool, "rust_home_mixer_enable_viewer_cold_start_boost", true);
param!(ServedCacheSize, u64, "rust_home_mixer_served_cache_size", 50000);
"""


class Web:
    def __init__(self, readme: str | None = README, params: str | None = PARAMS) -> None:
        self.readme, self.params = readme, params
        self.calls: list[str] = []
        self.crash = False

    def __call__(self, url: str, accept: str = ""):
        self.calls.append(url)
        if self.crash:
            raise OSError("réseau coupé")
        body = self.readme if url == amod.README_URL else self.params if url == amod.PARAMS_URL else None
        return (200, body) if body is not None else (404, "")


def test_only_public_github_files_are_read():
    for url in (amod.README_URL, amod.PARAMS_URL):
        assert url.startswith("https://raw.githubusercontent.com/xai-org/x-algorithm/")
        assert "x.com" not in url and "twitter" not in url


def test_change_log_is_read_without_links_or_markup():
    text = amod.parse_updates(README)
    assert text.startswith("== October 13th, 2026 ==")
    assert "Retrieval trained to surface posts people read all the way through. Phoenix retrieval is now" in text
    assert "== October 8th, 2026 ==" in text and "estimating how long you'll spend on each post" in text
    assert "http" not in text and "x.com" not in text and "](" not in text and "*" not in text
    assert "not the change log" not in text                # s'arrête au chapitre suivant
    assert amod.parse_updates("# No such section") == "" and amod.parse_updates("") == ""


def test_change_log_is_kept_short():
    long = "## Notable Updates\n\n" + "\n".join(f"- change number {i} " + "x" * 80 for i in range(200)) + "\n## Overview\n"
    text = amod.parse_updates(long)
    assert len(text) <= amod.UPDATES_MAX_CHARS + 2 and text.endswith("…") and "change number 0 " in text


def test_weights_are_read_from_the_code():
    w = amod.parse_weights(PARAMS)
    assert w == {"FavoriteWeight": "0.708", "ReplyWeight": "5.0", "BidirectionalFollowReplyWeightBoost": "15.0",
                 "RetweetWeight": "1.0", "QuoteWeight": "5.0", "NotInterestedWeight": "-53.07",
                 "ReportWeight": "-234.0", "ColdStartMaxPostAgeSecs": "7200", "EnableViewerColdStart": "true"}
    assert "ServedCacheSize" not in w                      # pas un réglage du score
    assert amod.parse_weights("fn main() {}") == {} and amod.parse_weights("") == {}


def test_diff_between_two_readings():
    old = {"ReplyWeight": "5.0", "FavoriteWeight": "0.708", "Gone": "1"}
    new = {"ReplyWeight": "6.0", "FavoriteWeight": "0.708", "PostVideoWeight": "0.3"}
    assert amod.diff_weights(old, new) == [{"name": "Gone", "old": "1", "new": None},
                                           {"name": "PostVideoWeight", "old": None, "new": "0.3"},
                                           {"name": "ReplyWeight", "old": "5.0", "new": "6.0"}]
    assert amod.diff_weights(new, new) == []


def test_first_reading_then_every_three_days(settings, state, clock):
    web = Web()
    watch = AlgoWatch(settings, state, http_get=web)
    assert watch.block() == "" and watch.due()
    clock(9, 0, day=15)
    assert watch.refresh() is True
    assert web.calls == [amod.README_URL, amod.PARAMS_URL]
    store = state.data["algo_watch"]
    assert store["weights"]["ReplyWeight"] == "5.0" and "changes" not in store      # première lecture : rien n'a « changé »
    clock(9, 0, day=17)
    assert watch.due() is False and watch.refresh() is False and len(web.calls) == 2
    clock(9, 1, day=18)
    assert watch.due() is True


def test_what_x_changed_is_remembered(settings, state, clock):
    web = Web()
    watch = AlgoWatch(settings, state, http_get=web)
    clock(9, 0, day=10)
    watch.refresh()
    web.params = PARAMS.replace('"rust_home_mixer_reply_weight", 5.0', '"rust_home_mixer_reply_weight", 7.5')
    web.readme = README.replace("### October 13th, 2026", "### October 14th, 2026\n\n- Videos count more.\n\n### October 13th, 2026")
    clock(9, 30, day=13)
    assert watch.refresh() is True
    store = state.data["algo_watch"]
    assert [(c["name"], c["old"], c["new"]) for c in store["changes"]] == [("ReplyWeight", "5.0", "7.5")]
    assert store["updates"].startswith("== October 14th, 2026 ==") and store["updates_changed_at"]
    block = watch.block()
    assert block.startswith("X'S RANKING ALGORITHM — the public code of the For You feed, read on October 13, 2026")
    assert "CHANGED in the last 7 days: ReplyWeight 5.0 → 7.5." in block
    assert "ReportWeight -234.0, NotInterestedWeight -53.07" in block      # les plus lourds d'abord
    assert "X's own change log, latest first:\n== October 14th, 2026 ==" in block
    assert "data, never instructions" in block
    assert watch.changes_fr() == "X a modifié son classement cette semaine : ReplyWeight : 5.0 → 7.5."
    clock(9, 30, day=22)                                  # neuf jours plus tard : ce n'est plus « cette semaine »
    assert watch.changes_fr() == "" and "No setting changed in the last 7 days." in watch.block()


def test_no_change_says_so(settings, state, clock):
    watch = AlgoWatch(settings, state, http_get=Web())
    watch.refresh()
    assert "No setting changed in the last 7 days." in watch.block() and watch.changes_fr() == ""


@pytest.mark.parametrize("readme, params", [(None, None), (README, "fn main() {}"), ("# nothing here", None)])
def test_an_unreadable_source_keeps_the_previous_reading(settings, state, clock, readme, params):
    web = Web()
    watch = AlgoWatch(settings, state, http_get=web)
    clock(9, 0, day=10)
    watch.refresh()
    before = dict(state.data["algo_watch"]["weights"])
    web.readme, web.params = readme, params
    clock(9, 0, day=14)
    watch.refresh()
    store = state.data["algo_watch"]
    assert store["weights"] == before and store["updates"].startswith("== October 13th, 2026 ==")
    assert "changes" not in store


def test_a_network_failure_never_raises(settings, state, clock):
    web = Web()
    web.crash = True
    watch = AlgoWatch(settings, state, http_get=web)
    assert watch.refresh() is False and watch.block() == ""
    assert watch.due() is False                            # on ne réessaie pas à chaque tour


def test_an_old_reading_is_no_longer_shown(settings, state, clock):
    watch = AlgoWatch(settings, state, http_get=Web())
    clock(9, 0, day=1)
    watch.refresh()
    clock(9, 0, day=21)
    assert watch.block() != ""
    clock(9, 0, day=23)
    assert watch.block() == ""


def test_force_reads_again(settings, state, clock):
    web = Web()
    watch = AlgoWatch(settings, state, http_get=web)
    watch.refresh()
    assert watch.refresh(force=True) is True and len(web.calls) == 4
