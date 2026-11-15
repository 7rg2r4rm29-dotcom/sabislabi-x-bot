"""Une journée complète simulée avec les VRAIS modules du bot.

Seuls Claude (FakeLLM), l'API X (FakeX), Telegram (FakeTelegram) et le web (un
flux d'actu et ses pages d'articles, servis par `FakeWeb`) sont simulés. On fait
tourner `Bot.tick()` toutes les 10 minutes du jeudi 06:00 au vendredi 09:00 et on
vérifie les règles qui protègent le compte :
  - rien ne part sur X sans un ✅ du propriétaire ;
  - le radar ne publie jamais (il propose un lien, c'est toi qui publies) ;
  - /pause bloque tout, /reprendre laisse 3 minutes pour annuler ;
  - un post non validé expire, sans jamais partir ;
  - chaque post raconte une actu lue (flux → choix de la rédaction en chef → article
    lu en entier → rédaction → relecture des faits).
"""
from __future__ import annotations

import random
import re
from datetime import timedelta, timezone

import pytest

from bot import sources, utils
from bot.scheduler import Bot
from tests.fakes import FakeLLM, FakeTelegram, FakeX, callback_data_for, make_tweet

ME = "42"
GM = "gm. one small automation today beats the big plan you never start."
# Créneau « tip » = « le futur arrive » ; créneau « news » = « l'histoire du jour ».
TIP = "A robot just folded a whole basket of laundry, alone, in a real home.\nNot in a factory. In a living room."
NEWS = "Doctors can now spot some illnesses years before you feel anything.\nWould you want to know that early?"
REPLY = "Yes: paste the full stack trace, not a summary. It changes everything."
RADAR = "Tried this yesterday: it works best when you paste the full error, not a summary."
RADAR_POST = "We just shipped a new way to build small apps directly inside the chat window."

# --- Le web simulé : un flux d'actu et les pages de ses deux articles -----------------
FEED_URL = "https://news.example.com/feed.xml"
ROBOT_URL = "https://news.example.com/2026/10/15/home-robot-laundry"
SCAN_URL = "https://news.example.com/2026/10/15/scanner-spots-illness"
ROBOT_TITLE = "A home robot now folds a whole basket of laundry on its own"
SCAN_TITLE = "Doctors test a scanner that spots illness years before symptoms"
ROBOT_FACT = "The robot needed about four minutes per shirt and never dropped a sock."
SCAN_FACT = "The scan takes less time than a coffee break and uses no needles at all."
# Chaque article renvoie vers la page OFFICIELLE de l'histoire (celle de l'entreprise) : c'est
# là, et jamais sur la page du média, que le bot prend la photo (ou la vidéo).
ROBOT_SOURCE = "https://robohome.example.org/news/laundry-robot"
SCAN_SOURCE = "https://medscan.example.org/press/early-scan"
ROBOT_PHOTO = "https://cdn.robohome.example.org/2026/10/home-robot.jpg"
SCAN_PHOTO = "https://cdn.medscan.example.org/2026/10/scanner.jpg"
ROBOT_VIDEO = "https://cdn.robohome.example.org/2026/10/home-robot.mp4"
NEWS_PHOTO = "https://news.example.com/img/robot-by-our-photographer.jpg"   # la photo du média : jamais prise
SOURCES = {ROBOT_TITLE: (ROBOT_SOURCE, ROBOT_PHOTO, "RoboHome"), SCAN_TITLE: (SCAN_SOURCE, SCAN_PHOTO, "MedScan Labs")}
FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Future Daily</title><link>https://news.example.com/</link>
<description>Tech and science news</description>
<item><title>{ROBOT_TITLE}</title><link>{ROBOT_URL}</link>
<description>The machine picked up shirts and socks and folded them without help, in a demo filmed in an
ordinary flat.</description><pubDate>Thu, 15 Oct 2026 03:00:00 +0000</pubDate></item>
<item><title>{SCAN_TITLE}</title><link>{SCAN_URL}</link>
<description>Teams say the quick scan found early warning signs in people who felt perfectly
fine.</description><pubDate>Thu, 15 Oct 2026 03:30:00 +0000</pubDate></item>
</channel></rss>"""


def _page(title: str, fact: str) -> str:
    paras = [f"{fact} Engineers and doctors who watched the first trials said they had never seen anything "
             "like it outside of a science fiction film.",
             "The team spent several years on the project, testing it again and again with ordinary people "
             "before showing it in public for the very first time this week.",
             "It is not in shops yet, and the people behind it say there is still a lot of work to do before "
             "anyone can buy one or use it every day."]
    source, _photo, _credit = SOURCES[title]
    return ("<!doctype html><html><head><title>" + title + "</title>"
            f'<meta property="og:image" content="{NEWS_PHOTO}"><meta property="og:site_name" content="Future Daily">'
            "</head><body><nav>Home · Tech</nav>"
            "<article><h1>" + title + "</h1>"
            f"<figure><img src=\"{NEWS_PHOTO}\"><figcaption>Photo by our photographer</figcaption></figure>"
            + "".join(f"<p>{t}</p>" for t in paras)
            + f'<p>The company <a href="{source}">announced it on its website</a>.</p>'
            + "</article></body></html>")


def _source_page(title: str, video: str = "") -> str:
    """La page officielle de l'histoire : sa photo, son crédit, et parfois sa vidéo."""
    _source, photo, credit = SOURCES[title]
    vid = f'<video autoplay muted><source src="{video}" type="video/mp4"></video>' if video else ""
    return ("<!doctype html><html><head><title>" + title + "</title>"
            f'<meta property="og:image" content="{photo}"><meta property="og:site_name" content="{credit}">'
            "</head><body><article><h1>" + title + "</h1>"
            f"<figure><img src=\"{photo}\"><figcaption>Image Credits: {credit}</figcaption></figure>{vid}"
            "<p>Today we are showing it for the first time.</p></article></body></html>")


class FakeWeb:
    """Remplace sources.default_http_get : le flux, les deux articles, 404 pour le reste."""

    def __init__(self) -> None:
        self.pages = {FEED_URL: FEED, ROBOT_URL: _page(ROBOT_TITLE, ROBOT_FACT),
                      SCAN_URL: _page(SCAN_TITLE, SCAN_FACT),
                      ROBOT_SOURCE: _source_page(ROBOT_TITLE), SCAN_SOURCE: _source_page(SCAN_TITLE)}
        self.requests: list[str] = []

    def __call__(self, url: str, accept: str = sources.FEED_ACCEPT) -> tuple[int, str]:
        self.requests.append(url)
        if url in self.pages:
            return 200, self.pages[url]
        return 404, ""

    def articles_read(self) -> list[str]:
        return [u for u in self.requests if u not in (FEED_URL, ROBOT_SOURCE, SCAN_SOURCE)]


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    w = FakeWeb()
    monkeypatch.setattr(sources, "default_http_get", w)
    return w


@pytest.fixture
def feed_settings(settings):
    settings.news_feeds = [{"name": "Future Daily", "url": FEED_URL, "weight": 2}]
    return settings


def _number_of(user: str, word: str) -> list[int]:
    """Numéro, dans la liste montrée à la rédaction en chef, du titre qui contient `word`."""
    for line in user.splitlines():
        m = re.match(r"\[(\d+)\]", line)
        if m and word in line.lower():
            return [int(m.group(1))]
    return []


def responder(label, system, user, schema):
    def post(text, hook):
        return {"hook_options": ["one", "two", "three"], "text": text, "source_ids": [1], "used_note_ids": [],
                "visual_url": "[1]", "poll_options": [], "hook": hook, "reason": "raison"}
    if label == "pick-tip":
        return {"picks": _number_of(user, "robot"), "reason": "le robot qui plie le linge"}
    if label == "pick-news":
        return {"picks": _number_of(user, "scanner"), "reason": "le scanner"}
    if label.startswith("pick-"):
        return {"picks": [], "reason": "rien d'assez fort pour un post à chaud"}
    if label == "post-gm":
        return {"text": GM, "used_note_ids": [], "poll_options": [], "reason": "raison"}
    if label == "post-tip":
        return post(TIP, "IT CAN DO WHAT?")
    if label == "post-news":
        return post(NEWS, "most people have no idea")          # forme d'accroche normalisée par le Writer
    if label == "factcheck":
        return {"claims": [{"claim": "the claim", "quote": "the story says so", "supported": True}],
                "ok": True, "issues": "", "plain": True, "hard_words": ""}
    if label == "source-link":
        return {"pick": 1, "reason": "la page de l'entreprise"}
    if label in ("photo-check", "video-check"):
        return {"ok": True, "issue": ""}
    if label == "comment-reply":
        return {"reply": True, "reason": "vraie question", "text": REPLY}
    if label == "radar-reply":
        return {"reply": True, "reason": "vrai apport", "text": RADAR, "angle": "test perso"}
    return None


def iso_utc(minutes_ago: float) -> str:
    dt = utils.now_local().astimezone(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Day:
    def __init__(self, settings, state, budget, clock) -> None:
        self.clock = clock
        self.tg = FakeTelegram()
        self.x = FakeX()
        self.x.ids_map = {"sama": "1", "karpathy": "2", "claudeai": "3"}
        self.state = state
        self.llm = FakeLLM({"": responder})
        self.bot = Bot(settings, llm=self.llm, x=self.x, telegram=self.tg, state=state,
                       budget=budget, prepare_visual=lambda *a: (None, None), rng=random.Random(0))
        self.approved_ids: set[str] = set()

    def card_index(self, needle: str) -> int:
        idx = [i for i, m in enumerate(self.tg.sent) if needle in m["text"] and self.tg.sent[i].get("buttons")]
        assert idx, f"aucune carte avec « {needle[:30]} »"
        return idx[-1]

    def item_for_card(self, i: int) -> dict:
        mid = self.tg.sent[i]["id"]
        return next(it for it in self.state.data["items"].values() if it.get("tg_message_id") == mid)

    def approve(self, needle: str) -> dict:
        i = self.card_index(needle)
        item = self.item_for_card(i)
        self.tg.push_callback(callback_data_for(self.tg, "✅", i), message_id=self.tg.sent[i]["id"])
        self.approved_ids.add(item["id"])
        return item

    def run(self, start: tuple[int, int, int], end: tuple[int, int, int], events: dict) -> None:
        """Un tour toutes les 10 min ; `events[(jour, h, m)]` est appelé juste avant le tour."""
        day, h, m = start
        t = self.clock(h, m, day=day)
        stop = self.clock(end[1], end[2], day=end[0])
        self.clock(h, m, day=day)
        while t <= stop:
            key = (t.day, t.hour, t.minute)
            if key in events:
                events[key]()
            self.bot.tick()
            t = t + timedelta(minutes=10)
            utils.set_clock(t)


def test_simulated_day(feed_settings, state, budget, clock, web):
    settings = feed_settings
    settings.radar_accounts = ["sama", "karpathy", "claudeai"]
    settings.radar_accounts_per_check = 3
    d = Day(settings, state, budget, clock)
    seen = {}

    def approve_gm():
        seen["gm"] = d.approve("one small automation")

    def note():
        d.tg.push_message("/note I automated my invoices with Claude in one evening")

    def comment_and_radar():
        gm_id = seen["gm"].get("published_ids", [None])[0]
        assert gm_id, "le GM validé doit être publié à 08:00"
        reply = make_tweet("901", "@sabislabi Does this also work with stack traces?", author_id="7",
                           created_at=iso_utc(5), in_reply_to_user_id=ME, conversation_id=gm_id,
                           referenced_reply_to=gm_id)
        d.x.mention_batches.append(([reply], {"7": {"id": "7", "username": "alice"}}, "901"))
        d.x.user_posts_map["1"] = [make_tweet("5001", RADAR_POST, author_id="1", created_at=iso_utc(10),
                                              likes=80)]

    def approve_reply():
        seen["reply"] = d.approve("stack traces")

    def approve_news_then_pause():
        seen["news"] = d.approve("years before you feel anything")
        d.tg.push_message("/pause")

    def resume():
        assert [p for p in d.x.posts if p["text"] == NEWS] == [], "rien pendant la pause"
        d.tg.push_message("/reprendre")

    d.run((15, 6, 0), (16, 9, 0), {
        (15, 7, 0): approve_gm,
        (15, 9, 0): note,
        (15, 10, 0): comment_and_radar,
        (15, 10, 40): approve_reply,
        (15, 17, 0): approve_news_then_pause,
        (15, 18, 30): resume,
    })

    posts = d.x.posts
    texts = [p["text"] for p in posts]
    # 1. Rien sans ✅ : chaque post vient d'un item validé à la main.
    published = [it for it in state.data["items"].values() if it.get("status") == "published"]
    assert {it["id"] for it in published} <= d.approved_ids
    assert len(posts) == len(published) == 3
    # GM à l'heure, réponse au commentaire, actu après la pause
    assert texts[0] == GM
    assert posts[1]["text"] == REPLY and posts[1]["reply_to"] == "901"
    assert texts[2] == NEWS
    news = state.item(seen["news"]["id"])
    assert utils.parse_iso(news["publish_at"]) == clock(18, 33)   # /reprendre à 18:30 + 3 min
    # 2. Le post « le futur arrive », jamais validé, a expiré sans partir ; le GM de vendredi attend.
    tips = state.items(kinds=("tip",))
    assert tips and all(t["status"] == "expired" for t in tips)
    assert TIP not in texts
    gm_friday = [it for it in state.items(kinds=("gm",)) if it.get("slot_date") == "2026-10-16"]
    assert gm_friday and gm_friday[0]["status"] in ("awaiting", "expired")
    # 3. Le radar a proposé une réponse… que le bot n'a jamais publiée.
    radar = state.items(kinds=("radar",))
    assert radar and radar[0]["texts"][0] == RADAR
    assert RADAR not in texts and all(p["reply_to"] != "5001" for p in posts)
    assert any("x.com/intent/post" in str(b.get("url", "")) for m in d.tg.sent for row in (m.get("buttons") or [])
               for b in row)
    # 4. /note gardée pour Claude
    assert state.data["notes"][-1]["text"].startswith("I automated my invoices")
    assert state.data.get("paused") is False
    # 5. Chaque post raconte une actu lue en entier : la rédaction en chef choisit sur les titres,
    #    le bot lit l'article choisi (et lui seul), le rédacteur écrit à partir de l'article.
    labels = d.llm.labels()
    assert labels.count("pick-tip") == 1 and labels.count("pick-news") == 1
    assert web.articles_read() == [ROBOT_URL, SCAN_URL]
    assert not any("x.com" in u or "twitter.com" in u for u in web.requests)
    post_calls = {c["label"]: c["user"] for c in d.llm.calls if c["label"] in ("post-tip", "post-news")}
    assert ROBOT_FACT in post_calls["post-tip"] and SCAN_TITLE not in post_calls["post-tip"]
    assert SCAN_FACT in post_calls["post-news"]
    news_meta = state.item(seen["news"]["id"])["meta"]
    assert [s["url"] for s in news_meta["sources"]] == [SCAN_URL]
    assert news_meta["hook"] == "MOST PEOPLE HAVE NO IDEA"
    # 6. « Ça vient de tomber » : le robot (de quoi intriguer tout le monde) a été montré UNE fois à
    #    la rédaction en chef, qui n'a rien retenu ; rien n'est parti à chaud.
    assert labels.count("pick-reactive") == 1
    assert ROBOT_TITLE in next(c["user"] for c in d.llm.calls if c["label"] == "pick-reactive")
    assert len(state.data["breaking_seen"]) == 1
    assert state.items(kinds=("reactive",)) == []


def _jpeg(width: int = 1200, height: int = 675) -> bytes:
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


def test_auto_mode_day_every_post_goes_out_with_the_photo_of_its_story(feed_settings, state, budget, clock,
                                                                        monkeypatch, web):
    """Le mode du serveur : personne ne valide. Chaque post est rédigé 45 min avant son heure ;
    la photo d'ouverture de l'article qu'il raconte (crédit : l'entreprise de l'actu) est
    téléchargée (ici : fausse photo, aucun réseau), Claude la regarde avec son crédit, puis le
    post part tout seul AVEC elle. Jamais de capture d'écran."""
    from bot import visuals
    from bot.config import parse_post_slots
    downloads: list[str] = []
    monkeypatch.setattr(visuals, "fetch_image_bytes", lambda url: downloads.append(url) or _jpeg())
    settings = feed_settings
    settings.approval_mode = "auto"
    settings.automated_label_ack = True
    settings.post_slots = parse_post_slots("tip@14:30,news@19:00")
    settings.radar_enabled = settings.comments_enabled = settings.reactive_enabled = False
    tg, x, llm = FakeTelegram(), FakeX(), FakeLLM({"": responder})
    bot = Bot(settings, llm=llm, x=x, telegram=tg, state=state, budget=budget,
              rng=random.Random(0))                      # le vrai visuals.prepare
    t = clock(13, 0)
    stop = clock(20, 0)
    while t <= stop:
        utils.set_clock(t)
        bot.tick()
        t += timedelta(minutes=5)

    assert [p["text"] for p in x.posts] == [TIP, NEWS]
    assert all(p["media_ids"] and len(p["media_ids"]) == 1 for p in x.posts)      # une image par post
    assert len(x.uploads) == 2 and not any(p["made_with_ai"] for p in x.posts)
    # La photo est celle de l'actu que chaque post raconte.
    assert downloads == [ROBOT_PHOTO, SCAN_PHOTO]
    checks = [c for c in llm.calls if c["label"] == "photo-check"]
    assert [len(c["images"]) for c in checks] == [1, 1]
    assert ROBOT_TITLE in checks[0]["user"] and TIP.splitlines()[0] in checks[0]["user"]
    assert 'credit line on the official page: "RoboHome"' in checks[0]["user"]
    assert SCAN_TITLE in checks[1]["user"] and '"MedScan Labs"' in checks[1]["user"]
    assert not [c for c in llm.calls if "screenshot" in c["label"]]
    items = sorted(state.items(statuses=("published",)), key=lambda i: i["publish_at"])
    assert [i["kind"] for i in items] == ["tip", "news"]
    assert all(i["image_kind"] == "photo" and i["meta"]["auto"] and "card" not in i["meta"] for i in items)
    assert all(str(i["image_path"]).startswith(str(utils.MEDIA_DIR / "photos")) for i in items)
    assert [i["meta"]["hook"] for i in items] == ["IT CAN DO WHAT?", "MOST PEOPLE HAVE NO IDEA"]
    assert [[s["url"] for s in i["meta"]["sources"]] for i in items] == [[ROBOT_URL], [SCAN_URL]]
    assert len(tg.photos) == 2                                                    # l'image arrive aussi sur Telegram
    assert all("photo de l'actu jointe à ce post" in ph["caption"] for ph in tg.photos)
    # Rédigés au plus près de l'heure (45 min avant), publiés à l'heure (aléa de publication à 0).
    assert [utils.parse_iso(i["created_at"]).strftime("%H:%M") for i in items] == ["13:45", "18:15"]
    assert [utils.parse_iso(i["publish_at"]).strftime("%H:%M") for i in items] == ["14:30", "19:00"]


def test_auto_mode_post_goes_out_as_text_when_claude_rejects_the_photo(feed_settings, state, budget,
                                                                       clock, monkeypatch, web):
    """Photo refusée par Claude (logo d'un média, photo d'agence…) : jamais publiée ; le post
    part en texte seul."""
    from bot import visuals
    from bot.config import parse_post_slots
    monkeypatch.setattr(visuals, "fetch_image_bytes", lambda url: _jpeg())

    def picky(label, system, user, schema):
        if label == "photo-check":
            return {"ok": False, "issue": "a news outlet's logo on the picture"}
        return responder(label, system, user, schema)
    settings = feed_settings
    settings.approval_mode = "auto"
    settings.automated_label_ack = True
    settings.post_slots = parse_post_slots("tip@14:30")
    settings.radar_enabled = settings.comments_enabled = settings.reactive_enabled = False
    x = FakeX()
    bot = Bot(settings, llm=FakeLLM({"": picky}), x=x, telegram=FakeTelegram(), state=state, budget=budget,
              rng=random.Random(0))
    t = clock(13, 40)
    while t <= clock(14, 40):
        utils.set_clock(t)
        bot.tick()
        t += timedelta(minutes=5)
    assert [p["text"] for p in x.posts] == [TIP]
    assert x.posts[0]["media_ids"] is None and x.uploads == []
    item = state.items(statuses=("published",))[0]
    assert item["image_path"] is None and item["image_kind"] is None
    assert not list((utils.MEDIA_DIR / "photos").glob("*.jpg"))                  # photo refusée : effacée


def test_careful_mode_agency_photo_is_never_used_and_costs_no_claude_call(feed_settings, state, budget, clock,
                                                                          monkeypatch, web):
    """PRESS_MEDIA_ENABLED=false (le mode prudent) : crédit Reuters / Getty refusé d'office, sans
    même télécharger la photo ni la montrer à Claude."""
    from bot import visuals
    from bot.config import parse_post_slots
    downloads: list[str] = []
    monkeypatch.setattr(visuals, "fetch_image_bytes", lambda url: downloads.append(url) or _jpeg())
    monkeypatch.setitem(SOURCES, ROBOT_TITLE, (ROBOT_SOURCE, ROBOT_PHOTO, "Jane Doe/Reuters"))
    web.pages[ROBOT_SOURCE] = _source_page(ROBOT_TITLE)
    settings = feed_settings
    settings.press_media_enabled = False
    settings.approval_mode = "auto"
    settings.automated_label_ack = True
    settings.post_slots = parse_post_slots("tip@14:30")
    settings.radar_enabled = settings.comments_enabled = settings.reactive_enabled = False
    x, llm = FakeX(), FakeLLM({"": responder})
    bot = Bot(settings, llm=llm, x=x, telegram=FakeTelegram(), state=state, budget=budget,
              rng=random.Random(0))
    t = clock(13, 40)
    while t <= clock(14, 40):
        utils.set_clock(t)
        bot.tick()
        t += timedelta(minutes=5)
    assert [p["text"] for p in x.posts] == [TIP] and x.posts[0]["media_ids"] is None
    assert downloads == [] and "photo-check" not in llm.labels()


@pytest.mark.parametrize("press_media", [True, False])
def test_without_official_page_the_articles_photo_like_a_news_alert_account(feed_settings, state, budget, clock,
                                                                            monkeypatch, web, press_media):
    """L'article ne renvoie vers aucune page officielle : sa propre photo est reprise comme la
    nôtre (choix du propriétaire, 15/11/2026) ; en mode prudent, texte seul et la photo du
    média n'est jamais téléchargée."""
    from bot import visuals
    from bot.config import parse_post_slots
    downloads: list[str] = []
    monkeypatch.setattr(visuals, "fetch_image_bytes", lambda url: downloads.append(url) or _jpeg())
    web.pages[ROBOT_URL] = web.pages[ROBOT_URL].replace(ROBOT_SOURCE, "https://www.youtube.com/watch?v=robot")
    settings = feed_settings
    settings.press_media_enabled = press_media
    settings.approval_mode = "auto"
    settings.automated_label_ack = True
    settings.post_slots = parse_post_slots("tip@14:30")
    settings.radar_enabled = settings.comments_enabled = settings.reactive_enabled = False
    x, llm = FakeX(), FakeLLM({"": responder})
    bot = Bot(settings, llm=llm, x=x, telegram=FakeTelegram(), state=state, budget=budget, rng=random.Random(0))
    t = clock(13, 40)
    while t <= clock(14, 40):
        utils.set_clock(t)
        bot.tick()
        t += timedelta(minutes=5)
    assert [p["text"] for p in x.posts] == [TIP]
    assert "source-link" not in llm.labels()                 # aucun lien possible : Claude n'est même pas appelé
    if press_media:
        assert downloads == [NEWS_PHOTO] and len(x.posts[0]["media_ids"]) == 1
    else:
        assert downloads == [] and x.posts[0]["media_ids"] is None


def test_auto_mode_post_goes_out_with_a_clip_of_the_official_video(feed_settings, state, budget, clock,
                                                                    monkeypatch, web, tmp_path):
    """La page officielle publie une vidéo : le bot en tire un extrait (vrai ffmpeg), Claude en
    regarde trois images, et le post part avec la VIDÉO (envoyée à X comme une vidéo)."""
    from bot import visuals
    if not visuals.ffmpeg_exe():
        pytest.skip("ffmpeg absent (paquet imageio-ffmpeg)")
    import subprocess
    from bot.config import parse_post_slots
    src = tmp_path / "official.mp4"
    subprocess.run([visuals.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                   check=True, capture_output=True, timeout=120)
    fetched: list[str] = []

    def fake_fetch_video(url, dest):
        fetched.append(url)
        dest.write_bytes(src.read_bytes())
        return True
    monkeypatch.setattr(visuals, "fetch_video_file", fake_fetch_video)
    monkeypatch.setattr(visuals, "fetch_image_bytes", lambda url: _jpeg())
    web.pages[ROBOT_SOURCE] = _source_page(ROBOT_TITLE, video=ROBOT_VIDEO)
    settings = feed_settings
    settings.approval_mode = "auto"
    settings.automated_label_ack = True
    settings.post_slots = parse_post_slots("tip@14:30")
    settings.radar_enabled = settings.comments_enabled = settings.reactive_enabled = False
    tg, x, llm = FakeTelegram(), FakeX(), FakeLLM({"": responder})
    bot = Bot(settings, llm=llm, x=x, telegram=tg, state=state, budget=budget, rng=random.Random(0))
    t = clock(13, 40)
    while t <= clock(14, 40):
        utils.set_clock(t)
        bot.tick()
        t += timedelta(minutes=5)
    assert [p["text"] for p in x.posts] == [TIP] and len(x.posts[0]["media_ids"]) == 1
    assert fetched == [ROBOT_VIDEO]
    assert [c[0] for c in x.calls if c[0].startswith("upload")] == ["upload_video"]
    item = state.items(statuses=("published",))[0]
    assert item["image_kind"] == "video" and item["image_path"].endswith(".mp4")
    checks = [c for c in llm.calls if c["label"] == "video-check"]
    assert len(checks) == 1 and len(checks[0]["images"]) == 3 and "photo-check" not in llm.labels()
    assert tg.photos and tg.photos[0].get("video") and "vidéo de l'actu jointe" in tg.photos[0]["caption"]


def test_auto_mode_requires_explicit_label_ack(settings):
    settings.approval_mode = "auto"
    settings.automated_label_ack = False
    assert any("Automated" in e for e in settings.validate())
    settings.automated_label_ack = True
    assert settings.validate() == []


@pytest.mark.parametrize("hours_down", [1, 4])
def test_bot_down_during_slot(settings, state, budget, clock, hours_down):
    """Bot arrêté au moment du GM validé : moins de 3 h de retard -> il part ; plus -> il ne part pas."""
    d = Day(settings, state, budget, clock)
    d.run((15, 6, 0), (15, 7, 0), {(15, 7, 0): lambda: d.approve("one small automation")})
    gm = state.items(kinds=("gm",))[0]
    assert gm["status"] == "approved" and d.x.posts == []
    clock(8 + hours_down, 0)
    d.bot.tick()
    if hours_down < 3:
        assert [p["text"] for p in d.x.posts] == [GM]
    else:
        assert d.x.posts == []
        assert state.item(gm["id"])["status"] == "expired"
        assert any("n'a pas pu partir à temps" in m["text"] for m in d.tg.sent)
