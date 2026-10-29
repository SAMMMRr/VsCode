import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerInlineQuery, SendPhoto, SendRichMessage
from aiogram.types import (
    InlineQueryResultArticle,
    InputRichBlockButtons,
    InputRichMessageContent,
    InputRichBlockPhoto,
)

import bot


class RichMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.movie = bot.Movie(
            tmdb_id=42,
            title="Тестовый фильм",
            year="2024",
            overview="Краткое описание.",
            poster_path="/poster.jpg",
            tmdb_rating=8.5,
            media_type="movie",
            genres=("Драма", "Фантастика"),
            runtime_minutes=125,
        )

    def test_rich_card_serializes_embedded_photo_and_callbacks(self) -> None:
        rich_message = bot.rich_movie_message(
            self.movie, 8.0, 3, card_token="test_token"
        )
        request = SendRichMessage(
            chat_id=-100123,
            rich_message=rich_message,
            protect_content=False,
        )
        payload = request.model_dump(
            mode="json",
            exclude_none=True,
        )

        self.assertEqual(request.__api_method__, "sendRichMessage")
        self.assertEqual(payload["chat_id"], -100123)
        blocks = payload["rich_message"]["blocks"]
        self.assertEqual(blocks[0]["type"], "photo")
        self.assertEqual(blocks[1]["type"], "heading")
        self.assertTrue(
            blocks[0]["photo"]["media"].endswith("/poster.jpg")
        )
        button_blocks = [
            block for block in rich_message.blocks
            if isinstance(block, InputRichBlockButtons)
        ]
        self.assertEqual([len(block.buttons) for block in button_blocks], [2, 2, 1])
        self.assertTrue(
            all(button.callback_data.startswith("rich:") for block in button_blocks for button in block.buttons)
        )
        self.assertIn("rich:rate:test_token", [
            button.callback_data
            for block in button_blocks
            for button in block.buttons
        ])
        self.assertNotIn("reply_markup", payload["rich_message"])

    def test_rating_prompt_contains_ten_choices(self) -> None:
        prompt = bot.rich_movie_message(
            self.movie, 0, 0, card_token="test_token", rating_prompt=True
        )
        button_blocks = [
            block for block in prompt.blocks
            if isinstance(block, InputRichBlockButtons)
        ]
        self.assertIsInstance(prompt.blocks[0], InputRichBlockPhoto)
        self.assertTrue(prompt.blocks[0].photo.media.endswith("/poster.jpg"))
        self.assertEqual(
            [[button.text for button in block.buttons] for block in button_blocks],
            [["1", "2", "3", "4", "5"], ["6", "7", "8", "9", "10"], ["↩ К карточке"]],
        )
        self.assertEqual(
            [
                button.callback_data
                for block in button_blocks[:2]
                for button in block.buttons
            ],
            [f"rich:ratevote:test_token:{value}" for value in range(1, 11)],
        )

    def test_group_rating_is_integer_without_stars(self) -> None:
        card = bot.rich_movie_message(
            self.movie, 8.5, 4, card_token="test_token"
        )
        paragraphs = [
            block.text
            for block in card.blocks
            if isinstance(block, bot.InputRichBlockParagraph)
        ]
        paragraph_text = " ".join(
            item if isinstance(item, str) else item.text
            for paragraph in paragraphs
            for item in (paragraph if isinstance(paragraph, list) else [paragraph])
        )
        self.assertTrue(
            "⭐ Средняя оценка группы: 9/10 · 4 оценок" in paragraph_text
        )
        self.assertFalse("Средняя оценка группы:" in paragraph_text and "⭐☆" in paragraph_text)
        self.assertEqual(bot.integer_rating(8.49), 8)
        self.assertEqual(bot.integer_rating(8.5), 9)

    def test_tv_episode_runtime_and_movie_runtime_labels(self) -> None:
        movie = bot.Movie(1, "Фильм", "2024", "", None, 0, runtime_minutes=125)
        series = bot.Movie(
            2, "Сериал", "2024", "", None, 0, media_type="tv", runtime_minutes=47
        )
        self.assertEqual(bot.movie_runtime_label(movie), "2 ч 5 мин")
        self.assertEqual(bot.movie_runtime_label(series), "47 мин / серия")

    def test_movie_overview_is_shortened_to_300_characters(self) -> None:
        overview = "Описание фильма состоит из нескольких слов. " * 20
        shortened = bot.shorten_movie_overview(overview)
        movie = bot.Movie(1, "Фильм", "2024", overview, None, 0)
        card = bot.rich_movie_message(movie, 0, 0, card_token="test_token")
        paragraphs = [
            block.text
            for block in card.blocks
            if isinstance(block, bot.InputRichBlockParagraph)
        ]

        self.assertLessEqual(len(shortened), 300)
        self.assertTrue(shortened.endswith("…"))
        self.assertIn(shortened, paragraphs)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_alternative_posters_prefer_requested_language(self) -> None:
        tmdb = bot.TMDB("test-token")
        tmdb.request = AsyncMock(
            return_value={
                "posters": [
                    {
                        "file_path": "/english.jpg",
                        "iso_639_1": "en",
                        "vote_average": 9,
                    },
                    {
                        "file_path": "/russian.jpg",
                        "iso_639_1": "ru",
                        "vote_average": 6,
                    },
                    {
                        "file_path": "/no_language.jpg",
                        "iso_639_1": None,
                        "vote_average": 10,
                    },
                    {
                        "file_path": "/current.jpg",
                        "iso_639_1": "ru",
                        "vote_average": 10,
                    },
                ]
            }
        )
        movie = bot.Movie(58, "Фильм", "2024", "", "/current.jpg", 0)

        with patch.object(bot, "TMDB_LANGUAGE", "ru-RU"):
            paths = await tmdb.alternative_poster_paths(movie)

        self.assertEqual(
            paths,
            ["/russian.jpg", "/english.jpg", "/no_language.jpg"],
        )
        tmdb.request.assert_awaited_once_with(
            "/movie/58/images",
            {
                "language": "ru-RU",
                "include_image_language": "ru,en,null",
            },
        )

    async def test_download_uses_alternative_when_primary_poster_is_missing(self) -> None:
        class FakeContent:
            async def iter_chunked(self, _size: int):
                _ = _size
                yield b"valid-image-bytes"

        class FakeResponse:
            def __init__(self, status: int):
                self.status = status
                self.headers = {"Content-Type": "image/jpeg"}
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                _ = _args
                return None

            def raise_for_status(self):
                if self.status >= 400:
                    raise bot.aiohttp.ClientResponseError(
                        request_info=cast(bot.aiohttp.RequestInfo, None),
                        history=(),
                        status=self.status,
                        message="Not Found",
                    )

        for primary_status, responses, alternatives_list, expected_paths in (
            (
                404,
                [FakeResponse(404), FakeResponse(200)],
                ["/available.jpg"],
                ["/missing.jpg", "/available.jpg"],
            ),
            (
                403,
                [FakeResponse(403), FakeResponse(403), FakeResponse(200)],
                ["/blocked.jpg", "/available.jpg"],
                ["/missing.jpg", "/blocked.jpg", "/available.jpg"],
            ),
        ):
            with self.subTest(primary_status=primary_status):
                movie = bot.Movie(58, "Фильм", "2024", "", "/missing.jpg", 0)
                session = SimpleNamespace(get=Mock(side_effect=responses))
                with patch.object(
                    bot.tmdb,
                    "alternative_poster_paths",
                    new=AsyncMock(return_value=alternatives_list),
                ) as alternatives:
                    image = await bot.download_tmdb_poster(
                        cast(bot.aiohttp.ClientSession, session), movie
                    )

                self.assertEqual(image, b"valid-image-bytes")
                alternatives.assert_awaited_once_with(movie)
                self.assertEqual(
                    [call.args[0] for call in session.get.call_args_list],
                    [f"{bot.TMDB_IMAGE}{path}" for path in expected_paths],
                )

    async def test_film_search_shows_and_removes_progress_after_success(self) -> None:
        status_message = SimpleNamespace(
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        message = SimpleNamespace(
            answer=AsyncMock(return_value=status_message),
        )
        movie = bot.Movie(42, "Фильм", "2024", "", None, 0)

        with (
            patch.object(bot.tmdb, "search", new=AsyncMock(return_value=movie)),
            patch.object(bot, "publish_movie", new=AsyncMock()) as publish_movie,
        ):
            await bot.find_movie(message, SimpleNamespace(args="Фильм"))

        message.answer.assert_awaited_once_with("🔎 Ищу фильм в TMDB…")
        publish_movie.assert_awaited_once_with(message, movie)
        status_message.delete.assert_awaited_once()
        status_message.edit_text.assert_not_awaited()

    async def test_film_search_replaces_progress_when_no_match(self) -> None:
        status_message = SimpleNamespace(
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        message = SimpleNamespace(answer=AsyncMock(return_value=status_message))

        with patch.object(bot.tmdb, "search", new=AsyncMock(return_value=None)):
            await bot.find_movie(message, SimpleNamespace(args="Несуществующий фильм"))

        status_message.edit_text.assert_awaited_once_with(
            "Ничего не нашёл. Попробуй оригинальное название или другой вариант написания."
        )
        status_message.delete.assert_not_awaited()

    async def test_film_search_shows_error_instead_of_leaving_progress(self) -> None:
        status_message = SimpleNamespace(
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        message = SimpleNamespace(answer=AsyncMock(return_value=status_message))

        with patch.object(
            bot.tmdb,
            "search",
            new=AsyncMock(side_effect=RuntimeError("TMDB unavailable")),
        ):
            await bot.find_movie(message, SimpleNamespace(args="Фильм"))

        status_message.edit_text.assert_awaited_once()
        self.assertIn(
            "Не получилось найти фильм в TMDB",
            status_message.edit_text.await_args.args[0],
        )
        status_message.delete.assert_not_awaited()

    async def test_tmdb_details_parse_movie_and_series_metadata(self) -> None:
        tmdb = bot.TMDB("test-token")
        tmdb.request = AsyncMock(
            side_effect=[
                {
                    "id": 42,
                    "title": "Фильм",
                    "release_date": "2024-01-01",
                    "genres": [{"id": 18, "name": "Драма"}],
                    "runtime": 125,
                    "vote_average": 8.5,
                },
                {
                    "id": 43,
                    "name": "Сериал",
                    "first_air_date": "2022-01-01",
                    "genres": [{"id": 10765, "name": "Фантастика"}],
                    "episode_run_time": [47, 51],
                    "vote_average": 7.5,
                },
            ]
        )
        movie = bot.Movie(42, "Фильм", "2024", "", None, 8.5)
        series = bot.Movie(
            43, "Сериал", "2022", "", None, 7.5, media_type="tv"
        )

        detailed_movie = await tmdb.details(movie)
        detailed_series = await tmdb.details(series)

        self.assertEqual(detailed_movie.genres, ("Драма",))
        self.assertEqual(detailed_movie.runtime_minutes, 125)
        self.assertEqual(detailed_series.genres, ("Фантастика",))
        self.assertEqual(detailed_series.runtime_minutes, 47)
        self.assertEqual(
            [call.args[0] for call in tmdb.request.await_args_list],
            ["/movie/42", "/tv/43"],
        )

    async def test_old_movies_schema_is_migrated_and_existing_ratings_survive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.sqlite3")
            async with aiosqlite.connect(path) as db:
                await db.executescript(
                    """
                    CREATE TABLE movies (
                        tmdb_id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL,
                        year TEXT NOT NULL,
                        overview TEXT NOT NULL,
                        poster_path TEXT,
                        tmdb_rating REAL NOT NULL DEFAULT 0
                    );
                    INSERT INTO movies VALUES (42, 'Старый фильм', '2020', 'Описание', NULL, 7.0);
                    CREATE TABLE ratings (
                        chat_id TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        tmdb_id INTEGER NOT NULL,
                        rating REAL NOT NULL CHECK(rating BETWEEN 0 AND 10),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, user_id, tmdb_id)
                    );
                    INSERT INTO ratings(chat_id,user_id,tmdb_id,rating) VALUES ('-100', 10, 42, 8);
                    """
                )

            store = bot.Store(path)
            await store.init()
            movie = await store.get_movie(42)
            self.assertIsNotNone(movie)
            assert movie is not None
            self.assertEqual(movie.media_type, "movie")
            self.assertEqual(movie.genres, ())
            self.assertIsNone(movie.runtime_minutes)
            self.assertEqual(await store.group_score(-100, 42), (8.0, 1))

            detailed = bot.Movie(
                42,
                "Старый фильм",
                "2020",
                "Описание",
                None,
                7.0,
                "tv",
                ("Драма",),
                47,
            )
            await store.save_movie(detailed)
            loaded = await store.get_movie(42)
            assert loaded is not None
            self.assertEqual(loaded.media_type, "tv")
            self.assertEqual(loaded.genres, ("Драма",))
            self.assertEqual(loaded.runtime_minutes, 47)

    async def test_favorite_toggle_and_rating_based_favorites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "store.sqlite3"))
            await store.init()
            await store.save_movie(self_movie := bot.Movie(42, "Фильм", "2024", "", None, 0))

            self.assertTrue(await store.toggle_favorite(-100, 5, 42))
            self.assertTrue(await store.is_favorite(-100, 5, 42))
            self.assertEqual((await store.favorites(-100, 5))[0][0], self_movie)
            self.assertFalse(await store.toggle_favorite(-100, 5, 42))
            self.assertFalse(await store.is_favorite(-100, 5, 42))

            await store.rate(-100, 5, 42, 8)
            self.assertEqual(len(await store.favorites(-100, 5)), 1)

    async def test_rich_card_token_is_bound_to_its_chat_and_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "cards.sqlite3"))
            await store.init()
            await store.save_movie(bot.Movie(42, "Фильм", "2024", "", None, 0))
            await store.register_rich_card("token_123456", -100, 77, 42)

            self.assertEqual(
                await store.get_rich_card_movie_id("token_123456", -100, 77),
                42,
            )
            self.assertIsNone(
                await store.get_rich_card_movie_id("token_123456", -200, 77)
            )
            self.assertIsNone(
                await store.get_rich_card_movie_id("different_token", -100, 77)
            )
            self.assertEqual(
                await store.get_rich_card_token(-100, 77, 42),
                "token_123456",
            )

    async def test_publish_sends_rich_message_and_registers_callback_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "publish.sqlite3"))
            await store.init()
            movie = bot.Movie(
                42,
                "Фильм",
                "2024",
                "Описание",
                "/poster.jpg",
                8.5,
                genres=("Драма",),
                runtime_minutes=100,
            )
            telegram = SimpleNamespace(
                send_rich_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=77)
                )
            )
            message = SimpleNamespace(
                bot=telegram,
                chat=SimpleNamespace(id=-100123),
                from_user=SimpleNamespace(id=55),
                message_thread_id=9,
            )

            with (
                patch.object(bot, "store", store),
                patch.object(bot.tmdb, "details", new=AsyncMock(return_value=movie)),
            ):
                await bot.publish_movie(message, movie)

            telegram.send_rich_message.assert_awaited_once()
            kwargs = telegram.send_rich_message.await_args.kwargs
            self.assertEqual(kwargs["chat_id"], -100123)
            self.assertEqual(kwargs["message_thread_id"], 9)
            self.assertIsInstance(kwargs["rich_message"], bot.InputRichMessage)
            token = await store.get_rich_card_token(-100123, 77, 42)
            self.assertEqual(
                await store.get_rich_card_movie_id(token, -100123, 77),
                42,
            )

    async def test_inline_result_token_supports_multiple_posted_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_cards.sqlite3"))
            await store.init()
            await store.save_movie(bot.Movie(42, "Фильм", "2024", "", None, 0))
            await store.register_inline_rich_card("token_123456", 42)

            self.assertEqual(
                await store.bind_inline_rich_card(
                    "token_123456", "inline-message-1", "chat-instance-1"
                ),
                42,
            )
            self.assertEqual(
                await store.bind_inline_rich_card(
                    "token_123456", "inline-message-1", "chat-instance-1"
                ),
                42,
            )
            self.assertEqual(
                await store.bind_inline_rich_card(
                    "token_123456", "inline-message-2", "chat-instance-1"
                ),
                42,
            )

    async def test_inline_favorite_is_listed_in_favorites_command_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_favorites.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "", None, 0)
            await store.save_movie(movie)
            await store.toggle_favorite("inline-user:5", 5, 42)

            self.assertEqual((await store.favorites(-100, 5))[0][0], movie)

    async def test_inline_query_returns_rich_message_articles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_query.sqlite3"))
            await store.init()
            movie = bot.Movie(
                42,
                "Фильм",
                "2024",
                "Описание",
                "/poster.jpg",
                8.5,
                genres=("Драма",),
                runtime_minutes=110,
            )
            await store.save_poster_file_id("/poster.jpg", "telegram-file-id")
            query = SimpleNamespace(
                query="Фильм",
                answer=AsyncMock(),
                bot=SimpleNamespace(),
            )

            with (
                patch.object(bot, "store", store),
                patch.object(
                    bot.tmdb,
                    "search_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot.tmdb,
                    "details_many",
                    new=AsyncMock(return_value=[movie]),
                ) as details_many,
                patch.object(
                    bot,
                    "schedule_poster_cache",
                    return_value=None,
                ) as schedule_cache,
            ):
                await bot.inline_movie_search(query)

            details_many.assert_awaited_once_with([movie])
            schedule_cache.assert_called_once_with(query.bot, [], 0)
            results = query.answer.await_args.kwargs["results"]
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0], InlineQueryResultArticle)
            self.assertEqual(results[0].title, "Фильм (2024)")
            self.assertIsInstance(
                results[0].input_message_content,
                InputRichMessageContent,
            )
            self.assertEqual(
                results[0].thumbnail_url,
                "https://image.tmdb.org/t/p/w500/poster.jpg",
            )
            payload = results[0].model_dump(mode="json", exclude_none=True)
            blocks = payload["input_message_content"]["rich_message"]["blocks"]
            photos = [block for block in blocks if block["type"] == "photo"]
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0]["photo"]["media"], "telegram-file-id")
            self.assertTrue(any(block["type"] == "buttons" for block in blocks))
            buttons = [
                button
                for block in blocks
                if block["type"] == "buttons"
                for button in block["buttons"]
            ]
            self.assertTrue(any(button["text"] == "⭐ Оценить" for button in buttons))
            paragraphs = [
                block["text"]
                for block in blocks
                if block["type"] == "paragraph"
            ]
            self.assertTrue(
                any(
                    "Драма · 1 ч 50 мин" in str(paragraph)
                    for paragraph in paragraphs
                )
            )
            self.assertTrue(
                all(button.get("callback_data", "").startswith("rich:")
                    for button in buttons if "callback_data" in button)
            )
            request_payload = AnswerInlineQuery(
                inline_query_id="query-id",
                results=results,
                cache_time=5,
                is_personal=True,
            ).model_dump(mode="json", exclude_none=True)
            self.assertEqual(
                request_payload["results"][0]["input_message_content"],
                payload["input_message_content"],
            )

    async def test_inline_search_keeps_results_when_poster_cache_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_query_no_poster.sqlite3"))
            await store.init()
            movie = bot.Movie(
                42,
                "Фильм",
                "2024",
                "Описание",
                "/unavailable.jpg",
                8.5,
            )
            query = SimpleNamespace(
                query="Фильм",
                answer=AsyncMock(),
                bot=SimpleNamespace(),
            )

            with (
                patch.object(bot, "store", store),
                patch.object(
                    bot.tmdb,
                    "search_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot.tmdb,
                    "details_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot,
                    "schedule_poster_cache",
                    return_value=None,
                ),
            ):
                await bot.inline_movie_search(query)

            results = query.answer.await_args.kwargs["results"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Фильм (2024)")
            self.assertEqual(
                results[0].thumbnail_url,
                f"{bot.TMDB_IMAGE}/unavailable.jpg",
            )
            rich_message = results[0].input_message_content.rich_message
            self.assertFalse(
                any(
                    isinstance(block, InputRichBlockPhoto)
                    for block in rich_message.blocks
                )
            )
            self.assertTrue(
                any(
                    isinstance(block, InputRichBlockButtons)
                    for block in rich_message.blocks
                )
            )

    async def test_inline_search_schedules_uncached_posters_after_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_query_cache_later.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "Описание", "/poster.jpg", 8.5)
            query = SimpleNamespace(
                query="Фильм",
                answer=AsyncMock(),
                bot=SimpleNamespace(),
            )
            with (
                patch.object(bot, "store", store),
                patch.object(
                    bot.tmdb,
                    "search_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot.tmdb,
                    "details_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot,
                    "telegram_poster_file_ids",
                    new=AsyncMock(side_effect=AssertionError("must run in background")),
                ),
                patch.object(
                    bot, "schedule_poster_cache", return_value=None
                ) as schedule_cache,
            ):
                await bot.inline_movie_search(query)

            query.answer.assert_awaited_once()
            schedule_cache.assert_called_once_with(query.bot, [movie], 0)

    async def test_inline_search_does_not_wait_for_poster_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_query_fast_poster.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "Описание", "/poster.jpg", 8.5)
            query = SimpleNamespace(
                query="Фильм",
                answer=AsyncMock(),
                bot=SimpleNamespace(),
            )
            cache_started = bot.asyncio.Event()
            release_cache = bot.asyncio.Event()
            cache_tasks: list[bot.asyncio.Task[None]] = []

            async def cache_poster() -> None:
                cache_started.set()
                await release_cache.wait()
                await store.save_poster_file_id("/poster.jpg", "cached-file-id")

            def schedule_cache(bot_client, uncached_movies, retry_after):
                _ = (bot_client, uncached_movies, retry_after)
                task = bot.asyncio.create_task(cache_poster())
                cache_tasks.append(task)
                return task

            with (
                patch.object(bot, "store", store),
                patch.object(
                    bot.tmdb,
                    "search_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(
                    bot.tmdb,
                    "details_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(bot, "schedule_poster_cache", side_effect=schedule_cache),
            ):
                await bot.inline_movie_search(query)

            self.assertTrue(cache_started.is_set())
            release_cache.set()
            await cache_tasks[0]

            result = query.answer.await_args.kwargs["results"][0]
            blocks = result.input_message_content.rich_message.blocks
            self.assertFalse(any(isinstance(block, InputRichBlockPhoto) for block in blocks))

    async def test_inline_search_returns_fallback_results_when_details_are_slow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_query_slow_details.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "Описание", None, 8.5)
            query = SimpleNamespace(
                query="Фильм",
                answer=AsyncMock(),
                bot=SimpleNamespace(),
            )

            async def slow_details(_movies: list[bot.Movie]) -> list[bot.Movie]:
                await bot.asyncio.sleep(0.05)
                return _movies

            with (
                patch.object(bot, "store", store),
                patch.object(
                    bot.tmdb,
                    "search_many",
                    new=AsyncMock(return_value=[movie]),
                ),
                patch.object(bot.tmdb, "details_many", new=slow_details),
                patch.object(bot, "INLINE_DETAILS_TIMEOUT_SECONDS", 0.001),
                patch.object(
                    bot, "schedule_poster_cache", return_value=None
                ) as schedule_cache,
            ):
                await bot.inline_movie_search(query)

            results = query.answer.await_args.kwargs["results"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Фильм (2024)")
            schedule_cache.assert_called_once_with(query.bot, [], 0)

    async def test_expired_inline_query_is_handled_without_traceback(self) -> None:
        query = SimpleNamespace(
            answer=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=AnswerInlineQuery(inline_query_id="expired", results=[]),
                    message="query is too old and response timeout expired",
                )
            )
        )

        answered = await bot.answer_inline_query_safely(
            query, [], cache_time=5
        )

        self.assertFalse(answered)
        query.answer.assert_awaited_once()

    async def test_inline_rich_rating_uses_inline_message_id_and_chat_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "inline_rating.sqlite3"))
            await store.init()
            await store.save_movie(
                bot.Movie(42, "Фильм", "2024", "", "/poster.jpg", 0)
            )
            await store.save_poster_file_id("/poster.jpg", "telegram-file-id")
            await store.register_inline_rich_card("token_123456", 42)
            callback = SimpleNamespace(
                data="rich:rate:token_123456",
                message=None,
                inline_message_id="inline-message-1",
                chat_instance="chat-instance-1",
                from_user=SimpleNamespace(id=5),
                answer=AsyncMock(),
                bot=SimpleNamespace(edit_message_text=AsyncMock()),
            )

            with patch.object(bot, "store", store):
                await bot.rich_movie_action(callback)
                prompt = callback.bot.edit_message_text.await_args.kwargs[
                    "rich_message"
                ]
                self.assertEqual(
                    prompt.blocks[0].photo.media,
                    "telegram-file-id",
                )
                callback.data = "rich:ratevote:token_123456:9"
                callback.bot.edit_message_text.reset_mock()
                callback.answer.reset_mock()
                await bot.rich_movie_action(callback)
                group_score = await store.group_score("inline:chat-instance-1", 42)

            self.assertEqual(group_score, (9.0, 1))
            callback.bot.edit_message_text.assert_awaited_once()
            self.assertEqual(
                callback.bot.edit_message_text.await_args.kwargs["inline_message_id"],
                "inline-message-1",
            )
            edited_blocks = (
                callback.bot.edit_message_text.await_args.kwargs["rich_message"].blocks
            )
            edited_photo = next(
                block for block in edited_blocks
                if isinstance(block, bot.InputRichBlockPhoto)
            )
            self.assertEqual(edited_photo.photo.media, "telegram-file-id")
            callback.answer.assert_awaited_once_with(
                "Оценка 9/10 сохранена.", show_alert=False
            )

    async def test_poster_file_id_is_uploaded_once_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "poster_cache.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "", "/poster.jpg", 0)
            telegram = SimpleNamespace(
                send_photo=AsyncMock(
                    return_value=SimpleNamespace(
                        photo=[
                            SimpleNamespace(file_id="small-file-id"),
                            SimpleNamespace(file_id="large-file-id"),
                        ]
                    )
                )
            )
            with (
                patch.object(bot, "store", store),
                patch.object(bot, "BOT_MEDIA_CACHE_CHAT_ID", -100123),
                patch.object(
                    bot,
                    "download_tmdb_poster",
                    new=AsyncMock(return_value=b"poster-bytes"),
                ),
            ):
                file_id = await bot.telegram_poster_file_id(telegram, movie)
                second_file_id = await bot.telegram_poster_file_id(telegram, movie)

            self.assertEqual(file_id, "large-file-id")
            self.assertEqual(second_file_id, "large-file-id")
            telegram.send_photo.assert_awaited_once()
            self.assertEqual(
                telegram.send_photo.await_args.kwargs["chat_id"],
                -100123,
            )
            photo = telegram.send_photo.await_args.kwargs["photo"]
            self.assertEqual(photo.data, b"poster-bytes")

    async def test_warm_poster_cache_retries_after_network_failure(self) -> None:
        movie = bot.Movie(42, "Фильм", "2024", "", "/poster.jpg", 0)
        with (
            patch.object(
                bot,
                "telegram_poster_file_ids",
                new=AsyncMock(side_effect=[{}, {"/poster.jpg": "cached-file-id"}]),
            ) as cache_posters,
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            await bot.warm_poster_cache(SimpleNamespace(), [movie], 0)

        self.assertEqual(cache_posters.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [0, 5],
        )

    async def test_multiple_inline_posters_are_uploaded_as_one_media_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "poster_group.sqlite3"))
            await store.init()
            movies = [
                bot.Movie(42, "Фильм 1", "2024", "", "/one.jpg", 0),
                bot.Movie(43, "Фильм 2", "2024", "", "/two.jpg", 0),
            ]
            telegram = SimpleNamespace(
                send_media_group=AsyncMock(return_value=[
                    SimpleNamespace(photo=[SimpleNamespace(file_id="file-one")]),
                    SimpleNamespace(photo=[SimpleNamespace(file_id="file-two")]),
                ]),
                send_photo=AsyncMock(),
            )
            with (
                patch.object(bot, "store", store),
                patch.object(bot, "BOT_MEDIA_CACHE_CHAT_ID", -100123),
                patch.object(bot, "poster_last_upload_at", 0.0),
                patch.object(
                    bot,
                    "download_tmdb_poster",
                    new=AsyncMock(
                        side_effect=lambda session, movie: (
                            session,
                            movie.poster_path.encode(),
                        )[1]
                    ),
                ),
            ):
                file_ids = await bot.telegram_poster_file_ids(
                    telegram, movies, wait_on_flood=False
                )

            self.assertEqual(
                file_ids,
                {"/one.jpg": "file-one", "/two.jpg": "file-two"},
            )
            telegram.send_media_group.assert_awaited_once()
            telegram.send_photo.assert_not_awaited()
            media = telegram.send_media_group.await_args.kwargs["media"]
            self.assertEqual(
                [item.media.data for item in media],
                [b"/one.jpg", b"/two.jpg"],
            )

    async def test_poster_upload_retries_after_telegram_flood_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "poster_retry.sqlite3"))
            await store.init()
            movie = bot.Movie(42, "Фильм", "2024", "", "/poster.jpg", 0)
            telegram = SimpleNamespace(
                send_photo=AsyncMock(
                    side_effect=[
                        TelegramRetryAfter(
                            method=SendPhoto(chat_id=-100123, photo="poster"),
                            message="flood control",
                            retry_after=30,
                        ),
                        SimpleNamespace(
                            photo=[SimpleNamespace(file_id="large-file-id")]
                        ),
                    ]
                )
            )
            with (
                patch.object(bot, "store", store),
                patch.object(bot, "BOT_MEDIA_CACHE_CHAT_ID", -100123),
                patch.object(bot, "poster_last_upload_at", 0.0),
                patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
                patch.object(
                    bot,
                    "download_tmdb_poster",
                    new=AsyncMock(return_value=b"poster-bytes"),
                ),
            ):
                file_id = await bot.telegram_poster_file_id(telegram, movie)

            self.assertEqual(file_id, "large-file-id")
            self.assertEqual(telegram.send_photo.await_count, 2)
            self.assertIn(31, [call.args[0] for call in sleep.await_args_list])

    async def test_unavailable_remote_image_batch_is_retried_per_poster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(str(Path(directory) / "poster_remote_fallback.sqlite3"))
            await store.init()
            movies = [
                bot.Movie(42, "Фильм 1", "2024", "", "/one.jpg", 0),
                bot.Movie(43, "Фильм 2", "2024", "", "/two.jpg", 0),
            ]
            telegram = SimpleNamespace(
                send_media_group=AsyncMock(
                    side_effect=TelegramBadRequest(
                        method=SendPhoto(chat_id=-100123, photo="poster"),
                        message="WEBPAGE_MEDIA_EMPTY",
                    )
                ),
                send_photo=AsyncMock(side_effect=[
                    SimpleNamespace(photo=[SimpleNamespace(file_id="file-one")]),
                    SimpleNamespace(photo=[SimpleNamespace(file_id="file-two")]),
                ]),
            )
            with (
                patch.object(bot, "store", store),
                patch.object(bot, "BOT_MEDIA_CACHE_CHAT_ID", -100123),
                patch.object(bot, "poster_last_upload_at", 0.0),
                patch.object(bot.asyncio, "sleep", new=AsyncMock()),
                patch.object(
                    bot,
                    "download_tmdb_poster",
                    new=AsyncMock(side_effect=bot.aiohttp.ClientError("offline")),
                ),
            ):
                file_ids = await bot.telegram_poster_file_ids(
                    telegram, movies, wait_on_flood=False
                )

            self.assertEqual(
                file_ids,
                {"/one.jpg": "file-one", "/two.jpg": "file-two"},
            )
            self.assertEqual(telegram.send_photo.await_count, 2)


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rating_and_favorite_callbacks_use_current_user_and_chat(self) -> None:
        movie = bot.Movie(42, "Фильм", "2024", "Описание", None, 8.0)
        fake_store = SimpleNamespace(
            get_movie=AsyncMock(return_value=movie),
            rate=AsyncMock(return_value=(8.0, 2, None)),
            group_score=AsyncMock(return_value=(8.0, 2)),
            is_favorite=AsyncMock(return_value=True),
            toggle_favorite=AsyncMock(return_value=True),
            get_rich_card_movie_id=AsyncMock(return_value=42),
            get_rich_card_token=AsyncMock(return_value="card_token_123"),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP),
            message_id=77,
        )
        callback = SimpleNamespace(
            data="rich:ratevote:card_token_123:8",
            message=message,
            inline_message_id=None,
            from_user=SimpleNamespace(id=55),
            answer=AsyncMock(),
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )

        with patch.object(bot, "store", fake_store):
            await bot.rich_movie_action(callback)

        fake_store.rate.assert_awaited_once_with(-100123, 55, 42, 8.0)
        callback.bot.edit_message_text.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "Оценка 8/10 сохранена.", show_alert=False
        )

        callback.data = "rich:favorite:card_token_123"
        callback.answer.reset_mock()
        callback.bot.edit_message_text.reset_mock()
        with patch.object(bot, "store", fake_store):
            await bot.rich_movie_action(callback)

        fake_store.toggle_favorite.assert_awaited_once_with(-100123, 55, 42)
        callback.answer.assert_awaited_once_with(
            "Добавлено в избранное.", show_alert=False
        )

    async def test_invalid_rich_callback_is_rejected(self) -> None:
        callback = SimpleNamespace(
            data="rich:ratevote:card_token_123:11",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP)
            ),
            inline_message_id=None,
            from_user=SimpleNamespace(id=55),
            answer=AsyncMock(),
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        with patch.object(bot.store, "get_movie", new_callable=AsyncMock) as get_movie:
            await bot.rich_movie_action(callback)
        get_movie.assert_not_awaited()
        callback.answer.assert_awaited_once_with("Некорректная кнопка.", show_alert=True)

    async def test_callback_token_cannot_target_another_chat_message(self) -> None:
        callback = SimpleNamespace(
            data="rich:favorite:card_token_123",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100999, type=ChatType.SUPERGROUP),
                message_id=77,
            ),
            inline_message_id=None,
            from_user=SimpleNamespace(id=55),
            answer=AsyncMock(),
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        fake_store = SimpleNamespace(
            get_rich_card_movie_id=AsyncMock(return_value=None),
            get_movie=AsyncMock(),
            toggle_favorite=AsyncMock(),
        )

        with patch.object(bot, "store", fake_store):
            await bot.rich_movie_action(callback)

        fake_store.get_movie.assert_not_awaited()
        fake_store.toggle_favorite.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "Карточка устарела. Найдите фильм командой /film.",
            show_alert=True,
        )

    async def test_trailer_and_similar_use_tmdb_responses(self) -> None:
        tmdb = bot.TMDB("test-token")
        movie = bot.Movie(42, "Фильм", "2024", "", None, 8.0)
        tmdb.request = AsyncMock(side_effect=[
            {"results": [
                {"site": "YouTube", "type": "Trailer", "key": "verified_key", "official": True}
            ]},
            {"results": [
                {
                    "id": 99,
                    "title": "Похожий фильм",
                    "release_date": "2023-01-01",
                    "vote_average": 7.2,
                }
            ]},
        ])
        trailer = await tmdb.trailer(movie)
        similar = await tmdb.similar(movie)
        self.assertEqual(trailer, "https://www.youtube.com/watch?v=verified_key")
        self.assertEqual(len(similar), 1)
        self.assertEqual(similar[0].tmdb_id, 99)
        self.assertEqual(similar[0].media_type, "movie")


if __name__ == "__main__":
    unittest.main()
