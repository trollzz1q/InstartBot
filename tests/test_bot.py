import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram.error import BadRequest  # noqa: E402

import bot  # noqa: E402
import content as c  # noqa: E402

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=c.MSK)


class FakeBot:
    def __init__(self):
        self.next_id = 100
        self.sent = []      # (kind, chat_id, text, kwargs, message_id)
        self.deleted = []
        self.fail_url_with_text = False
        self.fail_all_url_buttons = False
        self.fail_admin = False
        self.blocked = False
        self.fail_document = False

    def _msg(self, kind, chat_id, text, kw):
        self.next_id += 1
        self.sent.append((kind, chat_id, text, kw, self.next_id))
        return SimpleNamespace(message_id=self.next_id)

    async def send_message(self, chat_id, text, **kw):
        if self.blocked:
            raise RuntimeError("bot was blocked by the user")
        markup = kw.get("reply_markup")
        if markup:
            for row in markup.inline_keyboard:
                for b in row:
                    if b.url and (self.fail_all_url_buttons or
                                  (self.fail_url_with_text and "?text=" in b.url)):
                        raise BadRequest("Button_url_invalid")
        if self.fail_admin and chat_id == "ADMIN":
            raise RuntimeError("blocked")
        return self._msg("msg", chat_id, text, kw)

    async def send_photo(self, chat_id, photo, **kw):
        return self._msg("photo", chat_id, None, kw)

    async def send_document(self, chat_id, doc, **kw):
        if self.fail_document:
            raise RuntimeError("file_id is invalid")
        return self._msg("doc", chat_id, kw.get("caption"), dict(kw, file=doc))

    async def delete_message(self, chat_id, mid):
        self.deleted.append(mid)

    def texts(self, kind="msg"):
        return [s[2] for s in self.sent if s[0] == kind]

    def id_of(self, text_part):
        return [s[4] for s in self.sent if s[2] and text_part in s[2]][-1]


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def run_once(self, cb, when, data=None, name=None):
        job = SimpleNamespace(cb=cb, when=when, data=data, name=name, removed=False)
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self.jobs.append(job)
        return job


def make_user(uid=1, username="ivan", name="Иван Петров"):
    return SimpleNamespace(id=uid, username=username, full_name=name)


def make_update(user, data=None, message_id=None):
    q = None
    if data is not None:
        q = SimpleNamespace(data=data, answer=AsyncMock(),
                            message=SimpleNamespace(message_id=message_id))
    return SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=user.id),
                           callback_query=q)


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        patches = [
            patch.object(bot, "USERS_FILE", self.base / "users.json"),
            patch.object(bot, "GUIDE_FILE", self.base / "guide.json"),
            patch.dict("os.environ", {"UPLOAD_IDS": ""}),
            patch.object(bot, "ADMIN_CHAT_ID", "ADMIN"),
            patch.object(bot, "now", lambda: NOW),
            patch.object(bot, "BASE", self.base),  # по умолчанию нет картинок
            patch.object(c, "STEP_PAUSE", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeBot()
        self.jq = FakeJobQueue()
        self.ctx = SimpleNamespace(bot=self.fake, user_data={},
                                   application=SimpleNamespace(job_queue=self.jq))
        self.user = make_user()

    def add_images(self):
        (self.base / "images").mkdir(exist_ok=True)
        for _, img in c.TARIFFS.values():
            (self.base / img).write_bytes(b"\xff\xd8\xff")

    def set_guide(self):
        bot.save_guide("FILE_ID_123", "Гайд.pdf")

    async def start_flow(self, user=None):
        await bot.start(make_update(user or self.user), self.ctx)

    async def press(self, data, message_id=None, user=None):
        await bot.on_button(make_update(user or self.user, data, message_id), self.ctx)

    def final_ctx(self, uid=1):
        return SimpleNamespace(bot=self.fake, job=SimpleNamespace(data=uid))


class TestContent(unittest.TestCase):
    def test_manager_url_plain_and_encoded(self):
        with patch.object(c, "MANAGER_USERNAME", "marusyaa87"):
            self.assertEqual(bot.manager_url(), "https://t.me/marusyaa87")
            self.assertEqual(bot.manager_url("Хочу VIP"),
                             "https://t.me/marusyaa87?text=%D0%A5%D0%BE%D1%87%D1%83%20VIP")

    def test_manager_url_strips_at_sign_and_spaces(self):
        with patch.object(c, "MANAGER_USERNAME", " @marusyaa87 "):
            self.assertEqual(bot.manager_url(), "https://t.me/marusyaa87")

    def test_every_variant_points_to_existing_tariff(self):
        for key, text, label in c.VARIANTS:
            self.assertIn(key, c.TARIFFS)
            self.assertTrue(text and label)

    def test_final_markup_buttons(self):
        rows = bot.final_markup().inline_keyboard
        self.assertEqual([r[0].text for r in rows],
                         ["Нейросети", "Премиум", "Вип", "Задать вопрос"])
        self.assertEqual([r[0].callback_data for r in rows[:3]],
                         ["apply:neuro", "apply:premium", "apply:vip"])
        self.assertTrue(rows[3][0].url.startswith("https://t.me/"))

    def test_delay_range_is_20_to_30_minutes(self):
        self.assertEqual(c.FINAL_DELAY_MIN, (20, 30))


class TestStorage(BotTestCase):
    async def test_user_without_username_is_saved(self):
        bot.update_user(make_user(5, username=None))
        self.assertIsNone(bot.get_user(5)["username"])

    async def test_corrupted_users_file_does_not_crash(self):
        bot.USERS_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(bot.load_users(), {})
        bot.update_user(self.user)
        self.assertEqual(bot.get_user(1)["name"], "Иван Петров")

    async def test_missing_file_gives_empty(self):
        self.assertEqual(bot.load_users(), {})


class TestStart(BotTestCase):
    async def test_sends_structure_in_order(self):
        await self.start_flow()
        texts = self.fake.texts()
        self.assertEqual(texts[:3], c.INTRO)
        self.assertEqual(texts[3], c.VARIANTS[0][1])
        self.assertEqual(texts[4], c.VARIANTS[1][1])
        self.assertEqual(texts[5], c.VARIANTS[2][1])
        self.assertEqual(len(texts), 6)  # ключа и ссылки больше нет

    async def test_guide_sent_last_by_file_id_without_forward_label(self):
        self.set_guide()
        await self.start_flow()
        kind, _, caption, kw, _ = self.fake.sent[-1]
        self.assertEqual(kind, "doc")
        self.assertEqual(caption, c.GUIDE_CAPTION)
        self.assertEqual(kw["file"], "FILE_ID_123")

    async def test_no_guide_uploaded_flow_still_works(self):
        await self.start_flow()
        self.assertEqual(self.fake.texts("doc"), [])
        self.assertEqual(len(self.fake.texts()), 6)

    async def test_guide_send_failure_does_not_break_flow(self):
        self.set_guide()
        self.fake.fail_document = True
        await self.start_flow()
        self.assertEqual(len(self.fake.texts()), 6)
        self.assertEqual(len(self.jq.get_jobs_by_name("final-1")), 1)

    async def test_variants_are_photos_with_caption_and_button(self):
        self.add_images()
        await self.start_flow()
        kinds = [s[0] for s in self.fake.sent]
        self.assertEqual(kinds, ["msg"] * 3 + ["photo"] * 3)
        photos = [s for s in self.fake.sent if s[0] == "photo"]
        self.assertEqual([p[3]["caption"] for p in photos], [v[1] for v in c.VARIANTS])
        self.assertEqual([p[3]["reply_markup"].inline_keyboard[0][0].callback_data
                          for p in photos], ["apply:neuro", "apply:premium", "apply:vip"])

    async def test_long_text_goes_as_message_with_separate_photo(self):
        self.add_images()
        long_variants = [(k, "x" * 1100, label) for k, _, label in c.VARIANTS]
        with patch.object(c, "VARIANTS", long_variants):
            await self.start_flow()
        kinds = [s[0] for s in self.fake.sent]
        self.assertEqual(kinds, ["msg"] * 3 + ["msg", "photo"] * 3)
        self.assertIsNotNone(self.fake.sent[3][3]["reply_markup"])  # кнопка на тексте

    async def test_real_images_exist_and_captions_fit(self):
        real = Path(__file__).resolve().parent.parent
        for _, img in c.TARIFFS.values():
            self.assertTrue((real / img).exists(), img)
        for _, text, _ in c.VARIANTS:
            self.assertLessEqual(len(text), bot.CAPTION_LIMIT)

    async def test_variant_buttons_lead_to_tariffs(self):
        await self.start_flow()
        buttons = {}
        for s in self.fake.sent:
            markup = s[3].get("reply_markup")
            if markup:
                b = markup.inline_keyboard[0][0]
                buttons[b.text] = b.callback_data
        self.assertEqual(buttons, {"Нейросети": "apply:neuro", "Премиум": "apply:premium",
                                   "Вип": "apply:vip"})

    async def test_final_message_is_scheduled_in_20_30_minutes(self):
        await self.start_flow()
        job = self.jq.get_jobs_by_name("final-1")[0]
        delta = job.when - NOW
        self.assertTrue(timedelta(minutes=20) <= delta <= timedelta(minutes=30))
        self.assertIsNotNone(bot.get_user(1)["final_at"])

    async def test_restart_keeps_one_final_job(self):
        await self.start_flow()
        await self.start_flow()
        self.assertEqual(len(self.jq.get_jobs_by_name("final-1")), 1)

    async def test_repeated_start_deletes_old_screen_but_keeps_guide(self):
        self.set_guide()
        await self.start_flow()
        first = [s[4] for s in self.fake.sent]
        guide_id = first[-1]
        await self.start_flow()
        for mid in first[:-1]:
            self.assertIn(mid, self.fake.deleted)
        self.assertNotIn(guide_id, self.fake.deleted)

    async def test_new_messages_are_sent_before_old_are_deleted(self):
        await self.start_flow()
        order = []
        orig_send, orig_delete = self.fake.send_message, self.fake.delete_message

        async def send(*a, **k):
            order.append("send")
            return await orig_send(*a, **k)

        async def delete(*a, **k):
            order.append("delete")
            return await orig_delete(*a, **k)

        self.fake.send_message, self.fake.delete_message = send, delete
        await self.start_flow()
        self.assertLess(max(i for i, x in enumerate(order) if x == "send"),
                        min(i for i, x in enumerate(order) if x == "delete"))

    async def test_user_without_username_can_start(self):
        await self.start_flow(make_user(9, username=None))
        self.assertEqual(len(self.fake.texts()), 6)


class TestGuideUpload(BotTestCase):
    def doc_update(self, user):
        doc = SimpleNamespace(file_id="NEW_ID", file_name="Гайд.pdf")
        upd = make_update(user)
        upd.effective_message = SimpleNamespace(document=doc)
        return upd

    async def test_admin_upload_saves_file_id(self):
        with patch.object(bot, "ADMIN_CHAT_ID", "42"):
            await bot.on_document(self.doc_update(make_user(42)), self.ctx)
        self.assertEqual(bot.load_guide()["file_id"], "NEW_ID")
        self.assertIn("Гайд сохранён", self.fake.texts()[-1])

    async def test_upload_ids_env_allows_owner(self):
        with patch.dict("os.environ", {"UPLOAD_IDS": "7, 8"}):
            await bot.on_document(self.doc_update(make_user(8)), self.ctx)
        self.assertEqual(bot.load_guide()["file_id"], "NEW_ID")

    async def test_stranger_cannot_replace_guide(self):
        self.set_guide()
        await bot.on_document(self.doc_update(make_user(999)), self.ctx)
        self.assertEqual(bot.load_guide()["file_id"], "FILE_ID_123")
        self.assertEqual(self.fake.sent, [])

    async def test_corrupted_guide_file_means_no_guide(self):
        bot.GUIDE_FILE.write_text("{oops", encoding="utf-8")
        self.assertEqual(bot.load_guide(), {})

    async def test_message_without_document_is_ignored(self):
        upd = make_update(make_user(42))
        upd.effective_message = SimpleNamespace(document=None)
        with patch.object(bot, "ADMIN_CHAT_ID", "42"):
            await bot.on_document(upd, self.ctx)
        self.assertEqual(self.fake.sent, [])


class TestFinal(BotTestCase):
    async def test_final_has_buttons_and_replaces_screen(self):
        self.set_guide()
        await self.start_flow()
        screen = bot.get_user(1)["msgs"]
        gift_id = self.fake.sent[-1][4]  # гайд
        await bot.send_final(self.final_ctx())
        kind, chat, text, kw, final_id = self.fake.sent[-1]
        self.assertEqual(text, c.TEXT_FINAL)
        self.assertEqual(len(kw["reply_markup"].inline_keyboard), 4)
        for mid in screen:
            self.assertIn(mid, self.fake.deleted)
        self.assertNotIn(gift_id, self.fake.deleted)
        self.assertEqual(bot.get_user(1)["msgs"], [final_id])
        self.assertIsNone(bot.get_user(1)["final_at"])

    async def test_final_survives_blocked_user(self):
        await self.start_flow()
        self.fake.blocked = True
        await bot.send_final(self.final_ctx())  # не падает
        self.assertIsNone(bot.get_user(1)["final_at"])

    async def test_post_init_restores_pending_final(self):
        bot.update_user(make_user(1), final_at=(NOW - timedelta(hours=1)).isoformat())
        bot.update_user(make_user(2), final_at=None)
        await bot.post_init(SimpleNamespace(job_queue=self.jq))
        self.assertEqual([j.name for j in self.jq.jobs], ["final-1"])
        self.assertGreater(self.jq.jobs[0].when, NOW)


class TestApply(BotTestCase):
    async def test_apply_gives_link_with_ready_text_and_notifies_admin(self):
        await self.press("apply:vip", message_id=50)
        _, _, text, kw, _ = self.fake.sent[0]
        url = kw["reply_markup"].inline_keyboard[0][0].url
        self.assertTrue(url.startswith("https://t.me/"))
        self.assertIn("text=", url)
        admin = [s for s in self.fake.sent if s[1] == "ADMIN"][0]
        self.assertIn("Иван Петров", admin[2])
        self.assertIn("@ivan", admin[2])
        self.assertIn("VIP", admin[2])
        self.assertEqual(bot.get_user(1)["applied"], "VIP")

    async def test_each_button_maps_to_own_tariff(self):
        for key, (name, _) in c.TARIFFS.items():
            self.fake.sent.clear()
            await self.press(f"apply:{key}", message_id=1)
            admin = [s for s in self.fake.sent if s[1] == "ADMIN"][0]
            self.assertIn(name, admin[2])

    async def test_apply_without_username_does_not_crash(self):
        user = make_user(7, username=None, name="Аня <b>")
        await self.press("apply:premium", message_id=1, user=user)
        admin = [s for s in self.fake.sent if s[1] == "ADMIN"][0]
        self.assertIn("tg://user?id=7", admin[2])
        self.assertIn("&lt;b&gt;", admin[2])

    async def test_apply_username_with_underscore(self):
        await self.press("apply:neuro", message_id=1, user=make_user(8, username="a_b_c"))
        admin = [s for s in self.fake.sent if s[1] == "ADMIN"][0]
        self.assertIn("@a_b_c", admin[2])

    async def test_falls_back_to_plain_link_if_telegram_rejects_text(self):
        self.fake.fail_url_with_text = True
        await self.press("apply:vip", message_id=1)
        url = self.fake.sent[0][3]["reply_markup"].inline_keyboard[0][0].url
        self.assertNotIn("?text=", url)

    async def test_falls_back_to_plain_text_if_all_links_rejected(self):
        self.fake.fail_all_url_buttons = True
        await self.press("apply:vip", message_id=1)
        self.assertIn("@" + c.MANAGER_USERNAME.lstrip("@"), self.fake.sent[0][2])

    async def test_admin_failure_does_not_break_user_flow(self):
        self.fake.fail_admin = True
        await self.press("apply:vip", message_id=1)
        self.assertEqual(len(self.fake.sent), 1)

    async def test_missing_admin_chat_id_does_not_break(self):
        with patch.object(bot, "ADMIN_CHAT_ID", None):
            await self.press("apply:vip", message_id=1)
        self.assertEqual(len(self.fake.sent), 1)

    async def test_apply_keeps_screen_but_replaces_old_apply_message(self):
        await self.start_flow()
        screen = bot.get_user(1)["msgs"]
        await self.press("apply:vip", message_id=screen[0])
        first_apply = self.fake.id_of("Марине")
        await self.press("apply:premium", message_id=screen[1])
        for mid in screen:
            self.assertNotIn(mid, self.fake.deleted)
        self.assertIn(first_apply, self.fake.deleted)

    async def test_apply_from_final_message_keeps_it(self):
        await self.start_flow()
        await bot.send_final(self.final_ctx())
        final_id = self.fake.sent[-1][4]
        await self.press("apply:neuro", message_id=final_id)
        self.assertNotIn(final_id, self.fake.deleted)

    async def test_garbage_and_old_buttons_do_not_crash(self):
        for data in ["", "zzz", "apply:nope", "apply:", "gift", "dir:video", "tariffs"]:
            await self.press(data, message_id=1)
        self.assertEqual(self.fake.sent, [])

    async def test_button_answer_failure_does_not_break(self):
        upd = make_update(self.user, "apply:vip", 1)
        upd.callback_query.answer = AsyncMock(side_effect=RuntimeError("timeout"))
        await bot.on_button(upd, self.ctx)
        self.assertEqual(len(self.fake.sent), 2)  # сообщение пользователю и заявка Марине

    async def test_delete_failure_does_not_break(self):
        await self.start_flow()
        self.fake.delete_message = AsyncMock(side_effect=RuntimeError("too old"))
        await self.start_flow()
        self.assertEqual(self.fake.texts()[-1], c.VARIANTS[-1][1])


if __name__ == "__main__":
    unittest.main()
